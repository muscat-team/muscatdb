"""A request refused before it reaches a route says why and what to do.

"authentication required" did not say whether the user had not logged in or
had logged in but the request came round the proxy (the dev port reached
directly, or a proxy whose shared secret no longer matches): both look the same
from the page. "account not provisioned" did not say which identity arrived
or who can fix it, and "cross-origin request rejected" read like an attack
report to someone who had simply opened a stale tab.
"""

from __future__ import annotations

import pytest

from muscat_db import auth
from tests.test_cf_access_identity import SECRET, _client, _headers, db  # noqa: F401


@pytest.fixture
def required(db, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MUSCAT_REQUIRE_AUTH", "1")
    return db


def test_no_login_at_all_says_how_to_log_in(required):
    r = _client().get("/api/jobs")
    assert r.status_code == 401
    body = r.json()
    assert body["ok"] is False
    assert body["error"].startswith("You are not logged in.")
    assert "not the server's port directly" in body["error"]


def test_a_login_that_came_round_the_proxy_says_it_could_not_be_verified(required):
    r = _client().get("/api/jobs", headers={"X-Forwarded-User": "jerome", auth.PROXY_SECRET_HEADER: "wrong"})
    assert r.status_code == 401
    msg = r.json()["error"]
    assert msg.startswith("Your login could not be verified")
    assert "contact an admin" in msg


def test_an_unprovisioned_account_names_the_identity_and_who_can_fix_it(db):  # noqa: F811
    r = _client().get("/whoami", headers=_headers("stranger@univ.edu"))
    assert r.status_code == 403
    msg = r.json()["error"]
    assert "stranger@univ.edu" in msg
    assert "no MuSCAT-DB account" in msg and "contact an admin" in msg


def test_a_cross_origin_post_says_to_reload_the_page(db):  # noqa: F811
    r = _client().post("/api/jobs/rerun", json={"key": "x"},
                       headers={**_headers("jerome"), "Origin": "https://elsewhere.example"})
    assert r.status_code == 403
    msg = r.json()["error"]
    assert "did not come from a MuSCAT-DB page" in msg
    assert "Reload the page" in msg
