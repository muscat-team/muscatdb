"""Scheduling windows from an uploaded list of mid-transit times (#350).

The upload source schedules exactly the times the observer gave: no linear
ephemeris is fitted through them, and each row gets the same per-site
observability rating as a generated window. The parser has its own tests
(tests/test_midtimes.py); these pin the endpoint's contract.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from muscat_db import transit_obs
from muscat_db.web import app

# HIP 67522 seen from coj in April 2027, 3 h transit, no padding: worked out
# with the real classifier, so the test fails if the ratings drift.
FULL, PARTIAL, NONE = "2027-04-10T14:00:00Z", "2027-04-10T19:00:00Z", "2027-04-10T02:00:00Z"

_BASE = {
    "target": "HIP 67522",
    "planet": "b",
    "source": "upload",
    "duration": 3.0,
    "range_start": "2027-04-01",
    "range_end": "2027-04-30",
    "pad_before_min": 0,
    "pad_after_min": 0,
    "ra": 199.3,
    "dec": -40.6,
    "sites": ["coj"],
}


def _post(**overrides):
    return TestClient(app).post("/api/lco/windows", json={**_BASE, **overrides})


def test_each_uploaded_time_is_rated_full_partial_or_none():
    body = _post(midtimes_text="\n".join([FULL, PARTIAL, NONE])).json()

    assert body["ok"] is True, body
    by_mid = {w["mid"][:19]: w["observability"]["rating"] for w in body["windows"]}
    assert by_mid == {FULL[:19]: "full", PARTIAL[:19]: "partial", NONE[:19]: "none"}


def test_uploaded_times_are_not_refitted_to_a_straight_line(monkeypatch):
    """The page always posts t0/period; neither they nor a catalog may be used."""
    monkeypatch.setattr(
        "muscat_db.web._query_target_planets_catalog",
        lambda t: pytest.fail("catalog must not be consulted for source=upload"),
    )
    # deliberately uneven spacing: no linear ephemeris passes through these
    text = "2461500.5\n2461502.55\n2461504.45\n"
    body = _post(midtimes_text=text, t0=2461500.5, period=2.0, ra="", dec="").json()
    assert body["ok"] is False  # coordinates are required (see below)

    body = _post(midtimes_text=text, t0=2461500.5, period=2.0).json()
    assert [w["mid_bjd"] for w in body["windows"]] == [2461500.5, 2461502.55, 2461504.45]


def test_iso_utc_round_trips_to_the_same_utc_mid():
    w = _post(midtimes_text=FULL).json()["windows"][0]
    assert w["mid"] == "2027-04-10T14:00:00.000Z"
    # and mid_bjd is the BJD_TDB of that instant, a few minutes off UTC
    utc_jd = 2461505.5 + 14 / 24
    assert w["mid_bjd"] != pytest.approx(utc_jd, abs=1e-6)
    assert float(transit_obs.bjd_tdb_to_jd_utc(w["mid_bjd"], 199.3, -40.6)) == pytest.approx(utc_jd, abs=1e-8)


def test_windows_carry_the_keys_the_pipeline_consumes():
    w = _post(midtimes_text=FULL).json()["windows"][0]
    assert set(w) >= {"epoch", "epoch_abs", "mid_bjd", "mid", "start", "end", "observability"}


def test_uncertainty_fills_the_mid_unc_column():
    w = _post(midtimes_text="tc,tc_err_min\n" + FULL + ",3.5\n").json()["windows"][0]
    assert w["mid_unc_min"] == 3.5


def test_uploaded_epochs_are_shown_as_given():
    text = "tc,epoch\n2461502.55,412\n2461500.5,411\n"
    windows = _post(midtimes_text=text).json()["windows"]
    assert [(w["epoch"], w["epoch_abs"]) for w in windows] == [(411, 411), (412, 412)]


def test_without_epochs_transits_are_numbered_in_time_order():
    windows = _post(midtimes_text="2461502.55\n2461500.5\n").json()["windows"]
    assert [w["mid_bjd"] for w in windows] == [2461500.5, 2461502.55]
    assert [w["epoch"] for w in windows] == [0, 1]


def test_times_outside_the_range_are_counted_not_lost_silently():
    body = _post(midtimes_text=FULL + "\n2027-06-01T00:00:00Z\n2461500.5\n").json()
    assert body["uploaded"] == {
        "rows": 3, "in_range": 2, "out_of_range": 1, "duplicates_dropped": 0,
        "time_systems": {"bjd_tdb": 1, "utc": 2},
    }


def test_a_bad_line_rejects_the_upload_with_its_line_number():
    r = _post(midtimes_text=FULL + "\n3000.5\n")
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert body["errors"][0].startswith("line 2:")


@pytest.mark.parametrize("missing", [{"duration": ""}, {"range_start": ""}, {"ra": ""}])
def test_required_inputs_are_enforced(missing):
    r = _post(midtimes_text=FULL, **missing)
    assert r.status_code == 400 and r.json()["ok"] is False
