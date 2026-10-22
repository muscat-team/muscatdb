"""The /settings credential forms and the /guide page."""

from __future__ import annotations

import pytest


def _open_section(page, title: str):
    section = page.locator(".settings-section", has=page.locator("h3", has_text=title))
    section.locator("h3").click()
    assert "collapsed" not in section.get_attribute("class")
    return section


@pytest.mark.parametrize(("prefix", "title"), [("lco", "LCO API Token"), ("ads", "ADS API Token")])
def test_token_save_and_clear(page, live_server, prefix, title):
    page.goto("/settings")
    status = page.locator(f"#{prefix}-token-status")
    page.wait_for_function(f"document.getElementById('{prefix}-token-status').innerText.startsWith('User:')")
    assert "user token: not saved" in status.inner_text()
    _open_section(page, title)

    page.click(f"#save-{prefix}-token")
    assert "Paste a" in page.inner_text(f"#{prefix}-token-msg")

    secret = f"{prefix}-token-value-123"
    page.fill(f"#{prefix}-token", secret)
    page.click(f"#save-{prefix}-token")
    page.wait_for_function(f"document.getElementById('{prefix}-token-msg').innerText === 'Saved.'")
    page.wait_for_function(
        f"document.getElementById('{prefix}-token-status').innerText.includes('user token: saved')"
    )
    assert page.input_value(f"#{prefix}-token") == ""
    (stored,) = live_server.sql("SELECT settings FROM users WHERE username = 'admin'")[0]
    assert secret not in stored  # encrypted at rest

    page.click(f"#clear-{prefix}-token")
    page.wait_for_function(
        f"document.getElementById('{prefix}-token-status').innerText.includes('user token: not saved')"
    )


def test_eso_credentials_are_not_saved_when_eso_is_unreachable(page, live_server):
    page.problems.allowed += ["HTTP 502: POST /api/settings/eso-credentials-test", "status of 502"]
    page.goto("/settings")
    _open_section(page, "ESO Archive Credentials")
    page.click("#test-eso-credentials")
    assert page.inner_text("#eso-credentials-msg") == "Enter both username and password."

    page.fill("#eso-username", "someone")
    page.fill("#eso-password", "secret")
    page.click("#test-eso-credentials")
    page.wait_for_function("!document.getElementById('test-eso-credentials').disabled")
    msg = page.locator("#eso-credentials-msg")
    assert "err" in msg.get_attribute("class")
    assert "credentials verified and saved" not in msg.inner_text().lower()
    page.wait_for_function(
        "document.getElementById('eso-credentials-status').innerText.includes('user credentials: not saved')"
    )


def test_settings_sections_toggle(page):
    page.goto("/settings")
    section = _open_section(page, "Ephemeris Google Sheet")
    section.locator("h3").click()
    assert "collapsed" in section.get_attribute("class")


def test_guide_toc_links_scroll_to_their_card(page):
    page.goto("/guide")
    links = page.locator(".toc-link")
    assert links.count() >= 10
    for href in ("#timer-card", "#lco-archive-card", "#schema-card"):
        page.click(f'.toc-link[href="{href}"]')
        page.wait_for_function(f"location.hash === '{href}'")
        page.wait_for_function(
            f"""() => {{ const r = document.querySelector('{href}').getBoundingClientRect();
                         return r.top < window.innerHeight && r.bottom > 0; }}"""
        )


def _diagrams_rendered(page) -> None:
    page.wait_for_function(
        "document.querySelectorAll('.mermaid').length > 0 &&"
        " [...document.querySelectorAll('.mermaid')].every(e => e.getAttribute('data-processed') === 'true')"
    )


def test_guide_diagrams_render_and_follow_the_theme(page):
    page.goto("/guide")
    _diagrams_rendered(page)
    assert page.evaluate("window.__mermaidConfig.theme") == "default"

    # The guide reloads on a theme switch so mermaid redraws in the new palette.
    with page.expect_navigation():
        page.click("#theme-toggle")
    _diagrams_rendered(page)
    assert page.evaluate("window.__mermaidConfig.theme") == "dark"
