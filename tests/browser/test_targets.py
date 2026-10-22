"""The /targets list: search, filters, sorting, persistence, inline edits."""

from __future__ import annotations

ROWS = "#targets-table tbody tr"


def _visible(page) -> list[str]:
    return page.eval_on_selector_all(
        ROWS, "rows => rows.filter(r => r.style.display !== 'none').map(r => r.dataset.name)"
    )


def _status(page) -> str:
    return page.inner_text("#search-status")


def test_search_filters_rows_and_counts(page):
    page.goto("/targets")
    assert sorted(_visible(page)) == ["HAT-P-1", "TOI-1000"]
    assert _status(page) == "2 targets"

    page.fill("#target-search", "hat")
    assert _visible(page) == ["HAT-P-1"]
    assert _status(page) == "1 / 2 matching"

    page.check("#case-toggle")
    assert _visible(page) == []
    page.uncheck("#case-toggle")

    page.check("#regex-toggle")
    page.fill("#target-search", r"TOI-1\d{3}\b")
    assert _visible(page) == ["TOI-1000"]
    page.fill("#target-search", "(")
    assert _status(page).startswith("Invalid regex")


def test_instrument_badge_and_column_filter(page):
    page.goto("/targets")
    page.click('.instrument-badge[data-instrument="muscat2"]')
    assert _visible(page) == ["HAT-P-1"]
    page.click('.instrument-badge[data-instrument="muscat2"]')
    assert sorted(_visible(page)) == ["HAT-P-1", "TOI-1000"]

    page.fill('tr.filter-row input[data-col="date"]', "261003")
    assert _visible(page) == ["TOI-1000"]


def test_nframes_threshold_hides_small_nights(page):
    page.goto("/targets")
    toi = page.locator(f'{ROWS}[data-name="TOI-1000"] .date-entry')
    assert toi.count() == 2
    page.fill("#nframe-filter", "2")
    visible = [e for e in toi.all() if e.is_visible()]
    assert [e.inner_text().split()[0] for e in visible] == ["261001"]


def test_filters_and_sort_survive_reload(page):
    page.goto("/targets")
    page.fill("#target-search", "toi")
    page.click('.instrument-badge[data-instrument="muscat3"]')
    page.click('#targets-table th[data-sort-attr="name"]')
    page.reload()
    assert page.input_value("#target-search") == "toi"
    assert "active" in page.get_attribute('.instrument-badge[data-instrument="muscat3"]', "class")
    assert _visible(page) == ["TOI-1000"]
    assert page.evaluate("JSON.parse(localStorage.getItem('home:sort:targets-table')).col") == 0


def test_rename_normalized_name_inline(page, live_server):
    page.goto("/targets")
    cell = page.locator(f'{ROWS}[data-name="HAT-P-1"] .norm-name-cell')
    cell.locator(".norm-name-edit-hint").click()
    cell.locator("input").fill("HATP1-renamed")
    with page.expect_response(lambda r: "/norm-name" in r.url) as resp:
        cell.locator("button.save").click()
    assert resp.value.ok
    assert cell.locator("a").inner_text() == "HATP1-renamed"

    page.reload()
    assert "HATP1-renamed" in page.locator(f'{ROWS}[data-name="HAT-P-1"] .norm-name-cell').inner_text()


def test_add_project_tag(page):
    page.goto("/targets")
    cell = page.locator(f'{ROWS}[data-name="TOI-1000"] .tags-cell')
    assert cell.locator(".badge-tag").all_inner_texts() == ["demo"]
    cell.locator(".tag-add-hint").click()
    cell.locator("input.tag-add-input").fill("demo")
    with page.expect_navigation():
        cell.locator("button.save").click()
    # Adding a project the target already has is a no-op, not an error.
    assert page.locator(f'{ROWS}[data-name="TOI-1000"] .tags-cell .badge-tag').all_inner_texts() == ["demo"]
    page.locator(f'{ROWS}[data-name="TOI-1000"] .tags-cell .badge-tag').click()
    assert page.url.endswith("/tag?name=demo")
