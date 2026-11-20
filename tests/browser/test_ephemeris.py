"""The /ephemeris O-C page: load a target, add transit centers, fit, restore."""

from __future__ import annotations

from playwright.sync_api import expect
import re
import statistics

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
    assert fit["fit_method"] == "weighted"
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
    page.get_by_role("radio", name="Unweighted", exact=True).check()
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


def _ini(page) -> dict:
    """The [INIT] block of the harmonic options as {key: float}."""
    out = {}
    for line in page.input_value('#opt-ini_content').splitlines():
        if '=' in line and not line.strip().startswith('['):
            key, value = (part.strip() for part in line.split('=', 1))
            try:
                out[key] = float(value)
            except ValueError:
                pass
    return out


def _set_planets(page, planets):
    from tests.browser.helpers import reveal
    _load_toi1000(page)
    for _ in range(len(planets) - 1):
        page.click('#add-planet-btn')
    for planet, period in planets:
        page.fill('#planet-t0-' + planet, str(T0))
        page.fill('#planet-period-' + planet, period)
        page.locator('#planet-period-' + planet).blur()
    reveal(page, '#opt-ini_content')


# V1298 Tau periods from the NASA Exoplanet Archive: c-d sits near 3:2 and
# d-b and b-e near 2:1. The super-period is 1 / |j/P_outer - (j-1)/P_inner|.
V1298 = [('b', '24.140006'), ('c', '8.249164'), ('d', '12.401394'), ('e', '48.677714')]


def test_ttv_super_period_starts_from_the_nearest_first_order_resonance(page):
    _set_planets(page, V1298)
    expect(page.locator('#opt-planet_letters')).to_have_value('cdbe')
    ini = _ini(page)
    assert ini['per_cd'] == pytest.approx(1 / abs(3 / 12.401394 - 2 / 8.249164), abs=0.06)
    assert ini['per_db'] == pytest.approx(1 / abs(2 / 24.140006 - 1 / 12.401394), abs=0.06)
    assert ini['per_be'] == pytest.approx(1 / abs(2 / 48.677714 - 1 / 24.140006), abs=0.06)
    # b-e is the longest of the three, c-d next, d-b far shorter.
    assert ini['per_be'] > 2900 > ini['per_cd'] > 1800 > 500 > ini['per_db'] > 400


def test_ttv_super_period_far_from_resonance_keeps_the_fixed_default(page):
    # 5 d and 16 d: the nearest first-order term gives 13.3 d, below harmonic's
    # lower bound of five outer periods, so the page does not pretend to know.
    _set_planets(page, [('b', '5'), ('c', '16')])
    assert _ini(page)['per_bc'] == 1000.0


def test_ttv_super_period_follows_a_period_edit_until_the_block_is_edited(page):
    _set_planets(page, [('b', '10'), ('c', '15.2')])
    first = _ini(page)['per_bc']
    assert first == pytest.approx(1 / abs(3 / 15.2 - 2 / 10), abs=0.06)
    page.fill('#planet-period-c', '15.3')
    page.locator('#planet-period-c').blur()
    expect(page.locator('#opt-ini_content')).to_have_value(re.compile(r'per_bc = 255\.0'))
    # Once the observer edits the block, a later period change leaves it alone.
    edited = page.input_value('#opt-ini_content').replace('per_bc = 255.0', 'per_bc = 300.0')
    page.fill('#opt-ini_content', edited)
    page.fill('#planet-period-c', '15.4')
    page.locator('#planet-period-c').blur()
    page.wait_for_timeout(600)
    assert _ini(page)['per_bc'] == 300.0


def test_ttv_amplitude_starts_from_the_largest_o_minus_c(page):
    """Planet b has timings, so its amplitude follows the linear fit; planet c
    has none and keeps the fixed starting value."""
    from tests.browser.helpers import reveal
    _load_toi1000(page)
    # Two manual timings 3 and 5 minutes late give planet b a non-zero O-C.
    _add_transit_center(page, T0 + 2 * PERIOD + 3 / 1440)
    with page.expect_response("**/api/ephemeris/calculate") as resp:
        _add_transit_center(page, T0 + 5 * PERIOD + 5 / 1440)
    page.click('#add-planet-btn')
    page.fill('#planet-t0-c', str(T0))
    with page.expect_response("**/api/ephemeris/calculate") as resp:
        page.fill('#planet-period-c', '3.3')
        page.locator('#planet-period-c').blur()
    points = [p for p in resp.value.json()['results']['b']['points'] if p.get('checked') is not False]
    expected = max(max(abs(p['oc_min']) for p in points),
                   statistics.median(abs(p['oc_err_min']) for p in points)) / 1440
    assert expected > 0
    reveal(page, '#opt-ini_content')
    page.wait_for_function("() => !/a_bc = 0\\.02\\b/.test(document.getElementById('opt-ini_content').value)")
    assert _ini(page)['a_bc'] == pytest.approx(expected, rel=5e-3)
    assert _ini(page)['a_cb'] == 0.02


def _seed_amplitudes(page, results) -> dict:
    """Generate the [INIT] block from a given linear-fit result and parse it."""
    _set_planets(page, [('b', '10'), ('c', '15.2')])
    page.evaluate("results => { fitResults = results; rebuildINI(false); }", results)
    return _ini(page)


def _points(*pairs):
    return {'points': [{'oc_min': oc, 'oc_err_min': err, 'checked': True} for oc, err in pairs]}


def test_ttv_amplitude_keeps_a_small_positive_value(page):
    """harmonic rejects a zero amplitude, so a tiny seed must not round to zero."""
    ini = _seed_amplitudes(page, {'b': _points((0.0, 0.0043), (0.0, 0.0043))})   # 0.26 s
    assert ini['a_bc'] == pytest.approx(0.0043 / 1440, rel=5e-3)
    assert ini['a_bc'] > 0


def test_ttv_amplitude_floor_is_the_median_uncertainty(page):
    # Two timings with uncertainties of 1 and 9 minutes: the median is 5 minutes.
    ini = _seed_amplitudes(page, {'b': _points((0.0, 1.0), (0.0, 9.0))})
    assert ini['a_bc'] == pytest.approx(5 / 1440, rel=5e-3)
    # harmonic takes the median over every timing in the fit, not per planet:
    # 1, 9 and 21 minutes give 9. Planet c's own largest |O-C| is larger still.
    ini = _seed_amplitudes(page, {'b': _points((0.0, 1.0), (0.0, 9.0)), 'c': _points((30.0, 21.0))})
    assert ini['a_bc'] == pytest.approx(9 / 1440, rel=5e-3)
    assert ini['a_cb'] == pytest.approx(30 / 1440, rel=5e-3)


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


def test_legacy_fit_with_low_snr_and_unknown_sampler_diagnostics(page, live_server, request):
    summary = live_server.root / "timer/muscat3/261001/TOI-1000/fit1/out/summary.csv"
    original = summary.read_bytes()
    request.addfinalizer(lambda: summary.write_bytes(original))
    summary.write_text(",mean,sd\nror[0],0.25,0.125\n")
    _load_toi1000(page)
    row = page.locator("#datasets-body tr").first
    expect(row).to_contain_text("Detection SNR: 1.0")
    expect(row).to_contain_text("Unknown: Max R-hat, Divergences, Min bulk ESS, Min tail ESS")
    expect(row.get_by_role("checkbox")).to_be_checked()


@pytest.mark.parametrize("cache_case", ["matching", "removed_dataset", "older_version"])
def test_cached_fit_only_restores_for_current_inputs(page, cache_case):
    _load_toi1000(page)
    for epoch in EPOCHS:
        with page.expect_response("**/api/ephemeris/calculate"):
            _add_transit_center(page, T0 + epoch * PERIOD)
    grid = page.locator("#results-grid")
    expect(grid).to_contain_text("Exact fit (2 points")

    if cache_case == "removed_dataset":
        def remove_dataset(route):
            info = route.fetch().json()
            assert info["datasets"]
            route.fulfill(json={**info, "datasets": []})

        page.route("**/api/ephemeris/target-info?**", remove_dataset)
    elif cache_case == "older_version":
        page.evaluate("""() => {
            const key = 'muscat-ephem-opts-TOI1000-results';
            const cached = JSON.parse(localStorage.getItem(key));
            cached.version -= 1;
            localStorage.setItem(key, JSON.stringify(cached));
        }""")

    # With the new calculation held, any result on screen came from the cache.
    pending = []
    page.route("**/api/ephemeris/calculate", lambda route: pending.append(route))
    with page.expect_request("**/api/ephemeris/calculate"):
        page.reload()
    if cache_case == "matching":
        expect(grid).to_contain_text("Exact fit (2 points")
    else:
        expect(grid).not_to_contain_text("Exact fit (2 points")
        assert page.evaluate("localStorage.getItem('muscat-ephem-opts-TOI1000-results')") is None
    for route in pending:
        route.continue_()


def test_pending_auto_fit_does_not_interrupt_adding_a_planet(page):
    # Editing a catalog planet schedules a fit 350 ms later, while a newly
    # added manual planet is incomplete. The automatic fit must not interrupt.
    page.clock.install()
    _load_toi1000(page)
    page.click('#add-planet-btn')
    page.fill('#planet-period-b', '24.14')
    page.locator('#planet-period-b').blur()
    page.clock.run_for(500)
    expect(page.locator('#message-modal')).not_to_be_visible()
    page.fill('#planet-t0-c', str(T0))
    page.fill('#planet-period-c', '1.5')
    page.locator('#planet-period-c').blur()
    with page.expect_response('**/api/ephemeris/calculate'):
        page.clock.run_for(500)
    expect(page.locator('#message-modal')).not_to_be_visible()
