"""A cancelled job must never regress to ``cancelling`` in the jobs table
(issue #182, finding 4).

``cancel_run`` durably writes ``cancelled``. Before the fix, the next
``sync_jobs`` pass saw the still-dying process as ``cancelling`` and wrote that
back over it; a restart inside that window left a row no reconcile pass ever
resolves (orphan reconcile reads only ``running`` rows, the drain only
``pending``), so the Jobs page showed a live job forever.
"""

from __future__ import annotations

import os
import sqlite3
import time

import pytest

from muscat_db import database, jobs
from muscat_db import photometry as phot
from muscat_db import transit_fit as fit
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


def _row(key: str) -> dict:
    return next(j for j in database.get_persisted_jobs() if j["key"] == key)


def _save_cancelled(type_: str, **kw) -> None:
    get_job_store().save(
        type_=type_, inst=INST, date=DATE, target=TARGET, state="cancelled",
        returncode=jobs.CANCELLED_RC, elapsed=0, started_at=time.time(),
        error_desc="Cancelled by user", **kw,
    )


# -- unit ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("live", "rc", "expected"),
    [
        ("running", None, ("running", None)),
        ("finalizing", 0, ("running", None)),
        ("cancelling", None, ("cancelled", jobs.CANCELLED_RC)),
        ("cancelled", -15, ("cancelled", -15)),
        ("done", 0, ("done", 0)),
        ("error", 1, ("error", 1)),
    ],
)
def test_persisted_state(live, rc, expected):
    assert jobs.persisted_state(live, rc) == expected


# -- photometry ---------------------------------------------------------------


@pytest.fixture
def phot_job(tmp_path, monkeypatch, jobs_db):
    rdir = tmp_path / INST / DATE
    rdir.mkdir(parents=True)
    monkeypatch.setattr(phot, "results_dir", lambda inst, date: rdir)
    log = phot._run_log_path(rdir, INST, DATE, TARGET)
    log.write_text("$ run_photometry\nINFO: started\n")
    key = phot.job_key(INST, DATE, TARGET)
    job = phot.Job(
        key=key, inst=INST, date=DATE, target=TARGET, cmd=["x"],
        proc=_FakeProc(), logf=open(log, "a"), log_path=log, run_type="full",
    )
    job.cancelled = True
    with phot._LOCK:
        phot._JOBS.clear()
        phot._JOBS[key] = job
    yield job, f"photometry:{key}"
    with phot._LOCK:
        for j in phot._JOBS.values():
            j.logf.close()
        phot._JOBS.clear()


def test_photometry_sync_keeps_cancelled_while_process_dies(phot_job):
    job, db_key = phot_job
    _save_cancelled("photometry")

    phot.sync_jobs()  # process still alive: live state is "cancelling"

    assert _row(db_key)["state"] == "cancelled"


def test_photometry_sync_records_real_exit_code_once_terminal(phot_job):
    job, db_key = phot_job
    _save_cancelled("photometry")
    phot.sync_jobs()

    job.proc._rc = -15
    phot.sync_jobs()

    row = _row(db_key)
    assert (row["state"], row["returncode"]) == ("cancelled", -15)


# -- transit fit --------------------------------------------------------------


@pytest.fixture
def fit_job(tmp_path, jobs_db):
    log = tmp_path / "timer-fit.log"
    log.write_text("$ timer-fit\nINFO: started\n")
    key = fit.fit_job_key(INST, DATE, TARGET)
    job = fit.TransitFitJob(
        key=key, inst=INST, date=DATE, target=TARGET, cmd=["x"],
        proc=_FakeProc(), logf=open(log, "a"), log_path=log, run_type="full",
    )
    job.cancelled = True
    with fit._FIT_LOCK:
        fit._FIT_JOBS.clear()
        fit._FIT_JOBS[key] = job
    yield job
    with fit._FIT_LOCK:
        for j in fit._FIT_JOBS.values():
            j.logf.close()
        fit._FIT_JOBS.clear()


def test_transit_fit_sync_keeps_cancelled_while_process_dies(fit_job):
    _save_cancelled("transit_fit")

    fit.sync_jobs()

    rows = [j for j in database.get_persisted_jobs() if j["type"] == "transit_fit"]
    assert [r["state"] for r in rows] == ["cancelled"]
