"""The /fov optimizer: resolve a target, past pointings, optimize against Gaia."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import numpy as np
from playwright.sync_api import expect
import pytest

# TOI-1000 in the fixture TOI catalog (seed.TOI_ROWS).
RA, DEC = 117.929, 12.536
# The target (first) plus four comparison candidates a few arcmin away.
EAST_ARCSEC = np.array([0.0, 60.0, -90.0, 120.0, 30.0])
NORTH_ARCSEC = np.array([0.0, -45.0, 75.0, 30.0, -100.0])
GMAG = np.array([9.6, 10.1, 10.8, 11.5, 13.0])


@pytest.fixture
def gaia(monkeypatch):
    """A fixed Gaia field, recording each cone search instead of querying ESA/VizieR."""
    from muscat_db import fov

    queries = []

    def query_gaia_field(ra, dec, radius_arcsec, min_mag=0.0, max_mag=18.0, mag_limit=None):
        queries.append((ra, dec, radius_arcsec))
        return fov.StarField(
            ra=RA + EAST_ARCSEC / 3600 / np.cos(np.radians(DEC)),
            dec=DEC + NORTH_ARCSEC / 3600,
            gmag=GMAG,
            bp_rp=np.full(len(GMAG), 0.8),
            source="test",
        )

    monkeypatch.setattr(fov, "query_gaia_field", query_gaia_field)
    # A fresh memo, so neither another test's field nor this one leaks across tests.
    monkeypatch.setattr(fov, "_gaia_cache", type(fov._gaia_cache)(maxsize=8))
    return queries


def test_resolving_a_target_fills_coordinates_and_lists_past_pointings(page):
    page.goto("/fov")
    page.fill("#fov-target", "TOI-1000")
    page.click("#fov-resolve")
    expect(page.locator("#fov-msg")).to_have_text(f"Resolved to RA {RA}°, Dec {DEC}°")
    assert float(page.input_value("#fov-ra")) == RA
    assert float(page.input_value("#fov-dec")) == DEC
    expect(page.locator("#fov-observed-date option")).to_have_text(
        [
            "— none —",
            "sinistro · 261003 (TOI-1000, 1 frames)",
            "muscat3 · 261001 (TOI-1000, 3 frames)",
        ]
    )


def test_optimize_shows_the_pointing_comparisons_and_overlays(page, gaia):
    page.goto("/fov")
    page.fill("#fov-target", "TOI-1000")
    page.select_option("#fov-inst", "muscat3")
    with page.expect_response("**/api/fov/optimize") as resp:
        page.click("#fov-run")
    result = resp.value.json()
    assert result["ok"] and gaia

    n = result["n_comps"]
    assert n > 0
    expect(page.locator("#fov-msg")).to_have_text(f"Found {n} useful comparison stars.")
    expect(page.locator("#fov-results")).to_be_visible()
    expect(page.locator("#r-n")).to_have_text(str(n))
    expect(page.locator("#r-tg")).to_have_text("9.60")
    expect(page.locator("#fov-comps-body tr")).to_have_count(n)

    plotted = page.evaluate("A.__calls.filter(c => c.fn === 'catalog.addSources').map(c => c.n)")
    assert plotted[:2] == [1, n]  # the target, then the comparisons

    query = parse_qs(urlparse(page.url).query)
    assert query["inst"] == ["muscat3"] and query["target"] == ["TOI-1000"]


def test_a_shared_link_restores_the_form(page):
    page.goto(f"/fov?inst=muscat3&target=TOI-1000&ra={RA}&dec={DEC}&magmode=relative")
    assert page.input_value("#fov-inst") == "muscat3"
    assert page.input_value("#fov-target") == "TOI-1000"
    assert float(page.input_value("#fov-ra")) == RA
    assert page.input_value("#fov-mag-mode") == "relative"


def test_optimize_needs_a_target_or_coordinates(page):
    page.goto("/fov")
    page.fill("#fov-target", "")
    page.fill("#fov-ra", "")
    page.fill("#fov-dec", "")
    page.click("#fov-run")
    expect(page.locator("#fov-msg")).to_have_text("Enter a target name or RA and Dec.")


@pytest.mark.parametrize("failure", ["blocked", "pending-script", "pending-init", "rejected-init"])
def test_cds_outage_keeps_resolve_and_run_working(page, gaia, failure):
    def unavailable(route):
        if failure == "blocked":
            route.abort("blockedbyclient")
        elif failure == "pending-script":
            return  # Leave the download pending beyond the viewer deadline.
        else:
            init = "new Promise(() => {})" if failure == "pending-init" else "Promise.reject(new Error('CDS unavailable'))"
            route.fulfill(body=f"window.A = {{init: {init}}};", content_type="application/javascript")

    page.route("https://aladin.cds.unistra.fr/**", unavailable)
    page.goto("/fov", wait_until="domcontentloaded")
    page.fill("#fov-target", "TOI-1000")
    page.click("#fov-resolve")
    expect(page.locator("#fov-msg")).to_have_text(f"Resolved to RA {RA}°, Dec {DEC}°")
    page.select_option("#fov-inst", "muscat3")
    with page.expect_response("**/api/fov/optimize") as response:
        page.click("#fov-run")
    assert response.value.json()["ok"]
    expect(page.locator("#fov-results")).to_be_visible()
    expect(page.locator("#fov-sky-status")).to_have_text(
        "Sky view unavailable (CDS unreachable)", timeout=12_000,
    )


def test_late_viewer_initialization_plots_existing_results(page, gaia):
    from pathlib import Path

    stub = (Path(__file__).parent / "stubs" / "aladin.js").read_text()
    stub = stub.replace("init: Promise.resolve()", "init: new Promise(resolve => { window.finishSkyInit = resolve; })")
    page.route("**/aladin.js", lambda route: route.fulfill(body=stub, content_type="application/javascript"))
    page.goto("/fov")
    page.fill("#fov-target", "TOI-1000")
    page.select_option("#fov-inst", "muscat3")
    with page.expect_response("**/api/fov/optimize") as response:
        page.click("#fov-run")
    n = response.value.json()["n_comps"]
    expect(page.locator("#fov-results")).to_be_visible()
    assert page.evaluate("A.__calls.length") == 0
    page.evaluate("finishSkyInit()")
    expect(page.locator("#fov-sky-status")).to_have_text("")
    plotted = page.evaluate("A.__calls.filter(c => c.fn === 'catalog.addSources').map(c => c.n)")
    assert plotted[:2] == [1, n]
