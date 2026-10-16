"""Server-side TTV input resolution (#221): the O-C selection a client submits
is rebuilt into the fit's CSV and its recorded inputs from stored transit
fits, so the record cannot disagree with the data that was fitted."""

from __future__ import annotations

import math

import pytest

from muscat_db import ttv_selection as sel

EPHEM = {"b": {"t0": 2460000.0, "period": 2.0}, "c": {"t0": 2460000.5, "period": 5.0}}


def _fit(inst="sinistro", date="260101", target="TOI-1", run_id="r1", planet="b"):
    return {"kind": "product", "planet": planet, "instrument": inst, "date": date,
            "target": target, "run_id": run_id}


def _manual(planet="b", epoch=3, tc=2460006.01, unc=0.001, note="TESS"):
    return {"kind": "manual", "planet": planet, "epoch": epoch, "tc": tc, "tc_unc": unc, "note": note}


def _resolve(selection, letters="bc", ephem=EPHEM, fitted=None, available=True, hidden=lambda *a: False):
    fitted = fitted or (lambda inst, date, target, run_id: {"b": {"tc": 2460004.02, "unc": 0.002},
                                                            "c": {"tc": 2460005.49, "unc": 0.004}})
    return sel.resolve_selection(
        selection, letters, ephem,
        fitted_params=fitted,
        is_available=(available if callable(available) else (lambda *a: available)),
        is_hidden=hidden,
    )


def test_a_fit_point_takes_its_time_and_epoch_from_the_stored_fit():
    out, err = _resolve([_fit()])
    assert err is None
    assert out.csv_content == "planet,epoch,tc,tc_unc\n0,2,2460004.02,0.002\n"
    assert out.inputs == [{"kind": "product", "level": "transit_fit", "instrument": "sinistro",
                           "obsdate": "260101", "target": "TOI-1", "run_id": "r1"}]


def test_planet_column_is_the_index_among_the_selected_letters():
    out, _ = _resolve([_fit(planet="c")])
    assert out.csv_content.splitlines()[1].startswith("1,")


def test_manual_point_is_recorded_as_manual_and_keeps_its_own_values():
    out, err = _resolve([_manual()])
    assert err is None
    assert out.csv_content == "planet,epoch,tc,tc_unc\n0,3,2460006.01,0.001\n"
    assert out.inputs == [{"kind": "manual", "note": "TESS"}]


def test_rows_are_ordered_by_planet_then_epoch():
    out, _ = _resolve([_manual(planet="c", epoch=1, tc=2460005.6), _manual(epoch=9, tc=2460018.0),
                       _manual(epoch=1, tc=2460002.0)])
    assert [r.split(",")[:2] for r in out.csv_content.splitlines()[1:]] == [["0", "1"], ["0", "9"], ["1", "1"]]


def test_repeated_selection_items_collapse():
    out, _ = _resolve([_fit(), _fit()])
    assert len(out.csv_content.splitlines()) == 2
    assert len(out.inputs) == 1


@pytest.mark.parametrize("bad", [
    "nope", [1], [{"kind": "other", "planet": "b"}], [{"kind": "product", "planet": "b"}],
    [_fit(planet="z")], [{**_fit(), "run_id": 5}],
])
def test_malformed_selection_is_rejected(bad):
    out, err = _resolve(bad)
    assert out is None and err


def test_empty_selection_is_rejected():
    out, err = _resolve([])
    assert out is None and err


def test_a_fit_that_does_not_exist_is_rejected():
    out, err = _resolve([_fit()], available=False)
    assert out is None and "selection[0]" in err


def test_a_hidden_night_reads_the_same_as_a_missing_one():
    _, missing = _resolve([_fit()], available=False)
    _, hidden = _resolve([_fit()], hidden=lambda *a: True)
    assert hidden == missing


def test_a_fit_with_no_time_for_the_planet_is_rejected():
    out, err = _resolve([_fit(planet="c")], fitted=lambda *a: {"b": {"tc": 1.0, "unc": 0.1}})
    assert out is None and "selection[0]" in err


@pytest.mark.parametrize("unc", [None, 0, -1, math.nan, math.inf])
def test_a_fit_with_no_usable_uncertainty_is_rejected(unc):
    out, err = _resolve([_fit()], fitted=lambda *a: {"b": {"tc": 2460004.0, "unc": unc}})
    assert out is None and err


@pytest.mark.parametrize("point", [
    _manual(unc=0), _manual(unc=math.nan), _manual(tc=math.inf), _manual(epoch=1.5), _manual(epoch=True),
])
def test_a_bad_manual_point_is_rejected(point):
    out, err = _resolve([point])
    assert out is None and err


def test_a_fit_point_needs_a_valid_ephemeris_for_its_planet():
    out, err = _resolve([_fit()], ephem={"b": {"t0": 2460000.0, "period": 0}})
    assert out is None and err
    out, err = _resolve([_fit()], ephem={})
    assert out is None and err


def test_the_selection_size_is_capped():
    out, err = _resolve([_manual(epoch=i, tc=2460000.0 + i) for i in range(sel.MAX_SELECTION + 1)])
    assert out is None and "too many" in err


def test_planet_letters_and_ephemeris_keys_match_case_insensitively():
    out, err = _resolve([_fit(planet="B")], letters="BC", ephem={"B": EPHEM["b"], "C": EPHEM["c"]})
    assert err is None and out.csv_content.splitlines()[1].startswith("0,")


# -- the start endpoint --------------------------------------------------------

from fastapi.testclient import TestClient  # noqa: E402

from muscat_db.web import app  # noqa: E402


@pytest.fixture
def started(monkeypatch):
    """Capture what /api/ttv-fit/start hands to the fit launcher."""
    captured = {}
    monkeypatch.setattr("muscat_db.web.ttv.start_ttv_fit",
                        lambda target, options, user: captured.update(options=options) or {"ok": True})
    monkeypatch.setattr("muscat_db.web._require_visible_ttv_target", lambda t: None)
    monkeypatch.setattr("muscat_db.web._transit_fit_available", lambda: (lambda *a: True))
    monkeypatch.setattr("muscat_db.web._night_hidden", lambda *a: False)
    monkeypatch.setattr("muscat_db.web._get_run_fitted_params",
                        lambda inst, date, target, run_id: {"b": {"tc": 2460004.02, "unc": 0.002}})
    return captured


def _start(options):
    return TestClient(app, client=("127.0.0.1", 12345)).post(
        "/api/ttv-fit/start", json={"target": "TOI-1", "options": options})


def test_start_builds_the_csv_and_inputs_from_the_selection(started):
    forged = [{"kind": "product", "instrument": "muscat2", "date": "200101", "target": "X", "run_id": ""}]
    r = _start({"planet_letters": "b", "selection": [_fit()], "planets_ephem": EPHEM,
                "csv_content": "planet,epoch,tc,tc_unc\n0,99,1.0,1.0\n", "inputs": forged})
    assert r.status_code == 200
    assert started["options"]["csv_content"] == "planet,epoch,tc,tc_unc\n0,2,2460004.02,0.002\n"
    assert started["options"]["inputs"][0]["instrument"] == "sinistro"
    assert "selection" not in started["options"] and "planets_ephem" not in started["options"]


def test_start_without_a_selection_does_not_trust_submitted_inputs(started):
    forged = [{"kind": "product", "instrument": "muscat2", "date": "200101", "target": "X", "run_id": ""}]
    r = _start({"planet_letters": "b", "csv_content": "planet,epoch,tc,tc_unc\n0,1,1.0,1.0\n", "inputs": forged})
    assert r.status_code == 200
    assert started["options"]["inputs"] is None
    assert started["options"]["csv_content"].startswith("planet,epoch")


def test_start_rejects_an_invalid_selection_before_launching(started):
    r = _start({"planet_letters": "b", "selection": [_fit(planet="z")], "planets_ephem": EPHEM})
    assert r.status_code == 400
    assert "selection[0]" in r.json()["error"]
    assert "options" not in started
