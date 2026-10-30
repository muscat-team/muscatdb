"""Window selection, reconciliation pacing and state-race regressions for rescue."""

import copy
import json
import sqlite3

import pytest

from muscat_db import lco, lco_rescue
from muscat_db.lco_autoschedule import _timestamp
from tests.test_lco_rescue import rescue as rescue_fixture, row, poll
from tests.test_lco_rescue_safety import attempt, change_payload, uncertain_portal

rescue = rescue_fixture


def add_window(rescue, start, end):
    change_payload(rescue, lambda child: child["windows"].append({"start": start, "end": end}))


def clip_by_day(monkeypatch, invisible_day, cut_day=None, cut_end=None):
    def clip(kind, params, *a, **kw):
        assert kw["strict"]
        window = params["windows"][0]
        if str(window["start"]).startswith(invisible_day):
            raise lco.WindowNotObservable("Window 1 is not observable at OGG", status=400)
        if cut_day and str(window["start"]).startswith(cut_day):
            end = min(_timestamp(window["end"]), _timestamp(cut_end))
            window["end"] = end.isoformat().replace("+00:00", "Z")

    monkeypatch.setattr(lco, "_clip_windows_to_observability", clip)


def test_unobservable_current_window_does_not_block_other_windows(rescue, monkeypatch):
    path, _, _, _, calls = rescue
    add_window(rescue, "2026-11-02T10:00:00Z", "2026-11-02T12:00:00Z")
    clip_by_day(monkeypatch, "2026-11-01", "2026-11-02", "2026-11-02T11:10:00Z")
    poll(rescue)
    assert [c[0] for c in calls] == ["submit", "cancel"]
    window = calls[0][1]["requests"][0]["windows"][0]
    assert window == {"start": "2026-11-02T10:00:00Z", "end": "2026-11-02T11:10:00Z"}


def test_smaller_observable_window_wins_over_larger_clipped_one(rescue, monkeypatch):
    path, _, _, _, calls = rescue
    add_window(rescue, "2026-11-02T10:00:00Z", "2026-11-02T13:00:00Z")
    clip_by_day(monkeypatch, "never", "2026-11-02", "2026-11-02T10:20:00Z")
    poll(rescue)
    assert [c[0] for c in calls] == ["submit", "cancel"]
    window = calls[0][1]["requests"][0]["windows"][0]
    assert window == {"start": "2026-11-01T10:30:00Z", "end": "2026-11-01T12:00:00Z"}


def test_every_window_unobservable_is_reported_without_submission(rescue, monkeypatch):
    path, _, _, _, calls = rescue
    clip_by_day(monkeypatch, "2026-11-01")
    poll(rescue)
    assert not calls
    assert "no remaining window is observable" in row(path)["rescue_reason"]


@pytest.mark.parametrize(
    "error",
    [
        lco.LcoError("Cannot verify replacement observability: bad ra", status=400),
        RuntimeError("ephemeris unavailable"),
    ],
)
def test_observability_calculation_errors_fail_safe(rescue, monkeypatch, error):
    path, _, _, _, calls = rescue
    add_window(rescue, "2026-11-02T10:00:00Z", "2026-11-02T12:00:00Z")

    def clip(kind, params, *a, **kw):
        if str(params["windows"][0]["start"]).startswith("2026-11-01"):
            raise error

    monkeypatch.setattr(lco, "_clip_windows_to_observability", clip)
    poll(rescue)
    assert not calls
    assert attempt(path) is None
    assert str(error.args[0]).split(":")[0] in row(path)["rescue_reason"]


def test_uncertain_reconciliation_backs_off_to_daily(rescue, monkeypatch):
    path, now, _, _, calls = rescue
    uncertain_portal(rescue, monkeypatch, 0)
    searched = []
    portal = lco._lco_api_request

    def api(url, **kw):
        if "/requestgroups/?" in url:
            searched.append(url)
        return portal(url, **kw)

    monkeypatch.setattr(lco, "_lco_api_request", api)
    poll(rescue)
    times = []
    for step in range(1, 3 * 288):
        t = now + 1 + 300 * step
        before = len(searched)
        lco_rescue.resume_terminal_accepted(path=path, now=t, limit=10)
        if len(searched) > before:
            times.append(t)
    assert times[:3] == [now + 301, now + 901, now + 2101]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert gaps == sorted(gaps)
    assert max(gaps) == lco_rescue._MAX_RECONCILE_S
    assert [c[0] for c in calls] == ["submit"]
    assert attempt(path)["state"] == "uncertain"
    assert attempt(path)["reserved_seconds"] > 0
    with sqlite3.connect(path) as conn:
        events = [r[0] for r in conn.execute("SELECT event FROM lco_rescue_notices")]
    assert events.count("manual_review") == 1


def test_stale_post_success_cannot_revert_reconciled_attempt(rescue, monkeypatch):
    path, _, _, _, calls = rescue
    real_submit = lco.submit_requestgroup

    def submit(payload, user, **kw):
        result = real_submit(payload, user, **kw)
        with sqlite3.connect(path) as conn:
            conn.execute(
                "UPDATE lco_rescue_attempts SET state='submitted',result_json=? WHERE original_id=101",
                (json.dumps(result),),
            )
        return result

    monkeypatch.setattr(lco, "submit_requestgroup", submit)
    poll(rescue)
    assert [c[0] for c in calls] == ["submit"]
    assert attempt(path)["state"] == "submitted"


def test_stale_post_failure_cannot_release_reconciled_budget(rescue, monkeypatch):
    path, _, group, _, calls = rescue

    def submit(payload, user, **kw):
        calls.append(("submit", copy.deepcopy(payload)))
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE lco_rescue_attempts SET state='accepted' WHERE original_id=101")
        raise lco.LcoError("validation rejected", status=400)

    monkeypatch.setattr(lco, "submit_requestgroup", submit)
    poll(rescue)
    assert attempt(path)["state"] == "accepted"
    assert attempt(path)["reserved_seconds"] > 0
    assert "rejected" not in row(path)["rescue_reason"]


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda child: child["location"].pop("site"), "pinned site"),
        (lambda child: child.update(configuration_repeats=2), "configuration_repeats"),
    ],
)
def test_unsupported_rescue_opt_in_is_rejected_at_validation(rescue, change, message):
    from muscat_db import web

    group = copy.deepcopy(rescue[2])
    change(group["requests"][0])
    with pytest.raises(lco.LcoError, match=message) as error:
        web._validate_lco_rescue({"auto_resubmit": {"min_minutes": 30}}, group, "alice")
    assert error.value.status == 400
    assert web._validate_lco_rescue({}, group, "alice") is False
