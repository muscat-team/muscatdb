"""nginx-access.conf must make nginx, not the browser, the app's peer (#319).

cloudflared adds ``X-Forwarded-For: <browser IP>``. uvicorn runs with its
default ``proxy_headers=True`` (trusting 127.0.0.1), so whatever nginx forwards
as ``X-Forwarded-For`` becomes ``scope["client"]``. When nginx passed the
browser's address on, the loopback + shared-secret checks refused every Access
sign-in with a 403. Proxy headers stay on because they also carry the https
scheme (``X-Forwarded-Proto``) to the gateway and the directory redirects.

TestClient never runs uvicorn's proxy-header middleware, so these tests start a
real server with the options ``muscat-db serve`` passes to ``uvicorn.run`` and
send each location's headers as nginx would build them from the config file.
"""

from __future__ import annotations

import re
import socket
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
import socketio
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.routing import Mount

from muscat_db import cf_access, chat, cli, web
from muscat_db.auth import PROXY_SECRET_HEADER
from muscat_db.database import _apply_schema
from muscat_db.directory_listing import ListingStaticFiles

CONF = Path(__file__).resolve().parents[1] / "deploy" / "nginx-access.conf"
TEAM = "myteam.cloudflareaccess.com"
AUD = "abc123aud"
SECRET = "proxy-secret"
EMAIL = "andrew.example@univ.edu"
BROWSER_IP = "203.0.113.9"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


# ── the config itself ───────────────────────────────────────────────────


def _locations() -> dict[str, dict[str, str]]:
    """``proxy_set_header`` name -> value for each location block."""
    text = re.sub(r"#[^\n]*", "", CONF.read_text())
    blocks = {}
    for m in re.finditer(r"location\s+(=\s*)?(\S+)\s*\{(.*?)\n\s*\}", text, re.S):
        headers = dict(re.findall(r"proxy_set_header\s+(\S+)\s+([^;]*);", m.group(3)))
        blocks[m.group(2)] = {k.lower(): v.strip() for k, v in headers.items()}
    return blocks


def test_config_has_the_three_proxied_locations():
    assert set(_locations()) == {"/_auth/cf", "/", "/socket.io"}


@pytest.mark.parametrize("location", ["/_auth/cf", "/", "/socket.io"])
def test_every_location_forwards_nginx_itself_as_the_client(location):
    assert _locations()[location].get("x-forwarded-for") == "$remote_addr"


def test_the_browser_address_is_never_appended():
    assert "$proxy_add_x_forwarded_for" not in re.sub(r"#[^\n]*", "", CONF.read_text())


# ── a real uvicorn behind the configured headers ────────────────────────


def _as_nginx_sends(location: str, **extra) -> dict[str, str]:
    """Headers reaching the app for a tunnelled request through *location*.

    The client request carries cloudflared's ``X-Forwarded-For``; a location
    that does not set the header passes it on unchanged (the auth subrequest
    inherits the original request's headers).
    """
    values = {"$remote_addr": "127.0.0.1", "$proxy_add_x_forwarded_for": f"{BROWSER_IP}, 127.0.0.1"}
    configured = _locations()[location]
    headers = {"X-Forwarded-For": BROWSER_IP, PROXY_SECRET_HEADER: SECRET, **extra}
    if "x-forwarded-for" in configured:
        headers["X-Forwarded-For"] = values.get(configured["x-forwarded-for"], configured["x-forwarded-for"])
    if "x-forwarded-proto" in configured:
        headers["X-Forwarded-Proto"] = configured["x-forwarded-proto"]
    return headers


class _Keys:
    def get_signing_key_from_jwt(self, token):
        return SimpleNamespace(key=_KEY.public_key())


def _token() -> str:
    now = int(time.time())
    claims = {"iss": f"https://{TEAM}", "aud": [AUD], "iat": now, "exp": now + 600, "email": EMAIL}
    return jwt.encode(claims, _KEY, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = str(tmp_path / "muscat.db")
    conn = sqlite3.connect(path)
    _apply_schema(conn)
    conn.execute(
        "INSERT INTO users (username, display_name, email) VALUES ('andrew', 'andrew', ?)",
        (EMAIL,),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("MUSCAT_DB_PATH", path)
    monkeypatch.setenv("MUSCAT_PROXY_SECRET", SECRET)
    monkeypatch.setenv("MUSCAT_CF_TEAM_DOMAIN", TEAM)
    monkeypatch.setenv("MUSCAT_CF_AUD", AUD)
    # What _prepare_nginx_auth sets in production: anonymous is refused, so a
    # check that loses the identity fails instead of degrading to anonymous.
    monkeypatch.setenv("MUSCAT_REQUIRE_AUTH", "1")
    monkeypatch.setattr(cf_access, "_key_client", lambda domain: _Keys())
    return path


def _serve_options(monkeypatch) -> dict:
    """The keyword options ``muscat-db serve --nginx`` hands to ``uvicorn.run``."""
    captured: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw))
    monkeypatch.setattr(cli, "_prepare_nginx_auth", lambda: None)
    cli._run_server(
        db=cli.os.environ.get("MUSCAT_DB_PATH", "muscat.db"), host="127.0.0.1", port=0,
        reload=False, workers=1, nginx=True,
    )
    return {k: v for k, v in captured.items() if k not in ("host", "port", "reload", "workers")}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(app, monkeypatch):
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", **_serve_options(monkeypatch)
    ))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        if time.monotonic() > deadline:
            pytest.fail("uvicorn did not start")
        time.sleep(0.05)
    return srv, thread, f"http://127.0.0.1:{port}"


@pytest.fixture
def server(env, monkeypatch):
    srv, thread, url = _start(web.sio_app, monkeypatch)
    yield url
    srv.should_exit = True
    thread.join(timeout=10)


def test_auth_subrequest_accepts_a_valid_token(server):
    headers = _as_nginx_sends("/_auth/cf", **{cf_access.JWT_HEADER: _token()})
    r = httpx.get(f"{server}{cf_access.AUTH_PATH}", headers=headers)
    assert r.status_code == 204
    assert r.headers[cf_access.EMAIL_HEADER] == EMAIL


def test_mapped_email_reaches_the_app(server):
    r = httpx.get(f"{server}/whoami", headers=_as_nginx_sends("/", **{"X-Forwarded-User": EMAIL}))
    assert r.status_code == 200
    assert r.json() == {"user": "andrew"}


def test_chat_connects_as_the_mapped_user(server):
    sio = socketio.Client(reconnection=False)
    # The browser is on https:// and nginx says so, so its Origin is https too.
    origin = server.replace("http://", "https://")
    headers = _as_nginx_sends("/socket.io", **{"X-Forwarded-User": EMAIL, "Origin": origin})
    try:
        sio.connect(server, headers=headers, transports=["polling"], wait_timeout=10)
        assert chat._users_by_sid.get(sio.get_sid()) == "andrew"
    finally:
        sio.disconnect()


def test_a_remote_peer_is_still_refused(server):
    """The fix must not make a non-loopback client look like nginx."""
    headers = {"X-Forwarded-For": BROWSER_IP, PROXY_SECRET_HEADER: SECRET, cf_access.JWT_HEADER: _token()}
    assert httpx.get(f"{server}{cf_access.AUTH_PATH}", headers=headers).status_code == 403


def test_redirects_keep_the_https_scheme(tmp_path, monkeypatch):
    """Why proxy headers stay on: a directory redirect must not drop to http://."""
    (tmp_path / "obslog").mkdir()
    app = Starlette(routes=[Mount("/lc-monitor", ListingStaticFiles(directory=str(tmp_path), html=True))])
    srv, thread, url = _start(app, monkeypatch)
    try:
        r = httpx.get(f"{url}/lc-monitor/obslog", headers=_as_nginx_sends("/"), follow_redirects=False)
    finally:
        srv.should_exit = True
        thread.join(timeout=10)
    assert r.status_code in (301, 302, 307)
    assert r.headers["location"].startswith("https://")
