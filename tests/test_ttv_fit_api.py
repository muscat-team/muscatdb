"""HTTP schema and serialization compatibility for the TTV-fit router."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from muscat_db import ttv_fit as ttv, ttv_fit_api as api, web


@pytest.fixture
def client(mock_db, tmp_path, monkeypatch):
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path / "prose"))
    monkeypatch.setenv("MUSCAT_DATA_DIR", str(tmp_path / "raw"))
    return TestClient(web.app)


@pytest.fixture
def visible(monkeypatch):
    for name in ("_require_visible_object", "_require_visible_ttv_run", "_require_may_modify_run",
                 "_record_run_change"):
        monkeypatch.setattr(web, name, lambda *a, **k: None)


def test_openapi_exposes_request_and_response_models(client):
    paths = client.get("/openapi.json").json()["paths"]
    for path, model in (("start", "TtvStartRequest"), ("cancel", "TtvRunRequest"),
                        ("delete", "TtvRunRequest"), ("command", "TtvCommandRequest")):
        body = paths[f"/api/ttv-fit/{path}"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert body["$ref"].endswith(f"/{model}")
    for path, method, model in (("start", "post", "TtvActionResponse"), ("cancel", "post", "TtvActionResponse"),
                                ("delete", "post", "TtvActionResponse"), ("status", "get", "TtvJobStatus"),
                                ("command", "post", "TtvCommandResponse")):
        op = paths[f"/api/ttv-fit/{path}"][method]
        assert op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(f"/{model}")


def test_options_pass_through_and_absence_is_preserved():
    options = {"planet_letters": "b", "selection": None, "flag": "true", "empty": "", "list": []}
    dumped = api.TtvStartRequest.model_validate({"target": "T", "options": options}).model_dump(exclude_unset=True)
    assert dumped["options"] == options and "selection" in dumped["options"]
    assert "options" not in api.TtvStartRequest.model_validate({"target": "T"}).model_dump(exclude_unset=True)


def test_requests_reject_non_mapping_options():
    with pytest.raises(ValidationError):
        api.TtvStartRequest.model_validate({"target": "T", "options": [1]})
    with pytest.raises(ValidationError):
        api.TtvCommandRequest.model_validate({"target": "T", "options": "x"})


@pytest.mark.parametrize("path", ["start", "cancel", "delete", "command"])
def test_blank_target_stays_a_400_with_error_body(client, path):
    response = client.post(f"/api/ttv-fit/{path}", json={})
    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "target is required"}


def test_status_for_missing_target_and_unknown_run(client):
    assert client.get("/api/ttv-fit/status").status_code == 400
    body = client.get("/api/ttv-fit/status", params={"target": "TOI-1"}).json()
    assert body["state"] == "none"


@pytest.mark.parametrize("path,fn,result,status", [
    ("cancel", "cancel_ttv_fit", {"ok": False, "error": "no job to cancel"}, 200),
    ("cancel", "cancel_ttv_fit", {"ok": True, "already_finished": True}, 200),
    ("delete", "delete_ttv_fit", {"ok": False, "delete_failed": True, "error": "x"}, 500),
    ("delete", "delete_ttv_fit", {"ok": True, "count": 1}, 200),
])
def test_action_results_validate_and_map_status(client, visible, monkeypatch, path, fn, result, status):
    monkeypatch.setattr(ttv, fn, lambda *a, **k: result)
    response = client.post(f"/api/ttv-fit/{path}", json={"target": "T", "run_name": "r"})
    assert response.status_code == status and response.json() == result


@pytest.mark.parametrize("result,status", [
    ({"ok": True, "key": "k"}, 200),
    ({"ok": True, "key": "k", "queued": True}, 200),
    ({"ok": False, "error": "cap", "limit_exceeded": True}, 429),
    ({"ok": False, "error": "bad"}, 200),
])
def test_start_maps_status_and_passes_options(client, visible, monkeypatch, result, status):
    seen = {}

    def fake(target, options, user, **kwargs):
        seen["options"] = options
        return result

    monkeypatch.setattr(ttv, "start_ttv_fit", fake)
    response = client.post("/api/ttv-fit/start", json={"target": "T", "options": {"planet_letters": "b"}})
    assert response.status_code == status and response.json() == result
    assert seen["options"]["planet_letters"] == "b" and seen["options"]["inputs"] is None


def test_command_returns_ok_and_command(client, monkeypatch):
    monkeypatch.setattr(ttv, "get_ttv_command", lambda target, options: f"harmonic {target} {options}")
    body = client.post("/api/ttv-fit/command", json={"target": "T"}).json()
    assert body == {"ok": True, "command": "harmonic T {}"}
