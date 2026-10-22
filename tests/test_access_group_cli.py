"""`muscat-db access group ...`: groups, members and grants by hand (issue #228,
rollout step 4).

Grants written here are what the group evaluator reads, so each test checks
the effect through :func:`access_groups.load_access`, not only the rows. A
revoked grant is moved to ``group_grants_revoked`` rather than deleted, so who
could see what, and when, stays answerable.
"""

from __future__ import annotations

import sqlite3

import pytest
from typer.testing import CliRunner

from muscat_db import access, access_groups
from muscat_db.cli import app as cli_app
from muscat_db.database import _APP_OWNED_TABLES, _apply_schema

# (instrument, obsdate, object, proposal_id)
ROWS = [
    ("sinistro", "260101", "OPENTGT", "OPEN-001"),
    ("sinistro", "260102", "TOI-6109", "CON-002"),
    ("sinistro", "260103", "OTHERTGT", "CON-002"),
    ("muscat3", "260104", "TOI-6109", "CON-002"),
    ("qhy600", "260105", "HIDDENTGT", "KEY-SECRET"),
]


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "muscat.db")
    conn = sqlite3.connect(path)
    _apply_schema(conn)
    for inst, date, obj, pid in ROWS:
        conn.execute(
            "INSERT INTO summaries (instrument, obsdate, ccd, object, proposal_id) "
            "VALUES (?, ?, 0, ?, ?)",
            (inst, date, obj, pid),
        )
    for name in ("alice", "bob"):
        conn.execute("INSERT INTO users (username, display_name) VALUES (?, ?)", (name, name))
    conn.commit()
    conn.close()
    monkeypatch.setenv("MUSCAT_DB_PATH", path)
    return path


def _run(*args):
    return CliRunner().invoke(cli_app, ["access", "group", *args])


def _text(result) -> str:
    """CLI output with Rich's line wrapping undone."""
    return " ".join(result.output.split())


def _visible(db, user):
    acc = access_groups.load_access(db, user)
    return {r for r in ROWS if acc.frame_visible(*r)}


def _grant_ids(db, group):
    with sqlite3.connect(db) as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT id FROM group_grants WHERE groupname = ? ORDER BY id", (group,)
            )
        ]


# ── create, add, remove ─────────────────────────────────────────────────


def test_create_add_and_grant_a_target_to_a_group(db):
    assert _run("create", "toi6109", "--description", "TOI-6109 team").exit_code == 0
    assert _run("add", "toi6109", "alice").exit_code == 0
    result = _run("grant", "toi6109", "--target", "TOI-6109", "--by", "admin")
    assert result.exit_code == 0, result.output
    assert _visible(db, "alice") == {r for r in ROWS if r[2] == "TOI-6109"}
    assert _visible(db, "bob") == set()


def test_create_twice_is_reported_and_exits_1(db):
    _run("create", "team")
    result = _run("create", "team")
    assert result.exit_code == 1
    assert "already exists" in _text(result)


@pytest.mark.parametrize("name", ["everyone", "admin", "user:alice", "", "has space", "x" * 65])
def test_create_refuses_reserved_and_malformed_names(db, name):
    result = _run("create", name)
    assert result.exit_code == 1, result.output
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0


def test_add_to_a_missing_group_is_refused(db):
    result = _run("add", "nosuch", "alice")
    assert result.exit_code == 1
    assert "no group" in _text(result).lower()


@pytest.mark.parametrize("group,hint", [
    ("everyone", "access scope"),
    ("admin", "htpasswd promote"),
    ("user:alice", "user:"),
])
def test_add_to_an_implicit_group_is_refused_with_the_right_pointer(db, group, hint):
    result = _run("add", group, "bob")
    assert result.exit_code == 1
    assert hint in _text(result)


def test_add_an_unknown_user_warns_but_adds(db):
    _run("create", "team")
    result = _run("add", "team", "carol")
    assert result.exit_code == 0
    assert "no users row" in _text(result)


def test_remove_member_drops_their_access(db):
    _run("create", "team")
    _run("add", "team", "alice")
    _run("grant", "team", "--proposal", "CON-002")
    assert _visible(db, "alice")
    assert _run("remove", "team", "alice").exit_code == 0
    assert _visible(db, "alice") == set()
    assert _run("remove", "team", "alice").exit_code == 1


# ── grant shapes ────────────────────────────────────────────────────────


def test_grant_to_user_group_creates_it_with_the_user_as_member(db):
    result = _run("grant", "user:alice", "--proposal", "KEY-SECRET")
    assert result.exit_code == 0, result.output
    assert _visible(db, "alice") == {ROWS[4]}


def test_grant_fields_are_anded(db):
    _run("create", "team")
    _run("add", "team", "alice")
    _run("grant", "team", "--proposal", "CON-002", "--instrument", "sinistro")
    assert _visible(db, "alice") == {ROWS[1], ROWS[2]}


def test_grant_a_date_range(db):
    _run("create", "team")
    _run("add", "team", "alice")
    assert _run("grant", "team", "--from", "260102", "--to", "260104").exit_code == 0
    assert _visible(db, "alice") == {ROWS[1], ROWS[2], ROWS[3]}


def test_grant_a_single_night(db):
    _run("create", "team")
    _run("add", "team", "alice")
    _run("grant", "team", "--instrument", "muscat3", "--date", "260104")
    assert _visible(db, "alice") == {ROWS[3]}


def test_grant_proposal_uses_the_observed_spelling(db):
    _run("create", "team")
    _run("grant", "team", "--proposal", "con-002")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT proposal_id FROM group_grants").fetchone()[0] == "CON-002"


def test_identical_grant_is_not_duplicated(db):
    _run("create", "team")
    _run("grant", "team", "--proposal", "CON-002")
    result = _run("grant", "team", "--proposal", "CON-002")
    assert result.exit_code == 1
    assert len(_grant_ids(db, "team")) == 1


@pytest.mark.parametrize("args,msg", [
    ([], "at least one"),
    (["--instrument", "hubble"], "unknown instrument"),
    (["--date", "2601"], "YYMMDD"),
    (["--from", "260105", "--to", "260101"], "after"),
    (["--to", "260101"], "--from"),
    (["--date", "260101", "--from", "260101"], "either"),
])
def test_grant_rejects_bad_selectors(db, args, msg):
    _run("create", "team")
    result = _run("grant", "team", *args)
    assert result.exit_code == 1, result.output
    assert msg in _text(result)
    assert _grant_ids(db, "team") == []


def test_grant_to_a_missing_group_is_refused(db):
    result = _run("grant", "nosuch", "--proposal", "CON-002")
    assert result.exit_code == 1
    assert _grant_ids(db, "nosuch") == []


def test_grant_to_admin_is_refused(db):
    result = _run("grant", "admin", "--proposal", "CON-002")
    assert result.exit_code == 1
    assert "htpasswd promote" in _text(result)


def test_grant_that_matches_nothing_warns(db):
    _run("create", "team")
    result = _run("grant", "team", "--target", "NO-SUCH-TARGET")
    assert result.exit_code == 0
    assert "matches no observation" in _text(result)


# ── revoke keeps history ────────────────────────────────────────────────


def test_revoke_moves_the_grant_to_history(db):
    _run("create", "team")
    _run("add", "team", "alice")
    _run("grant", "team", "--proposal", "CON-002", "--note", "for the paper", "--by", "jerome")
    (gid,) = _grant_ids(db, "team")
    result = _run("revoke", str(gid), "--by", "john")
    assert result.exit_code == 0, result.output
    assert _grant_ids(db, "team") == []
    assert _visible(db, "alice") == set()
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT id, groupname, proposal_id, note, granted_by, revoked_by, revoked_at "
            "FROM group_grants_revoked"
        ).fetchone()
    assert row[:6] == (gid, "team", "CON-002", "for the paper", "jerome", "john")
    assert row[6]


def test_revoke_an_unknown_id_exits_1(db):
    assert _run("revoke", "999").exit_code == 1


def test_revoke_target_keeps_history_too(db):
    access.grant_target(db, "alice", "TOI-6109", "jerome")
    assert access.revoke_target(db, "alice", "TOI-6109")
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT target, groupname FROM group_grants_revoked"
        ).fetchall() == [("TOI-6109", "user:alice")]


def _revoked_proposal_grants(db):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT groupname, proposal_id, revoked_by FROM group_grants_revoked"
        ).fetchall()


def test_restrict_keeps_the_everyone_grant_in_history(db):
    access_groups.seed_from_blocklist(db)
    result = CliRunner().invoke(cli_app, ["access", "restrict", "CON-002", "--by", "john"])
    assert result.exit_code == 0, result.output
    assert _revoked_proposal_grants(db) == [("everyone", "CON-002", "john")]


def test_revoke_keeps_the_user_grant_in_history(db):
    access_groups.seed_from_blocklist(db)
    runner = CliRunner()
    runner.invoke(cli_app, ["access", "grant", "alice", "CON-002", "--by", "jerome"])
    result = runner.invoke(cli_app, ["access", "revoke", "alice", "CON-002", "--by", "john"])
    assert result.exit_code == 0, result.output
    assert _revoked_proposal_grants(db) == [("user:alice", "CON-002", "john")]


def test_revoked_grants_survive_a_rebuild():
    assert "group_grants_revoked" in _APP_OWNED_TABLES


# ── list and explain ────────────────────────────────────────────────────


def test_list_shows_groups_members_and_grant_ids(db):
    _run("create", "team", "--description", "the team")
    _run("add", "team", "alice")
    _run("grant", "team", "--target", "TOI-6109")
    (gid,) = _grant_ids(db, "team")
    result = _run("list")
    assert result.exit_code == 0, result.output
    assert "team" in _text(result) and "alice" in _text(result)
    assert f"#{gid}" in _text(result) and "TOI-6109" in _text(result)


def test_list_one_group(db):
    _run("create", "team")
    _run("create", "other")
    result = _run("list", "other")
    assert result.exit_code == 0
    assert "other" in _text(result) and "team" not in _text(result)


def test_explain_flags_a_grant_that_matches_nothing(db):
    _run("create", "team")
    _run("add", "team", "alice")
    _run("grant", "team", "--target", "NO-SUCH-TARGET")
    _run("grant", "team", "--target", "TOI-6109")
    info = access.explain_user(db, "alice")
    dead = info["dead_group_grants"]
    assert [g["target"] for g in dead] == ["NO-SUCH-TARGET"]
    result = CliRunner().invoke(cli_app, ["access", "explain", "alice"])
    assert "NO-SUCH-TARGET" in _text(result) and "matches no observation" in _text(result)
