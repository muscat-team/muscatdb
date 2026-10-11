"""A queued re-run must not be overwritten by the stale in-memory job it replaces.

The web process keeps a finished job in its registry (``_JOBS`` / ``_FIT_JOBS`` /
``_TTV_JOBS``) so the page can keep showing its log. A test run and a full run
share one job key, so clicking "Run Full Reduction" after a test run enqueues a
``pending`` row under the *same* key. The next ``sync_jobs`` pass used to see
registry state ``done`` != row state ``pending`` and write ``done`` back over the
queued row: the worker never saw it and no full run ever started. Observed on
the PostgreSQL control plane (issue #51), where ``MUSCAT_WORKER_MAX_SLOTS=0``
sends every full run through the queue, but the code path is backend-independent.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from muscat_db import database
from muscat_db import photometry as phot
from muscat_db import transit_fit as fit
from muscat_db import ttv_fit as ttv
from muscat_db.job_store import get_job_store

INST = "muscat4"
DATE = "260101"
TARGET = "HIP67522"


class _FakeProc:
    def __init__(self, rc=None):
        self._rc = rc
        self.pid = os.getpid()

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        return self._rc


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


@pytest.fixture(autouse=True)
def _no_finalize_grace(monkeypatch):
    # A finished fake process would otherwise sit in the non-terminal
    # "finalizing" state until its log had been quiet for the grace window.
    for mod in (phot, fit, ttv):
        monkeypatch.setattr(mod, "_FINALIZE_GRACE_S", 0.0)
        monkeypatch.setattr(mod, "_FINALIZE_GRACE_TERMINAL_S", 0.0)
        # No claimable slot: keep the queue drain from promoting the pending row
        # in the same pass, so the row's own state is what the test observes.
        monkeypatch.setattr(mod, "_MAX_FULL_JOBS", 0)


def _rows(type_: str) -> list[dict]:
    return [j for j in database.get_persisted_jobs() if j["type"] == type_]


def _queue(type_: str, started_at: float, **kw) -> None:
    get_job_store().enqueue(
        type_=type_, inst=kw.pop("inst", INST), date=kw.pop("date", DATE), target=TARGET,
        started_at=started_at, run_type="full", **kw,
    )


# -- photometry ---------------------------------------------------------------


@pytest.fixture
def phot_job(tmp_path, monkeypatch, jobs_db):
    rdir = tmp_path / INST / DATE
    rdir.mkdir(parents=True)
    monkeypatch.setattr(phot, "results_dir", lambda inst, date: rdir)
    log = phot._run_log_path(rdir, INST, DATE, TARGET)
    log.write_text("$ run_photometry\nINFO: photometry SUCCEEDED\n")
    key = phot.job_key(INST, DATE, TARGET)
    job = phot.Job(
        key=key, inst=INST, date=DATE, target=TARGET, cmd=["x"],
        proc=_FakeProc(rc=0), logf=open(log, "a"), log_path=log, run_type="test",
    )
    with phot._LOCK:
        phot._JOBS.clear()
        phot._JOBS[key] = job
    yield job
    with phot._LOCK:
        for j in phot._JOBS.values():
            j.logf.close()
        phot._JOBS.clear()


def test_photometry_sync_keeps_requeued_full_run_pending(phot_job):
    _queue("photometry", phot_job.started_at + 5)

    phot.sync_jobs()

    assert [r["state"] for r in _rows("photometry")] == ["pending"]


def test_photometry_sync_still_persists_its_own_terminal_state(phot_job):
    """Control: with no newer queued row the finished job is recorded as before."""
    get_job_store().save(
        type_="photometry", inst=INST, date=DATE, target=TARGET, state="running",
        returncode=None, elapsed=0, started_at=phot_job.started_at, run_type="test",
    )

    phot.sync_jobs()

    assert [r["state"] for r in _rows("photometry")] == ["done"]


# -- transit fit --------------------------------------------------------------


@pytest.fixture
def fit_job(tmp_path, jobs_db):
    log = tmp_path / "timer-fit.log"
    log.write_text("$ timer-fit\nINFO: done\n")
    key = fit.fit_job_key(INST, DATE, TARGET)
    job = fit.TransitFitJob(
        key=key, inst=INST, date=DATE, target=TARGET, cmd=["x"],
        proc=_FakeProc(rc=0), logf=open(log, "a"), log_path=log, run_type="test",
    )
    with fit._FIT_LOCK:
        fit._FIT_JOBS.clear()
        fit._FIT_JOBS[key] = job
    yield job
    with fit._FIT_LOCK:
        for j in fit._FIT_JOBS.values():
            j.logf.close()
        fit._FIT_JOBS.clear()


def test_transit_fit_sync_keeps_requeued_full_run_pending(fit_job):
    _queue("transit_fit", fit_job.started_at + 5)

    fit.sync_jobs()

    assert [r["state"] for r in _rows("transit_fit")] == ["pending"]


# -- ttv fit ------------------------------------------------------------------


@pytest.fixture
def ttv_job(tmp_path, jobs_db):
    log = tmp_path / "harmonic.log"
    log.write_text("$ harmonic\nINFO: done\n")
    key = ttv.ttv_job_key(TARGET)
    job = ttv.TTVFitJob(
        key=key, inst="_", date="_", target=TARGET, cmd=["x"],
        proc=_FakeProc(rc=0), logf=open(log, "a"), log_path=log, run_type="test",
    )
    with ttv._TTV_LOCK:
        ttv._TTV_JOBS.clear()
        ttv._TTV_JOBS[key] = job
    yield job
    with ttv._TTV_LOCK:
        for j in ttv._TTV_JOBS.values():
            j.logf.close()
        ttv._TTV_JOBS.clear()


def test_ttv_fit_sync_keeps_requeued_run_pending(ttv_job):
    _queue("ttv_fit", ttv_job.started_at + 5, inst="_", date="_")

    ttv.sync_jobs()

    assert [r["state"] for r in _rows("ttv_fit")] == ["pending"]
