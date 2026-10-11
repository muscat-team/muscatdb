"""Cancelling a job that runs under another process (a fleet worker).

A job launched by ``muscat-db worker`` is tracked only in that worker's
in-memory registry (``_JOBS`` / ``_FIT_JOBS`` / ``_TTV_JOBS``), so the web
process cannot signal it. Cancelling from the Jobs page used to cancel only
queued rows and report "no job to cancel" for a running one (photometry,
transit_fit) or, worse, mark a still-running ttv_fit row ``cancelled``.

The web process now records a cancel request on the durable row
(``JobRepository.request_cancel``) and the owning instance's next
``sync_jobs`` pass acts on it with its own local cancel path.
"""

from __future__ import annotations

import os
import signal
import sqlite3

import pytest

from muscat_db import database, job_store
from muscat_db import photometry as phot
from muscat_db import transit_fit as fit
from muscat_db import ttv_fit as ttv
from muscat_db.job_store import get_job_store

INST = "muscat4"
DATE = "260101"
TARGET = "HIP67522"
OTHER_HOST = "muscat-ut5:4242:deadbeef"


class _FakeProc:
    def __init__(self, rc=None):
        self._rc = rc
        self.pid = os.getpid()

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        return self._rc

    def terminate(self):
        raise AssertionError("must signal the process group, not the test runner")


@pytest.fixture
def jobs_db(tmp_path, monkeypatch):
    path = tmp_path / "muscat.db"
    monkeypatch.setenv("MUSCAT_DB_PATH", str(path))
    conn = sqlite3.connect(str(path))
    conn.executescript(database.SCHEMA)
    conn.commit()
    conn.close()
    database.clear_all_caches()
    monkeypatch.setattr(database, "refresh_target_status", lambda *a, **k: None)
    return path


@pytest.fixture
def killed(monkeypatch):
    """Record process-group signals instead of delivering them."""
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    for mod in (phot, fit, ttv):
        monkeypatch.setattr(mod, "_kill_after", lambda proc: None)
    return sent


def _scenarios():
    return [
        pytest.param(
            "photometry",
            lambda: phot.cancel_run(INST, DATE, TARGET),
            phot.sync_jobs,
            id="photometry",
        ),
        pytest.param(
            "transit_fit",
            lambda: fit.cancel_fit(INST, DATE, TARGET),
            fit.sync_jobs,
            id="transit_fit",
        ),
        pytest.param(
            "ttv_fit",
            lambda: ttv.cancel_ttv_fit(TARGET),
            ttv.sync_jobs,
            id="ttv_fit",
        ),
    ]


def _inst_date(type_: str) -> tuple[str, str]:
    return ("_", "_") if type_ == "ttv_fit" else (INST, DATE)


def _save_running(type_: str, instance_id: str, started_at: float) -> None:
    inst, date = _inst_date(type_)
    get_job_store().save(
        type_=type_, inst=inst, date=date, target=TARGET, state="running",
        returncode=None, elapsed=0, started_at=started_at, run_type="full",
        owner="worker", instance_id=instance_id,
    )


def _row(type_: str) -> dict:
    return next(j for j in database.get_persisted_jobs() if j["type"] == type_)


@pytest.mark.parametrize(("type_", "cancel", "sync"), _scenarios())
def test_web_cancel_of_a_job_on_another_instance_requests_instead_of_killing(
    type_, cancel, sync, jobs_db, killed
):
    _save_running(type_, OTHER_HOST, started_at=100.0)

    result = cancel()

    assert result["ok"] is True
    assert result.get("requested") is True
    assert killed == []
    # still running: only the owning worker may declare it cancelled
    assert _row(type_)["state"] == "running"
    assert [r["target"] for r in get_job_store().cancel_requested(type_, OTHER_HOST)] == [TARGET]


@pytest.mark.parametrize(("type_", "cancel", "sync"), _scenarios())
def test_web_cancel_with_no_job_at_all_still_reports_nothing_to_cancel(
    type_, cancel, sync, jobs_db, killed
):
    result = cancel()

    assert result["ok"] is False
    assert killed == []


def test_photometry_worker_acts_on_a_pending_cancel_request(jobs_db, killed, tmp_path, monkeypatch):
    rdir = tmp_path / INST / DATE
    rdir.mkdir(parents=True)
    monkeypatch.setattr(phot, "results_dir", lambda inst, date: rdir)
    log = phot._run_log_path(rdir, INST, DATE, TARGET)
    log.write_text("$ run_photometry\nINFO: working\n")
    key = phot.job_key(INST, DATE, TARGET)
    job = phot.Job(
        key=key, inst=INST, date=DATE, target=TARGET, cmd=["x"],
        proc=_FakeProc(rc=None), logf=open(log, "a"), log_path=log, run_type="full",
    )
    with phot._LOCK:
        phot._JOBS.clear()
        phot._JOBS[key] = job
    try:
        _save_running("photometry", job_store.current_instance_id(), job.started_at)
        assert get_job_store().request_cancel(f"photometry:{key}") is True

        phot.sync_jobs()

        assert job.cancelled is True
        assert killed and killed[0][1] == signal.SIGTERM
        row = _row("photometry")
        assert row["state"] == "cancelled"
        assert row["error_desc"] == "Cancelled by user"
    finally:
        with phot._LOCK:
            job.logf.close()
            phot._JOBS.clear()


def test_worker_pass_without_a_request_leaves_its_job_alone(jobs_db, killed, tmp_path, monkeypatch):
    rdir = tmp_path / INST / DATE
    rdir.mkdir(parents=True)
    monkeypatch.setattr(phot, "results_dir", lambda inst, date: rdir)
    log = phot._run_log_path(rdir, INST, DATE, TARGET)
    log.write_text("$ run_photometry\nINFO: working\n")
    key = phot.job_key(INST, DATE, TARGET)
    job = phot.Job(
        key=key, inst=INST, date=DATE, target=TARGET, cmd=["x"],
        proc=_FakeProc(rc=None), logf=open(log, "a"), log_path=log, run_type="full",
    )
    with phot._LOCK:
        phot._JOBS.clear()
        phot._JOBS[key] = job
    try:
        _save_running("photometry", job_store.current_instance_id(), job.started_at)

        phot.sync_jobs()

        assert job.cancelled is False
        assert killed == []
        assert _row("photometry")["state"] == "running"
    finally:
        with phot._LOCK:
            job.logf.close()
            phot._JOBS.clear()


def test_a_failing_cancel_never_aborts_the_reconciliation_pass():
    from muscat_db import jobs

    class _Store:
        def cancel_requested(self, type_, instance_id):
            return [{"key": "photometry:a"}, {"key": "photometry:b"}]

    seen = []

    def cancel_row(row):
        seen.append(row["key"])
        raise RuntimeError("boom")

    jobs.apply_cancel_requests(_Store(), "photometry", "me", cancel_row)

    assert seen == ["photometry:a", "photometry:b"]


def test_an_unreadable_request_list_never_aborts_the_reconciliation_pass():
    from muscat_db import jobs

    class _Store:
        def cancel_requested(self, type_, instance_id):
            raise OSError("db locked")

    jobs.apply_cancel_requests(_Store(), "photometry", "me", lambda row: None)
