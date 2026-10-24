"""Allow-only groups and grants (issue #228).

The fixture is a database from before the groups, with its rules in the
#144/#202 blocklist tables. What has to hold is that the migration
(``seed_from_blocklist``) gives every viewer exactly what the blocklist did,
and that the evaluator agrees with itself between Python (``frame_visible``)
and SQL (``sql_visible``).
"""

from __future__ import annotations

import sqlite3

import pytest
from typer.testing import CliRunner

from muscat_db import access, access_groups, target_keys
from muscat_db.access_groups import Access
from muscat_db.cli import app as cli_app
from muscat_db.database import _APP_OWNED_TABLES, _apply_schema

# (instrument, obsdate, object, proposal_id)
ROWS = [
    ("sinistro", "260101", "OPENTGT", "OPEN-001"),
    ("sinistro", "260102", "MIXEDTGT", "OPEN-001"),
    ("sinistro", "260103", "MIXEDTGT", "KEY-SECRET"),
    ("qhy600", "260104", "HIDDENTGT", "KEY-SECRET"),
    ("muscat2", "260105", "OPENTGT", ""),
    ("muscat3", "260106", "TOI-1234", "OTHER-002"),
]


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "muscat.db")
    conn = sqlite3.connect(path)
    _apply_schema(conn)
    for inst, date, obj, pid in ROWS:
        conn.execute(
            "INSERT INTO summaries (instrument, obsdate, ccd, object, proposal_id) "
            "VALUES (?, ?, 0, ?, ?)",
            (inst, date, obj, pid),
        )
    for name, admin, scoped in (
        ("alice", 0, 0),
        ("boss", 1, 0),
        ("fukui", 0, 0),
        ("andrew", 0, 1),
    ):
        conn.execute(
            "INSERT INTO users (username, display_name, is_admin, scoped) VALUES (?, ?, ?, ?)",
            (name, name, admin, scoped),
        )
    # A database from before the groups: its rules live in the blocklist
    # tables, which only the migration (`seed_from_blocklist`) reads.
    conn.execute("INSERT INTO restricted_proposals (proposal_id) VALUES ('key-secret')")
    conn.executemany(
        "INSERT INTO user_proposal_access (username, proposal_id) VALUES (?, ?)",
        [("fukui", "KEY-SECRET"), ("andrew", "OPEN-001")],
    )
    conn.commit()
    conn.close()
    return path


def _visible(db, user):
    acc = access_groups.load_access(db, user)
    return {r for r in ROWS if acc.frame_visible(*r)}


def test_unseeded_groups_grant_nothing(db):
    assert _visible(db, "alice") == set()


def test_seeding_gives_every_viewer_what_the_blocklist_did(db):
    access_groups.seed_from_blocklist(db)
    # spot checks of the shapes that matter
    assert ("sinistro", "260103", "MIXEDTGT", "KEY-SECRET") not in _visible(db, "alice")
    assert ("sinistro", "260103", "MIXEDTGT", "KEY-SECRET") in _visible(db, "fukui")
    assert ("muscat2", "260105", "OPENTGT", "") in _visible(db, "alice")
    assert _visible(db, "boss") == set(ROWS)
    assert ("muscat2", "260105", "OPENTGT", "") not in _visible(db, "andrew")
    assert _visible(db, "andrew") == {
        ("sinistro", "260101", "OPENTGT", "OPEN-001"),
        ("sinistro", "260102", "MIXEDTGT", "OPEN-001"),
    }
    assert all(r[3] != "KEY-SECRET" for r in _visible(db, None))


def test_seeding_is_idempotent_and_dry_run_writes_nothing(db):
    dry = access_groups.seed_from_blocklist(db, dry_run=True)
    assert dry.changes
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM group_grants").fetchone()[0] == 0
    first = access_groups.seed_from_blocklist(db)
    assert first.changes == dry.changes
    assert access_groups.seed_from_blocklist(db).changes == 0


def test_a_proposal_ingested_after_seeding_is_hidden_until_reseeded(db):
    access_groups.seed_from_blocklist(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO summaries (instrument, obsdate, ccd, object, proposal_id) "
            "VALUES ('sinistro', '260201', 0, 'NEWTGT', 'NEW-003')"
        )
    new = ("sinistro", "260201", "NEWTGT", "NEW-003")
    acc = access_groups.load_access(db, "alice")
    assert not acc.frame_visible(*new)  # allow-only default: hidden, not leaked
    access_groups.seed_from_blocklist(db)
    assert access_groups.load_access(db, "alice").frame_visible(*new)


def test_lco_frames_without_propid_stay_visible(db):
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO summaries (instrument, obsdate, ccd, object, proposal_id) "
            "VALUES ('sinistro', '260301', 0, 'BLANKTGT', '')"
        )
    report = access_groups.seed_from_blocklist(db)
    assert report.blank_lco_instruments == ["sinistro"]
    assert access_groups.load_access(db, "alice").frame_visible("sinistro", "260301", "BLANKTGT", "")
    assert ("sinistro", "260301", "BLANKTGT", "") not in _visible(db, "andrew")


def test_restricted_proposal_with_no_grant_is_hidden_from_everyone_but_admin(db):
    access_groups.seed_from_blocklist(db)
    for user in ("alice", "andrew", None):
        assert not any(r[3] == "KEY-SECRET" for r in _visible(db, user))
    assert any(r[3] == "KEY-SECRET" for r in _visible(db, "boss"))


def _every_row(db, acc):
    clause, params = acc.sql_visible()
    with sqlite3.connect(db) as conn:
        return {
            tuple(r)
            for r in conn.execute(
                f"SELECT instrument, obsdate, object, proposal_id FROM summaries WHERE {clause}",
                params,
            )
        }


def test_sql_visible_agrees_with_frame_visible(db):
    access_groups.seed_from_blocklist(db)
    for user in ("alice", "boss", "fukui", "andrew", None):
        acc = access_groups.load_access(db, user)
        assert _every_row(db, acc) == {r for r in ROWS if acc.frame_visible(*r)}, user


def _grant_to(db, group, **kw):
    with sqlite3.connect(db) as conn:
        access_groups.ensure_group(conn, group)
        access_groups.add_member(conn, group, "alice")
        access_groups.add_grant(conn, group, **kw)
        conn.commit()


def test_target_grant_resolves_spellings_and_agrees_in_sql(db):
    padded = ("sinistro", "260107", "TOI 01234.01", "KEY-SECRET")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO summaries (instrument, obsdate, ccd, object, proposal_id) "
            "VALUES (?, ?, 0, ?, ?)",
            padded,
        )
        conn.commit()
    _grant_to(db, "tgt", target="TOI-1234")
    acc = access_groups.load_access(db, "alice")
    assert acc.frame_visible(*padded)
    assert acc.frame_visible("muscat3", "260106", "TOI-1234", "OTHER-002")
    assert not acc.frame_visible("sinistro", "260103", "MIXEDTGT", "KEY-SECRET")
    assert _every_row(db, acc) >= {padded, ("muscat3", "260106", "TOI-1234", "OTHER-002")}
    assert _every_row(db, acc) == {r for r in ROWS + [padded] if acc.frame_visible(*r)}


def test_target_grant_spelled_without_the_hyphen_matches_the_observed_object(db):
    # Found in the #228 acceptance run: a grant typed without the hyphen the
    # OBJECT header carries matched no frame, because this path compared names
    # with only spaces and case ignored.
    _grant_to(db, "tgt", target="TOI1234")
    acc = access_groups.load_access(db, "alice")
    assert acc.frame_visible("muscat3", "260106", "TOI-1234", "OTHER-002")
    assert not acc.frame_visible("qhy600", "260104", "HIDDENTGT", "KEY-SECRET")


def test_target_grants_read_stored_object_keys(db, monkeypatch):
    # The per-request cost used to be one normalizer call per distinct object
    # (~3,500 on production) for every scoped page view. Stored keys make it
    # one SELECT; the rule runs only for objects or grants with no stored key.
    _grant_to(db, "tgt", target="TOI-1234")
    with sqlite3.connect(db) as conn:
        target_keys.refresh(conn)
        conn.commit()
    calls: list[str] = []
    real = target_keys.target_key
    monkeypatch.setattr(target_keys, "target_key", lambda n: calls.append(n) or real(n))
    acc = access_groups.load_access(db, "alice")
    assert calls == []
    assert acc.frame_visible("muscat3", "260106", "TOI-1234", "OTHER-002")


def test_a_name_override_does_not_change_what_a_target_grant_covers(db, monkeypatch):
    # An admin rename on the target page regroups objects for display. Before
    # stored keys, web's normalizer honoured overrides, so renaming HIDDENTGT to
    # TOI-1234 silently handed its KEY-SECRET night to every TOI-1234 grantee.
    from muscat_db import web
    from muscat_db.database import set_norm_name_override

    access_groups.seed_from_blocklist(db)
    _grant_to(db, "tgt", target="TOI-1234")
    set_norm_name_override(db, "HIDDENTGT", "TOI1234")
    monkeypatch.setenv("MUSCAT_DB_PATH", db)
    token = web._CURRENT_USER.set("alice")
    try:
        denied = web._viewer_denied()
    finally:
        web._CURRENT_USER.reset(token)
    assert not access.is_denied("OTHER-002", denied, instrument="muscat3", obsdate="260106", obj="TOI-1234")
    assert access.is_denied("KEY-SECRET", denied, instrument="qhy600", obsdate="260104", obj="HIDDENTGT")


def test_fields_of_one_grant_are_anded(db):
    _grant_to(db, "g", instrument="sinistro", date_from="260102", date_to="260103")
    acc = access_groups.load_access(db, "alice")
    assert acc.frame_visible("sinistro", "260102", "X", "P")
    assert acc.frame_visible("sinistro", "260103", "X", "P")
    assert not acc.frame_visible("sinistro", "260101", "X", "P")
    assert not acc.frame_visible("sinistro", "260104", "X", "P")
    assert not acc.frame_visible("qhy600", "260102", "X", "P")


def test_single_date_grant_defaults_date_to(db):
    with sqlite3.connect(db) as conn:
        access_groups.ensure_group(conn, "g")
        access_groups.add_grant(conn, "g", date_from="260102")
        assert conn.execute("SELECT date_to FROM group_grants").fetchone()[0] == "260102"
        with pytest.raises(ValueError):
            access_groups.add_grant(conn, "g", date_to="260102")


def test_a_wildcard_grant_is_rejected_by_the_schema(db):
    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO group_grants (groupname) VALUES ('g')")


def test_no_groups_tables_fails_closed(tmp_path):
    path = str(tmp_path / "old.db")
    sqlite3.connect(path).executescript("CREATE TABLE users (username TEXT, is_admin INTEGER);")
    acc = access_groups.load_access(path, "alice")
    assert acc.grants == ()
    assert acc.sql_visible() == ("0", [])


def test_empty_and_admin_clauses():
    assert Access("a", True, frozenset()).sql_visible() == ("1", [])
    assert Access("a", False, frozenset()).sql_visible() == ("0", [])
    assert Access("a", True, frozenset({"x"})).cache_key == ("admin",)
    key = Access("a", False, frozenset({"everyone", "user:a"})).cache_key
    assert key == Access("b", False, frozenset({"user:a", "everyone"})).cache_key


def test_grant_tables_survive_a_rebuild():
    for table in ("groups", "group_members", "group_exclusions", "group_grants"):
        assert table in _APP_OWNED_TABLES


def test_cli_seed_migrates_the_blocklist(db, monkeypatch):
    monkeypatch.setenv("MUSCAT_DB_PATH", db)
    runner = CliRunner()
    seeded = runner.invoke(cli_app, ["access", "groups-seed"])
    assert seeded.exit_code == 0, seeded.output
    assert "user:fukui  proposal_id=KEY-SECRET" in seeded.output
    assert ("sinistro", "260103", "MIXEDTGT", "KEY-SECRET") in _visible(db, "fukui")
    assert ("sinistro", "260103", "MIXEDTGT", "KEY-SECRET") not in _visible(db, "alice")
