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


def _tracked_while_in_flight(page, url: str) -> int:
    """In-flight request count the loading bar sees right after ``fetch(url)``."""
    return page.evaluate("url => { fetch(url); return window.MuscatFetchTracker.active; }", url)


def test_archive_download_progress_poll_does_not_flash_the_status_bar(page):
    """The LCO archive page polls a download's progress every 1.5 s. Counting
    that poll as page activity raised the status bar and then showed "Ready"
    on every tick for as long as the download ran."""
    page.route("**/api/lco/archive/**", lambda route: route.fulfill(json={"ok": True}))
    page.goto("/targets")
    page.wait_for_load_state("networkidle")
    page.wait_for_function("!document.getElementById('status-bar').classList.contains('visible')")

    assert _tracked_while_in_flight(page, "/api/lco/archive/download/job-1") == 0
    page.wait_for_load_state("networkidle")
    assert not page.evaluate("document.getElementById('status-bar').classList.contains('visible')")

    # A one-off query still counts, so the bar keeps reporting real loading.
    assert _tracked_while_in_flight(page, "/api/lco/archive/frames?request_id=1") == 1


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
