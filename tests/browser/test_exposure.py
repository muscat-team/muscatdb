"""The /exposure calculator: per-band results, peak counts, lookup, saved form."""

from __future__ import annotations

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
    assert "Recommended exposure" in page.locator(".exp-results").inner_text().title()


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
