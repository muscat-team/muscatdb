"""LCO archive search/download must not borrow the shared ``LCO_API_TOKEN`` for a
viewer who is denied a restricted proposal (issue #210, part of #144).

Reading the archive under the operator's account would hand a denied viewer a
restricted proposal's frames straight from LCO, bypassing every filter on our
own database. When the viewer has any denied proposal they must use their own
LCO token, whose reach is whatever their own LCO account can see.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from muscat_db import access, lco, web
from tests.test_access_control import (
    _PROXY_SECRET,
    _RESTRICTED,
    _build_db,
    _headers,
    _set_admin,
)

pytestmark = pytest.mark.usefixtures("mock_target_coord_resolution")

_FRAMES_URL = "/api/lco/archive/frames"
_REQUEST = {"request_id": "4236675", "reduction_level": "91"}
_DOWNLOAD_BODY = {"frames": [{"filename": "a.fits", "url": "https://example.invalid/a"}]}


@pytest.fixture
def searches(monkeypatch):
    """Record archive searches and downloads instead of calling LCO."""
    calls = {"search": 0, "download": 0}

    def fake_search(filters, user_name=None, max_frames=0, token=None):
        calls["search"] += 1
        return {"count": 0, "results": [], "truncated": False}

    def fake_start(frames, **kwargs):
        calls["download"] += 1
        return {"job_id": "j1", "state": "pending", "frames_total": len(frames)}

    monkeypatch.setattr(lco, "archive_search_all", fake_search)
    monkeypatch.setattr(lco, "start_archive_download", fake_start)
    monkeypatch.setattr(lco, "download_frames", lambda frames, overwrite=False: [])
    monkeypatch.setattr(web, "_persist_lco_archive_download_row", lambda row: None)
    monkeypatch.setattr(web, "_lco_archive_download_row", lambda job: {})
    monkeypatch.setenv("LCO_API_TOKEN", "shared-operator-token")
    return calls


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "muscat.db"
    _build_db(str(path))
    monkeypatch.setenv("MUSCAT_DB_PATH", str(path))
    return str(path)


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("MUSCAT_PROXY_SECRET", _PROXY_SECRET)
    web._index_cache.clear()
    return TestClient(web.app, client=("127.0.0.1", 12345))


def _own_token(monkeypatch, *users):
    monkeypatch.setattr(
        lco, "get_user_lco_token", lambda u: f"own-{u}" if u in users else None
    )


def _search(client, user=None):
    headers = _headers(user) if user else {}
    return client.get(_FRAMES_URL, headers=headers, params=_REQUEST)


def _download(client, path, user=None, **extra):
    headers = _headers(user) if user else {}
    return client.post(path, headers=headers, json={**_DOWNLOAD_BODY, **extra})


def test_denied_viewer_without_own_token_cannot_search(client, searches, monkeypatch):
    _own_token(monkeypatch)
    r = _search(client, "alice")
    assert r.status_code == 403
    assert searches["search"] == 0


def test_denied_viewer_without_own_token_cannot_download(client, searches, monkeypatch):
    _own_token(monkeypatch)
    background = _download(client, "/api/lco/archive/download", "alice", background=True)
    foreground = _download(client, "/api/lco/archive/download", "alice")
    assert background.status_code == 403
    assert foreground.status_code == 403
    assert searches["download"] == 0


def test_denied_viewer_without_own_token_cannot_exofop_download(client, searches, monkeypatch):
    _own_token(monkeypatch)
    r = client.post(
        "/api/lco/archive/exofop-download",
        headers=_headers("alice"),
        json={"target": "TOI-1", "tsdate": "2024-05-13"},
    )
    assert r.status_code == 403
    assert searches["search"] == 0


def test_anonymous_viewer_is_refused_when_something_is_restricted(client, searches, monkeypatch):
    _own_token(monkeypatch)
    assert _search(client).status_code == 403
    assert searches["search"] == 0


def test_denied_viewer_with_own_token_can_search_and_download(client, searches, monkeypatch):
    _own_token(monkeypatch, "alice")
    assert _search(client, "alice").status_code == 200
    assert searches["search"] == 1
    r = _download(client, "/api/lco/archive/download", "alice", background=True)
    assert r.status_code == 200
    assert searches["download"] == 1


def test_admin_keeps_the_shared_token(client, db, searches, monkeypatch):
    _own_token(monkeypatch)
    _set_admin(db, "root")
    assert _search(client, "root").status_code == 200
    assert searches["search"] == 1


def test_viewer_granted_every_restriction_keeps_the_shared_token(client, db, searches, monkeypatch):
    _own_token(monkeypatch)
    access.grant(db, "alice", _RESTRICTED, "admin")
    assert _search(client, "alice").status_code == 200
    assert searches["search"] == 1


def test_nothing_restricted_keeps_the_shared_token(tmp_path, monkeypatch, searches):
    path = tmp_path / "open.db"
    _build_db(str(path), restrict=False)
    monkeypatch.setenv("MUSCAT_DB_PATH", str(path))
    monkeypatch.setenv("MUSCAT_PROXY_SECRET", _PROXY_SECRET)
    web._index_cache.clear()
    _own_token(monkeypatch)
    client = TestClient(web.app, client=("127.0.0.1", 12345))
    assert _search(client, "alice").status_code == 200
    assert searches["search"] == 1
