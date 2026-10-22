"""Harness for the browser tests: the real app, a fixture database, headless Chromium.

Each test module gets its own server: :func:`live_server` builds an isolated
tree (database, raw-data/prose/timer/TTV/LCO dirs, catalog CSVs), points the
app at it through the same env vars production uses, and runs uvicorn on a
loopback port in a background thread. Module scope keeps the network guard and
env changes from leaking into the rest of the suite. The fixture database is
restored from its startup snapshot before each test so edits never leak between
tests.

Nothing leaves the machine:

* the server process may only open loopback sockets (:func:`_block_network`),
  so a code path that would call SIMBAD, LCO, NExSci or the archive fails fast
  instead of hitting a real service (and behaves the same on CI and off-host);
* the modules that launch pipelines cannot start a process
  (:func:`_block_launches`); tests that click Run stub the launcher instead;
* the browser may only reach the server; the four CDN libraries the pages load
  (socket.io, Plotly, Aladin Lite, mermaid) are answered with the small
  stand-ins in ``stubs/``, which record their calls on ``window``.

The :func:`page` fixture logs in as an admin via the proxy headers and fails
the test on any uncaught JS error, console error or failed same-origin
request, unless the test lists it in ``page.allowed_errors``.

Skips when Playwright or its Chromium is missing, unless
``MUSCAT_REQUIRE_BROWSER=1`` (set in CI), which turns that into a failure.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

if os.environ.get("MUSCAT_REQUIRE_BROWSER") == "1":
    import playwright.sync_api as sync_api
else:
    sync_api = pytest.importorskip("playwright.sync_api")

from tests.browser import seed  # noqa: E402

STUBS = Path(__file__).parent / "stubs"
REPO_DATA = Path(__file__).resolve().parents[2] / "data"
PROXY_SECRET = "browser-test-secret"
ADMIN = "admin"

# CDN URL prefix -> (stub file, content type)
CDN_STUBS = {
    "https://cdn.socket.io/": ("socketio.js", "application/javascript"),
    "https://cdn.plot.ly/": ("plotly.js", "application/javascript"),
    "https://aladin.cds.unistra.fr/AladinLite/api/v3/latest/aladin.js": ("aladin.js", "application/javascript"),
    "https://aladin.cds.unistra.fr/AladinLite/api/v3/latest/aladin.css": (None, "text/css"),
    "https://cdn.jsdelivr.net/npm/mermaid@": ("mermaid.mjs", "application/javascript"),
}


def _require_browser() -> bool:
    return os.environ.get("MUSCAT_REQUIRE_BROWSER") == "1"


# ── server-side network guard ────────────────────────────────────────────────


def _is_loopback(host) -> bool:
    if host in ("localhost", "", None):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


class NetworkBlocked(OSError):
    pass


def _block_network(mp: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def check(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address[0]):
            raise NetworkBlocked(f"network disabled in browser tests: {address!r}")

    def connect(sock, address):
        check(sock, address)
        return real_connect(sock, address)

    def connect_ex(sock, address):
        check(sock, address)
        return real_connect_ex(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        if not _is_loopback(host):
            raise socket.gaierror(f"network disabled in browser tests: {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    mp.setattr(socket.socket, "connect", connect)
    mp.setattr(socket.socket, "connect_ex", connect_ex)
    mp.setattr(socket, "getaddrinfo", getaddrinfo)


# ── no pipeline launches ─────────────────────────────────────────────────────

# Modules that start prose/timer/harmonic/funpack/postprocess processes. The
# deploy host has those conda envs, so an unguarded "Run" click in a test would
# start a real reduction; each module gets a subprocess that refuses instead.
LAUNCHING_MODULES = (
    "muscat_db.photometry",
    "muscat_db.transit_fit",
    "muscat_db.ttv_fit",
    "muscat_db.lco",
    "muscat_db.postprocess",
)


class LaunchBlocked(RuntimeError):
    pass


class _NoLaunchSubprocess:
    """The ``subprocess`` module, minus the ability to start a process."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        if name in ("run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"):
            def refuse(*args, **kwargs):
                raise LaunchBlocked(f"subprocess.{name} is disabled in browser tests: {args[:1]!r}")
            return refuse
        return getattr(self._real, name)


def _block_launches(mp: pytest.MonkeyPatch) -> None:
    import importlib
    import subprocess

    for name in LAUNCHING_MODULES:
        mp.setattr(importlib.import_module(name), "subprocess", _NoLaunchSubprocess(subprocess))


# ── isolated data tree ───────────────────────────────────────────────────────


def _redirect_repo_data(mp: pytest.MonkeyPatch, data_dir: Path) -> None:
    """Point every module-level path under the repo's data/ at *data_dir*.

    The catalog CSVs are read from ``<repo>/data`` (present on the host, absent
    on CI), partly via module constants and partly via a module's ``HERE`` at
    call time. Redirecting both makes local runs and CI see the same fixture files.
    """
    prefix = str(REPO_DATA)

    def moved(value):
        if isinstance(value, Path) and str(value).startswith(prefix):
            return data_dir / value.relative_to(REPO_DATA)
        if isinstance(value, str) and value.startswith(prefix):
            return str(data_dir / Path(value).relative_to(REPO_DATA))
        if isinstance(value, (list, tuple)) and value and all(isinstance(v, (str, Path)) for v in value):
            new = [moved(v) for v in value]
            if new != list(value):
                return type(value)(new)
        return value

    for name, module in list(sys.modules.items()):
        if not name.startswith("muscat_db") or module is None:
            continue
        for attr, value in list(vars(module).items()):
            new = moved(value)
            if new is not value and new != value:
                mp.setattr(module, attr, new)
    # catalog.HERE and web.HERE build "<HERE>/../../data/<file>" at call time
    # (both modules fixed their template/static dirs from HERE at import).
    import muscat_db

    real_pkg = Path(muscat_db.__file__).parent
    fake_pkg = data_dir.parent / "src" / "muscat_db"
    fake_pkg.mkdir(parents=True, exist_ok=True)
    for name, module in list(sys.modules.items()):
        if name.startswith("muscat_db") and module is not None:
            here = getattr(module, "HERE", None)
            if isinstance(here, Path) and here.resolve() == real_pkg.resolve():
                mp.setattr(module, "HERE", fake_pkg)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServer:
    def __init__(self, url: str, root: Path, db: Path):
        self.url = url
        self.root = root
        self.db = db
        self.snapshot = root / "baseline.sqlite"
        with sqlite3.connect(db) as source, sqlite3.connect(self.snapshot) as target:
            source.backup(target)

    def sql(self, query: str, params=()):
        with sqlite3.connect(self.db) as conn:
            return conn.execute(query, params).fetchall()


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    import uvicorn

    root = tmp_path_factory.mktemp("browser-site")
    paths = seed.build_tree(root)
    mp = pytest.MonkeyPatch()
    try:
        # Start from none of the app's variables, so nothing from the host
        # (API tokens, LCO submit switch, real data dirs) reaches the server.
        from muscat_db.config import ENV_VARS

        for var in ENV_VARS:
            mp.delenv(var.name, raising=False)
        # A loopback HTTP proxy (common in dev sandboxes) would pass the network
        # guard and then stall; without one, outbound calls fail at once, as on CI.
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            mp.delenv(var, raising=False)
        env = {
            "MUSCAT_DB_PATH": paths.db,
            "MUSCAT_DB_SECRET": "browser-test-db-secret",
            "MUSCAT2_HTML_DIR": paths.tmp / "muscat2-html",
            "MUSCAT_BOYLE_CATALOG": paths.data / "Boyle2026" / "final_catalog.feather",
            "MUSCAT_HARPS_RVBANK_CSV": paths.data / "HARPS_RVBank_ver02.csv",
            "MUSCAT_HARPS_RVBANK_ZIP": paths.data / "HARPS_RVBank_ver02.csv.zip",
            "MUSCAT_HARPS_TARGETS_CSV": paths.data / "HARPS_RVBank_targets.csv",
            "MUSCAT_LAMOST_STARS_CSV": paths.data / "LAMA_stars.csv",
            "MUSCAT_LAMOST_SPEC_CSV": paths.data / "LAMA_spec.csv",
            "MUSCAT_DATA_DIR": paths.raw,
            "MUSCAT_OBSLOG_DIR": paths.obslog,
            "MUSCAT_PROSE_DIR": paths.prose,
            "MUSCAT_TIMER_DIR": paths.timer,
            "MUSCAT_TTV_DIR": paths.ttv,
            "MUSCAT_LCO_DIR": paths.lco,
            "MUSCAT_TMPDIR": paths.tmp,
            "MUSCAT_GAIA_CACHE_DIR": paths.tmp / "gaia",
            "MUSCAT_DB_BACKUP_DIR": paths.tmp / "backups",
            "MUSCAT_LCO_MONITOR_ENABLED": "0",
            "MUSCAT_PROXY_SECRET": PROXY_SECRET,
        }
        for key, value in env.items():
            mp.setenv(key, str(value))

        from muscat_db import web  # noqa: F401  (imports every module whose paths we redirect)

        _redirect_repo_data(mp, paths.data)
        _block_network(mp)
        _block_launches(mp)
        seed.patch_app(mp, paths)

        port = _free_port()
        config = uvicorn.Config(
            "muscat_db.web:app", host="127.0.0.1", port=port, log_level="warning", lifespan="on",
            # uvloop resolves and connects in C, around the socket-level guard;
            # the stdlib loop keeps every outbound connection inside it.
            loop="asyncio",
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 30
        while not server.started:
            if not thread.is_alive() or time.time() > deadline:
                raise RuntimeError("browser-test server did not start")
            time.sleep(0.05)
        yield LiveServer(f"http://127.0.0.1:{port}", root, paths.db)
        server.should_exit = True
        thread.join(timeout=10)
    finally:
        mp.undo()


@pytest.fixture(autouse=True)
def _proxy_secret_for_live_server(monkeypatch):
    """Re-apply the proxy secret after the root conftest's per-test reset."""
    monkeypatch.setenv("MUSCAT_PROXY_SECRET", PROXY_SECRET)


# ── browser ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except sync_api.Error as exc:
            if _require_browser():
                raise
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        yield b
        b.close()


# Fixed LCO weather readings: the home page fetches weather-api.lco.global
# straight from the browser and shows the last reading of each datum.
WEATHER = {
    "Weather Ok To Open": {"Value": 1, "ValueString": "true"},
    "Weather Air Temperature Value": {"Value": 12.25, "ValueString": "12.25"},
    "Weather Wind Speed Value": {"Value": 3.5, "ValueString": "3.5"},
    "Weather Humidity Value": {"Value": 41.0, "ValueString": "41"},
}


def _weather(url: str) -> str:
    from urllib.parse import parse_qs, urlparse

    datum = parse_qs(urlparse(url).query).get("datumname", [""])[0]
    return json.dumps([WEATHER[datum]] if datum in WEATHER else [])


def _route_external(problems: "Problems"):
    def handle(route):
        url = route.request.url
        if url.startswith("https://weather-api.lco.global/query"):
            return route.fulfill(body=_weather(url), content_type="application/json")
        for prefix, (stub, kind) in CDN_STUBS.items():
            if url.startswith(prefix):
                body = (STUBS / stub).read_bytes() if stub else b""
                return route.fulfill(body=body, content_type=kind)
        problems.blocked.append(url)
        return route.abort("blockedbyclient")

    return handle


class Problems:
    """Errors seen on a page; the test fails on any not matched by ``allowed``."""

    def __init__(self, base_url: str):
        self.base = base_url
        self.items: list[str] = []
        self.allowed: list[str] = []
        self.blocked: list[str] = []  # external URLs refused (not a failure by itself)

    def add(self, text: str) -> None:
        self.items.append(text)

    def unexpected(self) -> list[str]:
        return [p for p in self.items if not any(a in p for a in self.allowed)]


def _watch(page, problems: Problems) -> None:
    page.on("pageerror", lambda exc: problems.add(f"pageerror: {exc}"))
    page.on(
        "console",
        lambda msg: problems.add(f"console.{msg.type}: {msg.text}")
        if msg.type == "error" and "ERR_BLOCKED_BY_CLIENT" not in msg.text else None,
    )

    def on_response(resp):
        if resp.url.startswith(problems.base) and resp.status >= 400:
            problems.add(f"HTTP {resp.status}: {resp.request.method} {resp.url[len(problems.base):]}")

    def on_failed(req):
        # ERR_ABORTED is the browser cancelling a request it no longer needs
        # (navigation away from a live log stream, a superseded fetch); server
        # failures surface as HTTP statuses above.
        if req.url.startswith(problems.base) and "ERR_ABORTED" not in (req.failure or ""):
            problems.add(f"requestfailed: {req.method} {req.url[len(problems.base):]} ({req.failure})")

    page.on("response", on_response)
    page.on("requestfailed", on_failed)


@pytest.fixture
def page(browser, live_server):
    context = browser.new_context(
        base_url=live_server.url,
        extra_http_headers={"X-Forwarded-User": ADMIN, "X-MuSCAT-Proxy-Secret": PROXY_SECRET},
        viewport={"width": 1400, "height": 900},
    )
    # Start with the floating chat collapsed, as a user would after first use:
    # expanded, it covers the bottom-right of every page and intercepts clicks.
    context.add_init_script(
        "try { if (localStorage.getItem('muscat-chat-min') === null)"
        " localStorage.setItem('muscat-chat-min', '1'); } catch (e) {}"
    )
    problems = Problems(live_server.url)
    context.route(lambda url: not url.startswith(live_server.url), _route_external(problems))
    context.on("page", lambda opened: _watch(opened, problems))
    pg = context.new_page()
    pg.set_default_timeout(10_000)
    pg.problems = problems
    yield pg
    context.close()
    bad = problems.unexpected()
    if bad:
        pytest.fail("browser problems on the page:\n  " + "\n  ".join(bad), pytrace=False)


@pytest.fixture(autouse=True)
def _restore_browser_database(live_server):
    """Each interaction starts from the same seed, independent of test order."""
    with sqlite3.connect(live_server.snapshot) as source, sqlite3.connect(live_server.db) as target:
        source.backup(target)


@pytest.fixture
def log_stream(page):
    """Control only the SSE transport; real page status handlers consume its events."""
    page.add_init_script("""
      window.__logStreams = [];
      class FixtureEventSource {
        static CLOSED = 2;
        constructor(url) { this.url = url; this.readyState = 1; window.__logStreams.push(this); }
        close() { this.readyState = 2; }
      }
      window.EventSource = FixtureEventSource;
    """)

    def send(state, log, **extra):
        page.wait_for_function("window.__logStreams.some(s => s.readyState !== 2)")
        page.evaluate("""payload => {
          const stream = window.__logStreams.filter(s => s.readyState !== 2).at(-1);
          stream.onmessage({data: JSON.stringify(payload)});
        }""", {"state": state, "log": log, "elapsed": 12, "returncode": None, **extra})

    return send
