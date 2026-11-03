"""Narrow-screen navigation menu and logout (#388)."""

from __future__ import annotations

import sqlite3

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import ADMIN, PROXY_SECRET

MENU = "#nav-menu"
BUTTON = "#nav-menu-btn"


@pytest.fixture
def phone(page):
    page.set_viewport_size({"width": 375, "height": 700})
    return page


def test_links_collapse_behind_menu_button(phone):
    phone.goto("/")
    expect(phone.locator(BUTTON)).to_be_visible()
    expect(phone.locator(BUTTON)).to_have_attribute("aria-expanded", "false")
    expect(phone.locator(BUTTON)).to_have_attribute("aria-controls", "nav-menu")
    expect(phone.locator(MENU)).to_be_hidden()
    expect(phone.locator("nav .brand")).to_be_visible()
    expect(phone.locator("#theme-toggle")).to_be_visible()


def test_desktop_header_has_no_menu_button(page):
    page.goto("/")
    expect(page.locator(BUTTON)).to_be_hidden()
    expect(page.locator('nav a[data-nav-section="targets"]')).to_be_visible()
    expect(page.locator("#nav-logout")).to_be_hidden()


def test_button_toggles_menu(phone):
    phone.goto("/")
    phone.click(BUTTON)
    expect(phone.locator(MENU)).to_be_visible()
    expect(phone.locator(BUTTON)).to_have_attribute("aria-expanded", "true")
    phone.click(BUTTON)
    expect(phone.locator(MENU)).to_be_hidden()


def test_escape_closes_menu_and_returns_focus(phone):
    phone.goto("/")
    phone.focus(BUTTON)
    phone.keyboard.press("Enter")
    expect(phone.locator(MENU)).to_be_visible()
    phone.keyboard.press("Escape")
    expect(phone.locator(MENU)).to_be_hidden()
    expect(phone.locator(BUTTON)).to_be_focused()


def test_outside_tap_closes_menu(phone):
    phone.goto("/")
    phone.click(BUTTON)
    phone.mouse.click(5, 695)  # below the open menu
    expect(phone.locator(MENU)).to_be_hidden()


def test_every_destination_is_two_taps_away(phone):
    phone.goto("/")
    phone.click(BUTTON)
    phone.locator('#nav-menu a[data-nav-section="jobs"]').click()
    phone.wait_for_url("**/jobs")
    expect(phone.locator(MENU)).to_be_hidden()


@pytest.mark.parametrize("width", [320, 375, 768])
def test_no_horizontal_scroll(page, width):
    page.set_viewport_size({"width": width, "height": 700})
    # /settings: its own content fits, so this isolates the header. Dense pages
    # (home cards, jobs table) overflow on their own and are out of scope (#388).
    page.goto("/settings")
    page.click(BUTTON)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.evaluate("document.querySelector('nav').getBoundingClientRect().right <= window.innerWidth")


def test_menu_shows_username_and_logout(phone):
    phone.goto("/")
    phone.click(BUTTON)
    expect(phone.locator("#nav-user-name")).to_have_text(ADMIN)
    expect(phone.locator("#nav-logout")).to_be_visible()


def test_basic_auth_logout_lands_on_page_without_username(phone):
    phone.problems.allowed += ["HTTP 401: GET /logout", "status of 401"]
    phone.goto("/")
    phone.click(BUTTON)
    phone.click("#nav-logout")
    phone.wait_for_url("**/logout")
    expect(phone.locator("h1")).to_have_text("Logged out")
    expect(phone.locator("body")).not_to_contain_text(ADMIN)


def test_basic_auth_logout_challenges_for_credentials(page):
    res = page.request.get("/logout")
    assert res.status == 401
    assert res.headers["www-authenticate"].startswith("Basic")


def test_cloudflare_logout_redirects_to_access(browser, live_server):
    with sqlite3.connect(live_server.db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (username, email, is_admin) VALUES ('cfuser', 'cf@example.org', 0)"
        )
    ctx = browser.new_context(
        base_url=live_server.url,
        extra_http_headers={"X-Forwarded-User": "cf@example.org", "X-MuSCAT-Proxy-Secret": PROXY_SECRET},
    )
    try:
        res = ctx.request.get("/logout", max_redirects=0)
        assert res.status == 302
        assert res.headers["location"] == "/cdn-cgi/access/logout"
    finally:
        ctx.close()
