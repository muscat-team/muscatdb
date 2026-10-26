"""Launch, re-run, delete and cancel routes: confirm, audit, notify (#307, #317).

A launch under a taken run id is refused with 409 unless it carries the
conflict's ``confirm_token``; the ownership 403 of #272 still comes first. A
confirmed overwrite, a delete and a cancel each leave a ``run_events`` row, and
the run's owner gets a private chat message when someone else made the change.

Fixture runs are ``RUN`` on the open night of ``OPENTGT``, set up by
``tests/test_run_owner_checks.py``.
"""
# ruff: noqa: F811

from __future__ import annotations

import json
import sqlite3

import pytest

from muscat_db import photometry as phot
from muscat_db import product_record
from muscat_db import run_events
from muscat_db import run_overwrite
from muscat_db import transit_fit as fit
from muscat_db import ttv_fit as ttv
from muscat_db.job_store import get_job_store
from tests.test_run_owner_checks import (  # noqa: F401
    DATE,
    INST,
    LAUNCHES,
    ROUTES,
    RUN,
    TARGET,
    _launch,
    _make_group,
    _own_all,
    _post,
    _tree,
    client,
    env,
    lax,
)

_LAUNCH = pytest.mark.parametrize("route", sorted(LAUNCHES))
LEVEL = {"photometry/run": "photometry", "transit-fit/run": "transit_fit", "ttv-fit/start": "ttv_fit"}


def _place(level):
    return ("", "") if level == "ttv_fit" else (INST, DATE)


def _launch_with(client, route, user, **extra):
    payload, rdir, path = LAUNCHES[route]
    body = {**json.loads(json.dumps(payload)), **extra}
    return _post(client, path, body, user=user), rdir()


def _events(env, level):
    return run_events.for_run(str(env), level, *_place(level), TARGET, RUN)


def _messages_to(env, user):
    with sqlite3.connect(env) as conn:
        try:
            return [r[0] for r in conn.execute(
                "SELECT text FROM chat_messages WHERE visible_to = ? ORDER BY id", (user,))]
        except sqlite3.OperationalError:
            return []


@pytest.fixture
def launches(monkeypatch):
    """Launchers that go through the route's guard and then succeed, so the
    route's after-launch audit runs. Returns the launch calls."""
    calls = []

    def ok(run_id, authorize_run):
        authorize_run(run_id)
        calls.append(run_id)
        return {"ok": True, "key": f"k/{run_id}", "run_id": run_id}

    monkeypatch.setattr(phot, "start_run",
                        lambda inst, date, target, *, options, authorize_run, **k: ok(options["run_name"], authorize_run))
    monkeypatch.setattr(fit, "start_fit",
                        lambda inst, date, target, options, *, authorize_run, **k: ok(options["run_name"], authorize_run))
    monkeypatch.setattr(ttv, "start_ttv_fit",
                        lambda target, options, user_name, authorize_run: ok(options["run_name"], authorize_run))
    return calls


# ── 409 instead of a silent overwrite ───────────────────────────────────


@_LAUNCH
def test_relaunching_your_own_run_asks_first_and_changes_nothing(lax, route):
    _own_all("alice")
    rdir = LAUNCHES[route][1]()
    before = _tree(rdir)

    resp, _ = _launch(lax, route, "alice")

    assert resp.status_code == 409
    body = resp.json()
    assert body["conflict"] == "run_exists" and body["own_run"] is True
    assert body["suggested_run_name"] == f"{RUN}_2"
    assert body["confirm_token"].startswith(run_overwrite.confirm_token(RUN, "alice", ""))
    assert _tree(rdir) == before


@_LAUNCH
def test_group_mate_relaunching_someone_elses_run_asks_first(lax, env, route):
    _own_all("alice")
    _make_group(env, "lab", "alice", "carol")
    rdir = LAUNCHES[route][1]()
    before = _tree(rdir)

    resp, _ = _launch(lax, route, "carol")

    assert resp.status_code == 409
    body = resp.json()
    assert (body["owner"], body["own_run"]) == ("alice", False)
    assert body["suggested_run_name"] == f"{RUN}_carol"
    assert _tree(rdir) == before


@_LAUNCH
def test_confirmed_overwrite_passes_the_guard(lax, env, route):
    _own_all("alice")
    _make_group(env, "lab", "alice", "carol")
    token = _launch(lax, route, "carol")[0].json()["confirm_token"]

    resp, _ = _launch_with(lax, route, "carol", confirm_overwrite=token)

    assert resp.status_code not in (403, 409)


@_LAUNCH
def test_a_confirmation_for_another_owner_does_not_count(lax, env, route):
    _own_all("alice")
    _make_group(env, "lab", "alice", "carol")
    stale = run_overwrite.confirm_token(RUN, "dave")

    resp, _ = _launch_with(lax, route, "carol", confirm_overwrite=stale)

    assert resp.status_code == 409


@_LAUNCH
def test_a_confirmation_from_before_someone_else_reran_the_run_does_not_count(lax, env, route):
    _own_all("alice")
    _make_group(env, "lab", "alice", "carol")
    token = _launch(lax, route, "carol")[0].json()["confirm_token"]
    rdir = LAUNCHES[route][1]()
    # dave re-runs it meanwhile: the record keeps alice as author.
    prior = product_record.read_record(rdir)
    rerun = product_record.carry_authorship(
        {**prior, "author": "dave", "created_at": "2099-01-01T00:00:00+00:00"}, prior)
    assert product_record.write_record(rdir, rerun) and rerun["author"] == "alice"

    resp, _ = _launch_with(lax, route, "carol", confirm_overwrite=token)

    assert resp.status_code == 409
    assert resp.json()["confirm_token"] != token


@_LAUNCH
def test_viewer_who_may_not_change_the_run_gets_403_not_a_popup(lax, route):
    _own_all("alice")
    token = run_overwrite.confirm_token(RUN, "alice")
    assert _launch(lax, route, "bob")[0].status_code == 403
    assert _launch_with(lax, route, "bob", confirm_overwrite=token)[0].status_code == 403


@_LAUNCH
def test_a_new_run_name_launches_without_asking(lax, route):
    _own_all("alice")
    resp, _ = _launch(lax, route, "alice", name="fresh-run")
    assert resp.status_code not in (403, 409)


def test_suggested_name_skips_a_run_that_already_exists(lax):
    _own_all("alice")
    phot.run_output_dir(INST, DATE, TARGET, f"{RUN}_2").mkdir(parents=True)
    body = _launch(lax, "photometry/run", "alice")[0].json()
    assert body["suggested_run_name"] == f"{RUN}_3"


# ── a run the viewer may not see ────────────────────────────────────────

# Inputs the fixture's viewers are denied: the restricted HIDDENTGT night.
_HIDDEN_INPUTS = {
    "photometry/run": lambda env: product_record.frames_inputs(env, INST, DATE, "HIDDENTGT")[0],
    "transit-fit/run": lambda env: [{"kind": "product", "level": "photometry", "instrument": INST,
                                     "obsdate": DATE, "target": "HIDDENTGT", "run_id": RUN}],
    "ttv-fit/start": lambda env: [{"kind": "product", "level": "transit_fit", "instrument": INST,
                                   "obsdate": DATE, "target": "HIDDENTGT", "run_id": RUN}],
}


def _hide_alices_run(env, route):
    """alice's run under RUN, on the open night, built from restricted inputs:
    absent from a group-mate's run list, though its name is taken."""
    rdir = LAUNCHES[route][1]()
    record = {**product_record.read_record(rdir), "author": "alice", "inputs": _HIDDEN_INPUTS[route](env)}
    assert product_record.write_record(rdir, record)
    return rdir


@_LAUNCH
@pytest.mark.parametrize("viewer", ["carol", "bob"], ids=["group-mate", "outsider"])
def test_a_hidden_run_is_only_a_taken_name(lax, env, route, viewer):
    _make_group(env, "lab", "alice", "carol")
    rdir = _hide_alices_run(env, route)
    before = _tree(rdir)

    resp, _ = _launch(lax, route, viewer)

    assert resp.status_code == 409
    body = resp.json()
    assert body["can_overwrite"] is False and "confirm_token" not in body
    assert body["suggested_run_name"] == f"{RUN}_{viewer}"
    assert "alice" not in resp.text
    assert _tree(rdir) == before


@_LAUNCH
def test_a_hidden_run_is_not_overwritten_even_with_its_owners_token(lax, env, route):
    _make_group(env, "lab", "alice", "carol")
    rdir = _hide_alices_run(env, route)
    token = _launch(lax, route, "alice")[0].json()["confirm_token"]  # alice sees her own run
    before = _tree(rdir)

    resp, _ = _launch_with(lax, route, "carol", confirm_overwrite=token)

    assert resp.status_code == 409 and resp.json()["can_overwrite"] is False
    assert _tree(rdir) == before
    assert _events(env, LEVEL[route]) == []


# ── Jobs page re-run ────────────────────────────────────────────────────


def _job_key(kind):
    return next(j["key"] for j in get_job_store().all() if j["type"] == kind and j["target"] == TARGET)


@pytest.mark.parametrize("kind", ["photometry", "transit_fit", "ttv_fit"])
def test_jobs_rerun_asks_and_save_as_launches_a_new_run(lax, launches, kind):
    _own_all("alice")
    key = _job_key(kind)

    first = _post(lax, "/api/jobs/rerun", {"key": key}, user="alice")
    assert first.status_code == 409
    name = first.json()["suggested_run_name"]

    again = _post(lax, "/api/jobs/rerun", {"key": key, "run_name": name}, user="alice")
    assert again.status_code == 200
    assert launches == [name]


@pytest.mark.parametrize("kind", ["photometry", "transit_fit", "ttv_fit"])
def test_jobs_rerun_overwrites_in_place_once_confirmed(lax, launches, kind):
    _own_all("alice")
    key = _job_key(kind)
    token = _post(lax, "/api/jobs/rerun", {"key": key}, user="alice").json()["confirm_token"]

    resp = _post(lax, "/api/jobs/rerun", {"key": key, "confirm_overwrite": token}, user="alice")

    assert resp.status_code == 200
    assert launches == [RUN]


# ── audit and notify ────────────────────────────────────────────────────


@_LAUNCH
def test_confirmed_overwrite_by_someone_else_is_audited_and_the_owner_told(lax, env, launches, route):
    level = LEVEL[route]
    _own_all("alice")
    _make_group(env, "lab", "alice", "carol")
    token = _launch(lax, route, "carol")[0].json()["confirm_token"]
    assert _events(env, level) == []  # a refused launch changed nothing

    assert _launch_with(lax, route, "carol", confirm_overwrite=token)[0].status_code == 200

    rows = _events(env, level)
    assert [(r["action"], r["actor"], r["owner"]) for r in rows] == [("overwrite", "carol", "alice")]
    messages = _messages_to(env, "alice")
    assert len(messages) == 1 and messages[0].startswith("carol overwrote your ")
    assert f"'{RUN}'" in messages[0]


@_LAUNCH
def test_overwriting_your_own_run_is_audited_without_a_message(lax, env, launches, route):
    level = LEVEL[route]
    _own_all("alice")
    token = _launch(lax, route, "alice")[0].json()["confirm_token"]

    assert _launch_with(lax, route, "alice", confirm_overwrite=token)[0].status_code == 200

    assert [r["action"] for r in _events(env, level)] == ["overwrite"]
    assert _messages_to(env, "alice") == []


@pytest.mark.parametrize("outcome", [
    {"ok": False, "error": "queue is full"},
    {"ok": True, "key": "k", "run_id": RUN, "already_running": True},
], ids=["refused", "already-running"])
def test_a_confirmed_launch_that_did_not_go_ahead_is_not_audited(lax, env, monkeypatch, outcome):
    def start_run(inst, date, target, *, options, authorize_run, **k):
        authorize_run(RUN)
        return outcome

    monkeypatch.setattr(phot, "start_run", start_run)
    _own_all("alice")
    _make_group(env, "lab", "alice", "carol")
    token = _launch(lax, "photometry/run", "carol")[0].json()["confirm_token"]

    _launch_with(lax, "photometry/run", "carol", confirm_overwrite=token)

    assert _events(env, "photometry") == []
    assert _messages_to(env, "alice") == []


@_LAUNCH
def test_a_queued_overwrite_is_audited_as_queued(lax, env, monkeypatch, route):
    """A queued launch has replaced nothing yet and may be cancelled before it
    starts, so neither the audit nor the owner's message may call it done."""
    def queued(run_id, authorize_run):
        authorize_run(run_id)
        return {"ok": True, "key": f"k/{run_id}", "queued": True, "run_id": run_id}

    monkeypatch.setattr(phot, "start_run",
                        lambda inst, date, target, *, options, authorize_run, **k: queued(options["run_name"], authorize_run))
    monkeypatch.setattr(fit, "start_fit",
                        lambda inst, date, target, options, *, authorize_run, **k: queued(options["run_name"], authorize_run))
    monkeypatch.setattr(ttv, "start_ttv_fit",
                        lambda target, options, user_name, authorize_run: queued(options["run_name"], authorize_run))
    _own_all("alice")
    _make_group(env, "lab", "alice", "carol")
    token = _launch(lax, route, "carol")[0].json()["confirm_token"]

    assert _launch_with(lax, route, "carol", confirm_overwrite=token)[0].status_code == 200

    rows = _events(env, LEVEL[route])
    assert [(r["action"], r["actor"], r["owner"]) for r in rows] == [("overwrite_queued", "carol", "alice")]
    messages = _messages_to(env, "alice")
    assert len(messages) == 1 and messages[0].startswith("carol queued an overwrite of your ")


@_LAUNCH
def test_a_new_run_is_not_an_overwrite(lax, env, launches, route):
    _own_all("alice")
    assert _launch(lax, route, "alice", name="fresh-run")[0].status_code == 200
    with sqlite3.connect(env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 0


DELETES = {
    "photometry/delete": "photometry",
    "transit-fit/delete": "transit_fit",
    "ttv-fit/delete": "ttv_fit",
}


@pytest.mark.parametrize("route", sorted(DELETES))
def test_delete_by_a_group_mate_is_audited_and_the_owner_told(client, env, route):
    level = DELETES[route]
    _own_all("alice")
    _make_group(env, "lab", "alice", "carol")
    payload, rdir = ROUTES[route]

    assert _post(client, f"/api/{route}", payload, user="carol").status_code == 200

    assert not rdir().exists()
    rows = _events(env, level)
    assert [(r["action"], r["actor"], r["owner"]) for r in rows] == [("delete", "carol", "alice")]
    messages = _messages_to(env, "alice")
    assert len(messages) == 1 and messages[0].startswith("carol deleted your ")


@pytest.mark.parametrize("route", sorted(DELETES))
def test_owner_deleting_their_own_run_is_audited_without_a_message(client, env, route):
    _own_all("alice")
    payload, _ = ROUTES[route]
    assert _post(client, f"/api/{route}", payload, user="alice").status_code == 200
    assert [r["action"] for r in _events(env, DELETES[route])] == ["delete"]
    assert _messages_to(env, "alice") == []


def test_a_failed_delete_is_not_audited(client, env, monkeypatch):
    _own_all("alice")
    monkeypatch.setattr(fit, "delete_fit", lambda *a, **k: {"ok": False, "delete_failed": True, "error": "x"})
    payload, _ = ROUTES["transit-fit/delete"]
    assert _post(client, "/api/transit-fit/delete", payload, user="alice").status_code == 500
    assert _events(env, "transit_fit") == []


CANCELS = {
    "photometry/cancel": ("photometry", phot, "cancel_run"),
    "transit-fit/cancel": ("transit_fit", fit, "cancel_fit"),
    "ttv-fit/cancel": ("ttv_fit", ttv, "cancel_ttv_fit"),
}


@pytest.mark.parametrize("route", sorted(CANCELS))
def test_cancel_by_a_group_mate_is_audited_and_the_owner_told(client, env, monkeypatch, route):
    level, module, name = CANCELS[route]
    monkeypatch.setattr(module, name, lambda *a, **k: {"ok": True, "key": "k"})
    _own_all("alice")
    _make_group(env, "lab", "alice", "carol")
    payload, _ = ROUTES[route]

    assert _post(client, f"/api/{route}", payload, user="carol").status_code == 200

    assert [(r["action"], r["actor"]) for r in _events(env, level)] == [("cancel", "carol")]
    assert _messages_to(env, "alice")[0].startswith("carol cancelled your ")


# ── the record after a re-run by someone else ───────────────────────────


def test_confirmed_photometry_overwrite_keeps_the_author_on_disk(lax, env):
    """The real start_run clears the run and rewrites its record before the
    (stubbed) process launch fails; the record must still name alice."""
    _own_all("alice")
    _make_group(env, "lab", "alice", "carol")
    rdir = LAUNCHES["photometry/run"][1]()
    token = _launch(lax, "photometry/run", "carol")[0].json()["confirm_token"]

    _launch_with(lax, "photometry/run", "carol", confirm_overwrite=token)

    record = product_record.read_record(rdir)
    assert (record["author"], record["modified_by"]) == ("alice", "carol")


def test_rerun_by_a_group_mate_keeps_the_run_its_authors(env, tmp_path):
    """The real TTV writer, as a launch calls it: the record keeps alice."""
    rdir = ttv.ttv_output_dir(TARGET, RUN)
    _own_all("alice")
    ttv._write_ttv_product(rdir, {"run_name": RUN, "inputs": []}, "carol")
    record = product_record.read_record(rdir)
    assert (record["author"], record["modified_by"]) == ("alice", "carol")
