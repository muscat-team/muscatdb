"""Coverage for reclaim-with-attempt-limit (architecture issue #51 step 3).

Before this, every sync_jobs() gave an orphaned running row with no evidence
of completion a single terminal write ("Process lost (server restart)"), with
no retry -- see jobs.py's module docstring and the review comments on #167
that flagged it. This module tests the fix directly: jobs.next_reconcile_attempt
decides retry-vs-abandon, and each pipeline's orphan branch now (a) leaves a
row alone if the underlying detached subprocess is still alive (a pid file
still names a live process), (b) retries by requeuing (state="pending", so the
existing pending-drain loop relaunches it with its preserved params) while
attempts remain, and (c) abandons with a terminal error once the limit is hit.

Most cases here monkeypatch database.get_persisted_jobs/save_job directly
(the same technique test_photometry.py's
TestTransitFitJobs.test_sync_jobs_marks_invalid_pending_target_error uses) so
the orphan-reconciliation decision is exercised in isolation, without an
actual subprocess ever launching: store.pending() re-reads the same frozen
get_persisted_jobs() list, which never reports a "pending" row, so the
drain-and-launch loop that follows always finds nothing to do. The
attempts-passthrough-on-relaunch tests use the real sqlite-backed store
instead, with subprocess.Popen stubbed to a trivial success, since those
specifically need the drain loop to run.
"""

from __future__ import annotations

import os

import pytest

from muscat_db import job_store, jobs as job_lifecycle
from muscat_db import photometry as phot, transit_fit as fit, ttv_fit as ttv


class _FakeProc:
    def __init__(self, pid: int = 4242):
        self.pid = pid

    def poll(self):
        return None

    def wait(self, timeout=None):
        return None


def _capture_saves(monkeypatch):
    saved = []
    monkeypatch.setattr("muscat_db.database.save_job", lambda **kwargs: saved.append(kwargs))
    return saved


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    """Every test in this module must never touch the real muscat.db: even
    with database.get_persisted_jobs/save_job mocked, sync_jobs() also calls
    store.reconcile_slots() (and, for the relaunch tests, store.claim_slot()),
    both of which run raw SQL straight through database.get_conn() against
    whatever MUSCAT_DB_PATH resolves to -- unaffected by those two mocks."""
    monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "muscat.db"))


class TestPhotometryReconcileRetry:
    def _row(self, **overrides):
        row = {
            "key": "photometry:qhy600/260101/TOI-1234",
            "type": "photometry",
            "inst": "qhy600",
            "date": "260101",
            "target": "TOI-1234",
            "state": "running",
            "started_at": 100.0,
            "elapsed": 0,
            "owner": "",
            "instance_id": "",
            "heartbeat_at": 0.0,
            "attempts": 0,
            "params": "",
            "run_id": "",
        }
        row.update(overrides)
        return row

    def test_orphaned_with_no_evidence_retries_as_pending(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path / "prose_out"))
        monkeypatch.setattr(phot, "_JOBS", {})
        monkeypatch.setattr("muscat_db.database.get_persisted_jobs", lambda: [self._row(attempts=0)])
        saved = _capture_saves(monkeypatch)

        phot.sync_jobs()

        assert len(saved) == 1
        assert saved[0]["state"] == "pending"
        assert saved[0]["attempts"] == 1
        assert saved[0].get("error_desc") in (None, "")

    def test_orphaned_at_attempt_limit_is_abandoned(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path / "prose_out"))
        monkeypatch.setattr(job_lifecycle, "_MAX_RECONCILE_ATTEMPTS", 3)
        monkeypatch.setattr(phot, "_JOBS", {})
        monkeypatch.setattr("muscat_db.database.get_persisted_jobs", lambda: [self._row(attempts=2)])
        saved = _capture_saves(monkeypatch)

        phot.sync_jobs()

        assert len(saved) == 1
        assert saved[0]["state"] == "error"
        assert saved[0]["attempts"] == 3
        assert saved[0]["error_desc"] == "Process lost (server restart); gave up after 3 attempts"

    def test_orphaned_with_live_pid_file_is_left_alone(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
        monkeypatch.setattr(phot, "_JOBS", {})
        rdir = phot.run_output_dir("qhy600", "260101", "TOI-1234")
        rdir.mkdir(parents=True)
        (rdir / phot._PID_FILE_NAME).write_text(str(os.getpid()))
        monkeypatch.setattr("muscat_db.database.get_persisted_jobs", lambda: [self._row(attempts=1)])
        saved = _capture_saves(monkeypatch)

        phot.sync_jobs()

        assert saved == []  # untouched: the detached subprocess may yet finish

    def test_orphaned_with_success_marker_is_done_regardless_of_attempts(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
        monkeypatch.setattr(phot, "_JOBS", {})
        rdir = phot.run_output_dir("qhy600", "260101", "TOI-1234")
        rdir.mkdir(parents=True)
        log = phot._run_log_path(rdir, "qhy600", "260101", "TOI-1234")
        log.write_text(phot._finalize_config().success_marker + "\n")
        monkeypatch.setattr("muscat_db.database.get_persisted_jobs", lambda: [self._row(attempts=4)])
        saved = _capture_saves(monkeypatch)

        phot.sync_jobs()

        assert len(saved) == 1
        assert saved[0]["state"] == "done"


class TestTransitFitReconcileRetry:
    def _row(self, **overrides):
        row = {
            "key": "transit_fit:qhy600/260101/TOI-1234",
            "type": "transit_fit",
            "inst": "qhy600",
            "date": "260101",
            "target": "TOI-1234",
            "state": "running",
            "started_at": 100.0,
            "elapsed": 0,
            "owner": "",
            "instance_id": "",
            "heartbeat_at": 0.0,
            "attempts": 0,
            "params": "",
            "run_id": "",
        }
        row.update(overrides)
        return row

    def test_orphaned_with_no_evidence_retries_as_pending(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_TIMER_DIR", str(tmp_path / "timer_out"))
        monkeypatch.setattr(fit, "_FIT_JOBS", {})
        monkeypatch.setattr("muscat_db.database.get_persisted_jobs", lambda: [self._row(attempts=1)])
        saved = _capture_saves(monkeypatch)

        fit.sync_jobs()

        assert len(saved) == 1
        assert saved[0]["state"] == "pending"
        assert saved[0]["attempts"] == 2

    def test_orphaned_at_attempt_limit_is_abandoned(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_TIMER_DIR", str(tmp_path / "timer_out"))
        monkeypatch.setattr(job_lifecycle, "_MAX_RECONCILE_ATTEMPTS", 2)
        monkeypatch.setattr(fit, "_FIT_JOBS", {})
        monkeypatch.setattr("muscat_db.database.get_persisted_jobs", lambda: [self._row(attempts=1)])
        saved = _capture_saves(monkeypatch)

        fit.sync_jobs()

        assert len(saved) == 1
        assert saved[0]["state"] == "error"
        assert saved[0]["attempts"] == 2
        assert saved[0]["error_desc"] == "Process lost (server restart); gave up after 2 attempts"

    def test_orphaned_with_live_pid_file_is_left_alone(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_TIMER_DIR", str(tmp_path))
        monkeypatch.setattr(fit, "_FIT_JOBS", {})
        rdir = fit.fit_output_dir("qhy600", "260101", "TOI-1234")
        rdir.mkdir(parents=True)
        (rdir / "timer-fit.pid").write_text(str(os.getpid()))
        monkeypatch.setattr("muscat_db.database.get_persisted_jobs", lambda: [self._row(attempts=1)])
        saved = _capture_saves(monkeypatch)

        fit.sync_jobs()

        assert saved == []


class TestTtvFitReconcileRetry:
    def _row(self, **overrides):
        row = {
            "key": "ttv_fit:sinistro/250710/HIP67522/default",
            "type": "ttv_fit",
            "inst": "sinistro",
            "date": "250710",
            "target": "HIP67522",
            "state": "running",
            "started_at": 100.0,
            "elapsed": 0,
            "owner": "",
            "instance_id": "",
            "heartbeat_at": 0.0,
            "attempts": 0,
            "params": "",
            "run_id": "default",
            "run_name": "default",
        }
        row.update(overrides)
        return row

    def test_orphaned_with_no_evidence_retries_as_pending(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_TTV_DIR", str(tmp_path / "ttv_out"))
        monkeypatch.setattr(ttv, "_TTV_JOBS", {})
        monkeypatch.setattr("muscat_db.database.get_persisted_jobs", lambda: [self._row(attempts=0)])
        saved = _capture_saves(monkeypatch)

        ttv.sync_jobs()

        assert len(saved) == 1
        assert saved[0]["state"] == "pending"
        assert saved[0]["attempts"] == 1

    def test_orphaned_at_attempt_limit_is_abandoned(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_TTV_DIR", str(tmp_path / "ttv_out"))
        monkeypatch.setattr(job_lifecycle, "_MAX_RECONCILE_ATTEMPTS", 5)
        monkeypatch.setattr(ttv, "_TTV_JOBS", {})
        monkeypatch.setattr("muscat_db.database.get_persisted_jobs", lambda: [self._row(attempts=4)])
        saved = _capture_saves(monkeypatch)

        ttv.sync_jobs()

        assert len(saved) == 1
        assert saved[0]["state"] == "error"
        assert saved[0]["attempts"] == 5
        assert saved[0]["error_desc"] == "Process lost (server restart); gave up after 5 attempts"

    def test_orphaned_with_live_pid_file_is_left_alone(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_TTV_DIR", str(tmp_path))
        monkeypatch.setattr(ttv, "_TTV_JOBS", {})
        rdir = ttv.ttv_output_dir("HIP67522", "default")
        rdir.mkdir(parents=True)
        (rdir / "harmonic.pid").write_text(str(os.getpid()))
        monkeypatch.setattr("muscat_db.database.get_persisted_jobs", lambda: [self._row(attempts=1)])
        saved = _capture_saves(monkeypatch)

        ttv.sync_jobs()

        assert saved == []


# --------------------------- attempts survive a retry relaunch ---------------------------
#
# The three tests above prove the orphan branch writes the right attempts
# count. These prove the other half: the pending-drain loop (which the mocked
# get_persisted_jobs() above deliberately never exercises) must read that
# count back and carry it into the relaunched "running" row -- otherwise a
# job that gets requeued once could never actually reach the attempt limit,
# since every relaunch would silently reset the counter to 0.


class TestAttemptsSurviveRelaunch:
    def test_photometry_relaunch_preserves_attempts(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "muscat.db"))
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path / "prose_out"))
        monkeypatch.setattr(phot.subprocess, "Popen", lambda *a, **k: _FakeProc())
        with phot._LOCK:
            phot._JOBS.clear()
        store = job_store.get_job_store()
        store.save(
            type_="photometry", inst="qhy600", date="260101", target="TOI-1234",
            state="pending", returncode=None, elapsed=0, started_at=100.0,
            run_type="full", attempts=2, run_id="default",
            # photometry's drain loop reconstructs a blank run_id via
            # build_run_id() (-> "default"); pin it explicitly so the
            # relaunch writes back to this same key instead of a new one.
            params='{"test_run": false, "options": {}, "run_id": "default"}',
        )

        phot.sync_jobs()

        row = next(j for j in store.all() if j["key"] == "photometry:qhy600/260101/TOI-1234/default")
        assert row["state"] == "running"
        assert row["attempts"] == 2

    def test_transit_fit_relaunch_preserves_attempts(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "muscat.db"))
        monkeypatch.setenv("MUSCAT_TIMER_DIR", str(tmp_path / "timer_out"))
        monkeypatch.setattr(fit.subprocess, "Popen", lambda *a, **k: _FakeProc())
        monkeypatch.setattr(fit, "get_csv_lightcurves", lambda inst, date, target: [])
        with fit._FIT_LOCK:
            fit._FIT_JOBS.clear()
        store = job_store.get_job_store()
        store.save(
            type_="transit_fit", inst="qhy600", date="260101", target="TOI-1234",
            state="pending", returncode=None, elapsed=0, started_at=100.0,
            run_type="full", attempts=3,
            params='{"test_run": false, "options": {}}',
        )

        fit.sync_jobs()

        row = next(j for j in store.all() if j["key"] == "transit_fit:qhy600/260101/TOI-1234")
        assert row["state"] == "running"
        assert row["attempts"] == 3

    def test_ttv_fit_relaunch_preserves_attempts(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "muscat.db"))
        monkeypatch.setenv("MUSCAT_TTV_DIR", str(tmp_path / "ttv_out"))
        monkeypatch.setattr(ttv.subprocess, "Popen", lambda *a, **k: _FakeProc())
        with ttv._TTV_LOCK:
            ttv._TTV_JOBS.clear()
        store = job_store.get_job_store()
        store.save(
            type_="ttv_fit", inst="_", date="_", target="HIP67522",
            state="pending", returncode=None, elapsed=0, started_at=100.0,
            run_type="full", run_id="default", run_name="default", attempts=4,
            params='{"options": {}}',
        )

        ttv.sync_jobs()

        row = next(j for j in store.all() if j["key"] == "ttv_fit:_/_/HIP67522/default")
        assert row["state"] == "running"
        assert row["attempts"] == 4
