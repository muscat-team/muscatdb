"""The pending-queue drain loops hand the queued row to ``claim_slot``.

Each pipeline's ``sync_jobs`` drains ``pending`` rows by claiming a durable
slot first. The claim must carry the row owner's running cap (issue #265), or
a user's queued jobs bypass the cap the moment they leave the queue. It must
also carry the row's own key as ``pending_key`` (issue #51), so the claim and
the row's move from pending to running are one step and a row another worker
already took is not launched a second time. The store-level behavior lives in
tests/_job_store_contract.py; these tests pin the call sites, which a refactor
of a drain loop could otherwise drop without any other test noticing.

The job store is a real SQLite store wrapped in a spy that records each
``claim_slot`` call. Engine launches are stubbed at ``subprocess.Popen``.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from muscat_db import database, job_limits
from muscat_db import photometry as phot
from muscat_db import transit_fit as fit
from muscat_db import ttv_fit as ttv
from muscat_db.job_store import get_job_store

INST = "muscat4"
DATE = "260101"
TARGET = "HIP67522"
USER = "ann"


class _FakeProc:
    def __init__(self):
        self.pid = os.getpid()

    def poll(self):
        return None

    def wait(self, timeout=None):
        return None


class _ClaimSpy:
    """Delegates to the real store and records every ``claim_slot`` call.

    With ``finish_after_snapshot`` set, ``pending()`` returns its rows and then
    marks them ``done``, the way a row looks to a drain whose snapshot is stale
    because another worker has already launched and finished it.
    """

    def __init__(self, store):
        self._store = store
        self.claims: list[dict] = []
        self.finish_after_snapshot = False

    def claim_slot(self, pipeline, holder_key, max_slots, **kwargs):
        self.claims.append(kwargs)
        return self._store.claim_slot(pipeline, holder_key, max_slots, **kwargs)

    def pending(self, type_):
        rows = self._store.pending(type_)
        if self.finish_after_snapshot:
            with database.get_conn(database.db_path()) as conn:
                conn.executemany("UPDATE jobs SET state = 'done' WHERE key = ?", [(r["key"],) for r in rows])
                conn.commit()
        return rows

    def __getattr__(self, name):
        return getattr(self._store, name)


@pytest.fixture
def jobs_db(tmp_path, monkeypatch):
    path = tmp_path / "muscat.db"
    monkeypatch.setenv("MUSCAT_DB_PATH", str(path))
    monkeypatch.delenv("MUSCAT_USER_MAX_RUNNING", raising=False)
    monkeypatch.delenv("MUSCAT_USER_MAX_QUEUED", raising=False)
    conn = sqlite3.connect(str(path))
    conn.executescript(database.SCHEMA)
    conn.commit()
    conn.close()
    database.clear_all_caches()
    monkeypatch.setattr(database, "refresh_target_status", lambda *a, **k: None)
    # A running cap other than the site default, so a claim that dropped the
    # user's caps (and fell back to no cap or the default) cannot pass.
    job_limits.set_limits(str(path), USER, running=2, queued=4)
    return path


@pytest.fixture
def launched(monkeypatch):
    """Record engine launches instead of starting processes."""
    cmds: list[list[str]] = []

    def _popen(cmd, **_kwargs):
        cmds.append(cmd)
        return _FakeProc()

    for mod in (phot, fit, ttv):
        monkeypatch.setattr(mod.subprocess, "Popen", _popen)
    return cmds


def _spy(monkeypatch, module) -> _ClaimSpy:
    spy = _ClaimSpy(get_job_store())
    monkeypatch.setattr(module, "get_job_store", lambda: spy)
    monkeypatch.setattr(module, "_MAX_FULL_JOBS", 1)
    return spy


def _pending_keys(type_: str) -> list[str]:
    return [r["key"] for r in get_job_store().pending(type_)]


def _states(type_: str) -> list[str]:
    return [j["state"] for j in database.get_persisted_jobs() if j["type"] == type_]


def _close(registry: dict) -> None:
    for job in registry.values():
        job.logf.close()
    registry.clear()


# -- photometry ---------------------------------------------------------------


@pytest.fixture
def phot_drain(tmp_path, monkeypatch, jobs_db, launched):
    rdir = tmp_path / "phot-run"
    monkeypatch.setattr(phot, "run_output_dir", lambda *a, **k: rdir)
    monkeypatch.setattr(phot, "build_command", lambda *a, **k: ["run_photometry"])
    monkeypatch.setattr(phot, "_write_run_meta", lambda *a, **k: None)
    monkeypatch.setattr(phot, "_write_run_product", lambda *a, **k: None)
    monkeypatch.setattr(phot, "_JOBS", {})
    get_job_store().enqueue(
        type_="photometry", inst=INST, date=DATE, target=TARGET, started_at=1.0,
        run_type="full", params=json.dumps({"test_run": False, "options": {}}),
        user_name=USER,
    )
    yield _spy(monkeypatch, phot)
    _close(phot._JOBS)



# -- transit fit --------------------------------------------------------------


@pytest.fixture
def fit_drain(tmp_path, monkeypatch, jobs_db, launched):
    rdir = tmp_path / "fit-run"
    monkeypatch.setattr(fit, "fit_output_dir", lambda *a, **k: rdir)
    monkeypatch.setattr(fit, "get_csv_lightcurves", lambda *a, **k: [])
    monkeypatch.setattr(fit, "_write_fit_inputs", lambda *a, **k: {})
    monkeypatch.setattr(fit, "_write_fit_product", lambda *a, **k: None)
    monkeypatch.setattr(fit, "_fit_command", lambda *a, **k: ["timer-fit"])
    monkeypatch.setattr(fit, "_write_log_banner", lambda *a, **k: None)
    monkeypatch.setattr(fit, "_FIT_JOBS", {})
    get_job_store().enqueue(
        type_="transit_fit", inst=INST, date=DATE, target=TARGET, started_at=1.0,
        run_type="full", params=json.dumps({"test_run": False, "options": {}}),
        user_name=USER,
    )
    yield _spy(monkeypatch, fit)
    _close(fit._FIT_JOBS)



# -- ttv fit ------------------------------------------------------------------


@pytest.fixture
def ttv_drain(tmp_path, monkeypatch, jobs_db, launched):
    monkeypatch.setenv("MUSCAT_TTV_DIR", str(tmp_path / "ttv"))
    monkeypatch.setattr(ttv, "_harmonic_prefix", lambda: ["harmonic"])
    monkeypatch.setattr(ttv, "_harmonic_version", lambda: "0.0.0")
    monkeypatch.setattr(ttv, "_TTV_JOBS", {})
    options = {"csv_content": "planet,epoch,tc,tc_unc\n", "ini_content": "[INIT]\n", "run_name": "r1"}
    get_job_store().enqueue(
        type_="ttv_fit", inst="_", date="_", target=TARGET, started_at=1.0,
        run_type="full", params=json.dumps({"options": options}),
        run_id="r1", run_name="r1", user_name=USER,
    )
    yield _spy(monkeypatch, ttv)
    _close(ttv._TTV_JOBS)


# -- the claims ---------------------------------------------------------------


_DRAINS = [
    pytest.param("phot_drain", phot.sync_jobs, "photometry", id="photometry"),
    pytest.param("fit_drain", fit.sync_jobs, "transit_fit", id="transit_fit"),
    pytest.param("ttv_drain", ttv.sync_jobs, "ttv_fit", id="ttv_fit"),
]


@pytest.mark.parametrize(("drain", "sync", "type_"), _DRAINS)
def test_drain_claims_with_the_row_owners_caps_and_the_rows_key(drain, sync, type_, request, launched):
    spy = request.getfixturevalue(drain)
    [row_key] = _pending_keys(type_)

    sync()

    assert spy.claims == [{"user_name": USER, "max_user_slots": 2, "pending_key": row_key}]
    assert len(launched) == 1


@pytest.mark.parametrize(("drain", "sync", "type_"), _DRAINS)
def test_drain_does_not_launch_a_row_that_is_no_longer_pending(drain, sync, type_, request, launched):
    spy = request.getfixturevalue(drain)
    spy.finish_after_snapshot = True

    sync()

    assert len(spy.claims) == 1
    assert launched == []
    assert _states(type_) == ["done"]
