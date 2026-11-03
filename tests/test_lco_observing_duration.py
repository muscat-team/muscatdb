"""The observing-duration field of /lco/schedule (#360).

The field shows the ``repeat_duration`` each request will submit and may be
set shorter. Its default comes from ``/api/lco/observing-duration``, which
runs the same derivation ``build_requestgroup`` uses, so these tests pin that
the two agree, that a shorter explicit value is submitted as given, and that a
longer one is refused rather than clamped.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from muscat_db import lco
from muscat_db.web import app

# A 3 h window: the fit is 10800 s less the 180 s setup overhead.
MUSCAT = {
    "name": "n", "proposal": "p", "target_name": "t",
    "ra": 10.0, "dec": -5.0, "kind": "muscat4",
    "exposure_times": {"g": 30, "r": 30, "i": 30, "z": 30},
    "windows": [{"start": "2026-07-04T07:00:00Z", "end": "2026-07-04T10:00:00Z"}],
}
FIT = 10800 - 180


def _config(params, kind="muscat4"):
    return lco.build_requestgroup(kind, params)["requests"][0]["configurations"][0]


def test_window_fit_is_what_build_requestgroup_submits():
    assert lco.window_repeat_duration("muscat4", MUSCAT) == FIT
    assert _config(MUSCAT)["repeat_duration"] == FIT


@patch("muscat_db.transit_obs.observable_interval")
def test_window_fit_leaves_the_params_alone(mock_interval):
    """Clipping strips the page's site hints and caps; the caller's dict keeps them."""
    mock_interval.return_value = {
        "start": "2026-07-04T07:00:00Z", "end": "2026-07-04T10:00:00Z",
        "fraction": 1.0, "hit_start_limit": False, "hit_end_limit": False,
    }
    params = {**MUSCAT, "windows": [{**MUSCAT["windows"][0], "site": "coj"}], "repeat_duration": 600}
    before = repr(params)
    assert lco.window_repeat_duration("muscat4", params) == FIT
    assert repr(params) == before


@patch("muscat_db.transit_obs.observable_interval")
def test_either_site_fit_matches_the_build(mock_interval):
    """The fit is sized to the listed site seeing the least, like the build."""
    by_site = {
        "lsc": {"start": "2026-07-04T07:00:00Z", "end": "2026-07-04T10:00:00Z",
                "fraction": 1.0, "hit_start_limit": False, "hit_end_limit": False},
        "cpt": {"start": "2026-07-04T07:00:00Z", "end": "2026-07-04T09:00:00Z",
                "fraction": 0.67, "hit_start_limit": False, "hit_end_limit": True},
    }
    mock_interval.side_effect = lambda ra, dec, window, site, **kw: by_site[site]
    params = {
        **MUSCAT, "kind": "sinistro", "type": "REPEAT_EXPOSE", "filter": "rp", "exposure_time": 60,
        "windows": [{**MUSCAT["windows"][0], "sites": ["lsc", "cpt"]}],
    }
    expected = 7200 - 900 - 180  # cpt's 2 h run, less its set-edge margin and the setup overhead
    assert lco.window_repeat_duration("sinistro", params) == expected
    assert _config(params, "sinistro")["repeat_duration"] == expected


def test_a_shorter_explicit_duration_is_submitted_as_given():
    assert _config({**MUSCAT, "repeat_duration": 3600})["repeat_duration"] == 3600


def test_a_longer_explicit_duration_is_refused_not_clamped():
    with pytest.raises(lco.LcoError, match="exceeds the 10620 s the window fits"):
        _config({**MUSCAT, "repeat_duration": FIT + 1})


@pytest.mark.parametrize("bad", [0, -60, "soon"])
def test_a_non_positive_or_non_numeric_duration_is_refused(bad):
    with pytest.raises(lco.LcoError):
        _config({**MUSCAT, "repeat_duration": bad})


def test_expose_requests_have_no_fit():
    assert lco.window_repeat_duration("sinistro", {**MUSCAT, "kind": "sinistro"}) is None
    assert lco.window_repeat_duration("muscat4", {**MUSCAT, "type": "EXPOSE"}) is None


def test_endpoint_returns_the_fit():
    body = TestClient(app).post("/api/lco/observing-duration", json=MUSCAT).json()
    assert body == {"ok": True, "repeat_duration": FIT}


def test_endpoint_returns_null_for_an_expose_request():
    body = TestClient(app).post("/api/lco/observing-duration", json={**MUSCAT, "type": "EXPOSE"}).json()
    assert body == {"ok": True, "repeat_duration": None}


@patch("muscat_db.transit_obs.observable_interval", return_value=None)
def test_endpoint_reports_an_unobservable_window(mock_interval):
    params = {**MUSCAT, "site": "coj", "windows": [{**MUSCAT["windows"][0], "site": "coj"}]}
    resp = TestClient(app).post("/api/lco/observing-duration", json=params)
    assert resp.status_code == 400
    assert "not observable at COJ" in resp.json()["error"]
