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


# HIP 67522 from coj, April 2027, 3 h transit: one full, one partial and one
# unobservable transit (see tests/test_lco_windows_upload.py).
UPLOAD_COORDS = "199.3, -40.6"
UPLOADED = "tc,tc_err\n2027-04-10T14:00:00Z,0.001\n2027-04-10T19:00:00Z,\n2027-04-10T02:00:00Z,\n"


def _upload_and_generate(page, tmp_path) -> None:
    page.goto("/lco/schedule")
    page.select_option("#sch-source", "upload")
    expect(page.locator("#sch-midtimes")).to_be_visible()
    page.fill("#sch-coords", UPLOAD_COORDS)  # by hand: no target lookup
    page.fill("#sch-range-start", "2027-04-01")
    page.fill("#sch-range-end", "2027-04-30")
    page.fill("#sch-duration", "3")
    page.fill("#sch-pad-before", "0")
    page.fill("#sch-pad-after", "0")
    page.select_option("#sch-sites", "coj")
    path = tmp_path / "midtimes.csv"
    path.write_text(UPLOADED)
    page.set_input_files("#sch-midtimes-file", str(path))
    expect(page.locator("#sch-midtimes")).to_have_value(UPLOADED)
    with page.expect_response("**/api/lco/windows", timeout=60_000):
        page.click("#btn-gen-windows")


def _visible_ratings(page) -> list[str]:
    return page.eval_on_selector_all(
        "#windows-body tr",
        "rows => rows.filter(r => r.style.display !== 'none').map(r => r.dataset.rating)",
    )


def test_uploaded_midtimes_are_rated_and_filtered(page, tmp_path):
    page.problems.allowed.extend(NO_TOKEN)
    _upload_and_generate(page, tmp_path)
    expect(page.locator("#windows-body tr")).to_have_count(3)
    expect(page.locator("#msg-windows")).to_contain_text("uploaded: 3 times (3 as UTC)")
    expect(page.locator("#win-counts")).to_have_text("3 transits: 1 full, 1 partial, 0 split, 1 none")
    # the uploaded uncertainty fills Mid unc; the rows without one show '-'
    expect(page.locator("#windows-body")).to_contain_text("± 1.44 min")

    expected = {"all": ["none", "full", "partial"], "obs": ["full", "partial"],
                "full": ["full"], "none": ["none"], "split": []}
    for mode, ratings in expected.items():
        page.select_option("#win-filter", mode)
        assert _visible_ratings(page) == ratings, mode

    with page.expect_download() as dl:
        page.click("#btn-win-csv")
    lines = open(dl.value.path()).read().splitlines()
    assert lines[0].startswith("epoch,mid_utc,mid_bjd_tdb,mid_unc_min")
    assert [line.split(",")[6] for line in lines[1:]] == ["none", "full", "partial"]

    # the list and the source survive a reload (which keeps the URL slug) and
    # a fresh visit, like the rest of the form
    for revisit in (page.reload, lambda: page.goto("/lco/schedule")):
        revisit()
        assert page.input_value("#sch-source") == "upload"
        assert page.input_value("#sch-midtimes") == UPLOADED.strip()
        expect(page.locator("#windows-body tr")).to_have_count(3)


def test_a_bad_uploaded_line_is_reported_by_number(page):
    page.problems.allowed.extend(NO_TOKEN + ("HTTP 400: POST /api/lco/windows", "status of 400"))
    page.goto("/lco/schedule")
    page.select_option("#sch-source", "upload")
    page.fill("#sch-coords", UPLOAD_COORDS)
    page.fill("#sch-range-start", "2027-04-01")
    page.fill("#sch-range-end", "2027-04-30")
    page.fill("#sch-duration", "3")
    page.fill("#sch-midtimes", "2461505.5\n3000.5\n")
    page.click("#btn-gen-windows")
    expect(page.locator("#msg-windows")).to_contain_text("line 2: tc '3000.5' looks like a reduced BJD")


def test_other_sources_hide_the_upload_box(page):
    page.problems.allowed.extend(NO_TOKEN)
    page.goto("/lco/schedule")
    expect(page.locator("#sch-midtimes")).to_be_hidden()
    page.select_option("#sch-source", "upload")
    expect(page.locator("#sch-midtimes")).to_be_visible()
    page.select_option("#sch-source", "nasa")
    expect(page.locator("#sch-midtimes")).to_be_hidden()


def test_reload_keeps_fields_outside_the_url_slug(page):
    """The slug carries only target/coords/range/...; a reload keeps it, so the
    stored form has to fill in everything else rather than be discarded."""
    page.problems.allowed.extend(NO_TOKEN)
    page.goto("/lco/schedule")
    page.fill("#sch-coords", f"{RA}, {DEC}")
    page.fill("#sch-pad-before", "17")
    page.select_option("#sch-twilight", "nautical")
    page.select_option("#win-filter", "none")
    assert "#" in page.url  # the slug is in place
    page.reload()
    assert page.input_value("#sch-pad-before") == "17"
    assert page.input_value("#sch-twilight") == "nautical"
    assert page.input_value("#win-filter") == "none"
    assert page.input_value("#sch-coords") == f"{RA}, {DEC}"


def test_a_link_to_another_target_drops_the_stored_targets_ephemeris(page):
    """Opening a shared #target= link must not pair that target with the t0,
    period, source or windows resolved for the target stored in this browser."""
    page.problems.allowed.extend(NO_TOKEN + ("HTTP 404", "status of 404"))
    page.goto("/lco/schedule")
    _generate_windows(page)
    expect(page.locator("#windows-body tr")).to_have_count(4)
    stored_t0 = page.input_value("#sch-t0")
    page.select_option("#sch-source", "upload")
    page.fill("#sch-midtimes", "2461505.5")
    page.fill("#sch-pad-before", "17")
    page.locator("#sch-pad-before").blur()

    page.goto("about:blank")  # a hash-only change would not reload the page
    page.goto("/lco/schedule#target=NOT-A-STORED-TARGET")
    assert page.input_value("#sch-target") == "NOT-A-STORED-TARGET"
    assert page.input_value("#sch-t0") != stored_t0
    assert page.input_value("#sch-source") != "upload"
    assert page.input_value("#sch-midtimes") == ""
    expect(page.locator("#windows-body tr")).to_have_count(0)
    # settings that are not about the target still carry over
    assert page.input_value("#sch-pad-before") == "17"


def test_airmass_ranked_plans_can_be_selected_and_restored(page):
    page.problems.allowed.extend(NO_TOKEN)
    page.goto("/lco/schedule")
    page.select_option("#sch-source", "upload")
    page.select_option("#sch-kind", "qhy600")
    page.fill("#sch-coords", "330, 5")
    page.fill("#sch-range-start", "2026-11-04")
    page.fill("#sch-range-end", "2026-11-05")
    page.fill("#sch-duration", "2")
    page.fill("#sch-pad-before", "0")
    page.fill("#sch-pad-after", "0")
    page.fill("#sch-moon-sep", "0")
    page.fill("#sch-midtimes", "2026-11-04T02:00:00Z")
    with page.expect_response("**/api/lco/windows", timeout=60_000):
        page.click("#btn-gen-windows")
    select = page.locator(".plan-select")
    expect(select).to_have_count(1)
    expect(select).to_have_value("site:elp")
    expect(select.locator('option[value="site:elp"]')).to_contain_text("mean X 1.12")
    expect(select.locator('option[value="site:lsc"]')).to_contain_text("mean X 1.50")
    relay_ids = select.locator('option[value^="relay:"]').evaluate_all("opts => opts.map(o => o.value)")
    assert "relay:lsc>elp" in relay_ids and "relay:elp>lsc" in relay_ids
    page.locator("#windows-body input[type=checkbox]").check()
    for relay_id in relay_ids:
        select.select_option(relay_id)
        expect(page.locator("#requests-body tr")).to_have_count(2)
        expect(select).to_have_value(relay_id)
    page.reload()
    expect(page.locator(".plan-select")).to_have_value(relay_ids[-1])


@pytest.mark.parametrize(('today', 'end'), [('2026-10-30', '2026-11-30'), ('2027-01-31', '2027-02-28'), ('2028-01-31', '2028-02-29')])
def test_default_range_is_one_calendar_month(page, today, end):
    page.problems.allowed.extend(NO_TOKEN)
    page.clock.set_fixed_time(datetime.datetime.fromisoformat(today + 'T12:00:00+00:00'))
    page.goto('/lco/schedule')
    expect(page.locator('#sch-range-start')).to_have_value(today)
    expect(page.locator('#sch-range-end')).to_have_value(end)


def test_upload_disables_ignored_controls_and_skips_their_validation(page):
    page.problems.allowed.extend(NO_TOKEN)
    page.goto('/lco/schedule')
    page.fill('#sch-t0', 'invalid')
    page.fill('#sch-period', 'invalid')
    page.select_option('#sch-source', 'upload')
    for field in ('sch-t0', 'sch-period', 'sch-rank'):
        expect(page.locator('#' + field)).to_be_disabled()
    page.fill('#sch-coords', UPLOAD_COORDS)
    page.fill('#sch-duration', '3')
    page.fill('#sch-range-start', '2027-04-01')
    page.fill('#sch-range-end', '2027-04-30')
    page.fill('#sch-midtimes', UPLOADED)
    with page.expect_response('**/api/lco/windows', timeout=60_000) as response:
        page.click('#btn-gen-windows')
    assert response.value.json()['ok']
    page.select_option('#sch-source', '')
    expect(page.locator('#sch-t0')).to_be_enabled()
    expect(page.locator('#sch-period')).to_be_enabled()
