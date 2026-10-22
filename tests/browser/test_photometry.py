"""The /photometry page: dataset picker, products, options, run/cancel, post-processing, delete.

Launch-type calls are stubbed in-process (the harness refuses real process
launches); the page, its scripts, request bodies and the handlers in front of
those stubs are the real ones. Ported from the former opt-in
``test_browser_form_submissions`` in tests/test_photometry_api.py.
"""

from __future__ import annotations

import pytest

from tests.browser.helpers import reveal
from tests.browser.seed import RUN_ID

DATASET = "inst=muscat3&date=261001&target=TOI-1000"
URL = f"/photometry?{DATASET}&run={RUN_ID}"
OPT_KEY = "phot:options:muscat3:261001:TOI-1000"


@pytest.fixture
def pipeline(monkeypatch):
    """Stub the photometry launch/cancel/status/delete and post-processing calls."""
    from muscat_db import photometry as phot
    from muscat_db import web

    calls = {"start": [], "cancel": [], "delete": [], "postprocess": [], "restore": []}
    state = {"value": "none"}

    def start_run(inst, date, target, **kwargs):
        calls["start"].append(kwargs)
        state["value"] = "running"
        return {"ok": True, "key": f"photometry:{inst}/{date}/{target}/{RUN_ID}", "run_id": RUN_ID}

    def cancel_run(*args, **kwargs):
        calls["cancel"].append(kwargs)
        state["value"] = "cancelled"
        return {"ok": True, "key": "x"}

    monkeypatch.setattr(phot, "start_run", start_run)
    monkeypatch.setattr(phot, "cancel_run", cancel_run)
    monkeypatch.setattr(phot, "job_status", lambda *a, **k: {
        "state": state["value"], "log": "browser test", "elapsed": 0, "returncode": None,
    })
    monkeypatch.setattr(phot, "delete_reduction", lambda *a, **k: calls["delete"].append(k) or {"ok": True, "count": 1})
    monkeypatch.setattr(web.postproc, "postprocess", lambda *a, **k: calls["postprocess"].append(k) or {
        "ok": True, "applied": k["apply"], "files": [], "n_files": 0, "restorable": True,
    })
    monkeypatch.setattr(web.postproc, "restore_clip", lambda *a, **k: calls["restore"].append(k) or {
        "ok": True, "restored": [], "n_files": 0,
    })
    return calls


def _loaded_images(page) -> list[str]:
    return page.eval_on_selector_all(
        "img[src*='/api/photometry/file/']",
        "is => is.filter(i => i.complete && i.naturalWidth > 0).map(i => i.src.split('/').pop().split('?')[0])",
    )


def test_products_of_the_run_are_shown(page):
    page.goto(URL)
    page.wait_for_load_state("networkidle")
    images = _loaded_images(page)
    for name in ("lightcurves", "covariates", "stacks"):
        assert f"TOI-1000_muscat3_261001_{name}.png" in images
    assert "TOI-1000_muscat3_gp_261001_ref.png" in images
    # Per-band products sit behind band tabs; rp shows (and loads) on its tab.
    rp_ref = page.locator("img[src*='TOI-1000_muscat3_rp_261001_ref.png']").locator("visible=true")
    assert rp_ref.count() == 0
    page.locator('button[data-band="rp"]').locator("visible=true").first.click()
    rp_ref.first.wait_for()
    page.wait_for_function("i => i.complete && i.naturalWidth > 0", arg=rp_ref.first.element_handle())
    for band in ("gp", "rp"):
        link = page.locator(f'a[href*="TOI-1000_muscat3_{band}_261001.csv"]').first
        assert link.inner_text().strip() == band
    assert page.eval_on_selector_all("#opt-ref_band option", "os => os.map(o => o.value)") == ["", "gp", "rp"]


def test_dataset_picker_navigates_to_a_dataset(page):
    page.goto("/photometry")
    page.select_option("#sel-inst", "muscat3")
    page.wait_for_function("[...document.querySelectorAll('#sel-date option')].some(o => o.value === '261001')")
    page.select_option("#sel-date", "261001")
    page.wait_for_function("[...document.querySelectorAll('#sel-target option')].some(o => o.value === 'TOI-1000')")
    with page.expect_navigation():
        page.select_option("#sel-target", "TOI-1000")
    assert "inst=muscat3" in page.url and "date=261001" in page.url and "target=TOI-1000" in page.url


def test_options_persist_per_dataset(page):
    page.goto(URL)
    for control in ("#opt-ref_band", "#opt-centroid_method", "#opt-max_num_stars", "#opt-overwrite"):
        reveal(page, control)
    page.select_option("#opt-ref_band", "rp")
    page.select_option("#opt-centroid_method", "com")
    page.fill("#opt-max_num_stars", "")
    page.locator("#opt-overwrite").uncheck()

    page.goto("/jobs")  # navigate away and back, as a user would
    page.goto(URL)
    assert page.input_value("#opt-ref_band") == "rp"
    assert page.input_value("#opt-centroid_method") == "com"
    assert page.input_value("#opt-max_num_stars") == ""
    assert not page.is_checked("#opt-overwrite")
    saved = page.evaluate(f"JSON.parse(localStorage.getItem('{OPT_KEY}'))")
    assert saved["ref_band"] == "rp" and saved["centroid_method"] == "com"

    page.goto("/photometry?inst=muscat2&date=261002&target=HAT-P-1")
    assert page.input_value("#opt-centroid_method") != "com"  # another dataset keeps its own


def test_wcs_method_follows_the_instrument(page):
    # muscat3 frames carry a WCS, so the solver choice is locked; muscat2's do not.
    page.goto(URL)
    assert page.is_disabled("#opt-wcs_method")
    page.goto("/photometry?inst=muscat2&date=261002&target=HAT-P-1")
    assert page.is_enabled("#opt-wcs_method")


def test_test_run_full_run_and_cancel(page, pipeline):
    page.goto(URL)
    page.locator("#opt-panel > summary").click()
    page.locator("#opt-panel details > summary").filter(has_text="Reference & Alignment").click()
    page.fill("#opt-max_num_stars", "")
    page.locator("#opt-overwrite").uncheck()
    with page.expect_response("**/api/photometry/run") as resp:
        page.click("#run-test-btn")
    assert resp.value.ok
    assert pipeline["start"][-1]["test_run"] is True
    assert pipeline["start"][-1]["options"]["max_num_stars"] == ""
    assert pipeline["start"][-1]["options"]["overwrite"] is False
    with page.expect_response("**/api/photometry/cancel"):
        page.click("#cancel-btn")

    page.goto(URL)
    with page.expect_response("**/api/photometry/run"):
        page.click("#run-full-btn")
    assert pipeline["start"][-1]["test_run"] is False
    with page.expect_response("**/api/photometry/cancel"):
        page.click("#cancel-btn")
    assert len(pipeline["cancel"]) == 2


def test_postprocess_test_apply_and_restore(page, pipeline):
    page.goto(URL)
    page.locator("#postproc-section > summary").click()
    page.fill("#opt-post_sigma", "")
    for button, applied in (("#postproc-test-btn", False), ("#postproc-apply-btn", True)):
        with page.expect_response("**/api/photometry/postprocess") as resp:
            page.click(button)
        assert resp.value.ok
        assert pipeline["postprocess"][-1]["apply"] is applied
    page.click("#message-modal-ok")

    page.goto(URL)
    page.locator("#postproc-section > summary").click()
    page.click("#postproc-restore-btn")
    with page.expect_response("**/api/photometry/postprocess/restore"):
        page.click("#message-modal-ok")
    assert len(pipeline["restore"]) == 1


def test_delete_reduction_after_confirmation(page, pipeline):
    page.goto(URL)
    page.click("#delete-btn")
    with page.expect_response("**/api/photometry/delete"):
        page.click("#delete-confirm-btn")
    assert len(pipeline["delete"]) == 1
