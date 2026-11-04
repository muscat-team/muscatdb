"""Safety regressions for PR364 review: no live observing calls."""

import copy
import json
import sqlite3

import pytest

from muscat_db import lco, lco_monitor, lco_rescue
from tests.test_lco_rescue import rescue as rescue_fixture, row, poll

rescue = rescue_fixture


def attempt(path):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        result = conn.execute("SELECT * FROM lco_rescue_attempts WHERE original_id=101").fetchone()
        return dict(result) if result else None


def change_payload(rescue, change):
    path, _, group, *_ = rescue
    with sqlite3.connect(path) as conn:
        params = json.loads(row(path)["payload_json"])
        change(params["_requestgroup"]["requests"][0])
        change(group["requests"][0])
        conn.execute(
            "UPDATE lco_observation_requests SET payload_json=? WHERE request_id=101",
            (json.dumps(params),),
        )


def retry(rescue):
    path, now, group, history, calls = rescue
    poll((path, now + 301, group, history, calls))


def test_canceled_observation_is_not_a_failure_trigger(rescue):
    rescue[3][0]["state"] = "CANCELED"
    poll(rescue)
    assert rescue[-1] == []
    assert attempt(rescue[0]) is None


@pytest.mark.parametrize("alternative", [False, True])
def test_viable_original_is_preserved_even_after_abort(rescue, monkeypatch, alternative):
    monkeypatch.setenv("MUSCAT_LCO_RESCUE_BUDGET_MINUTES", '{"P1": 240}')

    def widen(child):
        long = {**child["windows"][0], "end": "2026-11-01T14:00:00Z"}
        child["windows"] = [*child["windows"], long] if alternative else [long]

    change_payload(rescue, widen)
    poll(rescue)
    assert rescue[-1] == []
    assert attempt(rescue[0]) is None
    assert "still fits" in row(rescue[0])["rescue_reason"]


def test_not_attempted_waits_until_observation_end(rescue):
    rescue[3][0].update(state="NOT_ATTEMPTED", end="2026-11-01T11:00:00Z")
    poll(rescue)
    assert rescue[-1] == []


def test_unpinned_original_is_not_narrowed_to_a_failed_site(rescue):
    change_payload(rescue, lambda child: child["location"].pop("site", None))
    poll(rescue)
    assert rescue[-1] == []


def test_acceptance_and_registration_precede_original_cancellation(rescue, monkeypatch):
    path, _, _, _, calls = rescue
    real_api = lco._lco_api_request

    def api(url, method="GET", **kw):
        if url.endswith("/cancel/"):
            assert row(path, 202)["supersedes_request_id"] == 101
            assert attempt(path)["result_json"] != "{}"
        return real_api(url, method=method, **kw)

    monkeypatch.setattr(lco, "_lco_api_request", api)
    poll(rescue)
    assert [c[0] for c in calls] == ["submit", "cancel"]


def test_definite_rejection_preserves_original_and_releases_budget(rescue, monkeypatch):
    path, _, group, _, calls = rescue

    def reject(*a, **kw):
        calls.append(("submit", {}))
        raise lco.LcoError("validation rejected", status=400)

    monkeypatch.setattr(lco, "submit_requestgroup", reject)
    poll(rescue)
    retry(rescue)
    assert group["state"] == "PENDING"
    assert [c[0] for c in calls] == ["submit"]
    assert attempt(path)["state"] == "rejected"
    assert attempt(path)["reserved_seconds"] == 0
    with sqlite3.connect(path) as conn:
        notices = conn.execute("SELECT visible_to,text FROM chat_messages").fetchall()
    assert len(notices) == 1
    assert notices[0][0] == "alice"
    assert "rejected" in notices[0][1]


def uncertain_portal(rescue, monkeypatch, matches=1):
    _, _, group, _, calls = rescue
    accepted = []
    real_api = lco._lco_api_request

    def submit(payload, user, **kw):
        calls.append(("submit", copy.deepcopy(payload)))
        result = {**copy.deepcopy(payload), "id": 20, "state": "PENDING"}
        result["requests"][0].update(id=202, state="PENDING")
        accepted.append(result)
        raise TimeoutError("response lost after acceptance")

    def api(url, method="GET", **kw):
        if "/requestgroups/?" in url:
            assert "proposal=P1" in url and "name=" in url
            results = [copy.deepcopy(accepted[0]) for _ in range(matches)]
            if len(results) > 1:
                results[1]["id"] = 21
                results[1]["requests"][0]["id"] = 203
            return {"results": results, "next": None}
        return real_api(url, method=method, **kw)

    def get_group(identifier, **kw):
        if int(identifier) == 10:
            return copy.deepcopy(group)
        return copy.deepcopy(accepted[0])

    monkeypatch.setattr(lco, "submit_requestgroup", submit)
    monkeypatch.setattr(lco, "_lco_api_request", api)
    monkeypatch.setattr(lco, "get_requestgroup", get_group)
    return accepted


def test_accepted_then_timeout_reconciles_without_second_post(rescue, monkeypatch):
    path, _, group, _, calls = rescue
    accepted = uncertain_portal(rescue, monkeypatch)
    poll(rescue)
    assert group["state"] == "PENDING"
    assert attempt(path)["state"] == "uncertain"
    assert len(accepted[0]["name"]) <= 50
    retry(rescue)
    assert [c[0] for c in calls] == ["submit", "cancel"]
    assert row(path, 202)["supersedes_request_id"] == 101
    assert attempt(path)["state"] == "submitted"


@pytest.mark.parametrize("matches", [0, 2])
def test_missing_or_ambiguous_search_does_not_repeat_or_cancel(rescue, monkeypatch, matches):
    path, _, group, _, calls = rescue
    uncertain_portal(rescue, monkeypatch, matches)
    poll(rescue)
    retry(rescue)
    assert [c[0] for c in calls] == ["submit"]
    assert group["state"] == "PENDING"
    assert attempt(path)["state"] == "uncertain"
    assert attempt(path)["reserved_seconds"] > 0


def test_failed_cancel_keeps_accepted_replacement_and_retries_cleanup(rescue, monkeypatch):
    path, _, group, _, calls = rescue
    real_api = lco._lco_api_request

    def reject_cancel(url, method="GET", **kw):
        if url.endswith("/cancel/"):
            calls.append(("cancel", url))
            raise lco.LcoError("cancel refused", status=400)
        return real_api(url, method=method, **kw)

    monkeypatch.setattr(lco, "_lco_api_request", reject_cancel)
    poll(rescue)
    assert group["state"] == "PENDING"
    assert row(path, 202)["supersedes_request_id"] == 101
    assert attempt(path)["state"] == "cleanup_pending"
    assert attempt(path)["reserved_seconds"] > 0
    monkeypatch.setattr(lco, "_lco_api_request", real_api)
    retry(rescue)
    assert [c[0] for c in calls] == ["submit", "cancel", "cancel"]
    assert attempt(path)["state"] == "submitted"


@pytest.mark.parametrize("late_change", ["scheduled", "disabled"])
def test_second_safety_check_blocks_post_after_ipp(rescue, monkeypatch, late_change):
    path, _, _, history, calls = rescue

    def ipp(*a, **kw):
        if late_change == "scheduled":
            history.append({"id": 2, "state": "PENDING"})
        else:
            monkeypatch.setenv("MUSCAT_LCO_ALLOW_SUBMIT", "0")
        return {"P1": {"2M0": {"max_allowable_ipp_value": 1.5}}}

    monkeypatch.setattr(lco, "max_allowable_ipp", ipp)
    poll(rescue)
    assert calls == []
    assert attempt(path) is None


def test_running_original_after_acceptance_is_not_canceled(rescue, monkeypatch):
    path, _, group, history, calls = rescue
    real_submit = lco.submit_requestgroup

    def submit(*a, **kw):
        result = real_submit(*a, **kw)
        history.append({"id": 2, "state": "IN_PROGRESS"})
        return result

    monkeypatch.setattr(lco, "submit_requestgroup", submit)
    poll(rescue)
    assert [c[0] for c in calls] == ["submit"]
    assert group["state"] == "PENDING"
    assert row(path, 202)["supersedes_request_id"] == 101
    assert "duplicate" in row(path)["rescue_reason"]


def test_cannot_delete_tracking_needed_for_uncertain_reconciliation(rescue, monkeypatch):
    path, *_ = rescue
    uncertain_portal(rescue, monkeypatch, 0)
    poll(rescue)
    with pytest.raises(lco.LcoError) as error:
        lco_monitor.delete_request(101, path=path)
    assert error.value.status == 409


@pytest.mark.parametrize("mutation", ["payload", "name", "pagination"])
def test_uncertain_search_requires_complete_exact_match(rescue, monkeypatch, mutation):
    path, _, group, _, calls = rescue
    accepted = uncertain_portal(rescue, monkeypatch)
    poll(rescue)
    if mutation == "payload":
        accepted[0]["requests"][0]["configurations"][0]["target"]["ra"] += 1
    elif mutation == "name":
        accepted[0]["name"] += "_other"
    else:
        real_api = lco._lco_api_request

        def api(url, **kw):
            result = real_api(url, **kw)
            if "/requestgroups/?" in url:
                result["next"] = "https://example.invalid/untrusted"
            return result

        monkeypatch.setattr(lco, "_lco_api_request", api)
    retry(rescue)
    assert [c[0] for c in calls] == ["submit"]
    assert group["state"] == "PENDING"
    assert attempt(path)["reserved_seconds"] > 0


def test_uncertain_notice_is_private_and_deduplicated(rescue, monkeypatch):
    path, now, *_ = rescue
    uncertain_portal(rescue, monkeypatch, 0)
    poll(rescue)
    retry(rescue)
    poll((path, now + 602, *rescue[2:]))
    with sqlite3.connect(path) as conn:
        notices = conn.execute("SELECT visible_to FROM chat_messages").fetchall()
    assert notices == [("alice",)]


@pytest.mark.parametrize("monitor_state", ["canceled", "complete", "terminal_no_data"])
def test_reconciliation_sweep_survives_terminal_original(rescue, monkeypatch, monitor_state):
    path, now, *_ = rescue
    uncertain_portal(rescue, monkeypatch)
    poll(rescue)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE lco_observation_requests SET monitor_state=? WHERE request_id=101",
            (monitor_state,),
        )
    assert lco_rescue.resume_terminal_accepted(path=path, now=now + 301, limit=1) == 1
    assert row(path, 202)["supersedes_request_id"] == 101
    assert attempt(path)["state"] == "submitted"


def test_disabled_gate_preserves_both_after_acceptance(rescue, monkeypatch):
    path, _, group, _, calls = rescue
    real_submit = lco.submit_requestgroup

    def submit(*a, **kw):
        result = real_submit(*a, **kw)
        monkeypatch.setenv("MUSCAT_LCO_ALLOW_SUBMIT", "0")
        return result

    monkeypatch.setattr(lco, "submit_requestgroup", submit)
    poll(rescue)
    assert [c[0] for c in calls] == ["submit"]
    assert group["state"] == "PENDING"
    assert row(path, 202)["supersedes_request_id"] == 101
    assert attempt(path)["state"] == "cleanup_pending"


def test_deleted_original_cannot_claim_budget(rescue):
    path, now, *_ = rescue
    lco_monitor.delete_request(101, path=path)
    assert not lco_rescue._claim(path, 101, "P1", 60, 100, {}, {}, now)


def test_private_notice_uses_chat_wire_format(rescue, monkeypatch):
    from muscat_db import chat

    emitted = []
    monkeypatch.setattr(
        chat, "emit_persisted_system_message", lambda msg, owner: emitted.append((msg, owner))
    )
    poll(rescue)
    message, owner = emitted[0]
    assert owner == message["visible_to"] == "alice"
    assert message["user"] == "system"
    assert message["ts"] == rescue[1]
    assert message["reactions"] == []


def test_saved_acceptance_registers_even_if_personal_token_removed(rescue, monkeypatch):
    path, *_ = rescue
    real_register = lco_rescue._register

    def interrupted(*a, **kw):
        raise RuntimeError("registration interrupted")

    monkeypatch.setattr(lco_rescue, "_register", interrupted)
    poll(rescue)
    monkeypatch.setattr(lco_rescue, "_register", real_register)

    def no_token(*a, **kw):
        raise lco.LcoError("Personal token removed", status=403)

    monkeypatch.setattr(lco, "_get_lco_api_token", no_token)
    retry(rescue)
    assert row(path, 202)["supersedes_request_id"] == 101
    assert attempt(path)["state"] == "cleanup_pending"


def test_completed_original_after_acceptance_reports_duplicate_risk(rescue, monkeypatch):
    path, _, group, _, calls = rescue
    real_submit = lco.submit_requestgroup

    def submit(*a, **kw):
        result = real_submit(*a, **kw)
        group["state"] = group["requests"][0]["state"] = "COMPLETED"
        return result

    monkeypatch.setattr(lco, "submit_requestgroup", submit)
    poll(rescue)
    assert [c[0] for c in calls] == ["submit"]
    with sqlite3.connect(path) as conn:
        notices = conn.execute("SELECT text FROM chat_messages WHERE visible_to='alice'").fetchall()
    assert any("duplicate" in n[0] for n in notices)
