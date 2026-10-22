"""Behaviour every page inherits from base.html: theme, resizable columns, loading bar."""

from __future__ import annotations


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
