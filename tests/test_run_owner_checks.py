"""Who may delete or cancel a run (issue #272).

The six routes that destroy or stop a run used to check only proposal
visibility, so on an install with nothing restricted any logged-in user could
delete anyone's run. The rule now is:

* an admin may;
* the run's owner may (the author in its ``_product.json``, else the
  ``user_name`` of its job row);
* a user who shares an explicit group with the owner may (``everyone``,
  ``admin`` and the hidden ``user:<name>`` groups do not count);
* a run with no owner at all may be changed by any viewer who is in some
  explicit group.

Fixture runs are on the open night 260103 of ``OPENTGT``, so ``alice`` and
``bob`` can both see them.
"""
# ruff: noqa: F811

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from muscat_db import access_groups
from muscat_db import photometry as phot
from muscat_db import product_record
from muscat_db import transit_fit as fit
from muscat_db import ttv_fit as ttv
from muscat_db.job_store import get_job_store
# The ``env`` and ``client`` fixtures come from the module that builds the same
# open/restricted-night fixture runs this file acts on.
from tests.test_access_control_jobs import (  # noqa: F401
    INST,
    RUN,
    _post,
    client,
    env,
)

DATE = "260103"
TARGET = "OPENTGT"
_RUN = {"inst": INST, "date": DATE, "target": TARGET, "run": RUN, "run_id": RUN}
_TTV = {"target": TARGET, "run_name": RUN}

# (path, payload, run directory of the run it acts on)
ROUTES = {
    "photometry/delete": (_RUN, lambda: phot.run_output_dir(INST, DATE, TARGET, RUN)),
    "photometry/cancel": (_RUN, lambda: phot.run_output_dir(INST, DATE, TARGET, RUN)),
    "transit-fit/delete": (_RUN, lambda: fit.fit_output_dir(INST, DATE, TARGET, RUN)),
    "transit-fit/cancel": (_RUN, lambda: fit.fit_output_dir(INST, DATE, TARGET, RUN)),
    "ttv-fit/delete": (_TTV, lambda: ttv.ttv_output_dir(TARGET, RUN)),
    "ttv-fit/cancel": (_TTV, lambda: ttv.ttv_output_dir(TARGET, RUN)),
}
DELETES = [r for r in ROUTES if r.endswith("/delete")]
_ALL = pytest.mark.parametrize("route", sorted(ROUTES))


def _call(client, route, user):
    payload, rdir = ROUTES[route]
    return _post(client, f"/api/{route}", payload, user=user), rdir()


def _set_author(rdir, author):
    path = rdir / product_record.RECORD_NAME
    record = json.loads(path.read_text())
    record["author"] = author
    path.write_text(json.dumps(record))


def _own_all(author):
    for _, rdir in ROUTES.values():
        _set_author(rdir(), author)


def _make_group(db, name, *members, builtin=0):
    if name == access_groups.EVERYONE:
        # Creating `everyone` switches visibility onto the groups (#228 step 2),
        # so give it what a real seed gives it; a grantless `everyone` would
        # hide the run (404) before the owner check could refuse it (403).
        access_groups.seed_from_blocklist(db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT OR IGNORE INTO groups (name, builtin) VALUES (?, ?)", (name, builtin))
        conn.executemany(
            "INSERT OR IGNORE INTO group_members (groupname, username) VALUES (?, ?)",
            [(name, m) for m in members],
        )


# ── owned runs ──────────────────────────────────────────────────────────


@_ALL
def test_owner_may_change_their_run(client, route):
    _own_all("alice")
    resp, rdir = _call(client, route, "alice")
    assert resp.status_code != 403
    if route.endswith("/delete"):
        assert resp.status_code == 200 and not rdir.exists()


@_ALL
def test_owner_name_match_ignores_case(client, route):
    _own_all("Alice")
    assert _call(client, route, "alice")[0].status_code != 403


@_ALL
def test_admin_may_change_any_run(client, route):
    _own_all("alice")
    assert _call(client, route, "root")[0].status_code != 403


@_ALL
def test_other_user_is_refused_and_the_run_is_untouched(client, route):
    _own_all("alice")
    resp, rdir = _call(client, route, "bob")
    assert resp.status_code == 403
    assert rdir.is_dir() and (rdir / product_record.RECORD_NAME).is_file()


@_ALL
def test_user_sharing_a_group_with_the_owner_may_change_the_run(client, env, route):
    _own_all("alice")
    _make_group(env, "lab", "alice", "bob")
    assert _call(client, route, "bob")[0].status_code != 403


@_ALL
def test_a_group_the_owner_is_not_in_does_not_help(client, env, route):
    _own_all("alice")
    _make_group(env, "lab", "alice")
    _make_group(env, "other", "bob")
    assert _call(client, route, "bob")[0].status_code == 403


@_ALL
@pytest.mark.parametrize("name,builtin", [("everyone", 1), ("admin", 1), ("user:alice", 0)])
def test_implicit_groups_do_not_count_as_sharing(client, env, route, name, builtin):
    _own_all("alice")
    _make_group(env, name, "alice", "bob", builtin=builtin)
    assert _call(client, route, "bob")[0].status_code == 403


# ── owner from the job row, and runs with no owner ──────────────────────


def _job_owner(key, user):
    get_job_store().save(
        type_=key[0], inst=key[1], date=key[2], target=TARGET, state="done", returncode=0,
        elapsed=1, started_at=1.0, run_id=RUN, run_name=RUN, run_type="full", user_name=user,
    )


@pytest.mark.parametrize("route", ["photometry/delete", "transit-fit/delete"])
def test_owner_falls_back_to_the_job_row_when_the_record_has_no_author(client, route):
    kind = "photometry" if route.startswith("photometry") else "transit_fit"
    _own_all("")
    _job_owner((kind, INST, DATE), "alice")
    assert _call(client, route, "bob")[0].status_code == 403
    assert _call(client, route, "alice")[0].status_code == 200


@_ALL
def test_unowned_run_is_refused_to_a_user_in_no_group(client, route):
    _own_all("")
    resp, rdir = _call(client, route, "bob")
    assert resp.status_code == 403 and rdir.is_dir()


@_ALL
def test_unowned_run_may_be_changed_by_a_user_in_a_group(client, env, route):
    _own_all("")
    _make_group(env, "lab", "bob")
    assert _call(client, route, "bob")[0].status_code != 403


@_ALL
def test_unowned_run_in_only_an_implicit_group_is_still_refused(client, env, route):
    _own_all("")
    _make_group(env, "everyone", "bob", builtin=1)
    assert _call(client, route, "bob")[0].status_code == 403


@_ALL
def test_admin_may_change_an_unowned_run(client, route):
    _own_all("")
    assert _call(client, route, "root")[0].status_code != 403


# ── a failed delete is reported, not swallowed ──────────────────────────


@pytest.mark.parametrize("route", ["transit-fit/delete", "ttv-fit/delete"])
def test_failed_delete_is_an_error_and_keeps_the_job_row(client, monkeypatch, route):
    _own_all("alice")
    kind = "transit_fit" if route.startswith("transit") else "ttv_fit"
    before = {j["key"] for j in get_job_store().all() if j["type"] == kind}

    def boom(*a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr("shutil.rmtree", boom)
    resp, rdir = _call(client, route, "alice")
    assert resp.status_code == 500
    body = resp.json()
    assert body["ok"] is False and body["error"]
    assert rdir.is_dir()
    assert {j["key"] for j in get_job_store().all() if j["type"] == kind} == before


# ── the UI does not offer what the server would refuse ──────────────────


def _button(html: str, button_id: str) -> str:
    start = html.index(f'id="{button_id}"')
    return html[html.rfind("<button", 0, start): html.index(">", start)]


def _page(client, path, user, **params):
    return client.get(path, headers={"X-Forwarded-User": user, "X-MuSCAT-Proxy-Secret": "proxy-secret"},
                      params=params)


@pytest.mark.parametrize("path,params", [
    ("/photometry", {"inst": INST, "date": DATE, "target": TARGET, "run": RUN}),
    ("/transit-fit", {"inst": INST, "date": DATE, "target": TARGET, "run": RUN}),
])
@pytest.mark.parametrize("user,disabled", [("alice", False), ("root", False), ("bob", True)])
def test_page_delete_button_is_disabled_for_a_viewer_who_may_not_delete(
    client, path, params, user, disabled
):
    _own_all("alice")
    page = _page(client, path, user, **params)
    assert page.status_code == 200
    assert ("disabled" in _button(page.text, "delete-btn")) is disabled
    assert ("disabled" in _button(page.text, "cancel-btn")) is disabled


@pytest.mark.parametrize("user,can", [("alice", True), ("root", True), ("bob", False)])
def test_ttv_run_list_says_whether_the_viewer_may_change_each_run(client, user, can):
    _own_all("alice")
    resp = client.get("/api/ttv-fit/runs", params={"target": TARGET},
                      headers={"X-Forwarded-User": user, "X-MuSCAT-Proxy-Secret": "proxy-secret"})
    runs = {r["run_name"]: r for r in resp.json()["runs"]}
    assert runs[RUN]["can_modify"] is can


@pytest.mark.parametrize("kind", ["photometry", "transit_fit", "ttv_fit"])
@pytest.mark.parametrize("user,disabled", [("alice", False), ("bob", True)])
def test_jobs_page_cancel_button_is_disabled_for_a_viewer_who_may_not_cancel(client, kind, user, disabled):
    _own_all("alice")
    get_job_store().save(
        type_=kind, inst="" if kind == "ttv_fit" else INST, date="" if kind == "ttv_fit" else DATE,
        target=TARGET, state="running", returncode=None, elapsed=1, started_at=1.0,
        run_id=RUN, run_name=RUN, run_type="full", user_name="alice",
    )
    page = _page(client, "/jobs", user)
    assert page.status_code == 200
    row = next(
        r for r in page.text.split('<tr class="job-row"')
        if f'data-type="{kind}"' in r and 'data-status="running"' in r and f'data-job-target="{TARGET}"' in r
    )
    row = row[: row.index("</tr>")]
    cancel = row[row.rindex("<button", 0, row.index('data-job-action="cancel"')): row.index(">Cancel</button>")]
    assert ("disabled" in cancel) is disabled


# ── launching over an existing run (review of #276) ─────────────────────
#
# start_run / start_fit / start_ttv_fit delete the products of the run id they
# derive before launching, so a launch under another user's run name is a
# delete the six delete/cancel routes would have refused.

_OPTS = {"run_name": RUN, "overwrite": True}
LAUNCHES = {
    "photometry/run": (
        {"inst": INST, "date": DATE, "target": TARGET, "options": _OPTS, "test_run": False},
        lambda: phot.run_output_dir(INST, DATE, TARGET, RUN),
        "/api/photometry/run",
    ),
    "transit-fit/run": (
        {"inst": INST, "date": DATE, "target": TARGET, "options": {"run_name": RUN}},
        lambda: fit.fit_output_dir(INST, DATE, TARGET, RUN),
        "/api/transit-fit/run",
    ),
    "ttv-fit/start": (
        {"target": TARGET, "options": {"run_name": RUN}},
        lambda: ttv.ttv_output_dir(TARGET, RUN),
        "/api/ttv-fit/start",
    ),
}
_LAUNCH = pytest.mark.parametrize("route", sorted(LAUNCHES))


@pytest.fixture
def lax(env, client):
    """The client, but launches are stubbed to fail after the guard and a
    server error comes back as a 500 instead of raising."""
    from fastapi.testclient import TestClient

    from muscat_db import web

    def no_launch(*a, **k):
        raise OSError("launch stubbed")

    mp = pytest.MonkeyPatch()
    for mod in (phot, fit, ttv):
        mp.setattr(mod.subprocess, "Popen", no_launch)
    # start_run looks for the night's raw frames before it derives the run id.
    (Path(os.environ["MUSCAT_DATA_DIR"]) / "Sinistro" / DATE).mkdir(parents=True, exist_ok=True)
    yield TestClient(web.app, client=("127.0.0.1", 12345), raise_server_exceptions=False)
    mp.undo()


def _tree(rdir):
    return {p.relative_to(rdir).as_posix(): (p.stat().st_size, p.read_bytes() if p.is_file() else b"")
            for p in sorted(rdir.rglob("*"))}


def _launch(client, route, user, name=None):
    payload, rdir, path = LAUNCHES[route]
    if name:
        payload = json.loads(json.dumps(payload))
        payload["options"]["run_name"] = name
    return _post(client, path, payload, user=user), rdir()


@_LAUNCH
def test_launch_over_another_users_run_is_refused_and_the_files_stay(lax, route):
    _own_all("alice")
    rdir = LAUNCHES[route][1]()
    before = _tree(rdir)

    resp, _ = _launch(lax, route, "bob")

    assert resp.status_code == 403
    assert _tree(rdir) == before
    assert json.loads((rdir / product_record.RECORD_NAME).read_text())["author"] == "alice"


@_LAUNCH
def test_owner_admin_and_group_mate_may_relaunch_a_run(lax, env, route):
    _own_all("alice")
    _make_group(env, "lab", "alice", "carol")
    for user in ("alice", "root", "carol"):
        _own_all("alice")  # a launch rewrites the record with its own author
        assert _launch(lax, route, user)[0].status_code != 403, user


@_LAUNCH
def test_a_new_run_name_stays_open_to_anyone_who_can_see_the_night(lax, route):
    _own_all("alice")
    resp, _ = _launch(lax, route, "bob", name="bobs-own-run")
    assert resp.status_code != 403


@_LAUNCH
def test_launch_over_an_unowned_run_is_refused_to_a_user_in_no_group(lax, route):
    _own_all("")
    rdir = LAUNCHES[route][1]()
    before = _tree(rdir)

    assert _launch(lax, route, "bob")[0].status_code == 403
    assert _tree(rdir) == before


@pytest.mark.parametrize("kind", ["photometry", "transit_fit", "ttv_fit"])
def test_rerun_of_another_users_job_is_refused(lax, kind):
    _own_all("alice")
    key = next(j["key"] for j in get_job_store().all()
               if j["type"] == kind and j["target"] == TARGET)
    rdir = {"photometry": ROUTES["photometry/delete"], "transit_fit": ROUTES["transit-fit/delete"],
            "ttv_fit": ROUTES["ttv-fit/delete"]}[kind][1]()
    before = _tree(rdir)

    assert _post(lax, "/api/jobs/rerun", {"key": key}, user="bob").status_code == 403
    assert _tree(rdir) == before
    assert _post(lax, "/api/jobs/rerun", {"key": key}, user="alice").status_code != 403


POST = {"inst": INST, "date": DATE, "target": TARGET, "run": RUN}


def test_postprocess_apply_and_restore_are_refused_to_another_user(lax):
    _own_all("alice")
    rdir = ROUTES["photometry/delete"][1]()
    before = _tree(rdir)

    for path, extra in (("postprocess", {"apply": True}), ("postprocess/restore", {})):
        resp = _post(lax, f"/api/photometry/{path}", {**POST, **extra}, user="bob")
        assert resp.status_code == 403, path
    assert _tree(rdir) == before


def test_postprocess_dry_run_stays_open_to_anyone_who_can_see_the_run(lax):
    _own_all("alice")
    resp = _post(lax, "/api/photometry/postprocess", {**POST, "apply": False}, user="bob")
    assert resp.status_code != 403


def test_postprocess_apply_is_allowed_to_the_owner(lax):
    _own_all("alice")
    resp = _post(lax, "/api/photometry/postprocess", {**POST, "apply": True}, user="alice")
    assert resp.status_code != 403
