"""Regression tests for the high-severity 2026-07-17 audit findings."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from muscat_db.database import SCHEMA, save_job
from muscat_db.web import app


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_production_auth_rejects_direct_and_secretless_requests(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_REQUIRE_AUTH", "1")
    monkeypatch.setenv("MUSCAT_PROXY_SECRET", "proxy-secret")
    client = TestClient(app, client=("127.0.0.1", 12345))

    assert client.get("/").status_code == 401
    forged = client.get("/", headers={"X-Forwarded-User": "mallory"})
    assert forged.status_code == 401

    monkeypatch.delenv("MUSCAT_PROXY_SECRET")
    monkeypatch.setenv("MUSCAT_PROXY_SECRET_FILE", str(tmp_path / "missing-secret"))
    no_configured_secret = client.get(
        "/", headers={"X-Forwarded-User": "mallory"}
    )
    assert no_configured_secret.status_code == 401


def test_production_auth_accepts_authenticated_proxy_and_keeps_health_public(
    monkeypatch,
):
    monkeypatch.setenv("MUSCAT_REQUIRE_AUTH", "1")
    monkeypatch.setenv("MUSCAT_PROXY_SECRET", "proxy-secret")
    monkeypatch.setattr("muscat_db.web.ensure_user", lambda _user: None)
    client = TestClient(app, client=("127.0.0.1", 12345))

    response = client.get(
        "/",
        headers={
            "X-Forwarded-User": "alice",
            "X-MuSCAT-Proxy-Secret": "proxy-secret",
        },
    )
    assert response.status_code == 200
    assert client.get("/healthz").json() == {"ok": True}


def test_all_unsafe_routes_share_central_csrf_guard(monkeypatch):
    monkeypatch.setenv("MUSCAT_REQUIRE_AUTH", "1")
    monkeypatch.setenv("MUSCAT_PROXY_SECRET", "proxy-secret")
    monkeypatch.setattr("muscat_db.web.ensure_user", lambda _user: None)
    client = TestClient(app, client=("127.0.0.1", 12345))
    auth = {
        "X-Forwarded-User": "alice",
        "X-MuSCAT-Proxy-Secret": "proxy-secret",
    }

    missing = client.post(
        "/api/jobs/rerun",
        headers={**auth, "X-Test-No-Origin": "1"},
        json={"key": "irrelevant"},
    )
    foreign = client.put(
        "/api/targets/example/note",
        headers={**auth, "Origin": "https://evil.example"},
        json={"note": "x"},
    )
    tag_create_missing = client.post(
        "/api/tags",
        headers={**auth, "X-Test-No-Origin": "1"},
        json={"tag": "example"},
    )
    assert missing.status_code == 403
    assert foreign.status_code == 403
    assert tag_create_missing.status_code == 403


def test_jobs_page_never_interpolates_persisted_values_into_javascript(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "security.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
    monkeypatch.setenv("MUSCAT_DB_PATH", str(db_path))
    monkeypatch.setattr("muscat_db.photometry.sync_jobs", lambda: None)
    monkeypatch.setattr("muscat_db.transit_fit.sync_jobs", lambda: None)
    monkeypatch.setattr("muscat_db.ttv_fit.sync_jobs", lambda: None)
    monkeypatch.setattr("muscat_db.transit_fit._discover_orphan_fits", lambda _keys: [])
    monkeypatch.setattr("muscat_db.lco.archive_download_jobs", lambda: [])
    payload = "x');alert(1);//<img src=x onerror=alert(2)>"
    save_job(
        type_="photometry",
        inst="muscat3",
        date="260101",
        target=payload,
        state="done",
        returncode=0,
        elapsed=1,
        started_at=1.0,
        run_name=payload,
        run_id=payload,
    )

    page = TestClient(app).get("/jobs")
    assert page.status_code == 200
    assert "onclick=\"viewLog" not in page.text
    assert "onclick=\"reRunJob" not in page.text
    assert "onclick=\"hideRow" not in page.text
    assert "data-job-action=\"log\"" in page.text
    assert "<img src=x onerror=alert(2)>" not in page.text


def test_nginx_installer_keeps_proxy_secret_private_to_app_user():
    installer = (REPO_ROOT / "deploy" / "setup-nginx.sh").read_text()

    assert 'APP_USER="$(stat -c \'%U\' "$REPO_DIR")"' in installer
    assert 'chown "$APP_USER":root "$PROXY_SECRET_PATH"' in installer
    assert 'chmod 600 "$PROXY_SECRET_PATH"' in installer
    assert 'chmod 640 "$PROXY_SECRET_PATH"' not in installer


def test_nginx_restart_validates_secret_before_stopping_server(monkeypatch):
    from muscat_db import cli

    stopped = []
    monkeypatch.delenv("MUSCAT_REQUIRE_AUTH", raising=False)
    monkeypatch.setattr("muscat_db.auth.configured_proxy_secret", lambda: None)
    monkeypatch.setattr(cli, "_stop_running_servers", lambda _port: stopped.append(_port))

    with pytest.raises(RuntimeError, match="nginx mode requires"):
        cli.restart(
            db="muscat.db",
            host="127.0.0.1",
            port=8000,
            reload=False,
            workers=1,
            nginx=True,
        )

    assert stopped == []
    assert "MUSCAT_REQUIRE_AUTH" not in cli.os.environ


def test_nginx_restart_honors_explicit_port_override(monkeypatch):
    """`restart --nginx` must not silently override an explicit --port.

    Regression for a bug where --nginx unconditionally hardcoded port 8001
    regardless of --port. Bootstrapping a second environment (e.g. staging
    on 8003) via `restart --nginx --port 8003` stopped and replaced
    whatever was actually running on 8001 -- production, on a shared host.

    The harm was in `_stop_running_servers`, not the launch, so this must
    assert on the port it's called with rather than discard it.
    """
    from muscat_db import cli

    captured = {}
    monkeypatch.setattr(cli, "_prepare_nginx_auth", lambda: None)
    monkeypatch.setattr(
        cli,
        "_stop_running_servers",
        lambda p: (captured.update(stopped_port=p), [])[1],
    )
    monkeypatch.setattr(
        cli,
        "_run_server",
        lambda db, host, port, reload, workers, nginx: captured.update(
            db=db, host=host, port=port, reload=reload, workers=workers, nginx=nginx
        ),
    )

    cli.restart(
        db="muscat.db",
        host="127.0.0.1",
        port=8003,
        reload=False,
        workers=1,
        nginx=True,
    )

    assert captured["stopped_port"] == 8003
    assert captured["port"] == 8003
    assert captured["host"] == "127.0.0.1"


def test_nginx_restart_still_defaults_to_8001_without_explicit_port(monkeypatch):
    from muscat_db import cli

    captured = {}
    monkeypatch.setattr(cli, "_prepare_nginx_auth", lambda: None)
    monkeypatch.setattr(cli, "_stop_running_servers", lambda _port: [])
    monkeypatch.setattr(
        cli,
        "_run_server",
        lambda db, host, port, reload, workers, nginx: captured.update(port=port, host=host),
    )

    cli.restart(
        db="muscat.db",
        host="127.0.0.1",
        port=None,
        reload=False,
        workers=1,
        nginx=True,
    )

    assert captured["port"] == 8001
    assert captured["host"] == "127.0.0.1"


def test_nginx_serve_honors_explicit_port_override(monkeypatch):
    from muscat_db import cli

    captured = {}
    monkeypatch.setattr(
        cli,
        "_run_server",
        lambda db, host, port, reload, workers, nginx: captured.update(port=port, host=host),
    )

    cli.serve(
        db="muscat.db",
        host="127.0.0.1",
        port=8003,
        reload=False,
        workers=1,
        nginx=True,
    )

    assert captured["port"] == 8003
    assert captured["host"] == "127.0.0.1"


def test_nginx_run_server_announces_actual_port(monkeypatch, capsys):
    """`_run_server`'s nginx-mode banner must not hardcode 127.0.0.1:8001.

    Regression for a stale literal left behind by the --port override fix:
    `serve --nginx --port 8003` bound uvicorn to 8003 but printed "bound to
    127.0.0.1:8001", the exact command someone runs to bootstrap staging and
    then verify.
    """
    from muscat_db import cli

    monkeypatch.setattr(cli, "_prepare_nginx_auth", lambda: None)
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: None)

    cli._run_server(
        db="muscat.db", host="127.0.0.1", port=8003,
        reload=False, workers=1, nginx=True,
    )

    assert "127.0.0.1:8003" in capsys.readouterr().out


def test_auth_gate_ignores_a_host_header_that_forges_a_public_path(monkeypatch):
    """A malformed Host must not turn a protected route into a public one.

    Starlette <= 1.0.1 builds ``request.url`` from the Host header
    (GHSA-86qp-5c8j-p5mr), so a Host of ``evil/static/`` collapses
    ``request.url.path`` to ``/static/`` while ASGI still routes on the real
    path. Deriving ``protected`` from ``request.url.path`` therefore skipped
    both the 401 gate and the CSRF gate for any route, reachable by anyone
    who can talk to uvicorn directly. The decision must come from
    ``scope["path"]``, which the Host header cannot reach.
    """
    monkeypatch.setenv("MUSCAT_REQUIRE_AUTH", "1")
    monkeypatch.setenv("MUSCAT_PROXY_SECRET", "proxy-secret")
    client = TestClient(app, client=("127.0.0.1", 12345))

    assert client.get("/api/jobs/status").status_code == 401

    for forged in ("evil/static/", "scan.local/static/?x=", "evil/healthz"):
        assert client.get(
            "/api/jobs/status", headers={"Host": forged}
        ).status_code == 401, f"Host={forged!r} bypassed the auth gate"
