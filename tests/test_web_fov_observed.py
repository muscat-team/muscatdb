"""Tests for the /api/fov/observed-dates and /api/fov/observed-pointing
endpoints: overlaying a target's actual, previously-observed pointing (from
the frames obslog) on the FOV planner's sky view, alongside a freshly
proposed one.
"""
import sqlite3

import pytest
from starlette.testclient import TestClient

from muscat_db.web import app


def _insert_frame(db_path, **kw):
    defaults = dict(
        instrument="muscat3", obsdate="260101", ccd=0, filename="f.fits",
        object="WASP-12", jd_start=2460000.5, ut_start="00:00:00",
        exptime=10.0, read_mode="MUSCAT_FAST", filter="g",
        ra="06:36:38.00", declination="+29:40:20.0",
        airmass=1.1, focus=0.0, pa=None,
    )
    defaults.update(kw)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO frames
           (instrument, obsdate, ccd, filename, object, jd_start, ut_start,
            exptime, read_mode, filter, ra, declination, airmass, focus, pa)
           VALUES (:instrument, :obsdate, :ccd, :filename, :object, :jd_start,
                   :ut_start, :exptime, :read_mode, :filter, :ra, :declination,
                   :airmass, :focus, :pa)""",
        defaults,
    )
    conn.commit()
    conn.close()


# ── /api/fov/observed-dates ──────────────────────────────────────────────────

def test_observed_dates_requires_target(mock_db):
    r = TestClient(app).get("/api/fov/observed-dates")
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_observed_dates_dedupes_and_reports_across_instruments(mock_db, monkeypatch):
    monkeypatch.setattr(
        "muscat_db.web._get_datasets_for_normalized_target",
        lambda _db, norm_name: ([
            {"instrument": "muscat3", "date": "260102", "object": "WASP-12", "n_frames": 50},
            {"instrument": "muscat4", "date": "260101", "object": "WASP-12", "n_frames": 30},
            # duplicate (e.g. two filters counted separately upstream) collapses to one row
            {"instrument": "muscat4", "date": "260101", "object": "WASP-12", "n_frames": 30},
        ], "2026-07-01"),
    )

    r = TestClient(app).get("/api/fov/observed-dates?target=WASP-12")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert len(data["dates"]) == 2
    keys = {(d["instrument"], d["date"]) for d in data["dates"]}
    assert keys == {("muscat3", "260102"), ("muscat4", "260101")}


def test_observed_dates_empty_for_unknown_target(mock_db, monkeypatch):
    monkeypatch.setattr(
        "muscat_db.web._get_datasets_for_normalized_target",
        lambda _db, norm_name: ([], "2026-07-01"),
    )
    r = TestClient(app).get("/api/fov/observed-dates?target=NOT-A-REAL-TARGET")
    assert r.status_code == 200
    assert r.json()["dates"] == []


# ── /api/fov/observed-pointing ───────────────────────────────────────────────

def test_observed_pointing_requires_params(mock_db):
    r = TestClient(app).get("/api/fov/observed-pointing")
    assert r.status_code == 400


def test_observed_pointing_rejects_unknown_instrument(mock_db):
    r = TestClient(app).get(
        "/api/fov/observed-pointing?inst=not_an_instrument&obsdate=260101&obj=WASP-12"
    )
    assert r.status_code == 400


def test_observed_pointing_not_found(mock_db):
    r = TestClient(app).get(
        "/api/fov/observed-pointing?inst=muscat3&obsdate=260101&obj=WASP-12"
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert "no usable pointing" in data["error"].lower()


def test_observed_pointing_returns_footprint_polygon(mock_db):
    _insert_frame(mock_db, instrument="muscat3", obsdate="260101", object="WASP-12",
                  ra="06:36:38.00", declination="+29:40:20.0", pa=None)

    r = TestClient(app).get(
        "/api/fov/observed-pointing?inst=muscat3&obsdate=260101&obj=WASP-12"
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["ra"] == pytest.approx((6 + 36 / 60 + 38 / 3600) * 15.0, abs=1e-4)
    assert data["dec"] == pytest.approx(29 + 40 / 60 + 20 / 3600, abs=1e-4)
    assert data["pa_available"] is False
    assert data["pa_deg"] is None
    # No PA on record -> footprint is drawn unrotated (PA=0), not omitted.
    assert len(data["footprint"]) == 4
    assert data["fov_arcsec"] > 0


def test_observed_pointing_uses_frames_read_mode_for_footprint_size(mock_db):
    _insert_frame(mock_db, instrument="sinistro", obsdate="260101", object="WASP-12",
                  read_mode="full_frame")
    _insert_frame(mock_db, instrument="sinistro", obsdate="260102", object="WASP-12",
                  read_mode="central_2k_2x2")

    full = TestClient(app).get(
        "/api/fov/observed-pointing?inst=sinistro&obsdate=260101&obj=WASP-12"
    ).json()
    central = TestClient(app).get(
        "/api/fov/observed-pointing?inst=sinistro&obsdate=260102&obj=WASP-12"
    ).json()
    assert full["fov_arcsec"] > central["fov_arcsec"]
