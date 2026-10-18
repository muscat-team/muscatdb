"""Deleting a run must refuse while its pipeline process is still live.

Delete ``rmtree``s the run directory (taking ``*.pid`` and the logs with it)
and drops the job row, so a live run is left untracked and the orphan
reconciler then requeues it. All three pipelines share the guard.
"""

from __future__ import annotations

import os

import pytest

from muscat_db import jobs
from muscat_db import photometry as phot
from muscat_db import transit_fit as fit
from muscat_db import ttv_fit as ttv

INST, DATE, TARGET = "muscat2", "250127", "TOI123.01"


class _SpyStore:
    def __init__(self):
        self.deleted = []

    def delete(self, key):
        self.deleted.append(key)


def _photometry(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
    rdir = phot.run_output_dir(INST, DATE, TARGET, "default")
    return rdir, "photometry.pid", phot, lambda: phot.delete_reduction(INST, DATE, TARGET, run_id="default")


def _transit_fit(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_TIMER_DIR", str(tmp_path))
    rdir = fit.fit_output_dir(INST, DATE, TARGET, "default")
    return rdir, "timer-fit.pid", fit, lambda: fit.delete_fit(INST, DATE, TARGET, run_id="default")


def _ttv_fit(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_TTV_DIR", str(tmp_path))
    rdir = ttv.ttv_output_dir(TARGET, "myrun")
    return rdir, "harmonic.pid", ttv, lambda: ttv.delete_ttv_fit(TARGET, "myrun")


PIPELINES = pytest.mark.parametrize("setup", [_photometry, _transit_fit, _ttv_fit])


@PIPELINES
def test_delete_refused_while_run_is_live(setup, monkeypatch, tmp_path):
    rdir, pid_name, module, delete = setup(monkeypatch, tmp_path)
    rdir.mkdir(parents=True)
    (rdir / pid_name).write_text(str(os.getpid()))
    product = rdir / "keep_me.csv"
    product.write_text("x")
    store = _SpyStore()
    monkeypatch.setattr(module, "get_job_store", lambda: store)

    result = delete()

    assert result["ok"] is False
    assert result["error"] == jobs.LIVE_RUN_DELETE_ERROR
    assert product.read_text() == "x"
    assert store.deleted == []


@PIPELINES
def test_delete_proceeds_when_run_is_not_live(setup, monkeypatch, tmp_path):
    rdir, pid_name, module, delete = setup(monkeypatch, tmp_path)
    rdir.mkdir(parents=True)
    (rdir / pid_name).write_text("999999999")  # no such process
    (rdir / "old.csv").write_text("x")
    store = _SpyStore()
    monkeypatch.setattr(module, "get_job_store", lambda: store)

    result = delete()

    assert result["ok"] is True
    assert not rdir.exists()
    assert len(store.deleted) == 1


def test_run_is_live_sees_in_memory_process(tmp_path):
    class Running:
        def poll(self):
            return None

    class Finished:
        def poll(self):
            return 0

    pid_file = tmp_path / "x.pid"
    assert jobs.run_is_live(Running(), pid_file) is True
    assert jobs.run_is_live(Finished(), pid_file) is False
    assert jobs.run_is_live(None, pid_file) is False
