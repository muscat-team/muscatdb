"""P2 single-host proof (notes/DEPLOYMENT.md "Next steps" #1; architecture
issue #51): "run one `muscatdb worker` on ut2 against the SQLite control
plane; prove claim / lease / finalize / cancel across the web<->worker
boundary."

The pieces already have unit coverage in isolation -- owner/instance
reconciliation of already-running rows in tests/test_worker_job_ownership.py,
reclaim-with-attempt-limit in tests/test_job_reconcile_retry.py, the atomic
claim_slot race itself in tests/_job_store_contract.py (run against both the
SQLite and Postgres backends). Nothing before this drove one job through the
*whole* lifecycle -- pending -> claimed by a standalone worker process ->
leased/heartbeating -> finalized -- while also checking what a *different*
process (the web server) can and cannot still do to it once the worker owns
it. That's what this module proves, plus the one place the boundary still
has a real gap.

Like the rest of this suite, "the worker process" and "the web process" are
simulated in one Python process rather than two real OS processes: every
cross-process guarantee here comes from job_store.py's atomic SQL
(claim_slot's INSERT..WHERE, the owner/instance_id tags, the heartbeat
column) rather than from memory isolation, so driving both roles from one
interpreter proves the same thing a second real `muscat-db worker` OS
process would -- see test_worker_job_ownership.py's module docstring for the
same reasoning. ``worker.run()`` below is the exact function `muscat-db
worker` (src/muscat_db/cli.py) invokes; nothing about the proof is
CLI-specific.
"""

from __future__ import annotations

import os
import time

import pytest

from muscat_db import job_store, worker
from muscat_db import photometry as phot

INST = "qhy600"
DATE = "260101"


class _FakeProc:
    def __init__(self, pid: int = 5150):
        self.pid = pid
        self.returncode: int | None = None

    def poll(self):
        return self.returncode


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "muscat.db"))
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path / "prose_out"))
    # Snapshotting "web" (the pre-existing default) here, not "worker", so
    # monkeypatch's teardown restores it regardless of which role each test
    # below switches to via worker.run() -> job_store.set_owner().
    monkeypatch.setattr(job_store, "_OWNER", "web")
    with phot._LOCK:
        phot._JOBS.clear()
    yield
    with phot._LOCK:
        for job in phot._JOBS.values():
            try:
                job.logf.close()
            except OSError:
                pass
        phot._JOBS.clear()


def _db_key(target: str, run_id: str = "default") -> str:
    return f"photometry:{phot.job_key(INST, DATE, target, run_id)}"


def _row(store, key: str) -> dict:
    return next(j for j in store.all() if j["key"] == key)


def _enqueue_pending(store, target: str, run_id: str = "default") -> None:
    store.save(
        type_="photometry", inst=INST, date=DATE, target=target,
        state="pending", returncode=None, elapsed=0, started_at=time.time(),
        run_type="full", run_id=run_id,
        params=f'{{"test_run": false, "options": {{}}, "run_id": "{run_id}"}}',
    )


class TestClaimLeaseFinalize:
    """One job's full lifecycle through a standalone worker process."""

    def test_pending_job_is_claimed_leased_and_finalized_by_the_worker(self, monkeypatch):
        store = job_store.get_job_store()
        target = "TOI-P2-CLAIM"
        key = _db_key(target)
        _enqueue_pending(store, target)
        assert _row(store, key)["state"] == "pending"

        proc = _FakeProc()
        monkeypatch.setattr(phot.subprocess, "Popen", lambda *a, **k: proc)

        # CLAIM: a standalone worker process (worker.run is muscat-db
        # worker's own entrypoint function) drains the pending queue and
        # launches it.
        worker.run("photometry", once=True)
        row = _row(store, key)
        assert row["state"] == "running"
        assert row["owner"] == "worker", "a job the worker launches must be tagged owner=worker, not web"
        assert row["instance_id"], "a lease identity must be recorded at claim time"
        first_heartbeat = row["heartbeat_at"]

        # LEASE: a later pass while still running renews the heartbeat
        # instead of treating the row as orphaned -- it is not; the worker
        # still holds it in its own in-memory registry.
        time.sleep(0.02)
        worker.run("photometry", once=True)
        row = _row(store, key)
        assert row["state"] == "running"
        assert row["heartbeat_at"] >= first_heartbeat

        # FINALIZE: the subprocess exits; once its log goes quiescent past
        # the grace window, the next pass resolves it to a terminal state
        # through the same finalizing machine the web process uses.
        monkeypatch.setattr(phot, "_FINALIZE_GRACE_S", 0.05)
        proc.returncode = 0
        time.sleep(0.1)
        worker.run("photometry", once=True)
        row = _row(store, key)
        assert row["state"] == "done"
        assert row["returncode"] == 0


class TestCancelBoundary:
    """What "cancel across the boundary" actually covers today."""

    def test_pending_job_can_be_cancelled_before_the_worker_claims_it(self):
        """The part of the boundary that already works: a job still sitting
        in the durable queue only touches the jobs table, so either process
        can cancel it before any worker claims it (worker.py's module
        docstring: "Jobs still queued (not yet claimed) cancel fine either
        way")."""
        store = job_store.get_job_store()
        target = "TOI-P2-CANCEL-PENDING"
        key = _db_key(target)
        _enqueue_pending(store, target)

        result = phot.cancel_run(INST, DATE, target, "default")

        assert result == {"ok": True, "key": phot.job_key(INST, DATE, target, "default")}
        assert _row(store, key)["state"] == "cancelled"

    def test_worker_claimed_running_job_is_cancelled_from_the_web_process(self, monkeypatch):
        """Once a worker has claimed and launched a job it lives only in the
        *worker* process's in-memory _JOBS registry, so a web process cannot
        signal it. The web process records a cancel request on the durable
        row instead; the worker's next pass acts on it with its own local
        cancel path and records the terminal state."""
        store = job_store.get_job_store()
        target = "TOI-P2-CANCEL-RUNNING"
        key = _db_key(target)
        _enqueue_pending(store, target)
        monkeypatch.setattr(phot.subprocess, "Popen", lambda *a, **k: _FakeProc())
        worker.run("photometry", once=True)
        assert _row(store, key)["state"] == "running"

        signalled = []
        monkeypatch.setattr(os, "getpgid", lambda pid: pid)
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: signalled.append((pgid, sig)))
        monkeypatch.setattr(phot, "_kill_after", lambda proc: None)

        # The web process is a *different* instance: it never holds the job in
        # its own registry and must not mark the row cancelled itself.
        with monkeypatch.context() as web:
            web.setattr(phot, "current_instance_id", lambda: "web-host:1:web")
            mem_key = phot.job_key(INST, DATE, target, "default")
            with phot._LOCK:
                worker_job = phot._JOBS.pop(mem_key)
            result = phot.cancel_run(INST, DATE, target, "default")
        with phot._LOCK:
            phot._JOBS[mem_key] = worker_job

        assert result == {"ok": True, "key": mem_key, "requested": True}
        assert _row(store, key)["state"] == "running", "only the owning worker records the outcome"
        assert signalled == []

        worker.run("photometry", once=True)

        assert signalled and signalled[0][0] == 5150
        assert _row(store, key)["state"] == "cancelled"
