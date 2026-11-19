"""The /exposure calculator: per-band results, peak counts, lookup, saved form."""

from __future__ import annotations

from playwright.sync_api import expect
import pytest

URL = "/exposure?inst=muscat3&target=TOI-1000"
MAGS = {"gp": "10.2", "rp": "9.8", "ip": "9.6", "zs": "9.5"}


def _fill_mags(page) -> None:
    for band, mag in MAGS.items():
        page.fill(f'.mag-input[data-band="{band}"]', mag)


def _results(page) -> dict[str, list[str]]:
    rows = page.locator("#results-body tr").all()
    out = {}
    for row in rows:
        cells = [c.strip() for c in row.locator("td").all_inner_texts()]
        band = cells[0].split()[0] if cells and cells[0] else ""
        if band in MAGS:  # skip the per-star header rows
            out[band] = cells[2:]  # after the band and star-name columns
    return out


def test_calculate_gives_a_safe_exposure_per_band(page):
    page.goto(URL)
    _fill_mags(page)
    with page.expect_response("**/api/exposure/calculate") as resp:
        page.click("#calc-btn")
    assert resp.value.json()["ok"]
    results = _results(page)
    assert sorted(results) == sorted(MAGS)
    for band, cells in results.items():
        assert cells[0] == f"{float(MAGS[band]):.2f}"
        assert float(cells[1]) > 0  # exposure (s)
        assert cells[-1] == "SAFE"
    assert "recommended exposure" in page.locator(".exp-results").inner_text().lower()


def test_peak_counts_flag_saturation_at_a_fixed_exposure(page):
    page.goto(URL)
    _fill_mags(page)
    page.fill("#calc-exptime", "30")
    with page.expect_response("**/api/exposure/calculate"):
        page.click("#calc-peak-btn")
    results = _results(page)
    assert all(cells[1] == "30.0" for cells in results.values())
    assert all(cells[-1] == "SATURATED" for cells in results.values())


def test_form_is_restored_after_navigation(page):
    page.goto(URL)
    _fill_mags(page)
    page.fill("#airmass", "1.6")
    page.select_option("#sat-frac", "0.8")
    page.goto("/jobs")
    page.goto("/exposure")
    assert page.input_value("#sel-inst") == "muscat3"
    assert page.input_value("#target-name") == "TOI-1000"
    assert page.input_value('.mag-input[data-band="rp"]') == "9.8"
    assert page.input_value("#airmass") == "1.6"
    assert page.input_value("#sat-frac") == "0.8"

    page.click("#clear-mags")
    assert all(page.input_value(f'.mag-input[data-band="{b}"]') == "" for b in MAGS)


def test_lookup_reports_an_unreachable_catalog(page):
    page.goto(URL)
    page.click("#lookup-btn")
    status = page.locator("#lookup-status")
    page.wait_for_function("!document.getElementById('lookup-status').innerText.includes('Looking up')")
    assert status.inner_text().startswith("✗")
    assert "err" in status.get_attribute("class")


def test_lookup_needs_a_target_name(page):
    page.goto("/exposure?inst=muscat3")
    page.fill("#target-name", "")
    page.click("#lookup-btn")
    assert page.inner_text("#lookup-status") == "Enter a target name"


def test_successful_lookup_fills_magnitudes_and_persists(page, monkeypatch):
    from muscat_db import exposure
    monkeypatch.setattr(exposure, 'lookup_magnitudes', lambda *a, **k: ({b: float(m) for b, m in MAGS.items()}, 'Pan-STARRS'))
    page.goto(URL)
    page.click('#lookup-btn')
    expect(page.locator('#lookup-status')).to_contain_text('Pan-STARRS')
    for band, mag in MAGS.items():
        assert float(page.input_value(f'.mag-input[data-band="{band}"]')) == float(mag)
    page.reload()
    assert float(page.input_value('.mag-input[data-band="gp"]')) == 10.2


def test_calculation_requires_magnitudes_and_recovers_after_error(page):
    page.goto(URL)
    page.click('#calc-btn')
    expect(page.locator('#calc-status')).to_have_text('Enter at least one magnitude')
    _fill_mags(page)
    page.route('**/api/exposure/calculate', lambda r: r.fulfill(json={'ok': False, 'error': 'invalid seeing'}))
    page.click('#calc-btn')
    expect(page.locator('#calc-status')).to_contain_text('invalid seeing')
    expect(page.locator('#calc-btn')).to_be_enabled()
    page.unroute('**/api/exposure/calculate')
    page.click('#calc-btn')
    expect(page.locator('#results-body')).to_contain_text('safe')


def test_instrument_and_readout_mode_are_sent_to_the_calculator(page):
    page.goto('/exposure')
    page.select_option('#sel-inst', 'sinistro')
    expect(page.locator('#sel-confmode')).to_be_visible()
    modes = page.locator('#sel-confmode option').evaluate_all('os => os.map(o => o.value)')
    assert len(modes) > 1
    page.select_option('#sel-confmode', modes[-1])
    _fill_mags(page)
    with page.expect_response('**/api/exposure/calculate') as response:
        page.click('#calc-btn')
    payload = response.value.request.post_data_json
    assert payload['instrument'] == 'sinistro' and payload['confmode'] == modes[-1]
    page.reload()
    expect(page.locator('#sel-confmode')).to_have_value(modes[-1])


def test_exposure_and_peak_are_consistent_for_the_same_star(page):
    page.goto(URL)
    _fill_mags(page)
    with page.expect_response('**/api/exposure/calculate') as response:
        page.click('#calc-btn')
    result = response.value.json()
    gp = next(r for r in result['results'] if r['band'] == 'gp')
    page.fill('#calc-exptime', str(gp['exptime']))
    with page.expect_response('**/api/exposure/calculate') as peak_response:
        page.click('#calc-peak-btn')
    peak = next(r for r in peak_response.value.json()['results'] if r['band'] == 'gp')
    assert peak['peak_adu'] == pytest.approx(gp['peak_adu'], rel=0.05)
    # The rendered value rounds to whole counts, with a thousands separator.
    assert float(_results(page)['gp'][2].replace(',', '')) == pytest.approx(peak['peak_adu'], abs=1)


def test_comparison_handoff_is_consumed_persisted_and_cleared(page, monkeypatch):
    from muscat_db import exposure
    monkeypatch.setattr(exposure, 'lookup_magnitudes_with_fallback', lambda *a: ({b: 8.0 for b in MAGS}, 'Pan-STARRS', False))
    page.goto(URL)
    page.evaluate("""sessionStorage.setItem('fov.compHandoff.v1', JSON.stringify({
      target: 'TOI-1000', instrument: 'muscat3',
      comps: [{ra: 117.93, dec: 12.54, gmag: 8, bp_rp: 1, sep_arcsec: 40}]
    }))""")
    page.goto(URL + '&fromFov=1')
    expect(page.locator('#comps-status')).to_contain_text('Resolved 1/1')
    assert page.evaluate("sessionStorage.getItem('fov.compHandoff.v1')") is None
    _fill_mags(page)
    with page.expect_response('**/api/exposure/calculate') as response:
        page.click('#calc-btn')
    assert response.value.request.post_data_json['extra_sources'][0]['label'] == 'Comp 1'
    assert any(not r['is_target'] for r in response.value.json()['results'])
    page.reload()
    expect(page.locator('#comps-list')).to_contain_text('Comp 1')
    page.click('#clear-comps')
    expect(page.locator('#comps-section')).not_to_be_visible()
    page.reload()
    expect(page.locator('#comps-section')).not_to_be_visible()


def test_approximate_lookup_clears_missing_bands_and_persists(page):
    page.route('**/api/exposure/lookup-mags', lambda r: r.fulfill(json={
        'ok': True, 'mags': {'gp': 10.123456789, 'rp': 9.7, 'ip': 9.4},
        'source': 'TIC Gaia color transform', 'is_approx': True,
        'approx_reason': 'No usable measured griz in TIC or SDSS; empirical Gaia relation used.',
    }))
    page.goto(URL)
    _fill_mags(page)
    page.click('#lookup-btn')
    expect(page.locator('#lookup-status')).to_contain_text('Approximate')
    expect(page.locator('#lookup-status')).to_contain_text('No usable measured griz in TIC or SDSS')
    assert page.input_value('.mag-input[data-band="gp"]') == '10.123457'
    assert page.input_value('.mag-input[data-band="zs"]') == ''
    page.reload()
    assert page.input_value('.mag-input[data-band="zs"]') == ''
    assert page.input_value('.mag-input[data-band="gp"]') == '10.123457'
    expect(page.locator('#lookup-status')).to_contain_text('No usable measured griz in TIC or SDSS')
    page.fill('.mag-input[data-band="gp"]', '10.5')
    page.reload()
    expect(page.locator('#lookup-status')).to_contain_text('Approximate')
    expect(page.locator('#lookup-status')).to_contain_text('edited manually')


def _recommend_conditions(page):
    page.fill('#rec-sky_rate', '20')
    page.fill('#rec-read_noise_e', '6')
    page.fill('#rec-max_cycle_s', '90')


def test_precision_recommendation_uses_measured_inputs_and_persists(page):
    page.goto(URL)
    _fill_mags(page)
    page.fill('.mag-input[data-band="rp"]', '15')
    _recommend_conditions(page)
    with page.expect_response('**/api/exposure/recommend') as response:
        page.click('#recommend-btn')
    assert response.value.status == 200
    payload = response.value.request.post_data_json
    assert payload['mode'] == 'MUSCAT_FAST'
    assert payload['sky_rate'] == 20
    assert payload['max_cycle_s'] == 90
    expect(page.locator('#recommend-result')).to_contain_text('ppm in 600 s')
    expect(page.locator('#recommend-result')).to_contain_text('all four bands')
    page.goto('/jobs')
    page.goto('/exposure')
    expect(page.locator('#rec-sky_rate')).to_have_value('20')
    expect(page.locator('#rec-max_cycle_s')).to_have_value('90')
    page.click('#recommend-reset')
    expect(page.locator('#rec-sky_rate')).to_have_value('')
    expect(page.locator('#rec-max_cycle_s')).to_have_value('120')
    page.reload()
    expect(page.locator('#rec-sky_rate')).to_have_value('')


def test_precision_conditions_are_cleared_when_band_or_mode_changes(page):
    page.goto(URL)
    _recommend_conditions(page)
    page.select_option('#rec-band', 'zs')
    expect(page.locator('#rec-sky_rate')).to_have_value('')
    _recommend_conditions(page)
    page.select_option('#rec-muscat-mode', 'MUSCAT_SLOW')
    expect(page.locator('#rec-read_noise_e')).to_have_value('')
    page.reload()
    expect(page.locator('#rec-muscat-mode')).to_have_value('MUSCAT_SLOW')
    expect(page.locator('#rec-read_noise_e')).to_have_value('')


def test_precision_recommendation_reports_infeasible_settings(page):
    page.problems.allowed.extend(['HTTP 400: POST /api/exposure/recommend', 'status of 400'])
    page.goto(URL)
    _fill_mags(page)
    _recommend_conditions(page)
    page.fill('#rec-max_cycle_s', '2')
    page.click('#recommend-btn')
    expect(page.locator('#recommend-status')).to_contain_text('max cycle')
    expect(page.locator('#recommend-btn')).to_be_enabled()
    expect(page.locator('#recommend-result')).not_to_be_visible()


def test_precision_comparisons_and_selected_mode_reach_api(page, monkeypatch):
    from muscat_db import exposure
    monkeypatch.setattr(exposure, 'lookup_magnitudes_with_fallback', lambda *a: ({b: 15.0 for b in MAGS}, 'Pan-STARRS', False))
    page.goto('/exposure?inst=sinistro&target=TOI-1000')
    page.evaluate("""sessionStorage.setItem('fov.compHandoff.v1', JSON.stringify({
      target: 'TOI-1000', instrument: 'sinistro',
      comps: [{ra: 117.93, dec: 12.54, gmag: 15, bp_rp: 1, sep_arcsec: 40}]
    }))""")
    page.goto('/exposure?inst=sinistro&target=TOI-1000&fromFov=1')
    expect(page.locator('#comps-status')).to_contain_text('Resolved 1/1')
    page.fill('.mag-input[data-band="rp"]', '15')
    page.select_option('#sel-confmode', 'full_frame')
    page.select_option('#rec-site', 'lsc')
    expect(page.locator('#rec-muscat-mode')).not_to_be_visible()
    _recommend_conditions(page)
    with page.expect_response('**/api/exposure/recommend') as response:
        page.click('#recommend-btn')
    assert response.value.status == 200
    sent = response.value.request.post_data_json
    assert sent['mode'] == 'full_frame'
    assert sent['comp_mags'] == [15]
    expect(page.locator('#recommend-result')).to_be_visible()
    page.fill('.mag-input[data-band="rp"]', '16')
    expect(page.locator('#recommend-result')).not_to_be_visible()


def test_precision_medians_prefill_preserve_overrides_and_refresh(page):
    def defaults(route):
        sky = 40 if 'band=zs' in route.request.url else 25
        route.fulfill(json={'ok': True, 'values': {'sky_rate': sky, 'read_noise_e': 5, 'dark_rate': .02},
                            'counts': {'sky_rate': 12, 'read_noise_e': 12, 'dark_rate': 10}})
    page.route('**/api/exposure/recommend-defaults?*', defaults)
    page.goto(URL)
    expect(page.locator('#rec-sky_rate')).to_have_value('25')
    expect(page.locator('#rec-read_noise_e')).to_have_value('5')
    expect(page.locator('#rec-dark_rate')).to_have_value('0.02')
    expect(page.locator('#recommend-defaults-status')).to_contain_text('12 frames')
    page.fill('#rec-sky_rate', '17')
    page.fill('#rec-dark_rate', '0')
    page.locator('#rec-dark_rate').blur()
    page.reload()
    expect(page.locator('#recommend-defaults-status')).to_contain_text('12 frames')
    expect(page.locator('#rec-sky_rate')).to_have_value('17')
    expect(page.locator('#rec-dark_rate')).to_have_value('0')
    page.select_option('#rec-band', 'zs')
    expect(page.locator('#rec-sky_rate')).to_have_value('40')
    expect(page.locator('#rec-dark_rate')).to_have_value('0.02')
    page.fill('#rec-sky_rate', '99')
    page.click('#recommend-reset')
    expect(page.locator('#rec-sky_rate')).to_have_value('25')


def test_precision_delayed_defaults_do_not_replace_edits_or_new_band(page):
    pending = []
    page.route('**/api/exposure/recommend-defaults?*', lambda route: pending.append(route))
    page.goto(URL)
    page.wait_for_function("document.getElementById('recommend-defaults-status').textContent.includes('Loading')")
    page.fill('#rec-sky_rate', '17')
    with page.expect_response('**/api/exposure/recommend-defaults?*'):
        pending[0].fulfill(json={'ok': True, 'values': {'sky_rate': 25, 'read_noise_e': 5, 'dark_rate': .02},
                                 'counts': dict.fromkeys(('sky_rate', 'read_noise_e', 'dark_rate'), 12)})
    expect(page.locator('#rec-read_noise_e')).to_have_value('5')
    expect(page.locator('#rec-sky_rate')).to_have_value('17')
    with page.expect_request('**/api/exposure/recommend-defaults?*'):
        page.select_option('#rec-band', 'gp')
    with page.expect_request('**/api/exposure/recommend-defaults?*'):
        page.select_option('#rec-band', 'zs')
    for _ in range(100):
        if len(pending) == 3:
            break
        page.wait_for_timeout(50)
    assert len(pending) == 3
    pending[-1].fulfill(json={'ok': True, 'values': {'sky_rate': 40}, 'counts': {'sky_rate': 8}})
    expect(page.locator('#rec-sky_rate')).to_have_value('40')
    with page.expect_response('**/api/exposure/recommend-defaults?*'):
        pending[-2].fulfill(json={'ok': True, 'values': {'sky_rate': 99}, 'counts': {'sky_rate': 8}})
    expect(page.locator('#rec-sky_rate')).to_have_value('40')


def test_results_views_plot_limiting_stars_and_selected_peak_limit(page):
    page.goto(URL)
    _fill_mags(page)
    rows = [dict(band=band, source_label=label, is_target=label == 'Target',
                 mag=12, exptime=seconds, peak_adu=40000, pct_full_well=40,
                 is_saturated=False, fwhm_arcsec=1.5)
            for band, label, seconds in [('gp', 'Target', 20), ('gp', 'Comp 1', 10),
                                         ('zs', 'Target', 10), ('zs', 'Comp 1', 20)]]
    page.route('**/api/exposure/calculate', lambda route: route.fulfill(
        json={'ok': True, 'results': rows, 'instrument': 'muscat3'}))
    page.click('#calc-btn')
    expect(page.locator('#results-table')).to_be_visible()
    page.locator('[data-results-view="plot"]').click()
    plot = page.locator('#results-plot')
    expect(plot.locator('rect')).to_have_count(2)
    expect(plot.locator('circle[fill="none"]')).to_have_count(2)
    expect(plot).to_contain_text('Comp 1')
    expect(page.locator('#results-table')).to_be_hidden()
    page.select_option('#sat-frac', '0.8')
    page.click('#calc-peak-btn')
    expect(plot).to_contain_text('80% full well')
    page.locator('[data-results-view="table"]').click()
    expect(page.locator('#results-table')).to_be_visible()
