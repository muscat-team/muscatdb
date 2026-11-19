"""The /photometry page: dataset picker, products, options, run/cancel, post-processing, delete.

Launch-type calls are stubbed in-process (the harness refuses real process
launches); the page, its scripts, request bodies and the handlers in front of
those stubs are the real ones. Ported from the former opt-in
``test_browser_form_submissions`` in tests/test_photometry_api.py.
"""

from __future__ import annotations

from pathlib import Path
import re
import shutil

from playwright.sync_api import expect
import pytest

from tests.browser.helpers import QueuedJobStore, reveal
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
    page.wait_for_load_state("networkidle")
    # Products load as the user scrolls; offscreen figures must stay deferred.
    for name in ("lightcurves", "covariates", "stacks", "gp ref"):
        img = page.locator(f'img[alt="{name}"]')
        img.scroll_into_view_if_needed()
        page.wait_for_function("i => i.complete && i.naturalWidth > 0", arg=img.element_handle())
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
    page.wait_for_load_state("networkidle")
    for control in ("#opt-ref_band", "#opt-centroid_method", "#opt-max_num_stars", "#opt-overwrite"):
        reveal(page, control)
    page.select_option("#opt-ref_band", "rp")
    page.select_option("#opt-centroid_method", "com")
    page.fill("#opt-max_num_stars", "")
    page.locator("#opt-overwrite").uncheck()
    page.wait_for_function("""key => {
        const saved = JSON.parse(localStorage.getItem(key) || '{}');
        return saved.ref_band === 'rp' && saved.centroid_method === 'com' && saved.overwrite === false;
    }""", arg=OPT_KEY)

    page.goto("/jobs")  # navigate away and back, as a user would
    page.goto(URL)
    page.wait_for_load_state("networkidle")
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
    page.wait_for_load_state("networkidle")
    assert page.is_disabled("#opt-wcs_method")
    page.goto("/photometry?inst=muscat2&date=261002&target=HAT-P-1")
    assert page.is_enabled("#opt-wcs_method")


def test_test_run_full_run_and_cancel(page, pipeline):
    page.goto(URL)
    page.wait_for_load_state("networkidle")
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
    page.wait_for_load_state("networkidle")
    with page.expect_response("**/api/photometry/run"):
        page.click("#run-full-btn")
    assert pipeline["start"][-1]["test_run"] is False
    with page.expect_response("**/api/photometry/cancel"):
        page.click("#cancel-btn")
    assert len(pipeline["cancel"]) == 2


def test_postprocess_test_apply_and_restore(page, pipeline):
    page.goto(URL)
    page.wait_for_load_state("networkidle")
    page.locator("#postproc-section > summary").click()
    page.fill("#opt-post_sigma", "")
    for button, applied in (("#postproc-test-btn", False), ("#postproc-apply-btn", True)):
        with page.expect_response("**/api/photometry/postprocess") as resp:
            page.click(button)
        assert resp.value.ok
        assert pipeline["postprocess"][-1]["apply"] is applied
    page.click("#message-modal-ok")

    page.goto(URL)
    page.wait_for_load_state("networkidle")
    page.locator("#postproc-section > summary").click()
    page.click("#postproc-restore-btn")
    with page.expect_response("**/api/photometry/postprocess/restore"):
        page.click("#message-modal-ok")
    assert len(pipeline["restore"]) == 1


def test_delete_reduction_after_confirmation(page, pipeline):
    page.goto(URL)
    page.wait_for_load_state("networkidle")
    page.click("#delete-btn")
    with page.expect_response("**/api/photometry/delete"):
        page.click("#delete-confirm-btn")
    assert len(pipeline["delete"]) == 1


def test_successful_delete_drops_the_deleted_run_from_the_url(page, pipeline):
    page.goto(URL)
    page.wait_for_load_state("networkidle")
    page.click("#delete-btn")
    page.click("#delete-confirm-btn")
    page.wait_for_url(lambda url: "run=" not in url)
    assert page.url.endswith(f"/photometry?{DATASET}")


def test_default_settings_reset_persists_after_navigation(page):
    page.goto(URL)
    page.wait_for_load_state('networkidle')
    defaults = {s: page.input_value(s) for s in ('#opt-centroid_method', '#opt-max_num_stars')}
    for selector in (*defaults, '#opt-make_gif', '#defaults-btn'):
        reveal(page, selector)
    page.select_option('#opt-centroid_method', 'com')
    page.fill('#opt-max_num_stars', '17')
    page.locator('#opt-make_gif').uncheck()
    page.click('#defaults-btn')
    for selector, value in defaults.items():
        expect(page.locator(selector)).to_have_value(value)
    expect(page.locator('#opt-make_gif')).to_be_checked()
    page.goto('/jobs')
    page.goto(URL)
    page.wait_for_load_state('networkidle')
    for selector, value in defaults.items():
        expect(page.locator(selector)).to_have_value(value)


def test_log_remains_live_through_finalizing_and_reports_failure(page, pipeline, log_stream):
    page.goto(URL)
    page.wait_for_load_state('networkidle')
    page.click('#run-full-btn')
    log_stream('running', 'reducing frame 1')
    expect(page.locator('#run-log')).to_contain_text('reducing frame 1')
    log_stream('finalizing', 'worker: writing final products')
    expect(page.locator('#run-log')).to_contain_text('worker: writing final products')
    expect(page.locator('#cancel-btn')).to_be_visible()
    expect(page.locator('#run-full-btn')).to_be_disabled()
    log_stream('error', 'worker failed', returncode=1, error_desc='worker failed')
    expect(page.locator('#run-log')).to_contain_text('worker failed')
    expect(page.locator('#run-full-btn')).to_be_enabled()
    expect(page.locator('#cancel-btn')).not_to_be_visible()
    assert page.evaluate('__logStreams.at(-1).readyState') == 2


def test_completed_run_reloads_and_keeps_the_trailing_log(page, pipeline, log_stream):
    page.goto(URL)
    page.wait_for_load_state('networkidle')
    page.click('#run-test-btn')
    log_stream('running', 'reducing')
    with page.expect_navigation():
        log_stream('done', 'final worker output', returncode=0)
    page.wait_for_load_state('networkidle')
    assert 'run=r1' in page.url
    expect(page.locator('#run-log')).to_contain_text('final worker output')


def test_failed_launch_recovers_buttons_and_displays_reason(page):
    page.route('**/api/photometry/run', lambda r: r.fulfill(json={'ok': False, 'error': 'queue is full'}))
    page.goto(URL)
    page.wait_for_load_state('networkidle')
    page.click('#run-test-btn')
    expect(page.locator('#run-log')).to_contain_text('queue is full')
    expect(page.locator('#run-test-btn')).to_be_enabled()
    expect(page.locator('#run-full-btn')).to_be_enabled()


def test_delete_can_be_cancelled_without_a_request(page, pipeline):
    page.goto(URL)
    page.wait_for_load_state('networkidle')
    page.click('#delete-btn')
    page.click('#delete-cancel-btn')
    expect(page.locator('#delete-confirm-overlay')).not_to_be_visible()
    assert pipeline['delete'] == []


def test_delete_confirmation_names_the_selected_run(page, live_server, pipeline):
    parent = live_server.root / 'prose/muscat3/261001/_runs/TOI-1000'
    second = parent / 'r-extra'
    shutil.copytree(parent / RUN_ID, second)
    (second / '_webrun_meta.json').write_text('{"run_id":"r-extra","run_name":"another reduction"}')
    try:
        page.goto(f'/photometry?{DATASET}&run=r-extra')
        page.wait_for_load_state('networkidle')
        page.click('#delete-btn')
        message = page.locator('#delete-confirm-overlay')
        expect(message).to_contain_text('run another reduction')
        page.click('#delete-cancel-btn')
        assert pipeline['delete'] == []
    finally:
        page.context.close()
        shutil.rmtree(second)


def test_deleted_run_shows_not_found_with_links_to_remaining_runs(page):
    page.problems.allowed.append('HTTP 404: GET /photometry?')
    page.problems.allowed.append('status of 404')
    response = page.goto(f'/photometry?{DATASET}&run=gone')
    assert response.status == 404
    notice = page.locator('#run-not-found')
    expect(notice).to_contain_text('Run not found')
    expect(notice).to_contain_text('gone')
    # The page must not quietly show another run's products in its place.
    expect(page.locator('#delete-btn')).to_have_count(0)
    notice.get_by_role('link', name=RUN_ID).click()
    page.wait_for_url(f'**run={RUN_ID}')
    expect(page.locator('#run-not-found')).to_have_count(0)
    expect(page.locator('#delete-btn')).to_be_visible()


@pytest.mark.parametrize('state', ['pending', 'running'])
def test_queued_run_without_a_directory_is_not_reported_missing(page, monkeypatch, state):
    # A full run queued at capacity, or not yet claimed by a worker, has a job
    # row but no run directory; its /jobs link must not read as deleted.
    from muscat_db import web

    store = QueuedJobStore('photometry:muscat3/261001/TOI-1000/r-queued', state)
    monkeypatch.setattr(web, 'get_job_store', lambda: store)
    response = page.goto(f'/photometry?{DATASET}&run=r-queued')
    assert response.status == 200
    expect(page.locator('#run-not-found')).to_have_count(0)


def test_missing_raw_data_disables_launches(page, live_server):
    raw = live_server.root / 'raw' / 'MuSCAT3' / '261001'
    renamed = raw.with_name('held-261001')
    raw.rename(renamed)
    try:
        page.goto(URL)
        expect(page.locator('#run-test-btn')).to_be_disabled()
        expect(page.locator('#run-full-btn')).to_be_disabled()
    finally:
        renamed.rename(raw)


def test_switch_run_and_download_the_selected_lightcurve(page, live_server):
    parent = live_server.root / 'prose/muscat3/261001/_runs/TOI-1000'
    second = parent / 'r-extra'
    shutil.copytree(parent / RUN_ID, second)
    (second / '_webrun_meta.json').write_text('{"run_id":"r-extra","run_name":"another reduction"}')
    try:
        page.goto(URL)
        page.locator('a.site-chip', has_text='another reduction').click()
        page.wait_for_url('**run=r-extra')
        with page.expect_download() as download:
            page.locator('a[href*="TOI-1000_muscat3_gp_261001.csv"]').first.click()
        contents = Path(download.value.path()).read_text()
        assert contents.startswith('BJD_TDB,Flux,Flux_err')
        assert len(contents.splitlines()) == 61
    finally:
        # Lazy product images can still be loading after the CSV download.
        # Closing the browser alone can leave server-side file reads in flight.
        page.wait_for_load_state("networkidle")
        page.context.close()
        shutil.rmtree(second)


def test_copy_command_writes_the_displayed_command(page):
    page.context.grant_permissions(['clipboard-read', 'clipboard-write'])
    page.goto(URL)
    reveal(page, '#copy-cmd')
    page.wait_for_function("document.getElementById('full-cmd').textContent.includes('--target_name')")
    command = page.inner_text('#full-cmd')
    page.click('#copy-cmd')
    page.wait_for_function('expected => navigator.clipboard.readText().then(t => t === expected)', arg=command)


def test_pipeline_controls_persist_after_navigation_and_are_sent_on_run(page, pipeline):
    page.goto(URL)
    page.wait_for_load_state('networkidle')
    values = {
        'target_id': '1', 'comparison_ids': '2,3', 'max_num_stars': '25',
        'n_stars_align': '8', 'min_star_separation': '12', 'min_star_area': '15',
        'aper_radii': '8,16,2', 'annulus': '20,30', 'cutout_size': '80',
        'ccd_trim': '1,2', 'edge_margin': '12', 'bin_size_minutes': '2',
        'test_run_frames': '12', 'display_stack_nframes': '4',
        'exclude_after_jd': '2461314.9', 'exclude_before_jd': '2461314.7',
        'sig_bkg': '4', 'sig_fwhm': '4', 'sig_dx': '4', 'sig_dy': '4',
        'sig_flux': '4', 'flux_poly_deg': '2',
    }
    for key, value in values.items():
        reveal(page, '#opt-' + key)
        page.fill('#opt-' + key, value)
    flags = {}
    for key in ('make_gif', 'plot_gaia_sources', 'use_barycorrpy', 'overwrite'):
        selector = '#opt-' + key
        reveal(page, selector)
        flags[key] = not page.is_checked(selector)
        page.locator(selector).set_checked(flags[key])
    page.wait_for_function("""([key, values, flags]) => {
      const saved = JSON.parse(localStorage.getItem(key) || '{}');
      return Object.entries({...values, ...flags}).every(([k, v]) => saved[k] === v);
    }""", arg=[OPT_KEY, values, flags])
    page.goto('/jobs')
    page.goto(URL)
    page.wait_for_load_state('networkidle')
    for key, value in values.items():
        expect(page.locator('#opt-' + key)).to_have_value(value)
    for key, value in flags.items():
        assert page.is_checked('#opt-' + key) == value
    with page.expect_response('**/api/photometry/run'):
        page.click('#run-test-btn')
    options = pipeline['start'][-1]['options']
    text_keys = {'target_id', 'comparison_ids', 'aper_radii', 'annulus', 'ccd_trim',
                 'exclude_after_jd', 'exclude_before_jd'}
    for key, value in values.items():
        if key in text_keys:
            assert options[key] == value
        else:
            assert options[key] == pytest.approx(float(value))
    assert {key: options[key] for key in flags} == flags


def test_precision_options_persist_submit_and_reset(page, pipeline):
    page.goto(URL)
    page.wait_for_load_state("networkidle")
    values = dict(comp_ensemble="bright", comp_criterion="check", comp_min_flux_frac="0.4",
                  clip_star_sigma="5", clip_star_window="13")
    for key, value in values.items():
        selector = "#opt-" + key
        reveal(page, selector)
        if key in ("comp_ensemble", "comp_criterion"):
            page.select_option(selector, value)
        else:
            page.fill(selector, value)
    reveal(page, "#opt-save_flux_datacube")
    page.check("#opt-save_flux_datacube")
    page.wait_for_function("""([key, values]) => {
        const saved = JSON.parse(localStorage.getItem(key) || '{}');
        return saved.save_flux_datacube === true &&
            Object.entries(values).every(([k, v]) => saved[k] === v);
    }""", arg=[OPT_KEY, values])
    page.goto("/jobs")
    page.goto(URL)
    page.wait_for_load_state("networkidle")
    for key, value in values.items():
        expect(page.locator("#opt-" + key)).to_have_value(value)
    expect(page.locator("#opt-save_flux_datacube")).to_be_checked()
    with page.expect_response("**/api/photometry/run") as response:
        page.click("#run-full-btn")
    assert response.value.ok
    options = pipeline["start"][-1]["options"]
    assert options["comp_ensemble"] == "bright"
    assert options["comp_criterion"] == "check"
    for key in ("comp_min_flux_frac", "clip_star_sigma", "clip_star_window"):
        assert options[key] == float(values[key])
    assert options["save_flux_datacube"] is True
    with page.expect_response("**/api/photometry/cancel"):
        page.click("#cancel-btn")
    reveal(page, "#defaults-btn")
    page.click("#defaults-btn")
    page.goto("/jobs")
    page.goto(URL)
    for key, value in dict(comp_ensemble="greedy", comp_criterion="target",
                           comp_min_flux_frac="0.3", clip_star_sigma="", clip_star_window="11").items():
        expect(page.locator("#opt-" + key)).to_have_value(value)
    expect(page.locator("#opt-save_flux_datacube")).not_to_be_checked()


def test_noise_budget_and_flux_cube_download(page, live_server):
    import numpy as np
    from muscat_db import photometry as phot

    directory = phot.run_output_dir("muscat3", "261001", "TOI-1000", RUN_ID)
    archive = directory / "TOI-1000_muscat3_261001.npz"
    original = archive.read_bytes() if archive.exists() else None
    cube = directory / "TOI-1000_muscat3_261001_flux_datacube.npz"
    try:
        np.savez(archive, gp__noise__expected_ppt=2.04, gp__noise__achieved_ptp_ppt=2.0,
                 gp__noise__ratio=2/2.04)
        np.savez(cube, flux=np.zeros((2, 3, 4)))
        page.goto(URL)
        table = page.locator("#noise-budget")
        expect(table).to_contain_text("2.040")
        expect(table).to_contain_text("0.980")
        link = page.get_by_role("link", name="per-star flux cube (.npz)")
        with page.expect_download() as download:
            link.click()
        assert download.value.suggested_filename == cube.name
    finally:
        cube.unlink(missing_ok=True)
        if original is None:
            archive.unlink(missing_ok=True)
        else:
            archive.write_bytes(original)


def test_target_coordinate_hint_says_what_will_be_sent(page):
    """Issue #457: the page names the coordinate and its source before a run."""
    page.goto(URL)
    reveal(page, "#opt-target_coord")
    hint = page.locator("#hint-target_coord")
    expect(hint).to_have_text(re.compile(r"^--target_coord; empty → 117\.929000 12\.536000 deg from the (TOI|NASA Exoplanet Archive) catalog$"))
    expect(page.locator("#opt-target_coord")).to_have_attribute("placeholder", "auto")
    page.fill("#opt-target_coord", "117.93 12.54")
    expect(hint).to_have_text("--target_coord; sent as entered")
    # A saved entry is restored with its hint, and clearing it returns to the automatic one.
    page.wait_for_function(
        "key => JSON.parse(localStorage.getItem(key) || '{}').target_coord === '117.93 12.54'", arg=OPT_KEY)
    page.reload()
    reveal(page, "#opt-target_coord")
    expect(hint).to_have_text("--target_coord; sent as entered")
    page.fill("#opt-target_coord", "")
    expect(hint).to_contain_text("empty → 117.929000 12.536000 deg")


def test_barycorrpy_label_says_bjd_tdb_is_always_computed(page):
    page.goto(URL)
    reveal(page, "#opt-use_barycorrpy")
    label = page.locator("label").filter(has=page.locator("#opt-use_barycorrpy"))
    expect(label).to_contain_text("Compute BJD_TDB with barycorrpy (default: astropy)")
    expect(label.get_by_role("button", name="Help")).to_have_accessible_description(
        re.compile(r"BJD_TDB is always computed.*only switches the conversion"))


def test_results_state_the_time_system(page):
    page.goto(URL)
    expect(page.locator("#time-system-note")).to_contain_text("Times in the CSV files are BJD_TDB")
    expect(page.locator("#time-system-note")).to_contain_text("JD (UTC)")
