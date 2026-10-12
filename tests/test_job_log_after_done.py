"""The log box on the photometry, transit-fit and ephemeris (TTV) pages must
show the run's log file after the job is over, like the Jobs page's Log button.

Server side: ``/status`` carries the on-disk log even when no job row remains.
Client side: ``MuscatDom.showFinishedLog`` renders it (and drops a stale saved
log when the server has nothing); each page calls it on load.
"""
import json
import pathlib
import shutil
import subprocess
import textwrap

import pytest

from muscat_db import photometry as phot

ROOT = pathlib.Path(__file__).resolve().parent.parent / "src" / "muscat_db"
DOM_JS = ROOT / "static" / "js" / "dom.js"
TEMPLATES = ROOT / "templates"


class _EmptyStore:
    def all(self):
        return []

    def __getattr__(self, name):  # any other store call: nothing stored
        return lambda *a, **k: None


def test_photometry_status_returns_log_from_disk_without_job_row(tmp_path, monkeypatch):
    log = tmp_path / "web.log"
    log.write_text("reduction line 1\nreduction line 2\n")
    monkeypatch.setattr(phot, "get_job_store", lambda: _EmptyStore())
    monkeypatch.setattr(phot, "log_path", lambda *a, **k: log)

    s = phot.job_status("muscat3", "250728", "TOI-5205", "default")

    # state stays "none" (we cannot claim an outcome) but the log is surfaced
    assert s["state"] == "none"
    assert "reduction line 2" in s["log"]


def test_photometry_status_without_log_or_job_is_empty(monkeypatch):
    monkeypatch.setattr(phot, "get_job_store", lambda: _EmptyStore())
    monkeypatch.setattr(phot, "log_path", lambda *a, **k: None)
    s = phot.job_status("muscat3", "250728", "TOI-5205", "default")
    assert s["state"] == "none" and s["log"] == ""


# ---- client helper (executed with node against the real dom.js) -----------

node = shutil.which("node")


def _run_helper(status: dict, saved: str | None) -> dict:
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const store = {{}};
        if ({json.dumps(saved)} !== null) store['k'] = {json.dumps(saved)};
        global.window = global;
        global.document = {{ getElementById: () => null }};
        global.MuscatStorage = {{
          set: (k, v) => {{ store[k] = v; }},
          remove: (k) => {{ delete store[k]; }},
          get: (k, d) => (k in store ? store[k] : d),
        }};
        eval(fs.readFileSync({json.dumps(str(DOM_JS))}, 'utf8'));
        const logEl = {{ style: {{ display: 'none' }}, innerHTML: 'stale', textContent: 'stale' }};
        const shown = window.MuscatDom.showFinishedLog(logEl, {json.dumps(status)}, 'k');
        console.log(JSON.stringify({{ shown, display: logEl.style.display,
          html: logEl.innerHTML, saved: store['k'] === undefined ? null : store['k'] }}));
        """
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(node is None, reason="node not installed")
class TestShowFinishedLog:
    def test_done_run_shows_server_log(self):
        r = _run_helper({"state": "done", "log": "hello log", "returncode": 0}, saved=None)
        assert r["shown"] is True
        assert r["display"] == "block"
        assert "hello log" in r["html"]
        assert r["saved"] == "hello log"

    def test_error_and_cancelled_get_a_footer(self):
        e = _run_helper({"state": "error", "log": "boom", "returncode": 2}, saved=None)
        assert "exit code 2" in e["html"]
        c = _run_helper({"state": "cancelled", "log": "partial", "returncode": -1}, saved=None)
        assert "cancelled" in c["html"]

    def test_server_log_replaces_stale_saved_log(self):
        r = _run_helper(
            {"state": "done", "log": "real log", "returncode": 0},
            saved="⏳ Queued — waiting for the running full reduction to finish…",
        )
        assert "Queued" not in r["html"]
        assert r["saved"] == "real log"

    def test_nothing_on_server_drops_stale_saved_log(self):
        r = _run_helper({"state": "none", "log": ""}, saved="⏳ Queued — waiting…")
        assert r["shown"] is False
        assert r["saved"] is None
        assert r["display"] == "none"
        assert r["html"] == ""


# ---- each page wires the helper on load -----------------------------------

@pytest.mark.parametrize("name", ["photometry.html", "transit_fit.html", "ephemeris.html"])
def test_page_shows_finished_log_on_load(name):
    html = (TEMPLATES / name).read_text()
    assert "MuscatDom.showFinishedLog(" in html
