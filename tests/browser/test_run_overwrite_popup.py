"""The overwrite popup (#317) on the photometry, transit-fit and Jobs pages.

The launchers are stubbed, but the stubs call the route's real guard with the
run id the page asked for, as ``start_run`` / ``start_fit`` do, so the 409, the
confirmation token and the suggested name all come from the server. The seed's
``r1`` photometry run and ``fit1`` transit fit exist on disk, so launching
under those names asks first.
"""

from __future__ import annotations

import json

from playwright.sync_api import expect
import pytest

from tests.browser.seed import FIT_RUN_ID, RUN_ID
from tests.browser.test_transit_fit import URL as FIT_URL, _complete_priors, _query_toi_catalog

PHOT_URL = f"/photometry?inst=muscat3&date=261001&target=TOI-1000&run={RUN_ID}"
DONE_KEY = f"photometry:muscat3/261001/TOI-1000/{RUN_ID}"


@pytest.fixture(autouse=True)
def _conflicts_are_expected(page):
    """The 409 that asks is the behaviour under test, not a page error."""
    page.problems.allowed += ["HTTP 409", "status of 409"]


@pytest.fixture
def launched(monkeypatch):
    """Launchers that ask the route's guard and then report a running job.
    Returns the run ids actually launched."""
    from muscat_db import photometry as phot
    from muscat_db import transit_fit as fit
    from muscat_db.jobs import slugify_run_name

    calls = []

    def launch(options, authorize_run):
        run_id = slugify_run_name(options.get("run_name"))
        authorize_run(run_id)
        calls.append(run_id)
        return {"ok": True, "key": f"k/{run_id}", "run_id": run_id}

    def status(*a, **k):
        return {"state": "running" if calls else "none", "log": "", "elapsed": 0, "returncode": None}

    monkeypatch.setattr(phot, "start_run", lambda inst, date, target, *, options, authorize_run, **k: launch(options, authorize_run))
    monkeypatch.setattr(fit, "start_fit", lambda inst, date, target, options, *, authorize_run, **k: launch(options, authorize_run))
    monkeypatch.setattr(phot, "job_status", status)
    monkeypatch.setattr(fit, "job_status", status)
    return calls


@pytest.fixture
def alices_r1():
    """Make the seed's r1 run (the admin's own, from its job row) alice's,
    then put it back."""
    from muscat_db import photometry as phot
    from muscat_db import product_record

    rdir = phot.run_output_dir("muscat3", "261001", "TOI-1000", RUN_ID)
    path = rdir / product_record.RECORD_NAME
    before = path.read_bytes() if path.exists() else None
    product_record.write_record(rdir, product_record.build_record("photometry", author="alice", inputs=[]))
    yield
    if before is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(before)


def _open_photometry(page, run_name=RUN_ID):
    page.goto(PHOT_URL)
    page.wait_for_load_state("networkidle")
    page.locator("#opt-panel > summary").click()
    page.fill("#opt-run_name", run_name)


def _bodies(requests):
    return [json.loads(r.post_data) for r in requests]


def _recording(page, pattern):
    seen = []
    page.on("request", lambda r: seen.append(r) if pattern in r.url and r.method == "POST" else None)
    return seen


def test_save_as_is_the_default_and_launches_under_the_suggested_name(page, launched):
    requests = _recording(page, "/api/photometry/run")
    _open_photometry(page)

    page.click("#run-test-btn")

    expect(page.locator("#message-modal-title")).to_have_text(f'You already have a run named "{RUN_ID}"')
    expect(page.locator("#message-modal-input")).to_have_value(f"{RUN_ID}_2")
    expect(page.locator("#message-modal-input")).to_be_focused()
    expect(page.locator("#message-modal-ok")).to_have_text("Save as")
    expect(page.locator("#message-modal-alt")).to_have_text("Overwrite")
    assert launched == []

    with page.expect_response("**/api/photometry/run"):
        page.keyboard.press("Enter")

    expect(page.locator("#message-modal")).not_to_have_class("visible")
    assert launched == [f"{RUN_ID}_2"]
    assert page.input_value("#opt-run_name") == f"{RUN_ID}_2"
    assert [b["options"]["run_name"] for b in _bodies(requests)] == [RUN_ID, f"{RUN_ID}_2"]


def test_a_typed_run_name_is_used_instead_of_the_suggestion(page, launched):
    requests = _recording(page, "/api/photometry/run")
    _open_photometry(page)
    page.click("#run-test-btn")
    expect(page.locator("#message-modal-input")).to_be_focused()

    page.keyboard.type("wider-apertures")  # replaces the selected suggestion
    with page.expect_response("**/api/photometry/run"):
        page.keyboard.press("Enter")

    assert launched == ["wider_apertures"]
    assert page.input_value("#opt-run_name") == "wider-apertures"
    assert _bodies(requests)[-1]["options"]["run_name"] == "wider-apertures"


def test_a_typed_name_that_is_also_taken_asks_again(page, launched):
    _open_photometry(page)
    page.click("#run-test-btn")
    page.fill("#message-modal-input", RUN_ID)  # the same taken name

    with page.expect_response("**/api/photometry/run") as resp:
        page.click("#message-modal-ok")

    assert resp.value.status == 409
    expect(page.locator("#message-modal")).to_have_class("visible")
    expect(page.locator("#message-modal-input")).to_have_value(f"{RUN_ID}_2")
    assert launched == []
    page.click("#message-modal-cancel")


def test_save_as_is_disabled_while_the_name_is_blank(page, launched):
    _open_photometry(page)
    page.click("#run-test-btn")
    page.fill("#message-modal-input", "   ")
    expect(page.locator("#message-modal-ok")).to_be_disabled()
    page.keyboard.press("Enter")
    expect(page.locator("#message-modal")).to_have_class("visible")
    page.fill("#message-modal-input", "x")
    expect(page.locator("#message-modal-ok")).to_be_enabled()
    page.click("#message-modal-cancel")
    assert launched == []


def test_overwrite_relaunches_with_the_confirmation(page, launched):
    requests = _recording(page, "/api/photometry/run")
    _open_photometry(page)
    page.click("#run-test-btn")
    expect(page.locator("#message-modal-alt")).to_be_visible()

    with page.expect_response("**/api/photometry/run") as resp:
        page.click("#message-modal-alt")

    assert resp.value.ok
    assert launched == [RUN_ID]
    assert page.input_value("#opt-run_name") == RUN_ID
    second = _bodies(requests)[-1]
    assert second["confirm_overwrite"] == f"{RUN_ID}:admin"


def test_cancel_launches_nothing_and_recovers_the_buttons(page, launched):
    requests = _recording(page, "/api/photometry/run")
    _open_photometry(page)
    page.click("#run-test-btn")
    expect(page.locator("#message-modal-cancel")).to_be_visible()

    page.click("#message-modal-cancel")

    expect(page.locator("#run-log")).to_contain_text("Launch cancelled")
    expect(page.locator("#run-test-btn")).to_be_enabled()
    assert launched == [] and len(requests) == 1


def test_someone_elses_run_names_its_owner_and_suggests_the_viewers_name(page, launched, alices_r1):
    requests = _recording(page, "/api/photometry/run")
    _open_photometry(page)
    page.click("#run-test-btn")
    expect(page.locator("#message-modal-title")).to_have_text("This run belongs to alice")
    expect(page.locator("#message-modal-text")).to_contain_text("Overwriting replaces alice's results")
    expect(page.locator("#message-modal-input")).to_have_value(f"{RUN_ID}_admin")
    expect(page.locator("#message-modal-alt")).to_have_text("Overwrite alice's run")

    with page.expect_response("**/api/photometry/run"):
        page.click("#message-modal-alt")

    assert launched == [RUN_ID]
    assert _bodies(requests)[-1]["confirm_overwrite"] == f"{RUN_ID}:alice"


def test_a_free_run_name_launches_without_a_popup(page, launched):
    _open_photometry(page, run_name="brand-new")
    with page.expect_response("**/api/photometry/run") as resp:
        page.click("#run-test-btn")
    assert resp.value.status == 200
    assert launched == ["brand_new"]
    expect(page.locator("#message-modal")).not_to_have_class("visible")


def test_transit_fit_save_as_launches_under_the_suggested_name(page, launched):
    page.goto(FIT_URL)
    _query_toi_catalog(page)
    _complete_priors(page)
    page.fill("#opt-run_name", FIT_RUN_ID)

    page.click("#run-test-btn")
    expect(page.locator("#message-modal-input")).to_have_value(f"{FIT_RUN_ID}_2")
    with page.expect_response("**/api/transit-fit/run"):
        page.click("#message-modal-ok")

    assert launched == [f"{FIT_RUN_ID}_2"]
    assert page.input_value("#opt-run_name") == f"{FIT_RUN_ID}_2"


def test_jobs_rerun_asks_and_save_as_reruns_under_a_new_name(page, launched):
    requests = _recording(page, "/api/jobs/rerun")
    page.goto("/jobs")
    button = page.locator(f'tr.job-row[data-key="{DONE_KEY}"] [data-job-action="rerun"]')

    button.click()
    expect(page.locator("#message-modal-input")).to_have_value(f"{RUN_ID}_2")
    with page.expect_navigation():
        page.click("#message-modal-ok")

    assert launched == [f"{RUN_ID}_2"]
    assert _bodies(requests)[-1]["run_name"] == f"{RUN_ID}_2"


def test_other_modals_do_not_show_the_third_button(page):
    page.goto("/jobs")
    page.evaluate("window.showMessageModal('Note', 'plain message')")
    expect(page.locator("#message-modal-ok")).to_be_visible()
    expect(page.locator("#message-modal-alt")).to_be_hidden()
    expect(page.locator("#message-modal-field")).to_be_hidden()
    page.click("#message-modal-ok")

    # Not returning the modal's promise: evaluate would wait for an answer.
    page.evaluate("() => { window.showConfirmModal('Sure?', 'two buttons', {confirmLabel: 'Yes'}); }")
    expect(page.locator("#message-modal-cancel")).to_be_visible()
    expect(page.locator("#message-modal-alt")).to_be_hidden()
    page.click("#message-modal-cancel")


def test_jobs_rerun_cancel_restores_the_button(page, launched):
    page.goto("/jobs")
    button = page.locator(f'tr.job-row[data-key="{DONE_KEY}"] [data-job-action="rerun"]')
    button.click()
    page.click("#message-modal-cancel")
    expect(button).to_have_text("⟳ Re-run")
    expect(button).to_be_enabled()
    assert launched == []
