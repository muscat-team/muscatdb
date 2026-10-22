"""The raw obslog browser: /obs -> /{instrument} -> /{instrument}/{date} -> CCD."""

from __future__ import annotations

from playwright.sync_api import expect
import pytest


def test_drill_down_from_obs_to_frames_and_back(page):
    page.goto("/obs")
    page.click('.card a[href="/muscat3"]')
    assert page.url.endswith("/muscat3")

    page.click('a[href="/muscat3/261001"]')
    page.wait_for_url("**/muscat3/261001")
    assert page.locator("h2").all_inner_texts() == ["CCD 0", "CCD 1"]

    page.locator('a[href="/muscat3/261001/ccd0"]').first.click()
    page.wait_for_url("**/ccd0")
    frames = page.locator("tbody tr td:first-child").all_inner_texts()
    assert frames == ["ogg2m001-ep05-20261001-0001.fits", "ogg2m001-ep05-20261001-0002.fits"]

    page.click('h1 a.crumb[href="/muscat3/261001"]')
    assert page.url.endswith("/muscat3/261001")
    page.click('h1 a.crumb[href="/obs"]')
    assert page.url.endswith("/obs")


def test_date_page_links_each_target_to_photometry(page):
    page.goto("/muscat3/261001")
    link = page.locator('a[href^="/photometry?"]').first
    href = link.get_attribute("href")
    assert "inst=muscat3" in href and "date=261001" in href and "target=TOI-1000" in href
    link.click()
    page.wait_for_url("**/photometry?**")
    assert "TOI-1000" in page.locator("body").inner_text()


def _summary(page) -> dict[str, list[str]]:
    rows = page.locator("table tbody tr").all()
    return {r.locator("td").first.inner_text(): r.locator("td").all_inner_texts()[1:] for r in rows}


def _filter(page, min_frames: int) -> dict[str, list[str]]:
    page.fill("#min_frames", str(min_frames))
    page.click('form[action="/obs"] button[type="submit"]')
    page.wait_for_url(f"**/obs?min_frames={min_frames}")
    assert page.input_value("#min_frames") == str(min_frames)
    return _summary(page)


def test_data_summary_min_frames_filter(page):
    page.goto("/obs")
    summary = _summary(page)
    # (dates, science targets, frames). The default threshold (1000 frames)
    # counts none of these small fixture nights as a science target.
    assert summary["muscat3"] == ["1", "0", "3"]
    assert summary["muscat2"] == ["1", "0", "2"]

    summary = _filter(page, 1)
    assert [summary[i][1] for i in ("muscat3", "muscat2", "sinistro")] == ["1", "1", "1"]
    summary = _filter(page, 3)
    assert [summary[i][1] for i in ("muscat3", "muscat2", "sinistro")] == ["1", "0", "0"]


@pytest.mark.parametrize(("path", "message"), [
    ('/qhy600', 'No dates found for this instrument.'),
    ('/muscat3/261001/ccd3', 'No frames for this CCD.'),
])
def test_empty_obslog_pages_explain_the_absence_of_data(page, path, message):
    page.goto(path)
    expect(page.locator('.empty-cell')).to_have_text(message)
    page.locator('h1 a.crumb[href="/obs"]').click()
    page.wait_for_url('**/obs')


def test_ccd_table_shows_header_values_and_distinct_frames(page):
    page.goto('/muscat3/261001/ccd0')
    rows = page.locator('tbody tr')
    assert rows.count() == 2
    first = rows.first.locator('td').all_inner_texts()
    assert first[:2] == ['ogg2m001-ep05-20261001-0001.fits', 'TOI-1000']
    assert float(first[2]) == pytest.approx(2461314.80)
    assert float(first[4]) == 30
    assert first[5:9] == ['fast', 'gp', '07:51:43.0', '+12:32:11.0']
    assert rows.nth(1).locator('td').first.inner_text() != first[0]
