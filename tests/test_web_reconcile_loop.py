"""Tests for web.py's background job-reconciliation loop
(``_job_reconciliation_loop``), including instant dispatch (architecture
issue #51, "Signalling & live logs").

No pytest-asyncio in this project; follows tests/test_proxy.py's house
pattern of driving a coroutine with a bare ``asyncio.run(scenario())``.

The loop clamps its interval to a 0.5s floor
(``max(0.5, MUSCAT_JOB_RECONCILE_INTERVAL_S)``) regardless of how low the env
var is set, so every scenario here waits to observe another pass rather than
racing a wall-clock window sized just above that floor (issue #181: the old
fixed 0.6s window left ~0.1s of margin for a slow CI runner to fit a second
pass into, and flaked there).
"""

from __future__ import annotations

import asyncio

import muscat_db.web as web


async def _run_until(calls: list, at_least: int, *, timeout: float = 5.0) -> None:
    """Drive ``_job_reconciliation_loop`` until ``calls`` has recorded at
    least ``at_least`` passes, then cancel it.

    Waiting on an observed pass count instead of a fixed duration removes the
    time-race: the loop never has to *fit N passes into a deadline*, only to
    complete them, and a slow runner gets the full ``timeout`` backstop
    (~100x the margin of the old 0.6s window) instead of failing. A burn-in
    that times out raises ``TimeoutError`` from inside ``asyncio.run``, so a
    genuinely stuck loop still fails loudly rather than passing vacuously.
    """
    task = asyncio.create_task(web._job_reconciliation_loop())
    try:
        async with asyncio.timeout(timeout):
            while len(calls) < at_least:
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def test_loop_sleeps_on_the_event_loop_when_notify_is_disabled(monkeypatch):
    """With MUSCAT_JOB_NOTIFY unset (the default), the loop must run the
    exact line it always has -- a cancellable asyncio.sleep -- never the
    thread-blocking wait_for_work_or_sleep helper. Proven two ways: a second
    pass actually runs after the real ~0.5s sleep elapses, and the helper is
    never even called."""
    monkeypatch.setenv("MUSCAT_JOB_RECONCILE_INTERVAL_S", "0.01")
    monkeypatch.setattr(web, "notify_enabled", lambda: False)
    waits: list[float] = []
    monkeypatch.setattr(web, "wait_for_work_or_sleep", lambda t: waits.append(t) or False)
    calls: list[int] = []
    monkeypatch.setattr(web, "_reconcile_all_jobs", lambda: calls.append(1))

    asyncio.run(_run_until(calls, at_least=2))
    assert len(calls) >= 2
    assert waits == []


def test_loop_waits_on_the_store_signal_when_notify_is_enabled(monkeypatch):
    """With MUSCAT_JOB_NOTIFY=1, the loop's wait goes through
    wait_for_work_or_sleep (run off the event loop via asyncio.to_thread,
    since the store's wait_for_work is a blocking call), called with the
    resolved (clamped) interval every pass."""
    monkeypatch.setenv("MUSCAT_JOB_RECONCILE_INTERVAL_S", "0.01")
    monkeypatch.setattr(web, "notify_enabled", lambda: True)
    waits: list[float] = []
    monkeypatch.setattr(web, "wait_for_work_or_sleep", lambda t: waits.append(t) or False)
    calls: list[int] = []
    monkeypatch.setattr(web, "_reconcile_all_jobs", lambda: calls.append(1))

    asyncio.run(_run_until(calls, at_least=1))
    assert len(calls) >= 1
    assert waits and all(w == 0.5 for w in waits)  # clamped floor: max(0.5, 0.01)


def test_loop_keeps_running_when_a_pass_raises(monkeypatch):
    monkeypatch.setenv("MUSCAT_JOB_RECONCILE_INTERVAL_S", "0.01")
    monkeypatch.setattr(web, "notify_enabled", lambda: False)
    calls: list[int] = []

    def boom():
        calls.append(1)
        raise RuntimeError("simulated reconciliation failure")

    monkeypatch.setattr(web, "_reconcile_all_jobs", boom)

    asyncio.run(_run_until(calls, at_least=2))  # must not raise -- one bad pass never kills the loop
    assert len(calls) >= 2