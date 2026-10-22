"""The /toi and /nexsci catalog browsers: chips, search, plot, URL state.

Both pages build their table and Plotly scatter client-side from the fixture
catalogs (four TOIs, three NExSci planets). Chips OR within a group and AND
across groups; the state lives in the URL hash so it survives a reload.
"""

from __future__ import annotations


def _rows(page, tbody: str) -> list[str]:
    return page.eval_on_selector_all(
        f"{tbody} tr",
        "rs => rs.filter(r => r.offsetParent !== null).map(r => r.cells[0].innerText.replace('★', '').trim())",
    )


def _wait_count(page, sel: str, text: str) -> None:
    page.wait_for_function(
        "([s, t]) => document.querySelector(s).innerText === t", arg=[sel, text]
    )


def _last_plot(page) -> dict:
    return page.evaluate("Plotly.__calls.filter(c => c.fn === 'react' || c.fn === 'newPlot').slice(-1)[0]")


def test_toi_chips_combine_and_redraw_the_plot(page):
    page.goto("/toi")
    _wait_count(page, "#toi-count", "4 / 4 objects")
    assert _last_plot(page)["id"] == "toi-plot"

    page.click('.toi-chip[data-group="disp"][data-key="PC"]')
    _wait_count(page, "#toi-count", "1 / 4 objects")
    assert _rows(page, "#toi-tbody") == ["TOI-1000.01"]
    assert _last_plot(page)["traces"] == 1

    page.click('.toi-chip[data-group="disp"][data-key="KP"]')  # same group: OR
    _wait_count(page, "#toi-count", "2 / 4 objects")
    page.click('.toi-chip[data-group="obs"][data-key="south"]')  # other group: AND
    _wait_count(page, "#toi-count", "0 / 4 objects")
    page.click('.toi-chip[data-group="obs"][data-key="south"]')
    _wait_count(page, "#toi-count", "2 / 4 objects")
    assert _rows(page, "#toi-tbody") == ["TOI-1000.01", "TOI-1002.01"]


def test_toi_search_and_url_state_survive_reload(page):
    page.goto("/toi")
    page.click('.toi-chip[data-group="disp"][data-key="FP"]')
    page.check("#plot-logx")
    page.wait_for_function("location.hash.includes('disp=FP') && location.hash.includes('logx=1')")

    page.reload()
    _wait_count(page, "#toi-count", "1 / 4 objects")
    assert "active" in page.get_attribute('.toi-chip[data-group="disp"][data-key="FP"]', "class")
    assert page.is_checked("#plot-logx")

    page.click('.toi-chip[data-group="disp"][data-key="FP"]')
    page.fill("#toi-search", "HAT-P-1")
    _wait_count(page, "#toi-count", "1 / 4 objects")
    assert _rows(page, "#toi-tbody") == ["TOI-1002.01"]
    page.click("#toi-clear")
    _wait_count(page, "#toi-count", "4 / 4 objects")


def test_toi_plot_axes_follow_the_selects(page):
    page.goto("/toi")
    _wait_count(page, "#toi-count", "4 / 4 objects")
    before = len(page.evaluate("Plotly.__calls"))
    options = page.eval_on_selector_all("#plot-y option", "os => os.map(o => o.value)")
    new_y = next(v for v in options if v != page.input_value("#plot-y"))
    page.select_option("#plot-y", new_y)
    page.wait_for_function(f"Plotly.__calls.length > {before}")
    page.wait_for_function(f"location.hash.includes('y={new_y}')")


def test_nexsci_chips_and_muscat_db_membership(page):
    page.goto("/nexsci")
    _wait_count(page, "#nx-count", "3 / 3 planets")
    star = page.locator("#nx-tbody tr", has_text="HAT-P-1 b")
    assert "★" in star.inner_text()  # host has data in muscat-db

    page.click('.nx-chip[data-group="size"][data-key="Jupiter-size"]')
    _wait_count(page, "#nx-count", "2 / 3 planets")
    page.click('.nx-chip[data-group="indb"][data-key="1"]')
    _wait_count(page, "#nx-count", "1 / 3 planets")
    assert _rows(page, "#nx-tbody") == ["HAT-P-1 b"]
    page.click('.nx-chip[data-group="flags"][data-key="tertiary"]')
    _wait_count(page, "#nx-count", "0 / 3 planets")
    assert _last_plot(page)["traces"] == 0


def test_nexsci_search_and_reload(page):
    page.goto("/nexsci")
    page.fill("#nx-search", "kepler")
    _wait_count(page, "#nx-count", "1 / 3 planets")
    assert _rows(page, "#nx-tbody") == ["Kepler-51 b"]
    page.click('.nx-chip[data-group="method"][data-key="Transit"]')
    page.wait_for_function("location.hash.includes('Transit')")
    page.reload()
    assert "active" in page.get_attribute('.nx-chip[data-group="method"][data-key="Transit"]', "class")
