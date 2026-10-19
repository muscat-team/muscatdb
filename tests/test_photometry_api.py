"""HTTP schema and serialization compatibility for the photometry router."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from muscat_db import photometry as phot, photometry_api as api, web


DATASET = {"inst": "muscat4", "date": "250512", "target": "TOI-6715"}
TEMPLATE = Path(web.__file__).parent / "templates" / "photometry.html"


@pytest.fixture
def client(mock_db, tmp_path, monkeypatch):
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path / "prose"))
    monkeypatch.setenv("MUSCAT_DATA_DIR", str(tmp_path / "raw"))
    return TestClient(web.app)


def test_openapi_exposes_nested_run_options_and_responses(client):
    schema = client.get("/openapi.json").json()
    paths, models = schema["paths"], schema["components"]["schemas"]
    run = paths["/api/photometry/run"]["post"]
    assert run["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/RunRequest")
    assert "options" in models["RunRequest"]["properties"]
    assert "comparison_ids" in models["PhotometryOptions"]["properties"]
    for path, method, model in (
        ("run", "post", "ActionResponse"),
        ("cancel", "post", "ActionResponse"),
        ("delete", "post", "ActionResponse"),
        ("command", "post", "CommandResponse"),
        ("status", "get", "JobStatus"),
        ("status-batch", "post", "BatchResponse"),
        ("outputs", "get", "PhotometryOutputs"),
        ("runs", "get", "RunsResponse"),
        ("postprocess", "post", "PostprocessResponse"),
        ("postprocess/restore", "post", "RestoreResponse"),
    ):
        operation = paths[f"/api/photometry/{path}"][method]
        assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(f"/{model}")
    assert "finalizing" in models["JobStatus"]["properties"]["state"]["enum"]
    assert "400" in run["responses"]


def _assert_option_keys(payload, model):
    missing = set(payload) - model.model_fields.keys()
    assert not missing, f"collectOptions fields missing from model: {sorted(missing)}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_collect_options_drift_and_round_trip():
    # Execute the real function, including computed fields, against a tiny DOM
    # stand-in. No hand-maintained list of payload keys can mask GUI drift.
    function = re.search(r"  function collectOptions\(\) \{.*?\n  \}", TEMPLATE.read_text(), re.S).group()
    script = """
    const document = {querySelectorAll: () => [{value: 'gp'}, {value: 'rp'}]};
    const val = () => '', chk = () => false;
    """ + function + "\nprocess.stdout.write(JSON.stringify(collectOptions()));"
    payload = json.loads(subprocess.check_output([shutil.which("node"), "-e", script], text=True))
    _assert_option_keys(payload, api.PhotometryOptions)
    # Prove that the drift assertion would fail for a removed model field.
    class MissingField:
        model_fields = {k: v for k, v in api.PhotometryOptions.model_fields.items() if k != "sig_flux"}
    with pytest.raises(AssertionError, match="sig_flux"):
        _assert_option_keys(payload, MissingField)
    parsed = api.PhotometryOptions.model_validate(payload).model_dump(exclude_unset=True)
    assert phot.normalize_run_options(parsed) == phot.normalize_run_options(payload)


@pytest.mark.parametrize("options", [
    {}, {"bands": []}, {"bands": "gp,rp"}, {"overwrite": False},
    {"refid": "", "n_stars_align": "", "cutout_size": "  "},
    {"avoid_nearby_stars": False}, {"avoid_nearby_stars": True, "avoid_nearby_star": "12.5"},
    {"comparison_ids": "1,2", "exclude_after_jd": "2460000.5,2460001.5"},
])
def test_model_preserves_existing_option_semantics(options):
    parsed = api.PhotometryOptions.model_validate(options).model_dump(exclude_unset=True)
    assert phot.normalize_run_options(parsed) == phot.normalize_run_options(options)


@pytest.mark.parametrize("key, value, text", [
    ("ref_time", 2460000.5, "2460000.5"),
    ("exclude_after_jd", 2460000.5, "2460000.5"),
    ("exclude_before_jd", 2460000, "2460000"),
    ("comparison_ids", 12, "12"),
    ("avoid_comparison_ids", 3, "3"),
])
def test_numbers_are_accepted_for_text_options_as_before(key, value, text):
    parsed = api.PhotometryOptions.model_validate({key: value}).model_dump(exclude_unset=True)
    assert parsed == {key: text}
    assert phot.normalize_run_options(parsed) == phot.normalize_run_options({key: value})


@pytest.mark.parametrize("key", ["ref_time", "exclude_after_jd", "comparison_ids"])
def test_text_options_still_reject_booleans(key):
    with pytest.raises(ValidationError):
        api.PhotometryOptions.model_validate({key: True})


@pytest.mark.parametrize("key", ["target_id", "avoid_nearby_star", "cutout_size"])
def test_numeric_fields_do_not_turn_booleans_into_valid_numbers(key):
    with pytest.raises(ValidationError):
        api.PhotometryOptions.model_validate({key: True})


def test_run_converts_numbers_and_ignores_unknown_fields(client, monkeypatch):
    seen = {}
    def start(*args, **kwargs):
        seen.update(kwargs)
        return {"ok": True, "key": "job", "run_id": "default", "queued": True}
    monkeypatch.setattr(phot, "start_run", start)
    response = client.post("/api/photometry/run", json={**DATASET, "legacy": 1, "options": {
        "max_num_stars": "7", "overwrite": False, "sig_flux": "", "old_checkbox": True,
    }})
    assert response.status_code == 200, response.text
    assert seen["test_run"] is True
    assert seen["options"] == {"max_num_stars": 7, "overwrite": False, "sig_flux": ""}
    assert response.json() == {"ok": True, "key": "job", "run_id": "default", "queued": True}


@pytest.mark.parametrize("options, error", [
    ({"bands": []}, "select at least one band"),
    ({"comparison_ids": "1,--data_dir"}, "comma-separated integers"),
    ({"exclude_before_jd": "2460000,--results_dir"}, "comma-separated Julian dates"),
    ({"calib_dir": "/other/night"}, "cannot be set"),
])
def test_existing_validation_remains_authoritative(client, options, error):
    response = client.post("/api/photometry/command", json={**DATASET, "options": options})
    assert response.status_code == 200
    assert error in response.json()["error"]


def test_invalid_option_shape_is_rejected_before_launch(client, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid shape reached the launcher")
    monkeypatch.setattr(phot, "start_run", unexpected)
    response = client.post("/api/photometry/run", json={**DATASET, "options": {"bands": {"gp": True}}})
    assert response.status_code == 422


@pytest.mark.parametrize("state", ["none", "pending", "running", "cancelling", "finalizing", "done", "error", "cancelled"])
def test_status_responses_match_contract_without_adding_keys(client, monkeypatch, state):
    body = {"state": state, "log": "tail", "returncode": None, "elapsed": 9}
    monkeypatch.setattr(phot, "job_status", lambda *a, **k: body)
    response = client.get("/api/photometry/status", params=DATASET)
    assert response.status_code == 200
    assert response.json() == body
    api.JobStatus.model_validate(response.json())


def test_direct_json_responses_are_validated(client, monkeypatch):
    monkeypatch.setattr(phot, "job_status", lambda *a, **k: {"state": "typo"})
    with pytest.raises(ValidationError):
        client.get("/api/photometry/status", params=DATASET)


def test_cancel_preserves_legacy_run_alias(client, monkeypatch):
    seen = []
    monkeypatch.setattr(phot, "cancel_run", lambda *a, **k: seen.append(k) or {"ok": True, "already_finished": True})
    response = client.post("/api/photometry/cancel", json={**DATASET, "run": "legacy-name"})
    assert response.status_code == 200
    assert seen == [{"run_id": "legacy-name"}]
    assert response.json() == {"ok": True, "already_finished": True}


def test_discovery_returns_real_outputs_and_serves_them(client, tmp_path):
    directory = tmp_path / "prose" / DATASET["inst"] / DATASET["date"]
    directory.mkdir(parents=True)
    filename = "TOI-6715_muscat4_gp_250512.csv"
    (directory / filename).write_text("BJD_TDB,Flux\n2460807.84,1.0\n")
    response = client.get("/api/photometry/outputs", params=DATASET)
    assert response.status_code == 200
    body = response.json()
    api.PhotometryOutputs.model_validate(body)
    assert body["bands"]["gp"]["csv"]["file"] == filename
    runs = client.get("/api/photometry/runs", params=DATASET)
    assert runs.status_code == 200
    assert runs.json()["runs"][0]["is_legacy"] is True
    file = client.get(f"/api/photometry/file/muscat4/250512/{filename}")
    assert file.status_code == 200
    assert "BJD_TDB" in file.text


def test_hidden_outputs_and_runs_are_empty(client, monkeypatch):
    monkeypatch.setattr(web, "_night_hidden", lambda *a: True)
    monkeypatch.setattr(phot, "list_photometry_runs", lambda *a: pytest.fail("scanned hidden data"))
    monkeypatch.setattr(phot, "list_outputs", lambda *a, **k: pytest.fail("scanned hidden data"))
    assert client.get("/api/photometry/outputs", params=DATASET).json() == phot.empty_outputs()
    assert client.get("/api/photometry/runs", params=DATASET).json() == {"runs": []}


@pytest.mark.skipif(not os.environ.get("MUSCAT_BROWSER_TESTS"), reason="opt in with MUSCAT_BROWSER_TESTS=1 and install playwright")
def test_browser_form_submissions(client, tmp_path, monkeypatch):
    """Real Chromium inputs/fetch against TestClient, without a live server.

    Run with uv run --with playwright pytest ... -k browser_form. Only the
    external engines, chat transport, and SSE connection are stubbed; the page,
    scripts, POST bodies, handlers, and polling responses are the real ones.
    """
    sync_api = pytest.importorskip("playwright.sync_api")
    directory = tmp_path / "prose" / "muscat4" / "250512" / "_runs" / "TOI-6715" / "browser"
    directory.mkdir(parents=True)
    (directory / "TOI-6715_muscat4_gp_250512.csv").write_text("BJD_TDB,Flux\n2460807.84,1.0\n")
    (directory / "_webrun_meta.json").write_text(json.dumps({"run_id": "browser", "run_name": "browser"}))
    phot.raw_data_dir("muscat4", "250512").mkdir(parents=True)
    state = {"value": "none"}
    launches = []
    def start(*args, **kwargs):
        launches.append(kwargs)
        state["value"] = "running"
        return {"ok": True, "key": "browser", "run_id": "browser"}
    def cancel(*args, **kwargs):
        state["value"] = "cancelled"
        return {"ok": True, "key": "browser"}
    monkeypatch.setattr(phot, "start_run", start)
    monkeypatch.setattr(phot, "cancel_run", cancel)
    monkeypatch.setattr(phot, "job_status", lambda *a, **k: {
        "state": state["value"], "log": "browser test", "elapsed": 0, "returncode": None,
    })
    monkeypatch.setattr(phot, "delete_reduction", lambda *a, **k: {"ok": True, "count": 1})
    monkeypatch.setattr(web.postproc, "postprocess", lambda *a, **k: {
        "ok": True, "applied": k["apply"], "files": [], "n_files": 0, "restorable": True,
    })
    monkeypatch.setattr(web.postproc, "restore_clip", lambda *a, **k: {"ok": True, "restored": [], "n_files": 0})
    failures, errors, requests = [], [], []
    def dispatch(route):
        request = route.request
        url = urlsplit(request.url)
        if url.hostname != "testserver" or url.path.endswith("/log-stream"):
            route.abort()
            return
        if url.path.endswith("/chat.js"):
            route.fulfill(status=200, content_type="application/javascript", body="")
            return
        path = url.path + ("?" + url.query if url.query else "")
        response = client.request(request.method, path, content=request.post_data_buffer,
                                  headers={"Origin": "http://testserver", "Content-Type": "application/json"})
        if url.path.startswith("/api/photometry/"):
            requests.append((url.path, request.post_data_json if request.method == "POST" else None))
            if response.status_code >= 400:
                failures.append((path, response.status_code, response.text))
        headers = {k: v for k, v in response.headers.items() if k not in {"content-encoding", "content-length"}}
        route.fulfill(status=response.status_code, headers=headers, body=response.content)

    with sync_api.sync_playwright() as pw:
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        browser = pw.chromium.launch(executable_path=executable)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.route("**/*", dispatch)
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        url = "http://testserver/photometry?inst=muscat4&date=250512&target=TOI-6715&run=browser"
        page.goto(url)
        page.locator("#opt-panel > summary").click()
        page.locator("#opt-panel details > summary").filter(has_text="Reference & Alignment").click()
        page.locator("#opt-max_num_stars").fill("")
        page.locator("#opt-overwrite").uncheck()
        with page.expect_response(lambda r: r.url.endswith("/api/photometry/run")) as result:
            page.locator("#run-test-btn").click()
        assert result.value.status == 200
        assert launches[-1]["test_run"] is True
        assert launches[-1]["options"]["max_num_stars"] == ""
        assert launches[-1]["options"]["overwrite"] is False
        with page.expect_response(lambda r: r.url.endswith("/api/photometry/cancel")):
            page.locator("#cancel-btn").click()
        # Revisit the page to exercise saved settings and the full-run button.
        page.goto(url)
        assert page.locator("#opt-max_num_stars").input_value() == ""
        assert not page.locator("#opt-overwrite").is_checked()
        with page.expect_response(lambda r: r.url.endswith("/api/photometry/run")):
            page.locator("#run-full-btn").click()
        assert launches[-1]["test_run"] is False
        with page.expect_response(lambda r: r.url.endswith("/api/photometry/cancel")):
            page.locator("#cancel-btn").click()
        page.goto(url)
        page.locator("#postproc-section > summary").click()
        page.locator("#opt-post_sigma").fill("")
        for button in ("#postproc-test-btn", "#postproc-apply-btn"):
            with page.expect_response(lambda r: r.url.endswith("/api/photometry/postprocess")) as result:
                page.locator(button).click()
            assert result.value.status == 200
        page.locator("#message-modal-ok").click()
        page.goto(url)
        page.locator("#postproc-section > summary").click()
        page.locator("#postproc-restore-btn").click()
        with page.expect_response(lambda r: r.url.endswith("/api/photometry/postprocess/restore")):
            page.locator("#message-modal-ok").click()
        page.goto(url)
        page.locator("#delete-btn").click()
        with page.expect_response(lambda r: r.url.endswith("/api/photometry/delete")):
            page.locator("#delete-confirm-btn").click()
        assert not failures, failures
        assert not errors, errors
        posts = {path for path, payload in requests if payload is not None}
        assert {f"/api/photometry/{name}" for name in ("run", "command", "cancel", "delete", "postprocess", "postprocess/restore")} <= posts
        browser.close()
