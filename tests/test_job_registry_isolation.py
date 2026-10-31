"""The in-memory job registries start empty in every test.

``test_a_job_added_by_one_test`` leaves a ttv_fit job behind on purpose;
``test_the_next_test_does_not_see_it`` runs right after it in the same process
(pytest keeps definition order, and so does a single xdist worker) and fails if
the autouse reset in ``conftest.py`` stops clearing the registries. Under
``pytest -n`` the two may land on different workers, where the second test
passes trivially, so a serial run is what shows a regression.
"""

from __future__ import annotations

import os

from muscat_db import ttv_fit as ttv


class _Proc:
    pid = os.getpid()

    def poll(self):
        return None


def test_a_job_added_by_one_test(tmp_path):
    log = tmp_path / "harmonic.log"
    log.write_text("")
    with ttv._TTV_LOCK:
        ttv._TTV_JOBS["HIP67522/default"] = ttv.TTVFitJob(
            key="HIP67522/default", inst="_", date="_", target="HIP67522",
            cmd=["x"], proc=_Proc(), logf=open(log, "a"), log_path=log,
            run_type="full", run_id="default", run_name="default",
        )


def test_the_next_test_does_not_see_it():
    assert ttv._TTV_JOBS == {}
