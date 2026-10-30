from unittest.mock import Mock
import sqlite3

import pytest
from fastapi.testclient import TestClient

from muscat_db import lco, lco_monitor, web
from tests.test_lco_monitor import _submission


@pytest.fixture
def monitored(mock_db, monkeypatch):
    result, payload = _submission()
    lco_monitor.record_submission(result, payload, "alice")
    monkeypatch.setattr(web, "_request_user", lambda request: "alice")
    monkeypatch.setattr(web, "_jobs_user_is_admin", lambda user: user == "admin")
    return mock_db, TestClient(web.app)


def test_cancel_uses_own_token_and_gate(monkeypatch):
    call = Mock(return_value={"state": "CANCELED"})
    monkeypatch.setattr(lco, "_lco_api_request", call)
    monkeypatch.delenv("MUSCAT_LCO_ALLOW_SUBMIT", raising=False)
    with pytest.raises(lco.LcoError):
        lco.cancel_requestgroup(10, "alice")
    call.assert_not_called()
    monkeypatch.setenv("MUSCAT_LCO_ALLOW_SUBMIT", "1")
    lco.cancel_requestgroup(10, "alice")
    call.assert_called_once_with(
        "https://observe.lco.global/api/requestgroups/10/cancel/",
        method="POST",
        user_name="alice",
        require_own_token=True,
    )


def test_cancel_updates_group_and_stops_polling(monitored, monkeypatch):
    path, client = monitored
    result, payload = _submission(request_id=102)
    lco_monitor.record_submission(result, payload, "alice")
    cancel = Mock(return_value={"state": "CANCELED"})
    monkeypatch.setattr(lco, "cancel_requestgroup", cancel, raising=False)
    response = client.post("/api/lco/monitored-requests/101/cancel", json={"confirm": True})
    assert response.status_code == 200
    cancel.assert_called_once_with(10, "alice")
    assert {r["request_state"] for r in lco_monitor.list_requests()} == {"CANCELED"}
    poll = Mock()
    monkeypatch.setattr(lco_monitor, "process_request", poll)
    lco_monitor.process_due(path=path, now=9e12)
    poll.assert_not_called()


@pytest.mark.parametrize("action,method", [("cancel", "post"), ("", "delete")])
def test_mutations_require_owner_visibility_and_confirmation(
    monitored, monkeypatch, action, method
):
    _, client = monitored
    url = "/api/lco/monitored-requests/101" + ("/" + action if action else "")
    assert client.request(method, url, json={}).status_code == 400
    monkeypatch.setattr(web, "_request_user", lambda request: "bob")
    assert client.request(method, url, json={"confirm": True}).status_code == 403
    monkeypatch.setattr(web, "_request_user", lambda request: "admin")
    monkeypatch.setattr(web, "_viewer_denied", lambda: {"LCO2026B-001"})
    assert client.request(method, url, json={"confirm": True}).status_code == 404


def test_failed_cancel_does_not_change_local_state(monitored, monkeypatch):
    _, client = monitored
    monkeypatch.setattr(
        lco,
        "cancel_requestgroup",
        Mock(side_effect=lco.LcoError("refused", status=403)),
        raising=False,
    )
    assert (
        client.post("/api/lco/monitored-requests/101/cancel", json={"confirm": True}).status_code
        == 403
    )
    assert lco_monitor.list_requests()[0]["request_state"] == "PENDING"


def seed_ingested(path):
    with sqlite3.connect(path) as conn:
        for date in ("260720", "260721"):
            filename = "frame-" + date
            conn.execute(
                "INSERT INTO frames(instrument,obsdate,ccd,filename,object) VALUES ('muscat3',?,0,?,'Actual FITS target')",
                (date, filename),
            )
            conn.execute(
                "INSERT INTO lco_observation_frames(request_id,frame_id,filename,instrument,obsdate,state,metadata_json,updated_at) VALUES (101,?,?,'muscat3',?,'ingested','{}',0)",
                (date, filename + ".fits.fz", date),
            )
        conn.execute("UPDATE lco_observation_requests SET monitor_state='complete'")


def test_links_use_ingested_objects_and_delete_preserves_science(monitored):
    path, client = monitored
    seed_ingested(path)
    row = client.get("/api/lco/monitored-requests").json()["requests"][0]
    assert row["datasets"] == [
        {"instrument": "muscat3", "obsdate": date, "target": "Actual FITS target"}
        for date in ("260720", "260721")
    ]
    response = client.request("DELETE", "/api/lco/monitored-requests/101", json={"confirm": True})
    assert response.status_code == 200
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM lco_observation_requests").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM lco_observation_frames").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 2


@pytest.mark.parametrize("user", ["alice", "admin"])
def test_cancel_http_uses_acting_users_token(monitored, monkeypatch, user):
    _, client = monitored
    monkeypatch.setenv("MUSCAT_LCO_ALLOW_SUBMIT", "1")
    monkeypatch.setenv("LCO_API_TOKEN", "shared-never-used")
    monkeypatch.setattr(web, "_request_user", lambda request: user)
    monkeypatch.setattr(lco, "get_user_lco_token", lambda username: "token-for-" + username)
    response = Mock(status=200)
    response.read.return_value = b'{"id": 10, "state": "CANCELED"}'
    opener = Mock()
    opener.open.return_value.__enter__ = Mock(return_value=response)
    opener.open.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(lco, "_API_OPENER", opener)
    assert (
        client.post("/api/lco/monitored-requests/101/cancel", json={"confirm": True}).status_code
        == 200
    )
    req = opener.open.call_args.args[0]
    assert req.method == "POST"
    assert req.full_url.endswith("/requestgroups/10/cancel/")
    assert req.get_header("Authorization") == "Token token-for-" + user


def test_cancel_without_own_token_refused(monitored, monkeypatch):
    _, client = monitored
    monkeypatch.setenv("MUSCAT_LCO_ALLOW_SUBMIT", "1")
    monkeypatch.setenv("LCO_API_TOKEN", "shared-never-used")
    monkeypatch.setattr(lco, "get_user_lco_token", lambda user: None)
    assert (
        client.post("/api/lco/monitored-requests/101/cancel", json={"confirm": True}).status_code
        == 403
    )
    assert lco_monitor.list_requests()[0]["request_state"] == "PENDING"


@pytest.mark.parametrize("mutation", ["cancel", "delete"])
def test_inflight_poll_cannot_restore_mutated_tracking(monitored, monkeypatch, mutation):
    from tests.test_lco_monitor import _frame, _row

    path, _ = monitored
    snapshot = _row(path)

    def portal(*args, **kwargs):
        with sqlite3.connect(path) as conn:
            if mutation == "cancel":
                conn.execute(
                    "UPDATE lco_observation_requests SET request_state='CANCELED', monitor_state='canceled'"
                )
            else:
                conn.execute("DELETE FROM lco_observation_requests")
        return {"state": "PENDING"}

    monkeypatch.setattr(lco, "get_requestgroup", portal)
    monkeypatch.setattr(lco, "archive_search_all", lambda *a, **kw: {"results": [_frame(91, 1)]})
    mark_error = Mock()
    monkeypatch.setattr(lco_monitor, "_mark_error", mark_error)
    download = Mock()
    monkeypatch.setattr(lco, "start_archive_download", download)
    lco_monitor.process_request(snapshot, path=path)
    download.assert_not_called()
    mark_error.assert_not_called()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM lco_observation_frames").fetchone()[0] == 0
        row = conn.execute(
            "SELECT request_state,monitor_state FROM lco_observation_requests"
        ).fetchone()
        assert row == (("CANCELED", "canceled") if mutation == "cancel" else None)


def test_group_cancel_checks_every_child_owner(monitored, monkeypatch):
    _, client = monitored
    result, payload = _submission(request_id=102)
    lco_monitor.record_submission(result, payload, "bob")
    call = Mock()
    monkeypatch.setattr(lco, "cancel_requestgroup", call)
    assert (
        client.post("/api/lco/monitored-requests/101/cancel", json={"confirm": True}).status_code
        == 403
    )
    call.assert_not_called()


def test_relay_link_is_exact_and_not_cloned(monitored):
    _, client = monitored
    result, payload = _submission(request_id=102, requestgroup_id=11)
    lco_monitor.record_submission(result, payload, "alice")
    lco_monitor.link_relay(10, 11)
    rows = client.get("/api/lco/monitored-requests").json()["requests"]
    assert {r["request_id"]: r["relay_request_id"] for r in rows} == {101: 102, 102: 101}
    assert "_relay_requestgroup_id" not in lco_monitor.get_request_payload(101)


def test_successful_relay_submission_persists_partner(monitored, monkeypatch):
    from tests.test_web import _ipp_params

    _, client = monitored
    params = _ipp_params()
    digest = lco.payload_hash(lco.build_requestgroup(params["kind"], params))
    results = iter([_submission()[0], _submission(request_id=102, requestgroup_id=11)[0]])
    monkeypatch.setattr(lco, "submit_requestgroup", lambda *args: next(results))
    response = client.post(
        "/api/lco/split-submit",
        json={
            "confirm": True,
            "leg_a": params,
            "leg_b": params,
            "dry_run_hash_a": digest,
            "dry_run_hash_b": digest,
        },
    )
    assert response.status_code == 200
    rows = client.get("/api/lco/monitored-requests").json()["requests"]
    assert {row["request_id"]: row["relay_request_id"] for row in rows} == {101: 102, 102: 101}
