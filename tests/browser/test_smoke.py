"""Every page loads through the real app with no JS errors and its content rendered.

The ``page`` fixture fails a test on any uncaught exception, console error or
failed same-origin request, so each case here also proves the page's start-up
JavaScript (including the fetches it makes on load) runs cleanly.
"""

from __future__ import annotations

import pytest

# (path, <title> fragment, text the rendered page must show)
PAGES = [
    ("/", "Home", "Global Telescope Network Status"),
    ("/targets", "Targets", "TOI-1000"),
    ("/projects", "Projects", "demo"),
    ("/tag?name=demo", "demo", "TOI1000"),
    ("/target?name=TOI-1000", "TOI1000", "Target: TOI1000"),
    ("/obs", "Obs", "Data Summary"),
    ("/guide", "Pipeline Guide", "Pipeline Workflow"),
    ("/toi", "TOI Catalog", "TESS Objects of Interest"),
    ("/nexsci", "NExScI", "NASA Exoplanet Archive"),
    ("/photometry", "Photometry", "Photometry"),
    ("/photometry?inst=muscat3&date=261001&target=TOI-1000", "Photometry", "Pipeline options"),
    ("/transit-fit", "Transit Fit", "Transit Fitting Pipeline"),
    ("/transit-fit?inst=muscat3&date=261001&target=TOI-1000", "Transit Fit", "Transit Fit"),
    ("/exposure", "Exp Calc", "Calibration status"),
    ("/exposure?inst=muscat3&target=TOI-1000", "Exp Calc", "Calibration status"),
    ("/fov", "FOV", "Field-of-View Optimizer"),
    ("/fov?inst=muscat3&target=TOI-1000", "FOV", "Comparison Stars"),
    ("/ephemeris", "Ephemeris", "Select target star"),
    ("/settings", "Settings", "LCO API Token"),
    ("/lco/schedule", "LCO Schedule", "Imaging request"),
    ("/lco/archive", "LCO Archive", "Search LCO archive"),
    ("/jobs", "Jobs", "LCO Archive Downloads"),
    ("/muscat3", "muscat3", "261001"),
    ("/muscat3/261001", "261001", "CCD 1"),
    ("/muscat3/261001/ccd0", "261001", "ogg2m001-ep05-20261001-0001"),
    ("/muscat2/261002/ccd1", "261002", "MCT21_2610020001"),
    ("/chat/popout", "Chat", ""),
]


@pytest.mark.parametrize(("path", "title", "text"), PAGES, ids=[p[0] for p in PAGES])
def test_page_loads_cleanly(page, path, title, text):
    if path == "/lco/schedule":
        # No LCO token is configured here; the page asks for proposals on load
        # and handles the refusal quietly (test_lco_schedule covers a token).
        page.problems.allowed += ["GET /api/lco/proposals", "status of 403"]
    resp = page.goto(path)
    assert resp is not None and resp.status == 200
    page.wait_for_load_state("networkidle")
    assert title.lower() in page.title().lower()
    if text:
        assert text.lower() in page.locator("body").inner_text().lower()

