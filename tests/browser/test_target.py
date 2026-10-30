"""The /target page: datasets table, per-night notes, and the archive sections."""

from __future__ import annotations

from playwright.sync_api import expect
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


def test_exofop_section_reports_an_unreachable_exofop(page):
    # The server answers 503 while ExoFOP is unreachable; that is the point.
    page.problems.allowed += ["HTTP 503", "status of 503"]
    text = _open_section(page, "exofop-obslog-details")
    assert "No ExoFOP time-series entries reported" not in text
    assert "ExoFOP is unavailable" in text
    expect(page.locator("#exofop-obslog-retry")).to_be_visible()
    expect(page.locator("#exofop-tic-form")).to_be_hidden()


def test_exofop_obslog_names_the_planet_of_each_row(page):
    # Field names as ExoFOP's target.php?json returns them. A non-TOI row has
    # only tsplname, so the column falls back to it.
    report = {
        "ok": True, "toi": "1000", "tic": None, "total": 2,
        "time_series": [
            {"tstoi": "TOI 1000.01", "tsplname": "TIC 50365310.01",
             "tstel": "ASTEP-ANTARCTICA (0.4 m)", "tsdate": "2025-05-16", "tsnum": "3200"},
            {"tstoi": "", "tsplname": "TIC 50365310.02",
             "tstel": "ASTEP-ANTARCTICA (0.4 m)", "tsdate": "2025-06-01", "tsnum": "100"},
        ],
    }
    page.route("**/api/lco/archive/exofop?*", lambda route: route.fulfill(json=report))
    _open_section(page, "exofop-obslog-details")
    headers = page.locator("#exofop-obslog-table thead th").all_inner_texts()
    assert headers[0] == "Planet"
    first_cells = page.locator("#exofop-obslog-tbody tr td:first-child").all_inner_texts()
    assert first_cells == ["TOI 1000.01", "TIC 50365310.02"]


def test_photometry_link_opens_the_selected_night(page):
    page.goto(URL)
    page.locator(f'{ROWS}[data-date="261001"] a[title="photometry"]').click()
    page.wait_for_url('**/photometry?**')
    from urllib.parse import parse_qs, urlsplit
    query = parse_qs(urlsplit(page.url).query)
    assert query['inst'] == ['muscat3'] and query['date'] == ['261001']
    assert query['target'] == ['TOI-1000']


def test_dataset_sort_and_numeric_filters_combine(page):
    page.goto(URL)
    header = page.locator('#datasets-table th[data-sort-attr="date"]')
    header.click()
    first = _visible_dates(page)
    header.click()
    assert _visible_dates(page) == list(reversed(first))
    page.fill('#dataset-search', 'sinistro')
    assert _visible_dates(page) == ['261003']
    page.fill('tr.filter-row input[data-col="frames"]', '>1')
    assert _visible_dates(page) == []
    page.fill('#dataset-search', '')
    assert _visible_dates(page) == ['261001']


def test_note_can_be_edited_and_cancelled_without_losing_saved_text(page, live_server):
    page.goto(URL)
    cell = page.locator(f'{ROWS}[data-date="261001"] .note-cell')
    for text in ('first note', 'corrected note'):
        cell.click()
        cell.locator('textarea').fill(text)
        with page.expect_response(lambda r: r.url.endswith('/note') and r.request.method == 'PUT'):
            cell.locator('button.save').click()
        expect(cell.locator('.note-text')).to_have_text(text)
    cell.click()
    cell.locator('textarea').fill('discarded')
    cell.get_by_role('button', name='Cancel', exact=True).click()
    expect(cell.locator('.note-text')).to_have_text('corrected note')
    page.reload()
    expect(cell.locator('.note-text')).to_have_text('corrected note')


def test_ads_success_renders_papers_as_text_and_safe_links(page):
    page.route('**/api/ads/config', lambda r: r.fulfill(json={'token_configured': True}))
    page.route('**/api/targets/publications**', lambda r: r.fulfill(json={'ok': True, 'papers': [
        {'title': ['<b>A transit study</b>'], 'bibcode': '2026Test', 'author': ['A. Observer'], 'year': '2026'},
    ]}))
    page.goto(URL)
    page.locator('#ads-details > summary').click()
    page.click('#ads-search-btn')
    expect(page.locator('#ads-results-list')).to_contain_text('<b>A transit study</b>')
    assert page.locator('#ads-results-list b').count() == 0
    expect(page.locator('#ads-results-list a').first).to_have_attribute('href', 'https://ui.adsabs.harvard.edu/abs/2026Test/abstract')


def test_exofop_unchecked_rows_have_reasons_and_are_not_counted_as_present(page):
    reason = 'Target coordinates could not be resolved.'
    report = {
        'ok': True, 'toi': '1000', 'total': 3,
        'time_series': [
            {'tstel': 'LCO-SSO-1m0 (1.0 m)', 'tscam': 'Sinistro',
             'exists_checked': True, 'exists': True, 'dataset_local_frames': 12},
            {'tstel': 'LCO-CTIO (1.0 m)', 'tscam': 'Sinistro',
             'exists_checked': True, 'exists': False},
            {'tstel': 'LCO-SAAO (1.0 m)', 'tscam': 'Sinistro',
             'exists_checked': False, 'exists': False, 'exists_unchecked_reason': reason},
        ],
    }
    page.route('**/api/lco/archive/exofop?*', lambda route: route.fulfill(json=report))
    _open_section(page, 'exofop-obslog-details')
    rows = page.locator('#exofop-obslog-tbody tr')
    expect(rows.nth(0).locator('.exofop-status')).to_have_text('In muscat-db (12 fr)')
    expect(rows.nth(1).locator('.exofop-status')).to_have_text('Not in database')
    badge = rows.nth(2).locator('.exofop-status .badge')
    expect(badge).to_have_text('N/A')
    expect(badge).to_have_attribute('title', reason)
    expect(page.locator('#exofop-obslog-status')).to_have_text(
        '1/3 reported in muscat-db, 1 missing, 1 unchecked.'
    )
