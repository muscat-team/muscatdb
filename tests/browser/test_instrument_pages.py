"""The ObsLog drill-down: instrument -> night -> CCD, and the breadcrumbs back."""

from __future__ import annotations

from playwright.sync_api import expect


def _rows(page, table) -> list[list[str]]:
    return [
        [c.strip() for c in row.locator("td").all_inner_texts()]
        for row in table.locator("tbody tr").all()
    ]


def test_instrument_page_lists_each_night_with_its_counts(page):
    page.goto("/muscat3")
    assert _rows(page, page.locator("table")) == [["261001", "2", "3"]]  # date, CCDs, frames
    page.get_by_role("link", name="261001").click()
    expect(page).to_have_url("/muscat3/261001")


def test_night_page_has_one_table_per_ccd_linking_to_its_frames(page):
    page.goto("/muscat3/261001")
    assert page.locator("h2").all_inner_texts() == ["CCD 0", "CCD 1"]
    ccd0, ccd1 = page.locator("table").all()
    (row0,) = _rows(page, ccd0)
    # object, exptime, nframes
    assert row0[0] == "TOI-1000" and row0[1] == "30.0" and row0[-2] == "2"
    (row1,) = _rows(page, ccd1)
    assert row1[0] == "TOI-1000" and row1[-2] == "1"

    phot = ccd0.get_by_title("View photometry")
    assert phot.get_attribute("href") == "/photometry?inst=muscat3&date=261001&target=TOI-1000"

    ccd1.get_by_role("link", name="TOI-1000").click()
    expect(page).to_have_url("/muscat3/261001/ccd1")


def test_ccd_page_lists_the_frames_and_breadcrumbs_lead_back(page):
    page.goto("/muscat3/261001/ccd0")
    frames = _rows(page, page.locator("table"))
    assert [f[0] for f in frames] == [
        "ogg2m001-ep05-20261001-0001.fits",
        "ogg2m001-ep05-20261001-0002.fits",
    ]
    assert {f[6] for f in frames} == {"gp"}  # filter

    page.locator("h1").get_by_role("link", name="261001").click()
    expect(page).to_have_url("/muscat3/261001")
    page.locator("h1").get_by_role("link", name="muscat3").click()
    expect(page).to_have_url("/muscat3")
    page.locator("h1").get_by_role("link", name="Instruments").click()
    expect(page).to_have_url("/obs")


def test_ccd_page_for_a_ccd_with_no_frames_says_so(page):
    page.goto("/muscat2/261002/ccd3")
    expect(page.locator("td.empty-cell")).to_have_text("No frames for this CCD.")
