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

from muscat_db import job_store, transit_fit
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
