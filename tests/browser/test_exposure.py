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
