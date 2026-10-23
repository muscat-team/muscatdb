"""Workers on a shared control plane never open the SQLite catalog (issue #298).

Workers on ut3-ut7 reach ``muscat.db`` over NFS. SQLite's POSIX byte-range
locks and its WAL shared memory are unsupported there, and a worker whose NFS
client lost its state while holding one of those locks blocked every writer on
ut2 for hours (2026-10-23 and again 2026-10-24). These tests pin the pieces
that keep a worker off the catalog:

* ``database.connect`` refuses to open the catalog once a process forbids it,
  and ``worker.run`` forbids it whenever the control plane is shared;
* ``refresh_target_status`` is a no-op there, and the web process refreshes
  the targets of worker-finished jobs instead (``target_refresh``);
* a queued photometry row carries its frame inputs, computed on ut2 at enqueue,
  so the worker writes the product record without a catalog lookup.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import replace

import pytest

from muscat_db import database, job_limits, job_store, product_record, target_refresh, worker
from muscat_db import photometry as phot

INST = "muscat4"
DATE = "260101"
TARGET = "HIP67522"
FRAMES = {
    "inputs": [{
        "kind": "frames", "instrument": INST, "obsdate": DATE, "target": TARGET,
        "proposal_ids": ["P1"], "n_frames": 3, "blank_frames": 0, "resolved": True,
    }],
    "note": None,
}


@pytest.fixture(autouse=True)
def _restore_catalog_access():
    yield
    database.allow_catalog()
    job_store.set_owner("web")


@pytest.fixture
def catalog_db(tmp_path, monkeypatch):
    path = tmp_path / "muscat.db"
    monkeypatch.setenv("MUSCAT_DB_PATH", str(path))
    with database.get_conn(str(path)) as conn:
        conn.executescript(database.SCHEMA)
        conn.executemany(
            "INSERT INTO frames (instrument, obsdate, ccd, filename, object, proposal_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(INST, DATE, i, f"f{i}.fits", TARGET, "P1") for i in range(3)],
        )
        conn.commit()
    database.clear_all_caches()
    return path


class _SharedStoreStandIn:
    """A real SQLite job store standing in for Postgres.

    Its own calls bypass the catalog guard, because on production the jobs live
    in Postgres, not in ``muscat.db``. Everything else a pipeline does still
    meets the guard.
    """

    def __init__(self, store):
        self._store = store

    def __getattr__(self, name):
        attr = getattr(self._store, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            saved = database._CATALOG_FORBIDDEN
            database._CATALOG_FORBIDDEN = None
            try:
                return attr(*args, **kwargs)
            finally:
                database._CATALOG_FORBIDDEN = saved

        return call


# -- the guard ----------------------------------------------------------------


def test_connect_refuses_the_catalog_once_forbidden(catalog_db):
    database.forbid_catalog("worker on a shared control plane")

    with pytest.raises(database.CatalogUnavailableError, match="shared control plane"):
        database.connect(str(catalog_db))
    with pytest.raises(database.CatalogUnavailableError):
        with database.get_conn(readonly=True):
            pass


def test_allow_catalog_lifts_the_guard(catalog_db):
    database.forbid_catalog("test")
    database.allow_catalog()

    with database.get_conn(str(catalog_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM frames").fetchone() == (3,)


def test_readonly_connection_neither_writes_nor_switches_journal_mode(tmp_path):
    path = tmp_path / "plain.db"
    raw = sqlite3.connect(str(path))
    raw.execute("CREATE TABLE t (x)")
    raw.commit()
    raw.close()

    with database.get_conn(str(path), readonly=True) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO t VALUES (1)")


# -- the worker role ----------------------------------------------------------


@pytest.mark.parametrize("shared, allowed", [(True, False), (False, True)])
def test_worker_forbids_the_catalog_only_on_a_shared_control_plane(monkeypatch, shared, allowed):
    seen = []
    monkeypatch.setattr(job_store, "uses_shared_control_plane", lambda: shared)
    monkeypatch.setattr(worker, "_resolve_stale_cancelling", lambda: None)
    monkeypatch.setattr(worker, "_loop", lambda *a, **k: seen.append(database.catalog_allowed()))

    worker.run("photometry", once=True)

    assert seen == [allowed]
    assert database.catalog_allowed()  # restored when run() returns


# -- target status ------------------------------------------------------------


def _seed_target(path) -> None:
    with database.get_conn(str(path)) as conn:
        conn.execute(
            "INSERT INTO targets (object, n_dates, n_frames, instruments, dates, inst_dates, "
            "filters, total_exptime, is_identified, phot_status, fit_status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (TARGET, 1, 3, INST, DATE, f"{INST}:{DATE}", "g", 60.0, 1, "none", "none"),
        )
        conn.commit()


def test_refresh_target_status_is_a_no_op_without_the_catalog(catalog_db, monkeypatch):
    _seed_target(catalog_db)
    monkeypatch.setattr(database, "_aggregate_target_status", lambda *a: pytest.fail("scanned outputs"))
    database.forbid_catalog("test")

    database.refresh_target_status(TARGET)  # must not raise

    database.allow_catalog()
    with database.get_conn(str(catalog_db)) as conn:
        assert conn.execute("SELECT phot_status FROM targets").fetchone() == ("none",)


def test_refresh_holds_no_connection_while_scanning_outputs(catalog_db, monkeypatch):
    """The filesystem scan runs with no connection open on the catalog, so a
    slow scan over NFS cannot pin a read snapshot or the write lock."""
    _seed_target(catalog_db)
    real_connect = database.connect
    open_conns: list[sqlite3.Connection] = []
    open_during_scan = []

    class _Tracked(sqlite3.Connection):
        def close(self):
            if self in open_conns:
                open_conns.remove(self)
            super().close()

    def tracking_connect(path=None, *, timeout=30.0, readonly=False):
        real_connect(path, timeout=timeout, readonly=readonly).close()  # applies the guard and pragmas
        target = f"file:{path or database.db_path()}?mode=ro" if readonly else (path or database.db_path())
        conn = sqlite3.connect(target, uri=readonly, timeout=timeout, factory=_Tracked)
        open_conns.append(conn)
        return conn

    def scan(obj, inst_dates):
        open_during_scan.append(len(open_conns))
        return "full", "none"

    monkeypatch.setattr(database, "connect", tracking_connect)
    monkeypatch.setattr(database, "_aggregate_target_status", scan)

    database.refresh_target_status(TARGET)

    assert open_during_scan == [0]
    with database.get_conn(str(catalog_db)) as conn:
        assert conn.execute("SELECT phot_status FROM targets").fetchone() == ("full",)


class _Store:
    def __init__(self):
        self.active_rows: list[dict] = []
        self.rows: dict[str, dict] = {}

    def active(self):
        return list(self.active_rows)

    def get(self, key):
        return self.rows.get(key)


def _row(key, target, state, owner="worker"):
    return {"key": key, "target": target, "state": state, "owner": owner}


def test_watcher_reports_worker_jobs_that_left_the_active_set():
    store = _Store()
    watcher = target_refresh.FinishedJobWatcher()
    store.active_rows = [_row("a", "T-A", "running"), _row("b", "T-B", "pending", owner="")]
    assert watcher.poll(store) == set()  # the first pass only seeds

    store.active_rows = []
    store.rows = {"a": _row("a", "T-A", "done"), "b": _row("b", "T-B", "error")}

    assert watcher.poll(store) == {"T-A", "T-B"}
    assert watcher.poll(store) == set()  # each job is reported once


def test_watcher_ignores_web_jobs_and_jobs_still_in_flight():
    store = _Store()
    watcher = target_refresh.FinishedJobWatcher()
    store.active_rows = [_row("w", "T-W", "running", owner="web"), _row("r", "T-R", "running")]
    watcher.poll(store)

    store.active_rows = [_row("r", "T-R", "running")]
    store.rows = {"w": _row("w", "T-W", "done", owner="web")}

    assert watcher.poll(store) == set()  # the web process refreshed its own job inline


def test_watcher_rereads_a_finished_job_it_could_not_read():
    store = _Store()
    watcher = target_refresh.FinishedJobWatcher()
    store.active_rows = [_row("a", "T-A", "running")]
    watcher.poll(store)
    store.active_rows = []
    real_get = store.get
    store.get = lambda key: (_ for _ in ()).throw(RuntimeError("control plane blip"))
    assert watcher.poll(store) == set()

    store.get = real_get
    store.rows = {"a": _row("a", "T-A", "done")}

    assert watcher.poll(store) == {"T-A"}


def test_watcher_survives_a_store_error():
    class Broken:
        def active(self):
            raise RuntimeError("control plane down")

    assert target_refresh.FinishedJobWatcher().poll(Broken()) == set()


def test_web_refreshes_worker_finished_targets(monkeypatch):
    refreshed = []
    store = _Store()
    watcher = target_refresh.FinishedJobWatcher()
    monkeypatch.setattr(database, "refresh_target_status", refreshed.append)
    store.active_rows = [_row("a", "T-A", "running")]
    target_refresh.refresh_worker_finished_targets(watcher, store)

    store.active_rows = []
    store.rows = {"a": _row("a", "T-A", "cancelled")}
    target_refresh.refresh_worker_finished_targets(watcher, store)

    assert refreshed == ["T-A"]


# -- photometry frame inputs --------------------------------------------------


class _QueueingStore:
    def __init__(self):
        self.enqueued: list[dict] = []

    def claim_slot(self, *a, **k):
        return False

    def enqueue(self, **kwargs):
        self.enqueued.append(kwargs)


def test_queued_photometry_row_carries_its_frame_inputs(catalog_db, tmp_path, monkeypatch):
    from muscat_db.instruments import INSTRUMENTS

    raw_root = tmp_path / "raw"
    (raw_root / DATE).mkdir(parents=True)
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path / "out"))
    patched = dict(INSTRUMENTS)
    patched[INST] = replace(INSTRUMENTS[INST], data_subdir=str(raw_root))
    monkeypatch.setattr("muscat_db.photometry.INSTRUMENTS", patched)
    monkeypatch.setattr(phot, "_MAX_FULL_JOBS", 1)
    store = _QueueingStore()
    monkeypatch.setattr(phot, "get_job_store", lambda: store)

    result = phot.start_run(INST, DATE, TARGET, options={"run_name": ""}, test_run=False)

    assert result.get("queued") is True, result
    params = json.loads(store.enqueued[0]["params"])
    assert params["frames_inputs"]["note"] is None
    assert job_limits.RUNNING_CAP_PARAM in params
    [entry] = params["frames_inputs"]["inputs"]
    assert entry["proposal_ids"] == ["P1"] and entry["n_frames"] == 3


def test_failed_frame_lookup_queues_the_row_without_frame_inputs(monkeypatch):
    def broken(*a):
        raise OSError("catalog missing")

    monkeypatch.setattr(phot, "_lookup_frames_inputs", broken)

    assert phot.frames_inputs_param(INST, DATE, TARGET) is None


class _FakeProc:
    def __init__(self):
        self.pid = os.getpid()

    def poll(self):
        return None

    def wait(self, timeout=None):
        return None


@pytest.fixture
def worker_drain(catalog_db, tmp_path, monkeypatch):
    """A photometry drain run as a worker on a shared control plane."""
    rdir = tmp_path / "phot-run"
    launched = []
    monkeypatch.setattr(phot, "run_output_dir", lambda *a, **k: rdir)
    monkeypatch.setattr(phot, "build_command", lambda *a, **k: ["run_photometry"])
    monkeypatch.setattr(phot, "_write_run_meta", lambda *a, **k: None)
    monkeypatch.setattr(phot, "prose_version", lambda: "test")
    monkeypatch.setattr(phot.subprocess, "Popen", lambda cmd, **k: launched.append(cmd) or _FakeProc())
    monkeypatch.setattr(phot, "_JOBS", {})
    monkeypatch.setattr(phot, "_MAX_FULL_JOBS", 1)
    store = _SharedStoreStandIn(job_store.get_job_store())
    monkeypatch.setattr(phot, "get_job_store", lambda: store)

    def drain(params: dict):
        store.enqueue(
            type_="photometry", inst=INST, date=DATE, target=TARGET, started_at=1.0,
            run_type="full", params=json.dumps(params), user_name="ann",
        )
        job_store.set_owner("worker")
        database.forbid_catalog("worker on a shared control plane")
        try:
            phot.sync_jobs()
        finally:
            database.allow_catalog()
            for job in phot._JOBS.values():
                job.logf.close()
        return rdir, launched

    return drain


def _states():
    return [j["state"] for j in database.get_persisted_jobs() if j["type"] == "photometry"]


def test_worker_records_the_frame_inputs_it_was_queued_with(worker_drain):
    rdir, launched = worker_drain({"test_run": False, "options": {}, "frames_inputs": FRAMES})

    assert launched == [["run_photometry"]]
    assert set(_states()) == {"running"}  # launched, not failed on the catalog
    record = product_record.read_record(rdir)
    assert record is not None
    assert record["inputs"] == FRAMES["inputs"]


def test_worker_leaves_a_legacy_row_unrecorded_rather_than_undetermined(worker_drain):
    """A row queued before frame inputs travelled with it still launches. Its
    record is left absent, not written as undetermined: ``backfill-products``
    fills an absent record from the catalog but never rewrites one."""
    rdir, launched = worker_drain({"test_run": False, "options": {}})

    assert launched == [["run_photometry"]]
    assert set(_states()) == {"running"}  # launched, not failed on the catalog
    assert product_record.read_record(rdir) is None


# -- per-user running cap -----------------------------------------------------


def test_queued_row_carries_its_owners_running_cap(catalog_db):
    job_limits.set_limits(str(catalog_db), "ann", running=4)

    params = json.loads(job_limits.params_with_running_cap('{"options":{}}', "ann"))

    assert params == {"options": {}, job_limits.RUNNING_CAP_PARAM: 4}


def test_drain_with_the_catalog_reads_the_current_cap_not_the_carried_one(catalog_db):
    """A limit changed after a row was queued still applies wherever the
    catalog is readable (the web process, or a SQLite control plane)."""
    job_limits.set_limits(str(catalog_db), "ann", running=4)
    row = {"user_name": "ann", "params": json.dumps({job_limits.RUNNING_CAP_PARAM: 2})}

    assert job_limits.claim_caps_for_row(row) == {"user_name": "ann", "max_user_slots": 4}


def test_worker_drain_uses_the_carried_cap(catalog_db):
    job_limits.set_limits(str(catalog_db), "ann", running=4)
    row = {"user_name": "ann", "params": json.dumps({job_limits.RUNNING_CAP_PARAM: 2})}
    database.forbid_catalog("test")

    assert job_limits.claim_caps_for_row(row) == {"user_name": "ann", "max_user_slots": 2}


def test_legacy_row_on_a_worker_falls_back_to_the_site_cap(catalog_db, monkeypatch):
    monkeypatch.setenv("MUSCAT_USER_MAX_RUNNING", "1")
    job_limits.set_limits(str(catalog_db), "ann", running=4)
    database.forbid_catalog("test")

    caps = job_limits.claim_caps_for_row({"user_name": "ann", "params": '{"options":{}}'})

    assert caps == {"user_name": "ann", "max_user_slots": 1}


@pytest.mark.parametrize("cap", [-1, "3", True, 1.5])
def test_worker_drain_ignores_a_malformed_carried_cap(catalog_db, monkeypatch, cap):
    monkeypatch.setenv("MUSCAT_USER_MAX_RUNNING", "1")
    row = {"user_name": "ann", "params": json.dumps({job_limits.RUNNING_CAP_PARAM: cap})}
    database.forbid_catalog("test")

    assert job_limits.claim_caps_for_row(row)["max_user_slots"] == 1  # the site cap


@pytest.mark.parametrize("shared", [True, False])
def test_web_reconcile_pass_refreshes_worker_targets_only_on_a_shared_control_plane(monkeypatch, shared):
    from muscat_db import web

    calls = []
    for mod in (web.phot, web.fit, web.ttv):
        monkeypatch.setattr(mod, "sync_jobs", lambda: None)
    monkeypatch.setattr(web, "_lco_archive_download_rows", lambda: [])
    monkeypatch.setattr(web, "uses_shared_control_plane", lambda: shared)
    monkeypatch.setattr(web, "get_job_store", lambda: "store")
    monkeypatch.setattr(web, "refresh_worker_finished_targets", lambda watcher, store: calls.append(store))

    web._reconcile_all_jobs()

    assert calls == (["store"] if shared else [])
