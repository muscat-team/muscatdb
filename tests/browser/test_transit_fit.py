"""The /transit-fit page: results, light-curve selection, priors, options, run, delete.

``fit.start_fit`` / ``cancel_fit`` / ``delete_fit`` are stubbed in-process
(the harness refuses real launches); everything in front of them is real.
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import expect
import pytest

from tests.browser.helpers import reveal
from tests.browser.seed import FIT_RUN_ID


DATASET = "inst=muscat3&date=261001&target=TOI-1000"
URL = f"/transit-fit?{DATASET}"
OPT_KEY = "fit:options:muscat3:261001:TOI-1000"
CSVS = ["TOI-1000_muscat3_gp_261001.csv", "TOI-1000_muscat3_rp_261001.csv"]


@pytest.fixture
def fitter(monkeypatch):
    from muscat_db import transit_fit as fit

    calls = {"start": [], "cancel": [], "delete": []}

    def start_fit(inst, date, target, options, **kwargs):
        calls["start"].append({"options": options, **kwargs})
        return {"ok": True, "key": f"transit_fit:{inst}/{date}/{target}/{FIT_RUN_ID}", "run_id": FIT_RUN_ID}

    monkeypatch.setattr(fit, "start_fit", start_fit)
    monkeypatch.setattr(fit, "cancel_fit", lambda *a, **k: calls["cancel"].append(k) or {"ok": True})
    monkeypatch.setattr(fit, "delete_fit", lambda *a, **k: calls["delete"].append(k) or {"ok": True, "count": 1})
    return calls


def _csv_count(page) -> str:
    return page.inner_text("#csv-count")


def _query_toi_catalog(page) -> None:
    reveal(page, "#archive-source-select")
    page.select_option("#archive-source-select", "toi")
    with page.expect_response("**/api/transit-fit/query-archive**") as resp:
        page.click("#archive-query-btn")
    assert resp.value.json()["ok"]
    assert "Successfully loaded parameters for TOI-1000.01" in page.inner_text("#message-modal-text")
    page.click("#message-modal-ok")


def test_fit_results_are_shown(page):
    page.goto(URL)
    page.wait_for_load_state("networkidle")
    plot = page.locator(f"img[src*='/run/{FIT_RUN_ID}/fit.png']").first
    page.wait_for_function("i => i.complete && i.naturalWidth > 0", arg=plot.element_handle())
    body = page.locator("body").inner_text()
    assert "t0[0]" in body and "2461314.86" in body
    assert page.locator("a", has_text="Download all output").count() == 1


def test_light_curve_selection_persists(page):
    page.goto(URL)
    assert _csv_count(page) == "2 / 2 selected"
    page.click("#deselect-all-csvs")
    assert _csv_count(page) == "0 / 2 selected"
    page.locator(".csv-checkbox").first.check()
    assert _csv_count(page) == "1 / 2 selected"

    page.reload()
    assert _csv_count(page) == "1 / 2 selected"
    page.click("#select-all-csvs")
    assert _csv_count(page) == "2 / 2 selected"


def test_archive_query_fills_the_priors(page):
    page.goto(URL)
    card = "planet-card-0"
    reveal(page, f"#planet-period-{card}")
    assert page.input_value(f"#planet-period-{card}") == ""
    _query_toi_catalog(page)
    page.wait_for_function(f"document.getElementById('planet-period-{card}').value !== ''")
    assert page.input_value(f"#planet-period-{card}").startswith("2.17")
    assert page.input_value(f"#planet-t0-{card}").startswith("2461314.86")
    assert page.input_value("#opt-teff").startswith("6100")


def test_options_persist_per_dataset(page):
    page.goto(URL)
    for control in ("#opt-tune", "#opt-draws", "#opt-spline"):
        reveal(page, control)
    page.fill("#opt-tune", "500")
    page.fill("#opt-draws", "750")
    page.locator("#opt-spline").check()
    page.goto("/jobs")
    page.goto(URL)
    assert page.input_value("#opt-tune") == "500"
    assert page.input_value("#opt-draws") == "750"
    assert page.is_checked("#opt-spline")
    # Saved under a per-dataset key, so other datasets keep their own options.
    assert page.evaluate(f"JSON.parse(localStorage.getItem('{OPT_KEY}')).tune") == "500"


def test_save_clear_and_restore_values(page):
    page.goto(URL)
    for control in ("#opt-tune", "#opt-teff", "#save-btn"):
        reveal(page, control)
    page.fill("#opt-teff", "5800")
    page.fill("#opt-tune", "1234")
    page.click("#save-btn")
    assert page.inner_text("#message-modal-title") == "Success"
    page.click("#message-modal-ok")

    page.click("#clear-btn")
    assert page.input_value("#opt-teff") == ""
    assert page.input_value("#opt-tune") == "2000"

    page.click("#restore-btn")
    assert page.input_value("#opt-teff") == "5800"
    assert page.input_value("#opt-tune") == "1234"


def test_add_planet_card(page):
    page.goto(URL)
    cards = page.locator("#planet-cards-container > *")
    assert cards.count() == 1
    reveal(page, "#add-planet-btn")
    page.click("#add-planet-btn")
    assert cards.count() == 2
    assert "c" in cards.nth(1).inner_text().lower()


def _complete_priors(page) -> None:
    """Fill what the TOI catalog lacks, as a user would before running."""
    card = "planet-card-0"
    for field, value in (
        ("#opt-teff_unc", "100"), ("#opt-logg", "4.3"), ("#opt-logg_unc", "0.1"),
        ("#opt-feh", "0.0"), ("#opt-feh_unc", "0.1"),
        (f"#planet-b-{card}", "0,1"), (f"#planet-ror-{card}", "0.065,0.01"),
    ):
        reveal(page, field)
        page.fill(field, value)


def test_test_run_sends_the_selected_curves_and_priors(page, fitter):
    page.goto(URL)
    _query_toi_catalog(page)
    _complete_priors(page)
    page.locator(".csv-checkbox").nth(1).uncheck()
    with page.expect_response("**/api/transit-fit/run") as resp:
        page.click("#run-test-btn")
    assert resp.value.ok
    call = fitter["start"][-1]
    assert call["test_run"] is True
    assert [Path(c).name for c in call["selected_csvs"]] == CSVS[:1]
    assert float(call["options"]["period"]) == pytest.approx(2.17)


def test_run_is_blocked_until_required_priors_are_set(page, fitter):
    page.goto(URL)
    page.click("#run-test-btn")
    assert page.locator(".field-error.visible").count() > 0
    assert fitter["start"] == []


@pytest.mark.xfail(
    strict=True,
    reason="an archive query with no b or Rp/R* overwrites their default uniform "
    "priors (0,1 and 0,0.5) with blanks, so the run then fails validation",
)
def test_archive_query_keeps_default_b_and_ror_priors(page):
    page.goto(URL)
    card = "planet-card-0"
    reveal(page, f"#planet-b-{card}")
    assert page.input_value(f"#planet-b-{card}") == "0,1"
    _query_toi_catalog(page)
    assert page.input_value(f"#planet-b-{card}") == "0,1"
    assert page.input_value(f"#planet-ror-{card}") == "0,0.5"


def test_delete_fit_after_confirmation(page, fitter):
    page.goto(URL)
    page.click("#delete-btn")
    with page.expect_response("**/api/transit-fit/delete"):
        page.click("#delete-confirm-btn")
    assert fitter["delete"][-1]["run_id"] == FIT_RUN_ID


def _ready_to_fit(page):
    page.goto(URL)
    page.wait_for_load_state('networkidle')
    _query_toi_catalog(page)
    _complete_priors(page)


def test_full_run_cancel_and_terminal_log(page, fitter, log_stream):
    _ready_to_fit(page)
    if page.locator('#run-mode-select').count():
        reveal(page, '#run-mode-select')
        page.select_option('#run-mode-select', 'new')
    with page.expect_response('**/api/transit-fit/run') as response:
        page.click('#run-fit-btn')
    assert response.value.ok
    assert fitter['start'][-1]['test_run'] is False
    log_stream('pending', '')
    expect(page.locator('#run-log')).to_contain_text('Queued')
    log_stream('running', 'sampling chain 1')
    expect(page.locator('#run-log')).to_contain_text('sampling chain 1')
    with page.expect_response('**/api/transit-fit/cancel'):
        page.click('#cancel-btn')
    assert fitter['cancel'][-1]['run_id'] == FIT_RUN_ID
    log_stream('cancelled', 'last sampler output')
    expect(page.locator('#run-log')).to_contain_text('last sampler output')
    expect(page.locator('#run-log')).to_contain_text('cancelled')
    expect(page.locator('#run-fit-btn')).to_be_enabled()


def test_log_probability_uses_selected_curves_and_recovers_after_failure(page, monkeypatch):
    from muscat_db import transit_fit
    calls = []
    def compute(*args, **kwargs):
        calls.append(kwargs)
        return {'ok': True, 'logp': -123.456, 'text': 'logP= -123.456'}
    monkeypatch.setattr(transit_fit, 'compute_logp', compute)
    _ready_to_fit(page)
    page.locator('.csv-checkbox').nth(1).uncheck()
    page.click('#calc-logp-btn')
    expect(page.locator('#logp-result')).to_have_text('logP= -123.456')
    assert [Path(c).name for c in calls[-1]['selected_csvs']] == CSVS[:1]
    page.problems.allowed += ['HTTP 400: POST /api/transit-fit/logp', 'status of 400']
    monkeypatch.setattr(transit_fit, 'compute_logp', lambda *a, **k: {'ok': False, 'error': 'invalid model'})
    page.click('#calc-logp-btn')
    expect(page.locator('#opt-error')).to_contain_text('invalid model')
    expect(page.locator('#calc-logp-btn')).to_be_enabled()


def test_added_planet_can_be_removed_and_other_priors_are_preserved(page):
    _ready_to_fit(page)
    period = page.input_value('#planet-period-planet-card-0')
    reveal(page, '#add-planet-btn')
    page.click('#add-planet-btn')
    expect(page.locator('.planet-card')).to_have_count(2)
    page.locator('.planet-card').nth(1).locator('.planet-remove-btn').click()
    expect(page.locator('.planet-card')).to_have_count(1)
    expect(page.locator('#planet-period-planet-card-0')).to_have_value(period)


def test_no_selected_curves_cannot_start_a_fit(page):
    _ready_to_fit(page)
    page.problems.allowed += ['HTTP 400: POST /api/transit-fit/run', 'status of 400']
    page.click('#deselect-all-csvs')
    with page.expect_response('**/api/transit-fit/run') as response:
        page.click('#run-test-btn')
    assert response.value.status == 400
    expect(page.locator('#run-log')).to_contain_text('No lightcurves selected')
    expect(page.locator('#run-test-btn')).to_be_enabled()


def test_query_failure_preserves_entered_priors_and_allows_retry(page):
    _ready_to_fit(page)
    period = page.input_value('#planet-period-planet-card-0')
    page.route('**/api/transit-fit/query-archive**', lambda r: r.fulfill(json={'ok': False, 'error': 'catalog unavailable'}))
    page.click('#archive-query-btn')
    expect(page.locator('#message-modal-text')).to_contain_text('catalog unavailable')
    page.click('#message-modal-ok')
    expect(page.locator('#planet-period-planet-card-0')).to_have_value(period)
    expect(page.locator('#archive-query-btn')).to_be_enabled()


def test_failed_launch_reenables_run_buttons(page):
    _ready_to_fit(page)
    page.route('**/api/transit-fit/run', lambda r: r.fulfill(json={'ok': False, 'error': 'worker unavailable'}))
    page.click('#run-test-btn')
    expect(page.locator('#run-log')).to_contain_text('worker unavailable')
    expect(page.locator('#run-test-btn')).to_be_enabled()
    expect(page.locator('#run-fit-btn')).to_be_enabled()


def test_delete_fit_can_be_cancelled(page, fitter):
    page.goto(URL)
    page.click('#delete-btn')
    page.click('#delete-cancel-btn')
    expect(page.locator('#delete-confirm-overlay')).not_to_be_visible()
    assert fitter['delete'] == []


def test_missing_lightcurves_links_back_to_photometry(page):
    page.goto('/transit-fit?inst=sinistro&date=261003&target=TOI-1000')
    expect(page.get_by_role('heading', name='No Photometry CSVs Found')).to_be_visible()
    page.get_by_role('link', name='Go to Photometry Page').click()
    page.wait_for_url('**/photometry?**')


def test_multiple_planets_submit_distinct_priors_and_restore_after_reload(page, fitter):
    _ready_to_fit(page)
    reveal(page, '#add-planet-btn')
    page.click('#add-planet-btn')
    second = page.locator('.planet-card').nth(1)
    for selector, value in {
        '.planet-period': '4.34,0.001', '.planet-dur': '3,0.1',
        '.planet-t0': '2461314.87,0.001', '.planet-b': '0,1', '.planet-ror': '0.02,0.1',
    }.items():
        control = second.locator(selector)
        control.fill(value)
        control.press('Tab')
    with page.expect_response('**/api/transit-fit/run'):
        page.click('#run-test-btn')
    options = fitter['start'][-1]['options']
    assert options['planets'] == 'b,c'
    assert float(options['period_b']) == pytest.approx(2.17)
    assert float(options['period_c']) == pytest.approx(4.34)
    page.reload()
    expect(page.locator('.planet-card')).to_have_count(2)
    expect(page.locator('.planet-card').nth(1).locator('.planet-period')).to_have_value('4.34,0.001')


def test_download_all_outputs_contains_the_fit_and_summary(page):
    import zipfile
    page.goto(URL)
    with page.expect_download() as download:
        page.get_by_role('link', name='Download all output', exact=False).click()
    with zipfile.ZipFile(download.value.path()) as archive:
        names = archive.namelist()
        summary = next(n for n in names if n.endswith('summary.csv'))
        assert any(n.endswith('fit.png') for n in names)
        assert 't0[0],2461314.86,0.0004' in archive.read(summary).decode()



def test_sampler_and_detrending_options_persist_and_reach_the_fitter(page, fitter):
    _ready_to_fit(page)
    flags = {
        'spline': True, 'clip': True, 'chunk_offset': True,
        'add_bias': True, 'quadratic': True, 'include_mean': False,
        'plot_midtransit': False, 'plot_ingress_egress': False,
    }
    for key, checked in flags.items():
        reveal(page, '#opt-' + key)
        page.locator('#opt-' + key).set_checked(checked)
    values = {
        'tune': '600', 'draws': '800', 'chains': '2', 'cores': '2',
        'spline_knots': '10', 'clip_nsig': '4', 'chunk_thresh': '0.5',
        'trim_beg': '1', 'trim_end': '2',
    }
    for key, value in values.items():
        reveal(page, '#opt-' + key)
        page.fill('#opt-' + key, value)
    page.goto('/jobs')
    page.goto(URL)
    for key, value in values.items():
        expect(page.locator('#opt-' + key)).to_have_value(value)
    for key, checked in flags.items():
        assert page.is_checked('#opt-' + key) == checked
    with page.expect_response('**/api/transit-fit/run'):
        page.click('#run-test-btn')
    options = fitter['start'][-1]['options']
    assert {key: options[key] for key in values} == values
    assert {key: options[key] for key in flags} == {key: str(value).lower() for key, value in flags.items()}


def test_run_selector_loads_the_selected_fit_results(page, live_server):
    import shutil
    import yaml
    parent = live_server.root / 'timer/muscat3/261001/TOI-1000'
    extra = parent / 'fit-extra'
    shutil.copytree(parent / FIT_RUN_ID, extra)
    (extra / 'meta.yaml').write_text(yaml.safe_dump({'run_id': 'fit-extra', 'run_name': 'another fit', 'mode': 'full', 'site': 'ogg'}))
    (extra / 'out/summary.csv').write_text('parameter,mean,std\nt0[0],2461314.87,0.0005\n')
    try:
        page.goto(URL)
        page.locator('a.site-chip', has_text='another fit').click()
        page.wait_for_url('**run=fit-extra')
        expect(page.locator('body')).to_contain_text('2461314.87')
        assert 'fit-extra' in page.locator('img[src*="/fit.png"]').first.get_attribute('src')
    finally:
        shutil.rmtree(extra)


# Middle of the seeded light curves (2461314.80 to 2461314.918, see seed.LIGHTCURVE).
FLARE_MID = "2461314.85900"


def _flare_cell(page, row: int, param: str, which: str):
    return page.locator("#flare-rows .flare-row").nth(row).locator(
        f".flare-cell[data-param='{param}'][data-which='{which}']"
    )


def _enable_flares(page) -> None:
    reveal(page, "#opt-include_flare")
    page.locator("#opt-include_flare").check()


def test_flare_rows_default_to_the_data_and_persist_after_reload(page, fitter):
    _ready_to_fit(page)
    _enable_flares(page)
    # A new flare starts at the middle of the selected light curves.
    expect(_flare_cell(page, 0, "tpeak", "val")).to_have_value(FLARE_MID)
    page.click("#flare-add-btn")
    assert page.locator("#flare-rows .flare-row").count() == 2
    _flare_cell(page, 0, "tpeak", "val").fill("2461314.82")
    _flare_cell(page, 0, "tpeak", "unc").fill("0.005")
    _flare_cell(page, 1, "tpeak", "val").fill("2461314.90")
    _flare_cell(page, 1, "tpeak", "unc").fill("0.005")
    _flare_cell(page, 1, "ampl", "val").fill("0.02")

    page.goto("/jobs")
    page.goto(URL)
    reveal(page, "#opt-include_flare")
    assert page.is_checked("#opt-include_flare")
    assert page.locator("#flare-rows .flare-row").count() == 2
    expect(_flare_cell(page, 0, "tpeak", "val")).to_have_value("2461314.82")
    expect(_flare_cell(page, 1, "tpeak", "val")).to_have_value("2461314.90")
    expect(_flare_cell(page, 1, "ampl", "val")).to_have_value("0.02")

    with page.expect_response("**/api/transit-fit/run"):
        page.click("#run-test-btn")
    options = fitter["start"][-1]["options"]
    assert options["include_flare"] == "true"
    assert options["flare_tpeak"] == "2461314.82,0.005; 2461314.90,0.005"
    assert options["flare_fwhm"] == "0.02,0.01; 0.02,0.01"
    assert options["flare_ampl"] == "0.01,0.01; 0.02,0.01"


def test_flare_outside_the_data_blocks_the_run_until_the_row_is_removed(page, fitter):
    _ready_to_fit(page)
    _enable_flares(page)
    page.click("#flare-add-btn")
    _flare_cell(page, 1, "tpeak", "val").fill("2460000.5")

    page.click("#run-test-btn")
    assert "Flare 2 tpeak" in page.inner_text("#flare-rows-error")
    assert fitter["start"] == []

    page.locator(".flare-remove-btn").nth(1).click()
    assert page.locator("#flare-rows .flare-row").count() == 1
    # The last flare stays: Use Flare with no rows is not a valid setup.
    assert page.locator(".flare-remove-btn").first.is_disabled()
    with page.expect_response("**/api/transit-fit/run"):
        page.click("#run-test-btn")
    assert fitter["start"][-1]["options"]["flare_tpeak"] == f"{FLARE_MID},0.1"


def test_flare_prior_shape_converts_every_row(page):
    _ready_to_fit(page)
    _enable_flares(page)
    page.click("#flare-add-btn")
    _flare_cell(page, 0, "ampl", "val").fill("0.01")
    _flare_cell(page, 0, "ampl", "unc").fill("0.001")
    _flare_cell(page, 1, "ampl", "val").fill("0.02")
    _flare_cell(page, 1, "ampl", "unc").fill("0.002")
    page.select_option("#opt-flare_ampl_prior", "uniform")
    # Gaussian mean,unc becomes the 3-sigma bounds on both rows.
    expect(_flare_cell(page, 0, "ampl", "val")).to_have_value("0.007")
    expect(_flare_cell(page, 0, "ampl", "unc")).to_have_value("0.013")
    expect(_flare_cell(page, 1, "ampl", "val")).to_have_value("0.014")
    expect(_flare_cell(page, 1, "ampl", "unc")).to_have_value("0.026")
    assert _flare_cell(page, 1, "ampl", "val").get_attribute("placeholder") == "low"
    # A BJD keeps its precision through the conversion.
    page.select_option("#opt-flare_tpeak_prior", "uniform")
    expect(_flare_cell(page, 0, "tpeak", "val")).to_have_value("2461314.55900")
    expect(_flare_cell(page, 0, "tpeak", "unc")).to_have_value("2461315.15900")
