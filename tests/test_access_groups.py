"""Allow-only groups and grants (issue #228, rollout step 1).

The group model is not wired into the web app yet; what has to hold is that,
seeded from the #144/#202 blocklist, it gives every viewer exactly what the
blocklist does (``parity``), and that its evaluator agrees with itself between
Python (``frame_visible``) and SQL (``sql_visible``).
"""

from __future__ import annotations

import sqlite3

import pytest
from typer.testing import CliRunner

from muscat_db import access, access_groups
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
    ("muscat3", "260106", "TOI-6109", "OTHER-002"),
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
    conn.commit()
    conn.close()
    access.restrict(path, "key-secret", "test")
    access.grant(path, "fukui", "KEY-SECRET", "test")
    access.grant(path, "andrew", "OPEN-001", "test")
    return path


def _visible(db, user, normalize=None):
    acc = access_groups.load_access(db, user, normalize)
    return {r for r in ROWS if acc.frame_visible(*r)}


def test_unseeded_groups_grant_nothing_so_parity_flags_it(db):
    assert _visible(db, "alice") == set()
    assert access_groups.parity(db)


def test_seeded_groups_match_the_blocklist_for_every_viewer(db):
    access_groups.seed_from_blocklist(db)
    assert access_groups.parity(db) == []
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
    assert access_groups.parity(db)  # allow-only default, reported rather than leaked
    access_groups.seed_from_blocklist(db)
    assert access_groups.parity(db) == []


def test_lco_frames_without_propid_stay_visible(db):
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO summaries (instrument, obsdate, ccd, object, proposal_id) "
            "VALUES ('sinistro', '260301', 0, 'BLANKTGT', '')"
        )
    report = access_groups.seed_from_blocklist(db)
    assert report.blank_lco_instruments == ["sinistro"]
    assert access_groups.parity(db) == []
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
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO summaries (instrument, obsdate, ccd, object, proposal_id) "
            "VALUES ('sinistro', '260107', 0, 'TOI 6109', 'KEY-SECRET')"
        )
        conn.commit()
    _grant_to(db, "tgt", target="TOI-6109")
    norm = lambda s: s.upper().replace(" ", "").replace("-", "")  # noqa: E731
    acc = access_groups.load_access(db, "alice", norm)
    assert acc.frame_visible("sinistro", "260107", "TOI 6109", "KEY-SECRET")
    assert acc.frame_visible("muscat3", "260106", "TOI-6109", "OTHER-002")
    assert not acc.frame_visible("sinistro", "260103", "MIXEDTGT", "KEY-SECRET")
    assert _every_row(db, acc) >= {
        ("sinistro", "260107", "TOI 6109", "KEY-SECRET"),
        ("muscat3", "260106", "TOI-6109", "OTHER-002"),
    }
    assert _every_row(db, acc) == {
        r
        for r in ROWS + [("sinistro", "260107", "TOI 6109", "KEY-SECRET")]
        if acc.frame_visible(*r)
    }


def test_target_grant_without_normalizer_matches_spaces_and_case_only(db):
    _grant_to(db, "tgt", target="toi-6109")
    acc = access_groups.load_access(db, "alice")
    assert acc.frame_visible("muscat3", "260106", "TOI-6109", "OTHER-002")
    assert not acc.frame_visible("muscat3", "260106", "TOI6109", "OTHER-002")


def test_target_grants_normalize_each_object_once(db):
    # Production has thousands of objects and web's normalizer is not free, so
    # resolving several target grants must not normalize the catalog once per
    # grant. The grant spellings match no object verbatim, so every call on an
    # object name below comes from the object side.
    _grant_to(db, "tgt", target="toi 6109")
    _grant_to(db, "tgt2", target="hidden-tgt")
    seen: list[str] = []

    def norm(s):
        seen.append(s)
        return s.upper().replace(" ", "").replace("-", "")

    acc = access_groups.load_access(db, "alice", norm)
    objects = {r[2] for r in ROWS}
    assert max(seen.count(o) for o in objects) == 1
    assert acc.frame_visible("muscat3", "260106", "TOI-6109", "OTHER-002")
    assert acc.frame_visible("qhy600", "260104", "HIDDENTGT", "KEY-SECRET")
    assert not acc.frame_visible("sinistro", "260103", "MIXEDTGT", "KEY-SECRET")


def test_viewer_denied_reads_name_overrides_once(db, monkeypatch):
    # The web normalizer used to re-read target_overrides (a new connection
    # plus a schema migration) on every call: ~3500 times per page for a
    # scoped viewer with a target grant on the production catalog.
    from muscat_db import web

    access_groups.seed_from_blocklist(db)
    _grant_to(db, "tgt", target="toi 6109")
    reads: list[str] = []
    real = web._get_norm_name_overrides

    def counted(path):
        reads.append(path)
        return real(path)

    monkeypatch.setenv("MUSCAT_DB_PATH", db)
    monkeypatch.setattr(web, "_get_norm_name_overrides", counted)
    token = web._CURRENT_USER.set("alice")
    try:
        denied = web._viewer_denied()
    finally:
        web._CURRENT_USER.reset(token)
    assert len(reads) == 1
    assert not access.is_denied("OTHER-002", denied, instrument="muscat3", obsdate="260106", obj="TOI-6109")
    assert access.is_denied("KEY-SECRET", denied, instrument="sinistro", obsdate="260103", obj="MIXEDTGT")


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


def test_cli_seed_then_check(db, monkeypatch):
    monkeypatch.setenv("MUSCAT_DB_PATH", db)
    runner = CliRunner()
    assert runner.invoke(cli_app, ["access", "groups-check"]).exit_code == 1
    seeded = runner.invoke(cli_app, ["access", "groups-seed"])
    assert seeded.exit_code == 0, seeded.output
    ok = runner.invoke(cli_app, ["access", "groups-check"])
    assert ok.exit_code == 0, ok.output
    assert "agree" in ok.output
