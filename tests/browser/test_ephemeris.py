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


def test_linear_fit_reports_scatter_instead_of_convergence(page):
    _load_toi1000(page)
    for epoch, offset in [(0, .001), (1, -.002), (2, .001)]:
        with page.expect_response('**/api/ephemeris/calculate'):
            _add_transit_center(page, T0 + epoch * PERIOD + offset)
    expect(page.locator('#results-grid')).to_contain_text('reduced χ²')
    expect(page.locator('#results-grid')).to_contain_text('3 points')
    expect(page.locator('#results-grid')).to_contain_text('Heuristic caution')
    expect(page.locator('#results-grid')).not_to_contain_text('underestimate')
    expect(page.locator('#results-grid')).not_to_contain_text('converged successfully')


def test_ttv_initial_pairs_follow_periods(page):
    from tests.browser.helpers import reveal
    _load_toi1000(page)
    for _ in range(3):
        page.click('#add-planet-btn')
    for planet, period in [('b', '24.14'), ('c', '8.25'), ('d', '12.4'), ('e', '45')]:
        page.fill('#planet-t0-' + planet, str(T0))
        page.fill('#planet-period-' + planet, period)
        page.locator('#planet-period-' + planet).blur()
    # V1298 Tau periods: c (8.25 d) < d (12.4) < b (24.14) < e (45), so pairs run c-d, d-b, b-e.
    reveal(page, '#opt-planet_letters')
    expect(page.locator('#opt-planet_letters')).to_have_value('cdbe')
    ini = page.input_value('#opt-ini_content')
    assert 'a_cd =' in ini and 'a_db =' in ini and 'a_be =' in ini
    assert 'a_bc =' not in ini


def test_harmonic_warning_follows_selected_run(page, live_server, request):
    import json
    from muscat_db import ttv_fit

    for name, converged in (("short", False), ("long", True)):
        run = ttv_fit.ttv_output_dir("TOI1000", name)
        assert run.is_relative_to(live_server.root)
        run.mkdir(parents=True)
        request.addfinalizer(run.rmdir)
        request.addfinalizer((run / "fit_stats.json").unlink)
        (run / "fit_stats.json").write_text(json.dumps({"converged": converged}))
    _load_toi1000(page)
    panel = page.locator("#ttv-outputs-container")
    panel.get_by_role("button", name="short — Sampler reported non-convergence").click()
    expect(panel).to_contain_text("Done with warnings")
    expect(panel).to_contain_text("Acceptance fraction: Unknown")
    panel.get_by_role("button", name="long", exact=True).click()
    expect(panel).to_contain_text("Converged: Yes")
    expect(panel).not_to_contain_text("Done with warnings")


def test_flagged_midtime_requires_opt_in_for_ephemeris_and_ttv(page, live_server, request):
    run = live_server.root / "timer/muscat3/261001/TOI-1000/fit1"
    for path, content in [(run / "timer-fit.log", "There was 1 divergence after tuning."),
                          (run / "out/tc.txt", f"b {T0} 0.0004")]:
        assert not path.exists()
        path.write_text(content)
        request.addfinalizer(path.unlink)

    # Old saved views wrote every box, including the old implicit default.
    page.add_init_script("""if (!sessionStorage.getItem('old-view-seeded')) {
        localStorage.setItem('muscat-ephem-opts-TOI1000-checked',
          JSON.stringify({'TOI-1000|muscat3|261001|fit1': true}));
        sessionStorage.setItem('old-view-seeded', '1');
    }""")
    _load_toi1000(page)
    row = page.locator("#datasets-body tr").first
    expect(row).to_contain_text("1 divergence after tuning")
    check = row.get_by_role("checkbox")
    expect(check).not_to_be_checked()

    # Keep independent points in the fit so exclusion can also be checked in
    # the actual harmonic launch payload, without launching an engine.
    page.route("**/api/ttv-fit/start", lambda route: route.fulfill(json={"ok": False, "error": "test launch"}))
    for epoch in EPOCHS:
        with page.expect_response("**/api/ephemeris/calculate") as response:
            _add_transit_center(page, T0 + epoch * PERIOD)
    for included in (False, True):
        with page.expect_request("**/api/ttv-fit/start") as launch:
            if included:
                with page.expect_response("**/api/ephemeris/calculate") as response:
                    check.check()
                    page.locator("#run-ttv-btn").click()
            else:
                page.locator("#run-ttv-btn").click()
        points = response.value.json()["results"]["b"]["points"]
        assert [p["checked"] for p in points if p.get("run_id") == "fit1"] == [included]
        options = launch.value.post_data_json["options"]
        assert any(p.get("run_id") == "fit1" for p in options["selection"]) == included
        assert any(float(line.split(",")[2]) == T0 for line in options["csv_content"].splitlines()[1:]) == included
    page.reload()
    expect(page.locator("#datasets-body .dataset-checkbox")).to_be_checked()


def test_legacy_fit_with_snr_at_cutoff_and_unknown_sampler_diagnostics(page, live_server, request):
    summary = live_server.root / "timer/muscat3/261001/TOI-1000/fit1/out/summary.csv"
    original = summary.read_bytes()
    request.addfinalizer(lambda: summary.write_bytes(original))
    summary.write_text(",mean,sd\nror[0],0.75,0.125\n")
    _load_toi1000(page)
    row = page.locator("#datasets-body tr").first
    expect(row).to_contain_text("Unknown: Max R-hat, Divergences")
    expect(row.get_by_role("checkbox")).to_be_checked()
