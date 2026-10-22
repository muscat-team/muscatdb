"""Behaviour every page inherits from base.html: theme, resizable columns, loading bar."""

from __future__ import annotations

from playwright.sync_api import expect
import pytest


def _theme(page) -> str:
    return page.evaluate("document.documentElement.getAttribute('data-theme')")


def test_theme_toggle_persists_across_pages(page):
    page.goto("/targets")
    assert _theme(page) == "light"

    page.click("#theme-toggle")
    assert _theme(page) == "dark"

    page.goto("/jobs")
    assert _theme(page) == "dark"
    page.click("#theme-toggle")
    page.goto("/targets")
    assert _theme(page) == "light"


def test_dragged_column_width_is_restored_after_reload(page):
    page.goto("/targets")
    th = page.locator("#targets-table thead tr:first-child th").first
    start = th.bounding_box()["width"]
    handle = th.locator(".col-resizer").bounding_box()
    page.mouse.move(handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2)
    page.mouse.down()
    page.mouse.move(handle["x"] + 80, handle["y"] + handle["height"] / 2, steps=5)
    page.mouse.up()
    widened = th.bounding_box()["width"]
    assert widened > start + 40

    page.reload()
    restored = page.locator("#targets-table thead tr:first-child th").first.bounding_box()["width"]
    assert abs(restored - widened) < 2


def test_loading_bar_clears_after_load(page):
    page.goto("/targets")
    page.wait_for_load_state("networkidle")
    page.wait_for_function("!document.getElementById('status-bar').classList.contains('visible')")
    assert "done" in page.get_attribute("#loading-bar", "class")


@pytest.mark.parametrize(("section", "path"), [
    ("targets", "/targets"), ("obslog", "/obs"), ("projects", "/projects"),
    ("jobs", "/jobs"), ("settings", "/settings"), ("guide", "/guide"),
])
def test_navbar_links_navigate_and_mark_the_current_section(page, section, path):
    page.goto("/")
    page.locator(f'nav a[data-nav-section="{section}"]').click()
    page.wait_for_url(f"**{path}")
    assert page.url.endswith(path)
    expect(page.locator(f'nav a[data-nav-section="{section}"]')).to_have_attribute('aria-current', 'page')


def test_navbar_preserves_the_selected_dataset(page):
    page.goto("/transit-fit?inst=muscat3&date=261001&target=TOI-1000")
    page.goto("/jobs")
    page.click("#transit-fit-nav-link")
    page.wait_for_url("**/transit-fit?**")
    from urllib.parse import parse_qs, urlsplit
    query = parse_qs(urlsplit(page.url).query)
    assert {k: query[k] for k in ("inst", "date", "target")} == {
        "inst": ["muscat3"], "date": ["261001"], "target": ["TOI-1000"],
    }


def test_keyboard_can_open_and_cancel_a_modal_on_a_narrow_viewport(page):
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto("/projects")
    button = page.locator("#create-project-btn")
    button.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#create-project-name")).to_be_focused()
    page.keyboard.press("Escape")
    expect(page.locator("#create-project-overlay")).not_to_be_visible()
