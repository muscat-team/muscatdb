"""The /ephemeris O-C page: load a target, add transit centers, fit, restore."""

from __future__ import annotations

from playwright.sync_api import expect
import pytest

# TOI-1000.01 in the fixture TOI catalog (seed.TOI_ROWS).
T0, PERIOD = 2461314.86, 2.17
EPOCHS = (3, 8)


def _load_toi1000(page) -> None:
    page.goto("/ephemeris")
    expect(page.locator("#target-input option", has_text="TOI1000")).to_be_attached()
    page.select_option("#target-input", "TOI1000")
    page.click("#load-target-btn")
    expect(page.locator("#datasets-section")).to_be_visible()


def _add_transit_center(page, tc: float) -> None:
    page.get_by_role("button", name="+ Add transit center").click()
    row = page.locator("#datasets-body tr.manual-row").last
    row.locator(".manual-tc").fill(f"{tc:.5f}")
    row.locator(".manual-unc").fill("0.0008")
    row.locator(".manual-tc").blur()


def test_loading_a_target_shows_its_planet_and_completed_fits(page):
    _load_toi1000(page)
    expect(page.locator("#loaded-targets-container")).to_contain_text("TOI1000")
    card = page.locator("#planet-cards-container .planet-card")
    expect(card).to_have_count(1)
    assert card.get_attribute("data-planet") == "b"
    assert float(card.locator(".planet-t0").input_value()) == T0
    assert float(card.locator(".planet-period").input_value()) == PERIOD

    (row,) = page.locator("#datasets-body tr").all()
    expect(row).to_contain_text("MUSCAT3")
    expect(row).to_contain_text("261001")
    link = row.get_by_role("link", name="fit1")
    assert (
        link.get_attribute("href")
        == "/transit-fit?inst=muscat3&date=261001&target=TOI-1000&run=fit1"
    )
    expect(page.locator("#action-section")).to_be_visible()


def test_transit_centers_on_the_ephemeris_fit_with_zero_o_minus_c(page):
    _load_toi1000(page)
    for epoch in EPOCHS:
        with page.expect_response("**/api/ephemeris/calculate") as resp:
            _add_transit_center(page, T0 + epoch * PERIOD)
    result = resp.value.json()
    assert result["ok"]
    fit = result["results"]["b"]
    assert fit["n_fit"] == 2
    assert fit["period_fit"] == pytest.approx(PERIOD, abs=1e-6)
    assert fit["t0_fit"] == pytest.approx(T0, abs=1e-6)
    assert [p["epoch"] for p in fit["points"]] == list(EPOCHS)
    assert all(abs(p["oc_min"]) < 0.01 for p in fit["points"])

    expect(page.locator("#datasets-body .manual-epoch")).to_have_text([str(e) for e in EPOCHS])
    expect(page.locator("#plot-card")).to_be_visible()
    drawn = page.evaluate("window.Plotly.__calls.filter(c => c.id === 'oc-plot').length")
    assert drawn >= 1


def test_loaded_target_and_transit_centers_survive_a_reload(page):
    _load_toi1000(page)
    with page.expect_response("**/api/ephemeris/calculate"):
        _add_transit_center(page, T0 + EPOCHS[0] * PERIOD)
    page.reload()
    expect(page.locator("#loaded-targets-container")).to_contain_text("TOI1000")
    tc = page.locator("#datasets-body tr.manual-row .manual-tc")
    expect(tc).to_have_count(1)
    assert float(tc.input_value()) == pytest.approx(T0 + EPOCHS[0] * PERIOD, abs=1e-5)
