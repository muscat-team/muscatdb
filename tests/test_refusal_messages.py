"""A refused or failed launch says why, and what the user can do about it.

Two things used to hide the reason. An ownership refusal (issue #272) read
"only the run's owner, an admin, or someone in the owner's group may change it"
without saying who the viewer was taken to be, which group they were in or whose
run it was. That mattered on the dev server, reached without the authenticating
proxy: the viewer was anonymous, so every owned run was refused, with nothing to
say so. And every ``HTTPException`` came back as FastAPI's ``{"detail": ...}``
while the pages read ``res.error``, so the photometry page showed only
"Error: failed to start".
"""
# ruff: noqa: F811

from __future__ import annotations

from pathlib import Path

import pytest

from muscat_db import jobs, run_ownership
from tests.test_access_control_jobs import INST, _post, client, env  # noqa: F401
from tests.test_run_owner_checks import (  # noqa: F401
    LAUNCHES,
    ROUTES,
    _call,
    _launch,
    _make_group,
    _own_all,
    lax,
)

# -- the message itself --------------------------------------------------------


def _msg(viewer, groups=(), owner="alice", action="overwrite", offer_new_run=True):
    return run_ownership.refusal_message(viewer, frozenset(groups), owner, action, offer_new_run)


def test_message_names_the_viewer_their_group_and_the_owner():
    msg = _msg("bob", {"lab"})
    assert "You are logged in as bob under the lab group." in msg
    assert "This run belongs to alice, who is not in that group" in msg
    assert "You do not have permission to overwrite it." in msg
    assert "Please contact an admin if this is a mistake." in msg


def test_message_lists_several_groups_in_order():
    msg = _msg("bob", {"obs", "lab"})
    assert "under the lab and obs groups." in msg
    assert "who is not in any of those groups" in msg


def test_message_for_a_viewer_in_no_group():
    msg = _msg("bob")
    assert "You are logged in as bob, who is not in any group." in msg
    assert "only alice, an admin, or someone in a group with alice" in msg


def test_message_for_an_unowned_run_and_a_viewer_in_no_group():
    msg = _msg("bob", owner="")
    assert "This run has no recorded owner" in msg
    assert "only an admin or a member of a group" in msg
    assert "ask an admin to add you to a group" in msg


def test_message_for_a_viewer_who_is_not_logged_in():
    msg = _msg(None)
    assert msg.startswith("You are not logged in")
    assert "This run belongs to alice." in msg
    # The dev server reached without the proxy is the usual way to get here.
    assert "log in" in msg.lower()


@pytest.mark.parametrize("owner", ["anonymous", "Anonymous"])
def test_anonymous_owner_reads_as_no_owner(owner):
    assert "no recorded owner" in _msg("bob", owner=owner)


def test_new_run_name_is_offered_only_for_a_launch():
    assert "new run name" in _msg("bob", {"lab"}, offer_new_run=True)
    assert "new run name" not in _msg("bob", {"lab"}, action="cancel", offer_new_run=False)


# -- what the routes send back -------------------------------------------------


def test_launch_refusal_explains_itself(lax, env):
    _own_all("alice")
    _make_group(env, "lab", "bob")
    for route in sorted(LAUNCHES):
        _own_all("alice")
        resp, _ = _launch(lax, route, "bob")
        body = resp.json()
        assert resp.status_code == 403, route
        assert body["ok"] is False, route
        assert "You are logged in as bob under the lab group." in body["error"], route
        assert "alice" in body["error"] and "new run name" in body["error"], route
        # Clients that read FastAPI's key keep working.
        assert body["detail"] == body["error"], route


@pytest.mark.parametrize("route,action", [
    ("photometry/cancel", "cancel"), ("photometry/delete", "delete"),
    ("transit-fit/cancel", "cancel"), ("transit-fit/delete", "delete"),
    ("ttv-fit/cancel", "cancel"), ("ttv-fit/delete", "delete"),
])
def test_cancel_and_delete_refusals_name_the_action(client, route, action):
    _own_all("alice")
    resp, _ = _call(client, route, "bob")
    body = resp.json()
    assert resp.status_code == 403
    assert f"You do not have permission to {action} it." in body["error"]
    assert "new run name" not in body["error"]


def test_any_api_http_error_carries_ok_and_error(client):
    # A restricted night is a 404, the same as a missing one, so its existence
    # does not leak; the reason must still reach the page.
    resp = _post(client, "/api/photometry/run",
                 {"inst": INST, "date": "260102", "target": "HIDDENTGT"}, user="bob")
    assert resp.status_code == 404
    body = resp.json()
    assert body["ok"] is False and body["error"] == body["detail"]


def test_a_malformed_request_says_which_field_is_wrong(client):
    resp = _post(client, "/api/photometry/run", {"inst": "muscat3", "date": 260102, "target": ["x"]})
    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False
    assert "target" in body["error"]
    assert isinstance(body["detail"], list)  # FastAPI's own detail is kept


def test_a_failed_engine_launch_says_it_is_a_server_problem(lax):
    # Only photometry gets as far as Popen here: the fixture night has no light
    # curves for a transit fit and no fits for a TTV run. A new run name, so the
    # launch is not stopped first by the overwrite confirmation (#317).
    _own_all("alice")
    body = _launch(lax, "photometry/run", "alice", name="fresh-run")[0].json()
    assert body["ok"] is False
    assert "launch stubbed" in body["error"]
    assert "not a problem with your settings" in body["error"]


def test_server_fault_keeps_the_cause_and_points_to_an_admin():
    assert jobs.server_fault("The server could not start the transit fit", OSError("no timer")) == (
        "The server could not start the transit fit (no timer). "
        "This is not a problem with your settings; please contact an admin."
    )
    assert "()" not in jobs.server_fault("Something failed")


# -- the pages show what came back ---------------------------------------------

_TEMPLATES = Path(run_ownership.__file__).parent / "templates"


@pytest.mark.parametrize("template,endpoint", [
    ("photometry.html", "/api/photometry/run"),
    ("transit_fit.html", "/api/transit-fit/run"),
    ("ephemeris.html", "/api/ttv-fit/start"),
    ("jobs.html", "/api/jobs/rerun"),
])
def test_launch_handlers_parse_through_the_shared_helper(template, endpoint):
    """A bare ``fetch().then(r => r.json())`` turns a proxy's HTML error page
    into "Unexpected token '<'"; MuscatFetch.fetchJson words it instead."""
    html = (_TEMPLATES / template).read_text()
    assert f"MuscatFetch.fetchJson('{endpoint}'" in html
    assert f"fetch('{endpoint}'" not in html.replace(f"fetchJson('{endpoint}'", "")
