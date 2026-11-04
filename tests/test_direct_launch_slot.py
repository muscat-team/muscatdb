"""A direct launch must hold its concurrency slot and its running row together.

Before #325 item 3, ``start_run`` / ``start_fit`` / ``start_ttv_fit`` claimed a
slot, prepared the run directory, spawned the process, and only then saved the
``running`` row. Another process's ``reconcile_slots`` pass in that window saw a
slot with no running row, treated it as stale and freed it, so a second launch
could be admitted past the cap. The claim now registers the row in the same
transaction.
"""

from __future__ import annotations

import subprocess

import pytest

from muscat_db import job_store, transit_fit, ttv_fit
from tests.test_job_limits import (
    _photometry_launch,
    _transit_fit_launch,
    _ttv_launch,
    db,  # noqa: F401  (fixture)
)

CASES = [
    pytest.param(_photometry_launch, "photometry", "photometry:muscat3/260720/TOI-123", id="photometry"),
    pytest.param(_transit_fit_launch, "transit_fit", "transit_fit:muscat3/260720/TOI-123", id="transit_fit"),
    pytest.param(lambda mp, store, user, _tmp: _ttv_launch(mp, store, user), "ttv_fit", "ttv_fit:_/_/TOI-123", id="ttv_fit"),
]


@pytest.mark.parametrize("launch, pipeline, row_prefix", CASES)
def test_slot_survives_reconcile_between_claim_and_spawn(launch, pipeline, row_prefix, db, monkeypatch, tmp_path):  # noqa: F811
    store = job_store.DatabaseJobStore()
    seen = {}
    real_popen = subprocess.Popen
    monkeypatch.setattr(transit_fit, "_copy_lightcurve_for_timer", lambda src, dst: None)

    def spawn(*args, **kwargs):
        if not kwargs.get("start_new_session"):
            return real_popen(*args, **kwargs)  # helper probes (subprocess.run), not the job
        # Another process's sync pass lands between the claim and the spawn.
        seen["released"] = store.reconcile_slots(pipeline)
        seen["claimed"] = store.count_claimed(pipeline)
        seen["rows"] = [r for r in store.all() if r["key"].startswith(row_prefix)]
        raise OSError("spawn refused")

    monkeypatch.setattr(subprocess, "Popen", spawn)

    result = launch(monkeypatch, store, "ann", tmp_path)

    assert seen["released"] == 0, "reconcile freed a slot whose job was starting"
    assert seen["claimed"] == 1
    assert [r["state"] for r in seen["rows"]] == ["running"]
    # The spawn failed: nothing may stay running or hold the slot.
    assert result["ok"] is False
    assert store.count_claimed(pipeline) == 0
    assert [r["state"] for r in store.all() if r["key"].startswith(row_prefix)] == ["error"]


# --- the window between the claim and the spawn, inside this process --------
#
# The claim writes the ``running`` row before the process exists. Anything in
# this process that reads "running row, no tracked job" must not mistake that
# for an orphan to requeue or a job to cancel, and a launch that dies before
# the spawn must leave neither a ``running`` row nor a held slot behind.

SYNC = {
    "transit_fit": lambda: transit_fit.sync_jobs(),
    "ttv_fit": lambda: ttv_fit.sync_jobs(),
}
CANCEL = {
    "transit_fit": lambda: transit_fit.cancel_fit("muscat3", "260720", "TOI-123"),
    "ttv_fit": lambda: ttv_fit.cancel_ttv_fit("TOI-123", "r1"),
}
# photometry holds its module lock from claim to registration, so sync_jobs and
# cancel cannot interleave there; only the fit pipelines need the in-process guard.
FIT_CASES = [c for c in CASES if c.values[1] in SYNC]


def _spawn_hook(monkeypatch, on_first_spawn):
    """Replace Popen so the first job spawn runs *on_first_spawn* and then fails."""
    real_popen = subprocess.Popen
    spawns = []

    def spawn(*args, **kwargs):
        if not kwargs.get("start_new_session"):
            return real_popen(*args, **kwargs)
        spawns.append(1)
        if len(spawns) == 1:
            on_first_spawn()
        raise OSError("spawn refused")

    monkeypatch.setattr(subprocess, "Popen", spawn)
    return spawns


def _rows(store, row_prefix):
    return [r for r in store.all() if r["key"].startswith(row_prefix)]


@pytest.mark.parametrize("launch, pipeline, row_prefix", FIT_CASES)
def test_sync_jobs_in_the_window_does_not_launch_the_job_twice(launch, pipeline, row_prefix, db, monkeypatch, tmp_path):  # noqa: F811
    store = job_store.DatabaseJobStore()
    monkeypatch.setattr(transit_fit, "_copy_lightcurve_for_timer", lambda src, dst: None)
    seen = {}

    def during():
        SYNC[pipeline]()
        seen["rows"] = [r["state"] for r in _rows(store, row_prefix)]

    spawns = _spawn_hook(monkeypatch, during)

    launch(monkeypatch, store, "ann", tmp_path)

    assert seen["rows"] == ["running"], "sync_jobs requeued the row of a launch that was still starting"
    assert len(spawns) == 1


@pytest.mark.parametrize("launch, pipeline, row_prefix", FIT_CASES)
def test_cancel_in_the_window_is_refused_instead_of_lost(launch, pipeline, row_prefix, db, monkeypatch, tmp_path):  # noqa: F811
    store = job_store.DatabaseJobStore()
    monkeypatch.setattr(transit_fit, "_copy_lightcurve_for_timer", lambda src, dst: None)
    seen = {}

    def during():
        seen["cancel"] = CANCEL[pipeline]()
        seen["rows"] = [r["state"] for r in _rows(store, row_prefix)]

    _spawn_hook(monkeypatch, during)

    launch(monkeypatch, store, "ann", tmp_path)

    assert seen["cancel"]["ok"] is False
    assert seen["rows"] == ["running"], "a cancel in the window must not write a state the launch then overwrites"


def _fail_prep(monkeypatch, pipeline):
    from muscat_db import photometry

    def boom(*args, **kwargs):
        raise OSError("disk full")

    target = {
        "photometry": (photometry, "_write_run_meta"),
        "transit_fit": (transit_fit, "_write_fit_inputs"),
        "ttv_fit": (ttv_fit, "write_ttv_inputs"),
    }[pipeline]
    monkeypatch.setattr(*target, boom)


@pytest.mark.parametrize("launch, pipeline, row_prefix", CASES)
def test_a_failure_before_the_spawn_releases_the_claim(launch, pipeline, row_prefix, db, monkeypatch, tmp_path):  # noqa: F811
    store = job_store.DatabaseJobStore()
    monkeypatch.setattr(transit_fit, "_copy_lightcurve_for_timer", lambda src, dst: None)
    _fail_prep(monkeypatch, pipeline)

    with pytest.raises(OSError, match="disk full"):
        launch(monkeypatch, store, "ann", tmp_path)

    assert store.count_claimed(pipeline) == 0
    assert [r["state"] for r in _rows(store, row_prefix)] == ["error"]
    assert not job_store.is_launching(_rows(store, row_prefix)[0]["key"])


@pytest.mark.parametrize("launch, pipeline, row_prefix", CASES)
def test_a_failed_relaunch_keeps_the_previous_runs_row(launch, pipeline, row_prefix, db, monkeypatch, tmp_path):  # noqa: F811
    store = job_store.DatabaseJobStore()
    monkeypatch.setattr(transit_fit, "_copy_lightcurve_for_timer", lambda src, dst: None)
    _spawn_hook(monkeypatch, lambda: None)
    launch(monkeypatch, store, "ann", tmp_path)  # first attempt only to learn the row's identity
    first = _rows(store, row_prefix)[0]
    store.save(
        type_=first["type"], inst=first["inst"], date=first["date"], target=first["target"],
        run_id=first["run_id"], run_name=first["run_name"], state="done", returncode=0,
        elapsed=321, started_at=1000.0, run_type="full", params="{}", user_name="bob",
    )

    launch(monkeypatch, store, "ann", tmp_path)  # a relaunch whose spawn fails

    (row,) = _rows(store, row_prefix)
    assert (row["state"], row["elapsed"], row["user_name"]) == ("done", 321, "bob")
    assert store.count_claimed(pipeline) == 0
