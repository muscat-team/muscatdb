"""Exercise rescue through the monitor without contacting LCO."""

import copy
import json
import sqlite3

import pytest

from muscat_db import lco, lco_monitor, lco_rescue
from muscat_db.database import SCHEMA


@pytest.fixture
def rescue(monkeypatch, tmp_path):
    path = str(tmp_path / "rescue.db")
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
    monkeypatch.setenv("MUSCAT_LCO_ALLOW_SUBMIT", "1")
    monkeypatch.setenv("MUSCAT_LCO_RESCUE_BUDGET_MINUTES", '{"P1": 180}')
    monkeypatch.setattr(lco, "_get_lco_api_token", lambda user, **kw: "personal")
    window = {"start": "2026-11-01T10:00:00Z", "end": "2026-11-01T12:00:00Z"}
    child = {
        "location": {"site": "ogg", "telescope_class": "2m0"},
        "windows": [window],
        "configurations": [
            {
                "type": "REPEAT_EXPOSE",
                "repeat_duration": 7000,
                "target": {"name": "TOI-123", "ra": 20, "dec": 30},
                "constraints": {"max_airmass": 2.5, "min_lunar_distance": 18},
                "instrument_type": "2M0-SCICAM-MUSCAT",
                "instrument_configs": [{"exposure_time": 30}],
            }
        ],
    }
    payload = {
        "name": "Transit",
        "proposal": "P1",
        "operator": "SINGLE",
        "observation_type": "NORMAL",
        "requests": [child],
    }
    group = {**copy.deepcopy(payload), "id": 10, "state": "PENDING"}
    group["requests"][0].update(id=101, state="PENDING")
    params = {
        "kind": "muscat",
        "auto_resubmit": {"min_fraction": 0.5, "min_minutes": 30},
        "_requestgroup": payload,
        "requests": [child],
        "proposal": "P1",
    }
    now = lco_monitor._iso_timestamp("2026-11-01T10:30:00Z")
    lco_monitor.record_submission(group, params, "alice", path=path, now=now - 1800)
    history = [
        {
            "id": 1,
            "state": "ABORTED",
            "site": "ogg",
            "start": window["start"],
            "end": "2026-11-01T10:25:00Z",
        }
    ]
    calls = []
    monkeypatch.setattr(lco, "get_requestgroup", lambda *a, **kw: copy.deepcopy(group))
    monkeypatch.setattr(lco, "archive_search_all", lambda *a, **kw: {"results": []})
    monkeypatch.setattr(lco, "_clip_windows_to_observability", lambda *a, **kw: None)
    monkeypatch.setattr(
        lco, "max_allowable_ipp", lambda *a, **kw: {"P1": {"2M0": {"max_allowable_ipp_value": 1.5}}}
    )

    def api(url, method="GET", **kw):
        assert kw["token"] == "personal"
        if url.endswith("/observations/"):
            return copy.deepcopy(history)
        assert url.endswith("/cancel/") and method == "POST"
        calls.append(("cancel", url))
        group["state"] = "CANCELED"
        return copy.deepcopy(group)

    monkeypatch.setattr(lco, "_lco_api_request", api)

    def submit(payload, user, **kw):
        calls.append(("submit", copy.deepcopy(payload)))
        result = {**copy.deepcopy(payload), "id": 20, "state": "PENDING"}
        result["requests"][0].update(id=202, state="PENDING")
        return result

    monkeypatch.setattr(lco, "submit_requestgroup", submit)
    return path, now, group, history, calls


def row(path, identifier=101):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return dict(
            conn.execute(
                "SELECT * FROM lco_observation_requests WHERE request_id=?", (identifier,)
            ).fetchone()
        )


def poll(rescue):
    path, now, *_ = rescue
    lco_monitor.process_request(row(path), path=path, now=now)


def test_dropped_observation_resubmits_once_across_restart(rescue):
    path, _, _, _, calls = rescue
    poll(rescue)
    assert [call[0] for call in calls] == ["cancel", "submit"]
    payload = calls[1][1]
    assert payload["name"] == "Transit_auto"
    assert payload["requests"][0]["windows"][0]["start"] == "2026-11-01T10:30:00Z"
    assert (
        payload["requests"][0]["configurations"][0]["repeat_duration"]
        == 5400 - lco._REPEAT_EXPOSE_SETUP_OVERHEAD_S
    )
    assert row(path, 202)["supersedes_request_id"] == 101
    assert json.loads(row(path, 202)["payload_json"])["auto_resubmit"] == {}
    poll(rescue)
    assert len(calls) == 2


@pytest.mark.parametrize("state", ["CANCELED", "COMPLETED"])
def test_manual_cancellation_and_completion_are_never_replaced(rescue, state):
    path, _, group, _, calls = rescue
    group["state"] = state
    group["requests"][0]["state"] = state
    poll(rescue)
    assert not calls
    assert "not resubmitted" in row(path)["rescue_reason"]


@pytest.mark.parametrize("state", ["PENDING", "IN_PROGRESS"])
def test_existing_scheduled_observation_prevents_replacement(rescue, state):
    path, _, _, history, calls = rescue
    history.append({"id": 2, "state": state})
    poll(rescue)
    assert not calls
    assert "already has" in row(path)["rescue_reason"]


def test_short_remainder_is_not_replaced(rescue):
    path, now, group, history, calls = rescue
    now = lco_monitor._iso_timestamp("2026-11-01T11:50:00Z")
    poll((path, now, group, history, calls))
    assert not calls
    assert "threshold" in row(path)["rescue_reason"]


def test_uncertain_post_is_not_retried(rescue, monkeypatch):
    path, _, _, _, calls = rescue

    def timeout(*a, **kw):
        calls.append(("submit", {}))
        raise TimeoutError("response lost")

    monkeypatch.setattr(lco, "submit_requestgroup", timeout)
    poll(rescue)
    poll(rescue)
    assert [c[0] for c in calls] == ["cancel", "submit"]
    assert "manual reconciliation" in row(path)["rescue_reason"]


def test_proposal_budget_is_shared_and_reservation_is_atomic(rescue):
    path, now, *_ = rescue
    assert lco_rescue._claim(path, 101, "P1", 60, 100, {}, {}, now)
    assert not lco_rescue._claim(path, 102, "P1", 60, 100, {}, {}, now)
    assert not lco_rescue._claim(path, 101, "P1", 1, 100, {}, {}, now)


def test_personal_token_required_and_submit_gate_respected(rescue, monkeypatch):
    path, _, _, _, calls = rescue
    monkeypatch.setenv("MUSCAT_LCO_ALLOW_SUBMIT", "0")
    poll(rescue)
    assert not calls
    assert "disabled" in row(path)["rescue_reason"]
    monkeypatch.setenv("MUSCAT_LCO_ALLOW_SUBMIT", "1")

    def absent(*a, **kw):
        raise lco.LcoError("Save your personal token", status=403)

    monkeypatch.setattr(lco, "_get_lco_api_token", absent)
    poll(rescue)
    assert not calls
    assert "personal token" in row(path)["rescue_reason"]


def test_observed_exposure_and_visibility_are_excluded(rescue, monkeypatch):
    path, _, _, _, calls = rescue
    monkeypatch.setattr(
        lco,
        "archive_search_all",
        lambda *a, **kw: {
            "results": [
                {
                    "id": 1,
                    "filename": "ogg2m001-ep05-20261101-0001-e00.fits.fz",
                    "observation_date": "2026-11-01T10:59:30Z",
                    "exposure_time": 30,
                }
            ]
        },
    )
    # Exercise recovery directly to avoid downloading the fixture frame.
    request = row(path)
    _, now, group, *_ = rescue
    frames = lco.archive_search_all({})["results"]
    lco_rescue.recover(request, group, group["requests"][0], frames, path=path, now=now)
    assert calls[1][1]["requests"][0]["windows"][0]["start"] == "2026-11-01T11:00:00Z"


def test_observability_clipping_rechecks_original_threshold(rescue, monkeypatch):
    path, _, _, _, calls = rescue

    def clip(kind, params, *a, **kw):
        assert kw["strict"]
        params["windows"][0]["end"] = "2026-11-01T11:00:00Z"

    monkeypatch.setattr(lco, "_clip_windows_to_observability", clip)
    poll(rescue)
    assert not calls
    assert "observable remainder" in row(path)["rescue_reason"]


def test_cannot_cancel_sibling_requests(rescue):
    path, _, group, _, calls = rescue
    group["requests"].append({**copy.deepcopy(group["requests"][0]), "id": 102})
    poll(rescue)
    assert not calls
    assert "single-request" in row(path)["rescue_reason"]


def test_accepted_response_can_be_registered_after_restart(rescue, monkeypatch):
    path, _, _, _, calls = rescue
    real_register = lco_rescue._register

    def fail(*a, **kw):
        raise RuntimeError("database interrupted")

    monkeypatch.setattr(lco_rescue, "_register", fail)
    poll(rescue)
    assert len(calls) == 2
    monkeypatch.setattr(lco_rescue, "_register", real_register)
    poll(rescue)
    assert row(path, 202)["supersedes_request_id"] == 101
    assert len(calls) == 2


def test_failed_dry_run_never_cancels_or_submits(rescue, monkeypatch):
    path, _, _, _, calls = rescue
    monkeypatch.setattr(
        lco, "max_allowable_ipp", lambda *a, **kw: {"errors": {"proposal": "no allocation"}}
    )
    poll(rescue)
    assert not calls
    assert "dry-run" in row(path)["rescue_reason"]


def test_cancel_during_dry_run_is_preserved(rescue, monkeypatch):
    path, _, group, _, calls = rescue

    def validate(*a, **kw):
        group["state"] = "CANCELED"
        return {"P1": {}}

    monkeypatch.setattr(lco, "max_allowable_ipp", validate)
    poll(rescue)
    assert not calls
    assert "changed during validation" in row(path)["rescue_reason"]


def test_registration_recovery_precedes_terminal_archive_fast_path(rescue, monkeypatch):
    path, now, _, _, _ = rescue
    real_register = lco_rescue._register

    def fail(*a, **kw):
        raise RuntimeError("interrupted registration")

    monkeypatch.setattr(lco_rescue, "_register", fail)
    poll(rescue)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE lco_observation_requests SET request_state='CANCELED',raw_frame_count=1,reduced_frame_count=1,downloaded_count=1 WHERE request_id=101"
        )
    monkeypatch.setattr(lco_rescue, "_register", real_register)
    monkeypatch.setattr(lco_monitor, "_process_datasets", lambda *a, **kw: None)
    lco_monitor.process_request(row(path), path=path, now=now)
    assert row(path, 202)["supersedes_request_id"] == 101


def test_submission_validation_uses_actual_edited_proposal(rescue, monkeypatch):
    from muscat_db import web

    _, _, group, *_ = rescue
    payload = {"auto_resubmit": {"min_minutes": 30}, "proposal": "P1"}
    assert web._validate_lco_rescue(payload, group, "alice")
    with pytest.raises(lco.LcoError, match="budget"):
        web._validate_lco_rescue(payload, {**group, "proposal": "different-proposal"}, "alice")
    assert not web._validate_lco_rescue({"auto_resubmit": {"min_minutes": 0}}, group)


def test_proposal_usage_counts_accepted_and_submitted(rescue):
    path, *_ = rescue
    poll(rescue)
    usage = lco_rescue.proposal_usage(path)
    assert usage == [{"proposal": "P1", "attempts": 1, "submitted": 1, "reserved_minutes": 90.0}]


def test_failed_registration_does_not_block_archive_monitoring(rescue, monkeypatch):
    path, _, _, _, calls = rescue
    attempts = []

    def fail(*a, **kw):
        attempts.append(a)
        raise ValueError("Replacement response must contain exactly one child request")

    monkeypatch.setattr(lco_rescue, "_register", fail)
    poll(rescue)
    searches = []

    def search(*a, **kw):
        searches.append(a)
        return {"results": []}

    monkeypatch.setattr(lco, "archive_search_all", search)
    attempts.clear()
    poll(rescue)
    assert searches
    assert len(attempts) == 1
    assert len(calls) == 2
    state = row(path)
    assert state["error_count"] == 0
    assert "registration failed" in state["rescue_reason"]
    assert "exactly one child" in state["rescue_reason"]
