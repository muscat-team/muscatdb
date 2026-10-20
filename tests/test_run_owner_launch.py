"""Launching over, or rewriting, another user's run is refused (issue #272).

``start_run`` deletes the products of an existing run id before it launches,
and ``start_fit`` / ``start_ttv_fit`` clobber theirs, so a launch under the name
of someone else's run is a delete (and rewrites the record's author). The rule
for the delete routes therefore applies to every route that replaces or rewrites
an existing run:

* a launch under a run id that is already taken needs the same permission as a
  delete;
* a launch under a free run id stays open to everyone who can see the night;
* a re-run, a post-process ``apply`` and a post-process restore act on an
  existing run and need it too.

Unlike ``test_run_owner_checks`` these tests run the real ``start_*`` functions
(only ``Popen`` is stubbed), since that is where the files go.
"""
# ruff: noqa: F811

from __future__ import annotations

import subprocess

import pytest

from muscat_db import photometry as phot
from muscat_db import product_record
from muscat_db import web
from muscat_db.job_store import get_job_store
from tests.test_access_control_jobs import (  # noqa: F401
    INST,
    RUN,
    _post,
    client,
    env,
)
from tests.test_run_owner_checks import (  # noqa: F401
    DATE,
    TARGET,
    _make_group,
    _own_all,
    _set_author,
)


class _FakeProc:
    pid = 2**22 + 12345
    args: list = []
    returncode = 0
    stdout = stderr = None

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def communicate(self, *a, **k):
        return "", ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _no_subprocess(env, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakeProc())
    phot.raw_data_dir(INST, DATE).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(web, "_resolve_ttv_options", lambda options: (options, None))


def _phot_body(run_name=RUN):
    return {"inst": INST, "date": DATE, "target": TARGET, "test_run": True,
            "options": {"run_name": run_name, "overwrite": True}}


def _fit_body(run_name=RUN):
    return {"inst": INST, "date": DATE, "target": TARGET,
            "options": {"run_name": run_name, "run_mode": "new"}}


def _ttv_body(run_name=RUN):
    csv = "planet,epoch,tc,tc_unc\nb,0,2460000.1,0.001\nb,1,2460001.1,0.001\nb,2,2460002.1,0.001"
    return {"target": TARGET, "options": {"run_name": run_name, "clobber": True, "csv_content": csv}}


def _phot_dir():
    return phot.run_output_dir(INST, DATE, TARGET, RUN)


# path, body, run directory of the run it would replace
LAUNCHES = {
    "photometry/run": (_phot_body, "/api/photometry/run"),
    "transit-fit/run": (_fit_body, "/api/transit-fit/run"),
    "ttv-fit/start": (_ttv_body, "/api/ttv-fit/start"),
}
_LAUNCH = pytest.mark.parametrize("route", sorted(LAUNCHES))


def _launch(client, route, user, run_name=RUN):
    body, path = LAUNCHES[route]
    return _post(client, path, body(run_name), user=user)


def _author(rdir):
    return product_record.read_record(rdir)["author"]


def _rdir_of(route):
    from tests.test_run_owner_checks import ROUTES

    return ROUTES[route.split("/")[0] + "/delete"][1]()


@_LAUNCH
def test_launch_over_another_users_run_is_refused_and_leaves_it_untouched(client, route):
    _own_all("alice")
    rdir = _rdir_of(route)
    before = sorted(p.name for p in rdir.rglob("*"))
    resp = _launch(client, route, "bob")
    assert resp.status_code == 403
    assert sorted(p.name for p in rdir.rglob("*")) == before
    assert _author(rdir) == "alice"


@_LAUNCH
def test_owner_may_launch_over_their_own_run(client, route):
    _own_all("alice")
    assert _launch(client, route, "alice").status_code != 403


@_LAUNCH
def test_admin_may_launch_over_any_run(client, route):
    _own_all("alice")
    assert _launch(client, route, "root").status_code != 403


@_LAUNCH
def test_group_mate_may_launch_over_the_run(client, env, route):
    _own_all("alice")
    _make_group(env, "lab", "alice", "bob")
    assert _launch(client, route, "bob").status_code != 403


@pytest.mark.parametrize("route", ["photometry/run", "ttv-fit/start"])
def test_launch_under_a_free_run_name_stays_open_to_everyone(client, route):
    _own_all("alice")
    assert _launch(client, route, "bob", run_name="brandnew").status_code != 403


def test_unowned_existing_run_is_refused_to_a_user_in_no_group(client):
    _own_all("")
    assert _launch(client, "photometry/run", "bob").status_code == 403
    assert _phot_dir().is_dir()


# ── re-run of a job ─────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["photometry", "transit_fit", "ttv_fit"])
def test_rerun_of_another_users_job_is_refused(client, kind):
    _own_all("alice")
    key = next(j["key"] for j in get_job_store().all() if j["type"] == kind)
    resp = _post(client, "/api/jobs/rerun", {"key": key}, user="bob")
    assert resp.status_code == 403
    assert _author(_rdir_of(f"{kind.replace('_', '-')}/x")) == "alice"


def test_rerun_of_ones_own_job_is_allowed(client):
    _own_all("alice")
    key = f"photometry:{INST}/{DATE}/{TARGET}/{RUN}"
    assert _post(client, "/api/jobs/rerun", {"key": key}, user="alice").status_code != 403


# ── post-process rewrites the run's CSVs in place ───────────────────────

_PP = {"inst": INST, "date": DATE, "target": TARGET, "run": RUN}


@pytest.mark.parametrize("path,extra", [
    ("/api/photometry/postprocess", {"apply": True}),
    ("/api/photometry/postprocess/restore", {}),
])
def test_postprocess_changes_of_another_users_run_are_refused(client, path, extra):
    _own_all("alice")
    csv = _phot_dir() / f"{TARGET}_{INST}_gp_{DATE}.csv"
    before = csv.read_text()
    assert _post(client, path, {**_PP, **extra}, user="bob").status_code == 403
    assert csv.read_text() == before


def test_postprocess_preview_does_not_need_ownership(client):
    _own_all("alice")
    resp = _post(client, "/api/photometry/postprocess", {**_PP, "apply": False}, user="bob")
    assert resp.status_code != 403


def test_postprocess_apply_by_the_owner_is_not_refused(client):
    _own_all("alice")
    resp = _post(client, "/api/photometry/postprocess", {**_PP, "apply": True}, user="alice")
    assert resp.status_code != 403
    assert _set_author is not None
