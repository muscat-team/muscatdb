"""The Jobs page filter chips, clicked in headless Chromium.

The chip handlers in jobs.html only change DOM state (which chips are
highlighted, whether "Clear filter" shows, which sections are displayed), so
they are exercised in a real browser rather than by pulling functions out for
node. The page is rendered with the app's Jinja env from synthetic jobs; static
files come from the package and the live poll gets an empty response, so only
the chip logic runs. Skips when Playwright or its Chromium is not installed.
"""

from __future__ import annotations

import json
import mimetypes
import time
from pathlib import Path

import pytest

from muscat_db import web

sync_api = pytest.importorskip("playwright.sync_api")

STATIC = Path(web.__file__).parent / "static"
STATES = ("running", "pending", "done", "error", "cancelled")
PAGE_URL = "http://muscat.test/jobs"


def _job(type_: str, state: str, inst: str, target: str, started: float) -> dict:
    date = "261020"
    return dict(
        key=f"{type_}:{inst}/{date}/{target}/r1", type=type_, state=state, inst=inst,
        date=date, target=target, run_id="r1", run_name="r1", started_at=started,
        elapsed=120, user_name="alice", stored=True, can_delete=True, can_cancel=True,
        details="", run_type="", error_desc="", photometry_url="", obslog_url="",
        action_inst="", action_date="", can_run_dataset_action=False,
    )


def _jobs() -> list[dict]:
    now = time.time()
    return [
        _job("photometry", "done", "muscat3", "PHOT-DONE", now),
        _job("photometry", "error", "muscat4", "PHOT-ERR", now),
        _job("photometry", "running", "muscat3", "PHOT-RUN", now),
        _job("transit_fit", "done", "muscat4", "FIT-DONE", now),
        _job("lco_archive_download", "done", "sinistro", "LCO-DONE", now),
    ]


def _render(jobs: list[dict]) -> str:
    counts = {s: sum(j["state"] == s for j in jobs) for s in STATES}
    return web.jinja.get_template("jobs.html").render(
        jobs=jobs, counts=counts, is_admin=True, current_user="alice"
    )


def _serve(html: str):
    def handle(route):
        path = route.request.url.split("://", 1)[1].split("/", 1)[1].split("?")[0]
        if path == "jobs":
            return route.fulfill(body=html, content_type="text/html")
        if path.startswith("static/"):
            f = STATIC / path[len("static/"):]
            if f.is_file():
                kind = mimetypes.guess_type(f.name)[0] or "text/plain"
                return route.fulfill(body=f.read_bytes(), content_type=kind)
        if path == "api/jobs/status":
            return route.fulfill(
                body=json.dumps({"finished": {}, "running": []}),
                content_type="application/json",
            )
        return route.fulfill(status=204, body="")

    return handle


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except sync_api.Error as exc:
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        yield b
        b.close()


def _open(browser, jobs: list[dict]):
    page = browser.new_context().new_page()
    page.route("**/*", _serve(_render(jobs)))
    page.goto(PAGE_URL)
    page.wait_for_function("document.getElementById('search-status').textContent !== ''")
    return page


@pytest.fixture
def page(browser):
    p = _open(browser, _jobs())
    yield p
    p.context.close()


def _active_chips(page) -> set[str]:
    return set(page.eval_on_selector_all(
        ".job-count.active",
        "els => els.map(e => e.dataset.status || e.dataset.type || e.dataset.inst || e.dataset.time)",
    ))


def _visible_sections(page) -> set[str]:
    return set(page.eval_on_selector_all(
        ".job-section", "els => els.filter(s => s.style.display !== 'none').map(s => s.dataset.jobSection)"
    ))


def test_status_chip_keeps_other_chip_highlights(page):
    page.click('#type-summary [data-type="photometry"]')
    page.click('#inst-summary [data-inst="muscat3"]')
    page.click('#jobs-summary [data-status="running"]')
    assert _active_chips(page) == {"photometry", "muscat3", "running"}

    page.click('#jobs-summary [data-status="running"]')
    assert _active_chips(page) == {"photometry", "muscat3"}


@pytest.mark.parametrize("chip", [
    '#type-summary [data-type="photometry"]',
    '#inst-summary [data-inst="muscat3"]',
    '#time-summary [data-time="recent"]',
])
def test_clear_filter_shows_for_every_chip_group(page, chip):
    clear = page.locator("#clear-filter")
    assert clear.is_hidden()
    page.click(chip)
    assert clear.is_visible()
    page.click(chip)
    assert clear.is_hidden()


def test_filter_hiding_every_archive_row_hides_the_archive_section(page):
    page.click('#jobs-summary [data-status="error"]')
    assert _visible_sections(page) == {"photometry"}

    page.click("#clear-filter")
    assert "lco_archive_download" in _visible_sections(page)
    assert page.locator('tr.empty-job-row[data-empty-for="lco_archive_download"]').is_hidden()


@pytest.mark.parametrize("chip", [
    None,
    '#type-summary [data-type="lco_archive_download"]',
    '#jobs-summary [data-status="error"]',
])
def test_archive_section_says_nothing_tracked_when_it_has_no_jobs(browser, chip):
    page = _open(browser, [j for j in _jobs() if j["type"] != "lco_archive_download"])
    try:
        if chip:
            page.click(chip)
        assert "lco_archive_download" in _visible_sections(page)
        assert page.locator('tr.empty-job-row[data-empty-for="lco_archive_download"]').is_visible()
    finally:
        page.context.close()
