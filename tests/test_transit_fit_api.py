"""HTTP schema and serialization compatibility for the transit-fit router."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from muscat_db import transit_fit as fit, transit_fit_api as api, web


DATASET = {"inst": "muscat4", "date": "250512", "target": "TOI-6715"}


@pytest.fixture
def client(mock_db, tmp_path, monkeypatch):
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path / "prose"))
    monkeypatch.setenv("MUSCAT_DATA_DIR", str(tmp_path / "raw"))
    return TestClient(web.app)


def test_openapi_exposes_request_and_response_models(client):
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    for path, model in (("run", "FitRunRequest"), ("logp", "FitLogpRequest"),
                        ("cancel", "FitCancelRequest"), ("delete", "FitDeleteRequest")):
        body = paths[f"/api/transit-fit/{path}"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert body["$ref"].endswith(f"/{model}")
    for path, method, model in (
        ("run", "post", "FitActionResponse"), ("cancel", "post", "FitActionResponse"),
        ("delete", "post", "FitActionResponse"), ("logp", "post", "FitLogpResponse"),
        ("status", "get", "FitJobStatus"), ("outputs", "get", "FitOutputsResponse"),
    ):
        op = paths[f"/api/transit-fit/{path}"][method]
        assert op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(f"/{model}")


def test_options_keep_dynamic_planet_keys_and_string_flags():
    # Per-planet keys and the engine's string "true" flags must survive intact.
    options = {
        "period_b": "3.5", "t0_c": "", "ror_prior_c": "0.05", "trend": "true",
        "fixed": [], "flag": False, "empty": "",
    }
    dumped = api.FitRunRequest.model_validate({**DATASET, "options": options}).model_dump(exclude_unset=True)
    assert dumped["options"] == options
    assert dumped["options"]["trend"] == "true" and dumped["options"]["flag"] is False


def test_omitted_fields_stay_unset():
    dumped = api.FitRunRequest.model_validate(DATASET).model_dump(exclude_unset=True)
    assert "selected_csvs" not in dumped and "options" not in dumped and "test_run" not in dumped
    assert api.FitRunRequest.model_validate({**DATASET, "selected_csvs": []}).model_dump(
        exclude_unset=True)["selected_csvs"] == []


def test_requests_reject_wrong_envelope_types():
    with pytest.raises(ValidationError):
        api.FitRunRequest.model_validate({**DATASET, "options": ["not", "a", "mapping"]})
    with pytest.raises(ValidationError):
        api.FitRunRequest.model_validate({**DATASET, "selected_csvs": "a.csv"})


def test_run_rejects_non_mapping_options_with_422(client):
    assert client.post("/api/transit-fit/run", json={**DATASET, "options": []}).status_code == 422


def test_status_for_missing_job_matches_schema(client):
    body = client.get("/api/transit-fit/status", params=DATASET).json()
    assert body["state"] == "none" and body["returncode"] is None


def test_status_models_accept_every_state_and_invalid_target_error():
    for state in ("pending", "running", "cancelling", "finalizing", "done", "error", "cancelled"):
        api.FitJobStatus.model_validate({"state": state, "log": "", "returncode": None, "elapsed": 0,
                                         "run_type": "full"})
    api.FitJobStatus.model_validate({"state": "none", "log": "", "returncode": None, "elapsed": 0,
                                     "error": "invalid target"})


def test_outputs_for_empty_run_matches_schema(client):
    body = client.get("/api/transit-fit/outputs", params=DATASET).json()
    assert body == {"ok": True, "outputs": fit.empty_fit_outputs()}


def test_outputs_model_accepts_summary_and_plots():
    outputs = {**fit.empty_fit_outputs(), "has_any": True,
               "plots": [{"file": "a.png", "created_at": "x", "version": "1"}],
               "summary": {"headers": ["parameter", "mean"], "rows": [{"parameter": "ror[0]", "mean": "0.05"}]},
               "detection_snr": {"ror": 12.0}}
    api.FitOutputsResponse.model_validate({"ok": True, "outputs": outputs})


@pytest.mark.parametrize("path,result,status", [
    ("run", {"ok": False, "error": "target is required"}, 400),
    ("run", {"ok": False, "error": "cap", "limit_exceeded": True}, 429),
    ("run", {"ok": True, "key": "k", "queued": True, "run_id": "r"}, 200),
    ("cancel", {"ok": False, "error": "no job to cancel"}, 400),
    ("delete", {"ok": False, "delete_failed": True, "error": "x"}, 500),
])
def test_action_results_validate_and_map_status(client, monkeypatch, path, result, status):
    monkeypatch.setattr(fit, {"run": "start_fit", "cancel": "cancel_fit", "delete": "delete_fit"}[path],
                        lambda *a, **k: result)
    monkeypatch.setattr(web, "_require_visible_night", lambda *a, **k: None)
    monkeypatch.setattr(web, "_restrict_csv_selection", lambda i, d, t, s: s)
    monkeypatch.setattr(web, "_require_visible_run", lambda *a, **k: None)
    monkeypatch.setattr(web, "_require_may_modify_run", lambda *a, **k: None)
    monkeypatch.setattr(web, "_record_run_change", lambda *a, **k: None)
    response = client.post(f"/api/transit-fit/{path}", json={**DATASET, "run_id": ""})
    assert response.status_code == status
    assert response.json() == result


def test_run_passes_options_through_unchanged(client, monkeypatch):
    seen = {}

    def fake(inst, date, target, options, **kwargs):
        seen.update(options=options, **kwargs)
        return {"ok": True, "key": "k"}

    monkeypatch.setattr(fit, "start_fit", fake)
    monkeypatch.setattr(web, "_require_visible_night", lambda *a, **k: None)
    monkeypatch.setattr(web, "_restrict_csv_selection", lambda i, d, t, s: s)
    options = {"period_b": "3.5", "trend": "true", "fixed": []}
    assert client.post("/api/transit-fit/run", json={**DATASET, "options": options}).status_code == 200
    assert seen["options"] == options and seen["selected_csvs"] is None and seen["test_run"] is False
