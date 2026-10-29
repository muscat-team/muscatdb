"""The /lco/schedule planner: transit windows, then draft, check and submit.

Windows are computed locally, so they are tested against the fixture
ephemeris with no LCO account. The booking flow stubs only the LCO HTTP layer;
the server keeps MUSCAT_LCO_ALLOW_SUBMIT unset, so a confirmed submit must be
refused before anything is sent to LCO.
"""

from __future__ import annotations

import datetime

from playwright.sync_api import expect
import pytest

# TOI-1000.01 in the fixture TOI catalog (seed.TOI_ROWS).
T0, PERIOD = 2461314.86, 2.17
RA, DEC = 117.929, 12.536
RANGE = ("2026-11-01", "2026-11-10")
PROPOSAL = "LCO2026B-002"
NO_TOKEN = ("HTTP 403: GET /api/lco/proposals", "status of 403")


def _generate_windows(page) -> None:
    page.fill("#sch-target", "TOI-1000")
    page.locator("#sch-target").blur()
    expect(page.locator("#sch-period")).not_to_have_value("")
    page.fill("#sch-range-start", RANGE[0])
    page.fill("#sch-range-end", RANGE[1])
    with page.expect_response("**/api/lco/windows", timeout=60_000):
        page.click("#btn-gen-windows")


def _mid_times_utc(page) -> list[datetime.datetime]:
    heads = [
        h.strip().upper()
        for h in page.locator("#windows-body")
        .locator("xpath=ancestor::table//thead//th")
        .all_text_contents()
    ]
    col = heads.index("MID (UTC)")
    return [
        datetime.datetime.strptime(
            row.locator("td").nth(col).inner_text().strip(), "%Y-%m-%d %H:%M"
        )
        for row in page.locator("#windows-body tr").all()
    ]


def _expected_mid_utc(epoch: int) -> datetime.datetime:
    """T0 + n*P in BJD_TDB, moved to the geocenter and expressed in UTC."""
    from astropy import units as u
    from astropy.coordinates import EarthLocation, SkyCoord
    from astropy.time import Time

    bjd = Time(T0 + epoch * PERIOD, format="jd", scale="tdb")
    geocenter = EarthLocation.from_geocentric(0, 0, 0, unit=u.m)
    ltt = bjd.light_travel_time(SkyCoord(RA, DEC, unit="deg"), location=geocenter)
    return (bjd - ltt).utc.to_datetime()


def test_without_a_token_windows_still_follow_the_ephemeris(page):
    page.problems.allowed.extend(NO_TOKEN)
    page.goto("/lco/schedule")
    expect(page.locator("#msg-proposals")).to_have_text("Your LCO API token is not configured")
    _generate_windows(page)
    # Rows, not #msg-windows: a late target lookup can rewrite that message.
    expect(page.locator("#windows-body tr")).to_have_count(4)

    mids = _mid_times_utc(page)
    assert len(mids) == 4
    for mid in mids:
        epoch = round((mid - _expected_mid_utc(0)).total_seconds() / 86400 / PERIOD)
        assert abs((mid - _expected_mid_utc(epoch)).total_seconds()) <= 60  # shown to the minute

    # The first transit is visible from no site, so it cannot be picked.
    first = page.locator("#windows-body tr").first.locator("input[type=checkbox]")
    expect(first).to_be_hidden()

    page.reload()
    expect(page.locator("#windows-body tr")).to_have_count(4)
    assert page.input_value("#sch-target") == "TOI-1000"


def test_end_before_start_is_rejected(page):
    page.problems.allowed.extend(NO_TOKEN)
    page.goto("/lco/schedule")
    page.fill("#sch-coords", f"{RA}, {DEC}")  # by hand, so no target lookup races the check
    page.fill("#sch-range-start", RANGE[1])
    page.fill("#sch-range-end", RANGE[0])
    page.click("#btn-gen-windows")
    expect(page.locator("#msg-windows")).to_have_text("End date must be after start date.")


@pytest.fixture
def lco_account(monkeypatch):
    """A saved LCO token and a stand-in observation portal; records each call."""
    from muscat_db import lco

    calls = []

    def lco_api_request(
        url, method="GET", data=None, user_name=None, token=None, require_own_token=False
    ):
        calls.append((method, url.split("?")[0]))
        if url.startswith("https://observe.lco.global/api/proposals/"):
            return {"count": 1, "results": [{"id": PROPOSAL, "title": "TOI follow-up"}]}
        if url.startswith("https://observe.lco.global/api/instruments/"):
            return {"1M0-SCICAM-SINISTRO": {}, "0M4-SCICAM-QHY600": {}, "2M0-SCICAM-MUSCAT": {}}
        if url == "https://observe.lco.global/api/requestgroups/max_allowable_ipp/":
            return {PROPOSAL: {"1M0": {"semester": "2026B", "max_allowable_ipp_value": 1.5}}}
        raise AssertionError(f"unexpected LCO call: {method} {url}")

    monkeypatch.setattr(lco, "_lco_api_request", lco_api_request)
    monkeypatch.setattr(lco, "get_user_lco_token", lambda user: "test-token")
    monkeypatch.setattr(lco, "_site_instruments_cache", {})
    return calls


def test_draft_check_and_submit_never_books_while_submission_is_disabled(page, lco_account):
    page.problems.allowed.extend(("HTTP 403: POST /api/lco/submit", "status of 403"))
    page.goto("/lco/schedule")
    expect(page.locator("#msg-proposals")).to_have_text("Loaded 1 proposals")
    page.select_option("#sch-proposal", PROPOSAL)
    _generate_windows(page)

    page.locator("#windows-body tr", has_text="Full @ ELP").locator("input[type=checkbox]").check()
    request = page.locator("#requests-body tr").first
    expect(request).to_contain_text("not drafted")

    with page.expect_response("**/api/lco/build"):
        page.click("#btn-create-draft")
    expect(request).to_contain_text("draft")
    expect(page.locator("#btn-dry-run")).to_be_enabled()

    with page.expect_response("**/api/lco/ipp"):
        page.click("#btn-dry-run")
    expect(page.locator("#msg-ipp")).to_have_text(
        "1 of 1 request(s) passed. Submit is enabled for those."
    )
    expect(request).to_contain_text("checked")
    expect(page.locator("#btn-submit")).to_be_enabled()

    page.click("#btn-submit")
    expect(page.locator("#message-modal-ok")).to_have_text("Submit")
    with page.expect_response("**/api/lco/submit") as resp:
        page.click("#message-modal-ok")
    assert resp.value.status == 403
    expect(page.locator("#msg-ipp")).to_contain_text("0 booked, 1 failed")
    expect(request).to_contain_text("Live submission is disabled on the server")
    assert ("POST", "https://observe.lco.global/api/requestgroups/") not in lco_account


def test_shared_and_private_sheet_dropdown_selects_distinct_values(page):
    page.problems.allowed.extend(NO_TOKEN)
    page.route('**/api/ephemeris/target-info?*', lambda route: route.fulfill(json={
        'ok': True, 'planets': ['b'], 'datasets': [], 'ttv_runs': [],
        'sheet_configured': True, 'shared_sheet_configured': True,
        'shared_sheet_owner': 'jerome', 'sheet_ephemeris': {'b': {'t0': 2461314.5, 'period': 4.0}},
        'shared_sheet_ephemeris': {'b': {'t0': 2461315.5, 'period': 3.0}},
        'sheet_fit_ephemeris': {'b': {'t0': 2461316.5, 'period': 4.1}},
        'shared_sheet_fit_ephemeris': {'b': {'t0': 2461317.5, 'period': 3.1}},
    }))
    page.goto('/lco/schedule')
    page.fill('#sch-target', 'TOI-1000')
    page.locator('#sch-target').blur()
    expect(page.locator('#sch-source option[value="shared_gsheet"]')).to_have_text('Shared Google Sheet (jerome)')
    expect(page.locator('#sch-source option[value="gsheet"]')).to_have_text('My Google Sheet (private)')
    for source, period in [('shared_gsheet', '3'), ('gsheet', '4'), ('shared_gsheet_tc', '3.1'), ('gsheet_tc', '4.1')]:
        page.select_option('#sch-source', source)
        expect(page.locator('#sch-period')).to_have_value(period)


@pytest.mark.parametrize("shared", [False, True])
def test_unconfigured_sheet_sources_remain_visible_with_settings_link(page, shared):
    page.problems.allowed.extend(NO_TOKEN)
    page.route('**/api/ephemeris/target-info?*', lambda route: route.fulfill(json={
        'ok': True, 'planets': ['b'], 'datasets': [], 'ttv_runs': [],
        'sheet_configured': False, 'shared_sheet_configured': shared,
    }))
    page.goto('/lco/schedule')
    with page.expect_response('**/api/ephemeris/target-info?*'):
        page.fill('#sch-target', 'TOI-1000')
        page.locator('#sch-target').blur()
    expect(page.locator('#sch-source option[value="gsheet"]')).to_contain_text('not configured')
    for source in ('gsheet', 'gsheet_tc', 'shared_gsheet', 'shared_gsheet_tc'):
        option = page.locator('#sch-source option[value="' + source + '"]')
        if shared and source.startswith('shared_'):
            expect(option).to_be_enabled()
        else:
            expect(option).to_be_disabled()
    link = page.locator('#sheet-source-hint a')
    expect(link).to_be_visible()
    link.click()
    expect(page).to_have_url('/settings')
