"""Explicit group membership (issue #354).

The fixture is a seeded database in the implicit model: ``everyone`` holds
every user not excluded from it, and the scoped viewers ``andrew`` and
``hannah`` see only what ``unc`` and their own grants give them. What must
hold is that ``access explicit-groups`` gives every user, and the anonymous
viewer, exactly the rows they saw before, and that afterwards membership,
not exclusion, decides everything that used to read ``everyone``.
"""

from __future__ import annotations

import sqlite3

import pytest
from typer.testing import CliRunner

from muscat_db import access, access_explicit, access_groups as ag, run_ownership, target_keys
from muscat_db.cli import app as cli_app
from muscat_db.database import _apply_schema

KEY = access_explicit.KEY_PROPOSAL

# (instrument, obsdate, object, proposal_id)
ROWS = [
    ("sinistro", "260101", "OPENTGT", "OPEN-001"),  # shared with the team
    ("sinistro", "260102", "HIP1", "CON-002"),  # restricted; unc has a target grant
    ("sinistro", "260103", "OTHERTGT", "CON-002"),  # restricted
    ("qhy600", "260104", "KEYTGT", KEY),  # restricted; key collaborators only
    ("muscat2", "260105", "OPENTGT", ""),  # no-PROPID instrument
]
USERS = (("alice", 0, 0), ("boss", 1, 0), ("fukui", 0, 0), ("andrew", 0, 1), ("hannah", 0, 1))


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "muscat.db")
    monkeypatch.setenv("MUSCAT_DB_PATH", path)
    conn = sqlite3.connect(path)
    _apply_schema(conn)
    for inst, date, obj, pid in ROWS:
        conn.execute(
            "INSERT INTO summaries (instrument, obsdate, ccd, object, proposal_id) "
            "VALUES (?, ?, 0, ?, ?)",
            (inst, date, obj, pid),
        )
    conn.executemany(
        "INSERT INTO users (username, display_name, is_admin, scoped) VALUES (?, ?, ?, ?)",
        [(n, n, a, s) for n, a, s in USERS],
    )
    conn.executemany(
        "INSERT INTO restricted_proposals (proposal_id) VALUES (?)", [("CON-002",), (KEY,)]
    )
    conn.execute("INSERT INTO user_proposal_access (username, proposal_id) VALUES ('fukui', ?)", (KEY,))
    conn.commit()
    conn.close()
    ag.seed_from_blocklist(path)
    with sqlite3.connect(path) as conn:
        target_keys.refresh(conn)
        ag.ensure_group(conn, "unc", "UNC")
        for user in ("andrew", "hannah"):
            ag.add_member(conn, "unc", user)
        ag.add_grant(conn, "unc", proposal_id=KEY)
        ag.add_grant(conn, "unc", target="HIP1")
        conn.commit()
    return path


VIEWERS = (None, "alice", "boss", "fukui", "andrew", "hannah", "carol")


def _visible(db, user):
    acc = ag.load_access(db, user)
    return {r for r in ROWS if acc.frame_visible(*r)}


def _snapshot(db):
    return {v: _visible(db, v) for v in VIEWERS}


def test_fixture_is_the_implicit_model(db):
    assert _visible(db, "alice") == {ROWS[0], ROWS[4]}
    assert _visible(db, "andrew") == {ROWS[1], ROWS[3]}
    assert _visible(db, "fukui") == {ROWS[0], ROWS[3], ROWS[4]}
    # An account with no groups is implicitly in everyone today.
    assert _visible(db, "carol") == {ROWS[0], ROWS[4]}


def test_dry_run_reports_and_writes_nothing(db):
    before = _snapshot(db)
    report = access_explicit.run(db)
    assert not report.applied and not report.differences
    with sqlite3.connect(db) as conn:
        assert not ag.explicit_membership(conn)
        assert ag.group_exists(conn, ag.EVERYONE)
    assert _snapshot(db) == before


def test_migration_preserves_every_viewers_rows(db):
    before = _snapshot(db)
    report = access_explicit.run(db, extra_users=["carol"], apply=True)
    assert report.applied and report.differences == {}
    assert _snapshot(db) == before
    assert report.team_members == ["alice", "boss", "carol", "fukui"]
    assert report.key_members == ["andrew", "fukui", "hannah"]
    assert sorted(report.key_grants_revoked) == ["unc", "user:fukui"]
    assert report.removed_exclusions == ["andrew", "hannah"]
    with sqlite3.connect(db) as conn:
        assert ag.explicit_membership(conn)
        assert not ag.group_exists(conn, ag.EVERYONE)
        assert conn.execute("SELECT COUNT(*) FROM group_exclusions").fetchone()[0] == 0


def test_any_visibility_change_rolls_the_migration_back(db, monkeypatch):
    # A fault that drops the public copy would take the anonymous viewer's rows.
    real_copy = access_explicit._copy_grants

    def copy_except_public(conn, source, target):
        return 0 if target == ag.PUBLIC else real_copy(conn, source, target)

    monkeypatch.setattr(access_explicit, "_copy_grants", copy_except_public)
    before = _snapshot(db)
    report = access_explicit.run(db, apply=True)
    assert not report.applied
    assert report.differences == {"": (2, 0)}
    with sqlite3.connect(db) as conn:
        assert not ag.explicit_membership(conn)
        assert ag.group_exists(conn, ag.EVERYONE)
    assert _snapshot(db) == before


def test_migration_refuses_a_second_run_and_an_unseeded_db(db, tmp_path):
    access_explicit.run(db, apply=True)
    with pytest.raises(access_explicit.MigrationRefused, match="already explicit"):
        access_explicit.run(db)
    fresh = str(tmp_path / "fresh.db")
    with sqlite3.connect(fresh) as conn:
        _apply_schema(conn)
    with pytest.raises(access_explicit.MigrationRefused, match="groups-seed"):
        access_explicit.run(fresh)


def test_a_user_in_no_group_sees_nothing_after(db):
    access_explicit.run(db, apply=True)
    assert _visible(db, "newbie") == set()


def test_leaving_a_group_drops_its_rows_and_its_room(db):
    from muscat_db import chat
    access_explicit.run(db, apply=True)
    assert "group:lcokey" in chat._allowed_rooms("fukui")
    assert "chat:public" in chat._allowed_rooms("fukui")
    with sqlite3.connect(db) as conn:
        ag.remove_member(conn, access_explicit.KEY_GROUP, "fukui")
        conn.commit()
    assert ROWS[3] not in _visible(db, "fukui")
    assert "group:lcokey" not in chat._allowed_rooms("fukui")


def test_team_room_is_chat_public_not_a_group_room(db):
    from muscat_db import chat
    access_explicit.run(db, apply=True)
    assert chat._allowed_rooms("alice") == {"chat:public"}
    assert chat._allowed_rooms("andrew") == {"group:unc", "group:lcokey"}


def test_scoped_means_off_the_team_after(db):
    access_explicit.run(db, apply=True)
    assert access.is_scoped(db, "andrew") and not access.is_scoped(db, "alice")
    assert access.set_scoped(db, "alice", True)
    assert access.is_scoped(db, "alice")
    assert _visible(db, "alice") == set()
    assert access.set_scoped(db, "andrew", False)
    assert ROWS[0] in _visible(db, "andrew")


def test_unrestrict_and_restrict_follow_the_team_and_the_public_site(db):
    access_explicit.run(db, apply=True)
    assert access.unrestrict(db, "CON-002", granted_by="boss")
    assert ROWS[2] in _visible(db, "alice") and ROWS[2] in _visible(db, None)
    assert access.restrict(db, "CON-002", revoked_by="boss")
    assert ROWS[2] not in _visible(db, "alice") and ROWS[2] not in _visible(db, None)
    assert {r["proposal_id"] for r in access.list_restrictions(db)} == {"CON-002", KEY}


def test_team_membership_does_not_share_runs(db):
    access_explicit.run(db, apply=True)
    assert ag.TEAM_GROUP not in run_ownership.viewer_groups(db, "alice")
    assert run_ownership.viewer_groups(db, "fukui") == frozenset({access_explicit.KEY_GROUP})


def test_cli_dry_run_then_apply(db, monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_HTPASSWD_FILE", str(tmp_path / "htpasswd"))
    (tmp_path / "htpasswd").write_text("carol:$apr1$x$y\n")
    runner = CliRunner()
    dry = runner.invoke(cli_app, ["access", "explicit-groups"])
    assert dry.exit_code == 0, dry.output
    assert "Dry run" in dry.output and "carol" in dry.output
    applied = runner.invoke(cli_app, ["access", "explicit-groups", "--apply"])
    assert applied.exit_code == 0, applied.output
    assert "Applied" in applied.output
    again = runner.invoke(cli_app, ["access", "explicit-groups"])
    assert again.exit_code == 1 and "already explicit" in again.output


# ── htpasswd add: a new account names its group ───────────────────────


@pytest.fixture
def ht(db, monkeypatch, tmp_path):
    path = tmp_path / "htpasswd"
    path.write_text("")
    monkeypatch.setenv("MUSCAT_HTPASSWD_FILE", str(path))
    return path


def _add(*args):
    return CliRunner().invoke(cli_app, ["htpasswd", "add", *args, "--password", "pw-123456"])


def test_new_account_without_a_group_is_refused_after_the_migration(db, ht):
    access_explicit.run(db, apply=True)
    result = _add("newbie")
    assert result.exit_code == 1
    assert "--group" in result.output
    assert "newbie" not in ht.read_text()


def test_new_account_needs_no_group_before_the_migration(db, ht):
    # Before #354 a new account is implicitly on the team, as it always was.
    assert _add("newbie").exit_code == 0
    assert ROWS[0] in _visible(db, "newbie")


def test_new_account_joins_the_named_group(db, ht):
    access_explicit.run(db, apply=True)
    assert _add("newbie", "--group", "lcokey").exit_code == 0
    assert ROWS[3] in _visible(db, "newbie") and ROWS[0] not in _visible(db, "newbie")
    assert _add("teammate", "--group", ag.TEAM_GROUP).exit_code == 0
    assert ROWS[0] in _visible(db, "teammate")


def test_team_group_works_before_the_migration(db, ht):
    assert _add("teammate", "--group", ag.TEAM_GROUP).exit_code == 0
    assert not access.is_scoped(db, "teammate")


def test_no_group_is_an_explicit_opt_out_and_updates_need_no_group(db, ht):
    access_explicit.run(db, apply=True)
    result = _add("loner", "--no-group")
    assert result.exit_code == 0 and "sees nothing" in result.output
    assert _visible(db, "loner") == set()
    assert _add("loner").exit_code == 0  # a password change is not a new account


def test_unknown_or_reserved_group_is_refused(db, ht):
    assert _add("newbie", "--group", "nosuch").exit_code == 1
    assert _add("newbie", "--group", ag.PUBLIC).exit_code == 1


# ── the team's shared ephemeris sheet (#351 follow-up in #354) ──────────


def test_shared_sheet_is_withheld_from_non_members(db, monkeypatch):
    from muscat_db import web
    from muscat_db.database import set_user_ephem_sheet

    monkeypatch.setenv("MUSCAT_SHARED_EPHEM_SHEET_USER", "boss")
    set_user_ephem_sheet("boss", "https://docs.google.com/spreadsheets/d/ABCDEFGHIJKLMNOPQRSTUVWX/edit")
    access_explicit.run(db, apply=True)
    as_user = {}
    monkeypatch.setattr(web, "_request_user", lambda request: as_user["name"])
    for name, expect_sheet in (("alice", True), ("boss", True), ("andrew", False), (None, False)):
        as_user["name"] = name
        cfg = web._user_ephem_sheet_cfg(object(), shared=True)
        assert (cfg is not None) is expect_sheet, name


# ── review follow-ups ───────────────────────────────────────────────────


def test_groups_seed_is_refused_after_the_migration(db):
    access_explicit.run(db, apply=True)
    result = CliRunner().invoke(cli_app, ["access", "groups-seed"])
    assert result.exit_code == 1 and "explicit" in result.output
    with pytest.raises(RuntimeError, match="explicit"):
        ag.seed_from_blocklist(db)
    with sqlite3.connect(db) as conn:
        assert not ag.group_exists(conn, ag.EVERYONE)


def test_a_name_only_in_a_group_is_checked_and_kept_on_the_team(db):
    # dora has no users row and no htpasswd entry, only a group membership:
    # the old model still showed her everyone's rows.
    with sqlite3.connect(db) as conn:
        ag.ensure_group(conn, "visitors")
        ag.add_member(conn, "visitors", "dora")
        conn.commit()
    before = _visible(db, "dora")
    report = access_explicit.run(db, apply=True)
    assert report.applied and "dora" in report.team_members
    assert _visible(db, "dora") == before


def test_a_pre_existing_team_group_with_grants_is_refused(db):
    with sqlite3.connect(db) as conn:
        ag.ensure_group(conn, ag.TEAM_GROUP)
        ag.add_grant(conn, ag.TEAM_GROUP, proposal_id="CON-002")
        conn.commit()
    with pytest.raises(access_explicit.MigrationRefused, match="already holds"):
        access_explicit.run(db, apply=True)


def test_team_grants_and_revokes_are_mirrored_to_the_public_site(db):
    access_explicit.run(db, apply=True)
    selector = ag.check_selector(proposal_id="CON-002", instrument=None, target=None, date=None,
                                 date_from=None, date_to=None, instruments=("sinistro",))
    grant_id = access.grant_group(db, ag.TEAM_GROUP, selector, "share", "boss")
    assert ROWS[2] in _visible(db, "alice") and ROWS[2] in _visible(db, None)
    access.revoke_group_grant(db, grant_id, "boss")
    assert ROWS[2] not in _visible(db, "alice") and ROWS[2] not in _visible(db, None)


def test_granting_to_public_directly_is_refused(db):
    access_explicit.run(db, apply=True)
    result = CliRunner().invoke(cli_app, ["access", "group", "grant", ag.PUBLIC, "--proposal", "CON-002"])
    assert result.exit_code == 1 and "mirrors" in result.output
    assert ROWS[2] not in _visible(db, None)


def test_legacy_chat_keeps_a_custom_group_named_like_the_team(db):
    from muscat_db import chat
    with sqlite3.connect(db) as conn:
        ag.ensure_group(conn, ag.TEAM_GROUP)
        ag.add_member(conn, ag.TEAM_GROUP, "alice")
        conn.commit()
    assert f"group:{ag.TEAM_GROUP}" in chat._allowed_rooms("alice")
