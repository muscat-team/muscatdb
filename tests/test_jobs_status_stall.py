"""Job-status polling must not be able to stall the server.

Production stalled twice on 2026-11-07. Every Jobs-page poll rescanned the
whole transit-fit output tree for fits run outside the web UI (about 1.2 s per
call), and a burst of identical polls each repeated that work, until they held
every worker thread. Requests queued behind them, including nginx's login
check, timed out and browsers got 500s.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from muscat_db import transit_fit, web


def _fit_row(key: str) -> dict:
    return dict(key=key, type="transit_fit", inst="muscat3", date="261020", target="T",
                state="done", elapsed=0, started_at=0)


@pytest.fixture
def scanner(monkeypatch):
    """A disk scan that records its calls and serves whatever `found` holds."""
    calls: list[set] = []
    found: list[dict] = []

    def scan(existing):
        calls.append(set(existing))
        return [dict(r) for r in found]

    monkeypatch.setattr(transit_fit, "_discover_orphan_fits", scan)
    transit_fit.invalidate_orphan_fits()
    yield calls, found
    transit_fit.invalidate_orphan_fits()


@pytest.fixture
def no_store_rows(monkeypatch):
    monkeypatch.setattr(web, "_jobs_with_lco_archive_rows", lambda: [])
    monkeypatch.setattr(web, "_visible_jobs", lambda rows: rows)


def test_status_polls_do_not_rescan_the_fit_tree(scanner, no_store_rows):
    calls, _ = scanner
    client = TestClient(web.app)
    for _ in range(5):
        assert client.post("/api/jobs/status", json={"watch": []}).status_code == 200
    assert len(calls) == 1, f"{len(calls)} disk scans for 5 polls"


def test_a_background_refresh_picks_up_a_fit_run_outside_the_web_ui(scanner, no_store_rows):
    _, found = scanner
    client = TestClient(web.app)
    assert client.get("/api/jobs/status").json()["counts"]["done"] == 0
    found.append(_fit_row("transit_fit:muscat3/261020/T/cli-run"))
    assert client.get("/api/jobs/status").json()["counts"]["done"] == 0  # not rescanned per poll
    transit_fit.refresh_orphan_fits()
    assert client.get("/api/jobs/status").json()["counts"]["done"] == 1


def test_a_stale_snapshot_is_rescanned_when_the_background_refresh_stops(
        scanner, no_store_rows, monkeypatch):
    calls, found = scanner
    assert transit_fit.orphan_fits(set()) == []
    found.append(_fit_row("transit_fit:muscat3/261020/T/cli-run"))
    monkeypatch.setattr(transit_fit, "_ORPHAN_SCAN_MAX_AGE_S", 0.0)
    assert [r["key"] for r in transit_fit.orphan_fits(set())] == ["transit_fit:muscat3/261020/T/cli-run"]
    assert len(calls) == 2


def test_rows_already_in_the_job_store_are_not_reported_as_orphans(scanner):
    _, found = scanner
    found.extend([_fit_row("transit_fit:a"), _fit_row("transit_fit:b")])
    assert [r["key"] for r in transit_fit.orphan_fits({"transit_fit:a"})] == ["transit_fit:b"]


def test_callers_get_their_own_copies_of_orphan_rows(scanner):
    _, found = scanner
    found.append(_fit_row("transit_fit:a"))
    transit_fit.orphan_fits(set())[0]["elapsed"] = 999
    assert transit_fit.orphan_fits(set())[0]["elapsed"] == 0


def test_swapping_the_scanner_takes_effect_at_once(scanner, monkeypatch):
    """The static-site export swaps in an empty scanner so live runs never reach
    the public snapshot; a cached scan from the real scanner must not leak."""
    _, found = scanner
    found.append(_fit_row("transit_fit:live"))
    assert transit_fit.orphan_fits(set())
    monkeypatch.setattr(transit_fit, "_discover_orphan_fits", lambda _existing: [])
    assert transit_fit.orphan_fits(set()) == []


def test_deleting_a_fit_drops_it_from_the_snapshot(scanner, monkeypatch, tmp_path):
    _, found = scanner
    monkeypatch.setenv("MUSCAT_TIMER_DIR", str(tmp_path))
    run = tmp_path / "muscat3" / "261020" / "T" / "cli-run"
    (run / "out").mkdir(parents=True)
    found.append(_fit_row("transit_fit:muscat3/261020/T/cli-run"))
    assert transit_fit.orphan_fits(set())
    found.clear()
    assert transit_fit.delete_fit("muscat3", "261020", "T", run_id="cli-run")["ok"]
    assert transit_fit.orphan_fits(set()) == []


def test_concurrent_status_polls_share_one_job_list(scanner, monkeypatch):
    """A burst of identical polls (many open tabs, or tabs still running the
    old every-3-seconds poller) must cost one computation, not one each."""
    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow_rows():
        calls.append(1)
        started.set()
        release.wait(5)
        return [dict(_fit_row("transit_fit:x"), state="running")]

    monkeypatch.setattr(web, "_jobs_with_lco_archive_rows", slow_rows)
    monkeypatch.setattr(web, "_visible_jobs", lambda rows: rows)
    results = []

    def poll():
        results.append(web.jobs_status())

    threads = [threading.Thread(target=poll) for _ in range(8)]
    threads[0].start()
    assert started.wait(5)
    for t in threads[1:]:
        t.start()
    time.sleep(0.3)  # let the other polls arrive while the first is still computing
    release.set()
    for t in threads:
        t.join(10)
    assert len(results) == 8
    assert all(r["counts"]["running"] == 1 for r in results)
    assert len(calls) == 1, f"{len(calls)} job-list computations for 8 concurrent polls"


def test_a_poll_after_a_job_changes_sees_the_change(scanner, monkeypatch):
    """Sharing is only between polls already in progress together: nothing is
    kept afterwards, so a later poll never gets a stale job list."""
    job = dict(_fit_row("transit_fit:x"), state="running")
    monkeypatch.setattr(web, "_jobs_with_lco_archive_rows", lambda: [job])
    monkeypatch.setattr(web, "_visible_jobs", lambda rows: rows)
    assert web.jobs_status()["counts"]["running"] == 1
    job["state"] = "done"
    assert web.jobs_status()["counts"] == {"running": 0, "done": 1, "error": 0, "cancelled": 0, "pending": 0}


def test_jobs_page_changes_to_rows_do_not_leak_into_shared_rows(scanner, monkeypatch):
    rows = [dict(_fit_row("transit_fit:x"))]
    monkeypatch.setattr(web, "_jobs_with_lco_archive_rows", lambda: rows)
    web._all_job_rows()[0]["elapsed"] = 999
    assert rows[0]["elapsed"] == 0
