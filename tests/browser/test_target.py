"""The /target page: datasets table, per-night notes, and the archive sections."""

from __future__ import annotations

import pytest

URL = "/target?name=TOI-1000"
ROWS = "#datasets-table tbody tr"

# Sections that query an outside service (or an absent local catalog) when
# opened, and what each says offline: a readable reason, never a hang or a JS
# error. ESO and LAMOST answer 502 when their archive is unreachable.
ARCHIVE_SECTIONS = {
    "ads-details": ("save your token in Settings", False),
    "harps-rv-details": ("HARPS RVBank lookup failed", False),
    "eso-archive-details": ("ESO archive lookup failed", True),
    "lamost-archive-details": ("LAMOST archive lookup failed", True),
    "lamost-rv-details": ("LAMA_stars catalog not found", False),
}
STILL_LOADING = ("Querying", "Loading", "Searching")


def _visible_dates(page) -> list[str]:
    return page.eval_on_selector_all(
        ROWS, "rows => rows.filter(r => r.style.display !== 'none').map(r => r.dataset.date)"
    )


def test_datasets_show_pipeline_status_and_filter(page):
    page.goto(URL)
    assert sorted(_visible_dates(page)) == ["261001", "261003"]
    muscat3 = page.locator(f'{ROWS}[data-date="261001"]')
    assert muscat3.get_attribute("data-phot") == "Y" and muscat3.get_attribute("data-fit") == "Y"
    assert page.locator(f'{ROWS}[data-date="261003"]').get_attribute("data-phot") == "N"

    page.fill('#datasets-table tr.filter-row input[data-col="phot"]', "Y")
    assert _visible_dates(page) == ["261001"]
    page.fill('#datasets-table tr.filter-row input[data-col="phot"]', "")
    page.fill("#dataset-search", "sinistro")
    assert _visible_dates(page) == ["261003"]


def test_dataset_links_open_photometry_and_fit(page):
    page.goto(URL)
    row = page.locator(f'{ROWS}[data-date="261001"]')
    row.locator('a[title="transit fit"]').click()
    page.wait_for_url("**/transit-fit?**")
    assert "inst=muscat3" in page.url and "date=261001" in page.url


def test_note_add_edit_delete(page, live_server):
    page.goto(URL)
    cell = page.locator(f'{ROWS}[data-date="261003"] .note-cell')
    cell.click()
    cell.locator("textarea").fill("clouds <b>after</b> egress")
    with page.expect_response(lambda r: r.url.endswith("/note") and r.request.method == "PUT"):
        cell.locator("button.save").click()
    assert cell.locator(".note-text").inner_text() == "clouds <b>after</b> egress"
    assert cell.locator(".note-text b").count() == 0  # stored and shown as text
    assert live_server.sql(
        "SELECT note FROM target_notes WHERE obsdate = '261003' AND instrument = 'sinistro'"
    ) == [("clouds <b>after</b> egress",)]

    page.reload()
    cell = page.locator(f'{ROWS}[data-date="261003"] .note-cell')
    assert cell.locator(".note-text").inner_text() == "clouds <b>after</b> egress"

    cell.click()
    with page.expect_response(lambda r: r.url.endswith("/note") and r.request.method == "DELETE"):
        cell.locator("button.del").click()
    assert cell.locator(".note-placeholder").is_visible()
    assert live_server.sql("SELECT note FROM target_notes WHERE obsdate = '261003'") == []


def _open_section(page, section: str) -> str:
    page.goto(URL)
    page.locator(f"#{section} > summary").click()
    page.wait_for_load_state("networkidle")
    page.wait_for_function(
        """([id, words]) => {
             const t = document.getElementById(id).innerText;
             return !words.some(w => t.includes(w + '…') || t.includes(w + '...'));
           }""",
        arg=[section, list(STILL_LOADING)],
    )
    return page.locator(f"#{section}").inner_text()


@pytest.mark.parametrize("section", list(ARCHIVE_SECTIONS))
def test_archive_section_settles_offline_with_a_reason(page, section):
    message, answers_502 = ARCHIVE_SECTIONS[section]
    if answers_502:
        page.problems.allowed += ["HTTP 502", "status of 502"]
    assert message in _open_section(page, section)


@pytest.mark.xfail(
    strict=True,
    reason="exofop.fetch_time_series swallows network errors and returns [], so an "
    "unreachable ExoFOP reads as 'no entries reported'",
)
def test_exofop_section_reports_an_unreachable_exofop(page):
    text = _open_section(page, "exofop-obslog-details")
    assert "No ExoFOP time-series entries reported" not in text
