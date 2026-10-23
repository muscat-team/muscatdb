"""The /settings credential forms and the /guide page."""

from __future__ import annotations

import re

from playwright.sync_api import expect
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


@pytest.mark.parametrize(('prefix', 'title'), [('lco', 'LCO API Token'), ('ads', 'ADS API Token')])
def test_saved_token_status_survives_reload_without_exposing_the_secret(page, prefix, title):
    page.goto('/settings')
    _open_section(page, title)
    page.fill(f'#{prefix}-token', 'browser-secret-value')
    page.click(f'#save-{prefix}-token')
    expect(page.locator(f'#{prefix}-token-msg')).to_have_text('Saved.')
    page.reload()
    _open_section(page, title)
    expect(page.locator(f'#{prefix}-token-status')).to_contain_text('user token: saved')
    expect(page.locator(f'#{prefix}-token')).to_have_value('')
    assert 'browser-secret-value' not in page.content()


def test_eso_success_saves_encrypted_credentials_then_clears_them(page, live_server, monkeypatch):
    from muscat_db import web
    import httpx
    original = web.http_client.get_async_client
    class ESOClient:
        async def get(self, url, **kwargs):
            assert url == web._ESO_TOKEN_URL
            assert kwargs['params']['username'] == 'browser-observer'
            return httpx.Response(200, json={'id_token': 'fixture-identity-token'})
    monkeypatch.setattr(web.http_client, 'get_async_client', lambda: ESOClient())
    page.goto('/settings')
    _open_section(page, 'ESO Archive Credentials')
    page.fill('#eso-username', 'browser-observer')
    page.fill('#eso-password', 'browser-password')
    page.click('#test-eso-credentials')
    expect(page.locator('#eso-credentials-status')).to_contain_text('user credentials: saved')
    assert 'browser-password' not in live_server.sql("SELECT settings FROM users WHERE username = 'admin'")[0][0]
    monkeypatch.setattr(web.http_client, 'get_async_client', original)
    page.reload()
    _open_section(page, 'ESO Archive Credentials')
    expect(page.locator('#eso-credentials-status')).to_contain_text('user credentials: saved')
    expect(page.locator('#eso-password')).to_have_value('')
    page.click('#clear-eso-credentials')
    expect(page.locator('#eso-credentials-status')).to_contain_text('user credentials: not saved')
    page.reload()
    _open_section(page, 'ESO Archive Credentials')
    expect(page.locator('#eso-credentials-status')).to_contain_text('user credentials: not saved')


def test_google_sheet_columns_mapping_save_reload_and_clear(page, monkeypatch):
    from muscat_db import web
    columns = ['Star', 'Center', 'Cycle', 'Days', 'Hours', 'Error']
    calls = []
    def tab_columns(url, tab):
        calls.append((url, tab))
        return columns
    monkeypatch.setattr(web.gsheet_ephemeris, 'tab_columns', tab_columns)
    page.goto('/settings')
    _open_section(page, 'Ephemeris Google Sheet')
    page.fill('#ephem-sheet-url', 'https://docs.google.com/spreadsheets/d/browser_fixture_sheet/edit')
    page.fill('#ephem-sheet-ephem-tab', 'planets')
    page.fill('#ephem-sheet-tc-tab', 'timings')
    page.click('#load-ephem-sheet-columns')
    expect(page.locator('#ephem-sheet-cols-msg')).to_contain_text('6 ephemeris / 6 transit-center columns')
    ephem = '#ephem-sheet-ephem-cols'
    tc = '#ephem-sheet-tc-cols'
    for key, value in {'target': 'Star', 't0': 'Center', 'period': 'Days', 'duration': 'Hours'}.items():
        page.select_option(f'{ephem} select[data-col-field="{key}"]', value)
    for key, value in {'target': 'Star', 'epoch': 'Cycle', 'tc': 'Center', 'tc_unc': 'Error'}.items():
        page.select_option(f'{tc} select[data-col-field="{key}"]', value)
    page.click('#save-ephem-sheet')
    expect(page.locator('#ephem-sheet-status')).to_contain_text('sheet: saved')
    assert {tab for _, tab in calls} == {'planets', 'timings'}
    page.reload()
    _open_section(page, 'Ephemeris Google Sheet')
    expect(page.locator('#ephem-sheet-status')).to_contain_text('sheet: saved')
    expect(page.locator(f'{ephem} select[data-col-field="period"]')).to_have_value('Days')
    expect(page.locator(f'{tc} select[data-col-field="tc_unc"]')).to_have_value('Error')
    expect(page.locator('#ephem-sheet-url')).to_have_value('')
    page.click('#clear-ephem-sheet')
    expect(page.locator('#ephem-sheet-status')).to_contain_text('sheet: not saved')
    page.reload()
    _open_section(page, 'Ephemeris Google Sheet')
    expect(page.locator('#ephem-sheet-status')).to_contain_text('sheet: not saved')


def test_google_sheet_rejects_invalid_url_and_recovers(page):
    page.problems.allowed += ['HTTP 400: POST /api/settings/ephem-sheet', 'status of 400']
    page.goto('/settings')
    _open_section(page, 'Ephemeris Google Sheet')
    page.fill('#ephem-sheet-url', 'https://example.org/private')
    page.click('#save-ephem-sheet')
    expect(page.locator('#ephem-sheet-msg')).to_have_class(re.compile('err'))
    expect(page.locator('#save-ephem-sheet')).to_be_enabled()
    expect(page.locator('#ephem-sheet-status')).to_contain_text('sheet: not saved')
