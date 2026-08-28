"""Regression coverage for the worker/web reconciliation collision (PR #111
review): every ``sync_jobs()`` treats a DB row in ``state='running'`` that its
own in-memory registry does not recognize as orphaned, and marks it
``error: "Process lost (server restart)"``, releasing its concurrency slot.
That inference only holds if the calling process is the only one that ever
launches jobs for the pipeline -- false once a standalone ``muscatdb worker``
process runs the same ``sync_jobs()`` for the same pipeline the web process's
own background loop already does. Running the worker alongside the web
process, with nothing else, would falsely kill every job the web process is
actively tracking within one of the web process's own 2s reconciliation
passes.

These tests call the real, unstubbed ``sync_jobs()`` for all three pipelines
(unlike tests/test_worker.py, which only ever exercises the loop mechanics
around a stubbed/monkeypatched ``sync_jobs``) to prove: a running row owned by
a different role is left untouched, while a role reconciling its own orphaned
row still works exactly as before the owner tag existed.
"""

from __future__ import annotations

from muscat_db import job_store, photometry as phot, transit_fit as fit, ttv_fit as ttv


def _row(store, key: str) -> dict:
    return next(j for j in store.all() if j["key"] == key)


class TestPhotometryOwnershipIsolation:
    def test_other_owners_running_job_is_left_running(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "muscat.db"))
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path / "prose_out"))
        with phot._LOCK:
            phot._JOBS.clear()
        store = job_store.get_job_store()
        key = "photometry:qhy600/260101/TOI-1234"
        store.save(
            type_="photometry", inst="qhy600", date="260101", target="TOI-1234",
            state="running", returncode=None, elapsed=0, started_at=100.0,
            run_type="full", owner="web",
        )

        monkeypatch.setattr(job_store, "_OWNER", "worker")
        phot.sync_jobs()

        row = _row(store, key)
        assert row["state"] == "running"
        assert row.get("error_desc") in (None, "")

    def test_owning_role_still_reconciles_its_own_orphaned_job(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "muscat.db"))
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path / "prose_out"))
        with phot._LOCK:
            phot._JOBS.clear()
        store = job_store.get_job_store()
        key = "photometry:qhy600/260101/TOI-1234"
        store.save(
            type_="photometry", inst="qhy600", date="260101", target="TOI-1234",
            state="running", returncode=None, elapsed=0, started_at=100.0,
            run_type="full", owner="worker",
        )

        monkeypatch.setattr(job_store, "_OWNER", "worker")
        phot.sync_jobs()

        row = _row(store, key)
        assert row["state"] == "error"
        assert row["error_desc"] == "Process lost (server restart)"


class TestTransitFitOwnershipIsolation:
    def test_other_owners_running_job_is_left_running(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "muscat.db"))
        monkeypatch.setenv("MUSCAT_TIMER_DIR", str(tmp_path / "timer_out"))
        with fit._FIT_LOCK:
            fit._FIT_JOBS.clear()
        store = job_store.get_job_store()
        key = "transit_fit:qhy600/260101/TOI-1234"
        store.save(
            type_="transit_fit", inst="qhy600", date="260101", target="TOI-1234",
            state="running", returncode=None, elapsed=0, started_at=100.0,
            run_type="full", owner="web",
        )

        monkeypatch.setattr(job_store, "_OWNER", "worker")
        fit.sync_jobs()

        row = _row(store, key)
        assert row["state"] == "running"
        assert row.get("error_desc") in (None, "")

    def test_owning_role_still_reconciles_its_own_orphaned_job(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "muscat.db"))
        monkeypatch.setenv("MUSCAT_TIMER_DIR", str(tmp_path / "timer_out"))
        with fit._FIT_LOCK:
            fit._FIT_JOBS.clear()
        store = job_store.get_job_store()
        key = "transit_fit:qhy600/260101/TOI-1234"
        store.save(
            type_="transit_fit", inst="qhy600", date="260101", target="TOI-1234",
            state="running", returncode=None, elapsed=0, started_at=100.0,
            run_type="full", owner="worker",
        )

        monkeypatch.setattr(job_store, "_OWNER", "worker")
        fit.sync_jobs()

        row = _row(store, key)
        assert row["state"] == "error"
        assert row["error_desc"] == "Process lost (server restart)"


class TestTtvFitOwnershipIsolation:
    def test_other_owners_running_job_is_left_running(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "muscat.db"))
        monkeypatch.setenv("MUSCAT_TTV_DIR", str(tmp_path / "ttv_out"))
        with ttv._TTV_LOCK:
            ttv._TTV_JOBS.clear()
        store = job_store.get_job_store()
        key = "ttv_fit:sinistro/250710/HIP67522/default"
        store.save(
            type_="ttv_fit", inst="sinistro", date="250710", target="HIP67522",
            state="running", returncode=None, elapsed=0, started_at=100.0,
            run_type="full", run_id="default", run_name="default", owner="web",
        )

        monkeypatch.setattr(job_store, "_OWNER", "worker")
        ttv.sync_jobs()

        row = _row(store, key)
        assert row["state"] == "running"
        assert row.get("error_desc") in (None, "")

    def test_owning_role_still_reconciles_its_own_orphaned_job(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "muscat.db"))
        monkeypatch.setenv("MUSCAT_TTV_DIR", str(tmp_path / "ttv_out"))
        with ttv._TTV_LOCK:
            ttv._TTV_JOBS.clear()
        store = job_store.get_job_store()
        key = "ttv_fit:sinistro/250710/HIP67522/default"
        store.save(
            type_="ttv_fit", inst="sinistro", date="250710", target="HIP67522",
            state="running", returncode=None, elapsed=0, started_at=100.0,
            run_type="full", run_id="default", run_name="default", owner="worker",
        )

        monkeypatch.setattr(job_store, "_OWNER", "worker")
        ttv.sync_jobs()

        row = _row(store, key)
        assert row["state"] == "error"
        assert row["error_desc"] == "Process lost (server restart)"
