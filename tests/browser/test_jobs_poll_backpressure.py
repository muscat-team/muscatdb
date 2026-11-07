"""The /jobs status poll must not pile up requests on a slow server.

Each POST /api/jobs/status runs the full Jobs-page reconciliation, about 2 s
of CPU on production. A fixed setInterval kept firing every 3 s whether or
not the previous poll had answered, so one slow response made every open
Jobs tab stack concurrent requests until the worker thread pool was full and
even /healthz and the Cloudflare auth subrequest queued behind them (nginx
then answered 500).
"""

from __future__ import annotations

POLL_URL = '**/api/jobs/status'
# Longer than two poll intervals (3 s), so a fixed interval would have fired
# at least twice in this window.
WAIT_MS = 7000


def _hold_status_polls(page):
    """Capture every status poll without answering it."""
    held = []
    page.route(POLL_URL, lambda route: held.append(route))
    return held


def _empty_status():
    return {'finished': {}, 'running': {}, 'counts': {}}


def test_no_new_poll_is_sent_while_one_is_still_pending(page):
    held = _hold_status_polls(page)
    page.goto('/jobs')
    page.wait_for_timeout(WAIT_MS)
    assert len(held) == 1, f'{len(held)} overlapping status polls were sent'

    # Once the slow response lands, polling resumes.
    held[0].fulfill(json=_empty_status())
    page.wait_for_timeout(WAIT_MS)
    assert len(held) == 2


def test_hidden_tab_stops_polling_and_resumes_when_visible(page):
    held = []

    def answer(route):
        held.append(route)
        route.fulfill(json=_empty_status())

    page.route(POLL_URL, answer)
    page.goto('/jobs')
    page.evaluate("""() => {
        Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
        Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'});
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    # Let any poll that was already in flight or due settle first.
    page.wait_for_timeout(3500)
    before = len(held)
    page.wait_for_timeout(WAIT_MS)
    assert len(held) == before, 'a hidden Jobs tab kept polling'

    page.evaluate("""() => {
        Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
        Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'visible'});
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    page.wait_for_timeout(1000)
    assert len(held) > before, 'polling did not resume immediately when the tab became visible'
