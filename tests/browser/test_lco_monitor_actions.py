"""Monitoring actions through the rendered schedule page and real local API."""

import sqlite3
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect
import pytest

from muscat_db import lco, lco_monitor
from tests.test_lco_monitor import _submission
from tests.test_lco_monitor_actions import seed_ingested


@pytest.fixture
def monitoring(page, live_server):
    page.problems.allowed.extend(("HTTP 403: GET /api/lco/proposals", "status of 403"))
    result, payload = _submission()
    lco_monitor.record_submission(result, payload, "admin", path=str(live_server.db))
    return live_server.db


def test_complete_links_and_local_delete(page, monitoring):
    seed_ingested(monitoring)
    page.goto("/lco/schedule")
    row = page.locator("#monitor-body tr").filter(has=page.locator('[data-clone-id="101"]'))
    links = row.locator('a[aria-label^="Photometry:"]')
    expect(links).to_have_count(2)
    for index, date in enumerate(("260720", "260721")):
        assert parse_qs(urlparse(links.nth(index).get_attribute("href")).query) == {
            "inst": ["muscat3"],
            "date": [date],
            "target": ["Actual FITS target"],
        }
    assert row.get_by_role("button", name="Clone").evaluate('(b) => b.classList.contains("sm")')
    # A rejected confirmation has no side effects.
    row.get_by_role("button", name="Delete", exact=True).click()
    page.click("#message-modal-cancel")
    expect(row).to_have_count(1)
    row.get_by_role("button", name="Delete", exact=True).click()
    page.click("#message-modal-ok")
    expect(page.locator("#monitor-body")).to_contain_text("No scheduler requests")
    with sqlite3.connect(monitoring) as conn:
        assert conn.execute("SELECT COUNT(*) FROM lco_observation_frames").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM frames WHERE object='Actual FITS target'"
            ).fetchone()[0]
            == 2
        )


@pytest.mark.parametrize("state", ["monitoring", "downloading", "processing"])
def test_unfinished_has_no_photometry_link(page, monitoring, state):
    seed_ingested(monitoring)
    with sqlite3.connect(monitoring) as conn:
        conn.execute("UPDATE lco_observation_requests SET monitor_state=?", (state,))
    page.goto("/lco/schedule")
    expect(page.locator('[data-clone-id="101"]')).to_be_visible()
    expect(page.locator('#monitor-body a[aria-label^="Photometry:"]')).to_have_count(0)


@pytest.mark.parametrize("state", ["COMPLETED", "WINDOW_EXPIRED", "CANCELED"])
def test_terminal_has_no_cancel(page, monitoring, state):
    with sqlite3.connect(monitoring) as conn:
        conn.execute("UPDATE lco_observation_requests SET request_state=?", (state,))
    page.goto("/lco/schedule")
    expect(page.locator('[data-clone-id="101"]')).to_be_visible()
    expect(page.get_by_role("button", name="Cancel on LCO", exact=True)).to_have_count(0)


def test_cancel_relay_both_legs(page, monitoring, monkeypatch):
    result, payload = _submission(request_id=102, requestgroup_id=11)
    lco_monitor.record_submission(result, payload, "admin", path=str(monitoring))
    lco_monitor.link_relay(10, 11, path=str(monitoring))
    calls = []
    monkeypatch.setattr(lco, "cancel_requestgroup", lambda group, user: calls.append((group, user)))
    page.goto("/lco/schedule")
    page.locator('[data-cancel-monitor="101"]').click()
    page.click("#message-modal-ok")
    expect(page.locator("#message-modal-text")).to_contain_text("other relay leg")
    page.click("#message-modal-ok")
    expect(page.get_by_role("button", name="Cancel on LCO", exact=True)).to_have_count(0)
    assert calls == [(10, "admin"), (11, "admin")]
    expect(page.locator("#monitor-body")).to_contain_text("CANCELED")


def test_other_submitters_actions_are_hidden(page, monitoring, monkeypatch):
    from muscat_db import web

    monkeypatch.setattr(web, "_jobs_user_is_admin", lambda user: False)
    with sqlite3.connect(monitoring) as conn:
        conn.execute("UPDATE lco_observation_requests SET user_name='someone-else'")
    page.goto("/lco/schedule")
    expect(page.locator('[data-clone-id="101"]')).to_be_visible()
    expect(page.locator("[data-delete-monitor], [data-cancel-monitor]")).to_have_count(0)


def test_relay_partial_failure_keeps_success_visible(page, monitoring, monkeypatch):
    result, payload = _submission(request_id=102, requestgroup_id=11)
    lco_monitor.record_submission(result, payload, "admin", path=str(monitoring))
    lco_monitor.link_relay(10, 11, path=str(monitoring))

    def cancel(group, user):
        if group == 11:
            raise lco.LcoError("Portal refused second leg", status=403)
        return {"state": "CANCELED"}

    monkeypatch.setattr(lco, "cancel_requestgroup", cancel)
    page.problems.allowed.append("HTTP 403: POST /api/lco/monitored-requests/102/cancel")
    page.goto("/lco/schedule")
    page.locator('[data-cancel-monitor="101"]').click()
    page.click("#message-modal-ok")
    expect(page.locator("#message-modal-text")).to_contain_text("other relay leg")
    page.click("#message-modal-ok")
    expect(page.locator("#message-modal-text")).to_contain_text("First leg canceled")
    page.click("#message-modal-ok")
    expect(page.locator('[data-cancel-monitor="101"]')).to_have_count(0)
    expect(page.locator('[data-cancel-monitor="102"]')).to_be_visible()


def test_telescope_opens_ingested_dataset(page, monitoring):
    with sqlite3.connect(monitoring) as conn:
        filename = conn.execute(
            "SELECT filename FROM frames WHERE instrument='muscat3' AND obsdate='261001' AND object='TOI-1000' LIMIT 1"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO lco_observation_frames(request_id,frame_id,filename,instrument,obsdate,state,metadata_json,updated_at) "
            "VALUES (101,'1',?,'muscat3','261001','ingested','{}',0)",
            (filename + ".fits.fz",),
        )
        conn.execute("UPDATE lco_observation_requests SET monitor_state='complete'")
    page.goto("/lco/schedule")
    with page.expect_navigation():
        page.locator('#monitor-body a[aria-label^="Photometry:"]').first.click()
    expect(page.locator("#sel-inst")).to_have_value("muscat3")
    expect(page.locator("#sel-date")).to_have_value("261001")
    expect(page.locator("#sel-target")).to_have_value("TOI-1000")


def test_cancel_one_relay_leg(page, monitoring, monkeypatch):
    result, payload = _submission(request_id=102, requestgroup_id=11)
    lco_monitor.record_submission(result, payload, "admin", path=str(monitoring))
    lco_monitor.link_relay(10, 11, path=str(monitoring))
    calls = []
    monkeypatch.setattr(lco, "cancel_requestgroup", lambda group, user: calls.append(group))
    page.goto("/lco/schedule")
    page.locator('[data-cancel-monitor="101"]').click()
    page.click("#message-modal-ok")
    expect(page.locator("#message-modal-cancel")).to_have_text("Only this leg")
    page.click("#message-modal-cancel")
    expect(page.locator('[data-cancel-monitor="101"]')).to_have_count(0)
    expect(page.locator('[data-cancel-monitor="102"]')).to_be_visible()
    assert calls == [10]
