"""Integration tests for the Google Sheet sources on /api/lco/windows.

The sheet fetch itself is stubbed (``gsheet_ephemeris.query_*`` monkeypatched)
so no network is touched; the user's sheet *configuration* is real (written to a
temp DB via ``set_user_ephem_sheet``).
"""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from muscat_db import web
from muscat_db.database import set_user_ephem_sheet
from muscat_db.web import app


VALID_ID = "ABCDEFGHIJKLMNOPQRSTUVWX"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{VALID_ID}/edit"
PROXY_SECRET = "proxy-secret"
AUTH_HEADERS = {"X-Forwarded-User": "alice", "X-MuSCAT-Proxy-Secret": PROXY_SECRET}


def _authed_client(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "windows.db"))
    monkeypatch.setenv("MUSCAT_DB_SECRET", "test-secret")
    monkeypatch.setenv("MUSCAT_REQUIRE_AUTH", "1")
    monkeypatch.setenv("MUSCAT_PROXY_SECRET", PROXY_SECRET)
    return TestClient(app, client=("127.0.0.1", 12345))


_WINDOW_BODY = {
    "target": "WASP-12",
    "planet": "b",
    "range_start": "2026-07-01",
    "range_end": "2026-07-12",
    "pad_before_min": 0,
    "pad_after_min": 0,
}


def test_windows_gsheet_source_resolves_ephemeris(monkeypatch, tmp_path):
    client = _authed_client(monkeypatch, tmp_path)
    set_user_ephem_sheet("alice", SHEET_URL)
    monkeypatch.setattr(
        web.gsheet_ephemeris,
        "query_target_ephemeris",
        lambda target, url, tab, col_map=None: {"b": {"t0": 2459000.5, "period": 3.0, "duration": 2.0}},
    )

    resp = client.post(
        "/api/lco/windows", headers=AUTH_HEADERS, json={**_WINDOW_BODY, "source": "gsheet"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["planet"] == "b"
    assert data["period"] == 3.0
    assert data["duration"] == 2.0
    assert len(data["windows"]) >= 1


def test_windows_gsheet_source_accepts_candidate_planet(monkeypatch, tmp_path):
    # The sheet keys the planet under "b" (from a ".01" name); a request that
    # asks for planet ".01" must resolve to that "b" entry.
    client = _authed_client(monkeypatch, tmp_path)
    set_user_ephem_sheet("alice", SHEET_URL)
    monkeypatch.setattr(
        web.gsheet_ephemeris,
        "query_target_ephemeris",
        lambda target, url, tab, col_map=None: {"b": {"t0": 2459000.5, "period": 3.0, "duration": 2.0}},
    )
    resp = client.post(
        "/api/lco/windows",
        headers=AUTH_HEADERS,
        json={**_WINDOW_BODY, "planet": ".01", "source": "gsheet"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["planet"] == "b"
    assert data["period"] == 3.0


@pytest.mark.parametrize("source,owner", [("gsheet_tc", "alice"), ("shared_gsheet_tc", "jerome")])
def test_windows_gsheet_tc_source_runs_linear_fit(monkeypatch, tmp_path, source, owner):
    client = _authed_client(monkeypatch, tmp_path)
    monkeypatch.setenv("MUSCAT_SHARED_EPHEM_SHEET_USER", "jerome")
    set_user_ephem_sheet(owner, SHEET_URL)
    # Two transit centers 10 epochs apart -> period ~= 3.0 d, plus a catalog
    # duration seed the fit itself does not produce.
    monkeypatch.setattr(
        web.gsheet_ephemeris,
        "query_target_transit_centers",
        lambda target, url, tab, col_map=None: {
            "rows": [
                {"planet": "b", "source_epoch": 0, "tc": 2459000.5, "tc_unc": 0.001},
                {"planet": "b", "source_epoch": 10, "tc": 2459030.5, "tc_unc": 0.001},
            ],
            "time_system": None,
        },
    )
    monkeypatch.setattr(
        web.gsheet_ephemeris, "query_target_ephemeris", lambda target, url, tab, col_map=None: {}
    )
    monkeypatch.setattr(
        web, "_query_target_planets_catalog",
        lambda target: {"b": {"t0": 2459000.5, "period": 3.0, "duration": 2.4}},
    )

    resp = client.post(
        "/api/lco/windows", headers=AUTH_HEADERS, json={**_WINDOW_BODY, "source": source}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["period"] == 3.0
    # Duration was backfilled from the catalog seed.
    assert data["duration"] == 2.4
    assert len(data["windows"]) >= 1


def test_windows_gsheet_requires_configured_sheet(monkeypatch, tmp_path):
    client = _authed_client(monkeypatch, tmp_path)
    # No set_user_ephem_sheet call: alice has no sheet.
    resp = client.post(
        "/api/lco/windows", headers=AUTH_HEADERS, json={**_WINDOW_BODY, "source": "gsheet"}
    )
    assert resp.status_code == 400
    assert "Google Sheet" in resp.json()["error"]


def test_shared_sheet_and_personal_sheet_resolve_independently(monkeypatch, tmp_path):
    client = _authed_client(monkeypatch, tmp_path)
    monkeypatch.setenv("MUSCAT_SHARED_EPHEM_SHEET_USER", "jerome")
    shared_url = SHEET_URL.replace(VALID_ID, "ZYXWVUTSRQPONMLKJIHGFEDC")
    set_user_ephem_sheet("jerome", shared_url)
    set_user_ephem_sheet("alice", SHEET_URL)
    queried = []

    def query(target, url, tab, col_map=None):
        queried.append(url)
        return {"b": {"t0": 2459000.5, "period": 3.0 if url == shared_url else 4.0, "duration": 2.0}}

    monkeypatch.setattr(web.gsheet_ephemeris, "query_target_ephemeris", query)
    for source, expected in [("shared_gsheet", 3.0), ("gsheet", 4.0)]:
        resp = client.post("/api/lco/windows", headers=AUTH_HEADERS, json={**_WINDOW_BODY, "source": source})
        assert resp.status_code == 200, resp.text
        assert resp.json()["period"] == expected
        assert shared_url not in resp.text and SHEET_URL not in resp.text
    assert queried == [shared_url, SHEET_URL]
    # Another user gets the shared source, but cannot inherit Alice's private one.
    headers = {**AUTH_HEADERS, "X-Forwarded-User": "bob"}
    response = client.post("/api/lco/windows", headers=headers, json={**_WINDOW_BODY, "source": "shared_gsheet"})
    assert response.status_code == 200
    response = client.post("/api/lco/windows", headers=headers, json={**_WINDOW_BODY, "source": "gsheet"})
    assert response.status_code == 400


def test_shared_sheet_checks_target_visibility_before_read(monkeypatch, tmp_path):
    from fastapi import HTTPException
    client = _authed_client(monkeypatch, tmp_path)
    set_user_ephem_sheet("jerome", SHEET_URL)

    def deny(*args):
        raise HTTPException(404, "target not found")

    monkeypatch.setattr(web, "_require_visible_object", deny)
    monkeypatch.setattr(web, "_sheet_ephemeris", lambda *a: pytest.fail("hidden target sheet was read"))
    response = client.post("/api/lco/windows", headers=AUTH_HEADERS, json={**_WINDOW_BODY, "source": "shared_gsheet"})
    assert response.status_code == 404


def test_shared_owner_settings_disclose_sharing(monkeypatch, tmp_path):
    client = _authed_client(monkeypatch, tmp_path)
    monkeypatch.setenv("MUSCAT_SHARED_EPHEM_SHEET_USER", "jerome")
    for user in ("alice", "jerome"):
        response = client.get("/api/settings/ephem-sheet-status", headers={**AUTH_HEADERS, "X-Forwarded-User": user})
        assert response.json()["shared_with_everyone"] is (user == "jerome")


def test_target_info_keeps_sources_separate_and_suppresses_hidden_sheets(monkeypatch, tmp_path):
    client = _authed_client(monkeypatch, tmp_path)
    monkeypatch.setenv("MUSCAT_SHARED_EPHEM_SHEET_USER", "jerome")
    shared_url = SHEET_URL.replace(VALID_ID, "ZYXWVUTSRQPONMLKJIHGFEDC")
    set_user_ephem_sheet("jerome", shared_url)
    set_user_ephem_sheet("alice", SHEET_URL)
    monkeypatch.setattr(web.fit, "sync_jobs", lambda: None)
    monkeypatch.setattr(web.fit, "_discover_orphan_fits", lambda keys: [])
    monkeypatch.setattr(web, "_target_coordinates", lambda target: None)
    for name in ("_query_target_planets_catalog", "_query_target_planets_nasa", "_query_target_planets_toi"):
        monkeypatch.setattr(web, name, lambda target: {})
    monkeypatch.setattr(web.ttv, "list_ttv_runs", lambda target: [])
    monkeypatch.setattr(web, "_sheet_fit_ephemeris", lambda *args: {})
    queried = []

    def read(target, cfg):
        queried.append(cfg["url"])
        return {"b": {"t0": 2459000.5, "period": 3.0 if cfg["url"] == shared_url else 4.0}}

    monkeypatch.setattr(web, "_sheet_ephemeris", read)
    response = client.get("/api/ephemeris/target-info", params={"target": "WASP-12"}, headers=AUTH_HEADERS)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["shared_sheet_ephemeris"]["b"]["period"] == 3.0
    assert data["sheet_ephemeris"]["b"]["period"] == 4.0
    assert not data["shared_sheet_is_mine"]
    assert shared_url not in response.text and SHEET_URL not in response.text
    queried.clear()
    monkeypatch.setattr(web, "_object_hidden", lambda *args, **kwargs: True)
    response = client.get("/api/ephemeris/target-info", params={"target": "WASP-12"}, headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert response.json()["shared_sheet_ephemeris"] == {}
    assert response.json()["sheet_ephemeris"] == {}
    assert queried == []
