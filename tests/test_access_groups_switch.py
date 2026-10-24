"""The group model is the only source of visibility (issue #228).

The groups decide, allow-only: a proposal no group was granted is hidden from
every non-admin, including one ingested after the seed. ``restrict``,
``unrestrict``, ``grant``, ``revoke`` and ``scope`` write the groups only, and
the first of them seeds the groups on a fresh database. The #144/#202 blocklist
tables are read only by ``access groups-seed``, the migration; a database that
still holds rules there but has no groups fails closed.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from muscat_db import access, access_groups, web
from muscat_db.cli import app as cli_app
from muscat_db.coord import CoordRepr
from muscat_db.database import (
    SCHEMA,
    ScopedDenied,
    _insert_summary_rows,
    _populate_targets,
    _summary_rows,
    sql_not_denied,
)

pytestmark = pytest.mark.usefixtures("mock_target_coord_resolution")

_PROXY_SECRET = "proxy-secret"
_NEW = "NEW2026B-099"


def _headers(user: str) -> dict:
    return {"X-Forwarded-User": user, "X-MuSCAT-Proxy-Secret": _PROXY_SECRET}


def _insert_frames(conn, rows):
    conn.executemany(
        """INSERT INTO frames
           (instrument, obsdate, ccd, filename, object, jd_start, ut_start,
            exptime, read_mode, filter, ra, declination, airmass, focus, pa,
            proposal_id)
           VALUES (?, ?, 0, ?, ?, ?, '00:00:00', 10, 'fast', 'gp', '', '', 1, 0, 0, ?)""",
        rows,
    )


def _rebuild_summaries(conn) -> None:
    conn.execute("DELETE FROM summaries")
    _insert_summary_rows(conn, _summary_rows(conn))
    conn.execute("DELETE FROM targets")
    _populate_targets(conn)


def _build_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.create_aggregate("coord_repr", 2, CoordRepr)
    conn.executescript(SCHEMA)
    _insert_frames(conn, [
        ("sinistro", "260101", "SIN_2601010001", "OPENTGT", 1.0, "OPEN-001"),
        ("sinistro", "260102", "SIN_2601020001", "MIXEDTGT", 2.0, "OPEN-001"),
        ("sinistro", "260103", "SIN_2601030001", "MIXEDTGT", 3.0, "KEY-SECRET"),
        ("qhy600", "260104", "QHY_2601040001", "HIDDENTGT", 4.0, "KEY-SECRET"),
        ("muscat2", "260105", "MSCT2_2601050001", "M2TGT", 5.0, ""),
    ])
    _rebuild_summaries(conn)
    for name, admin in (("alice", 0), ("boss", 1), ("fukui", 0)):
        conn.execute(
            "INSERT INTO users (username, display_name, is_admin) VALUES (?, ?, ?)",
            (name, name, admin),
        )
    conn.execute(
        "INSERT OR REPLACE INTO db_meta (key, value) VALUES ('last_build_at', '1700000000')"
    )
    conn.commit()
    conn.close()
    access.restrict(path, "KEY-SECRET", "test")
    access.grant(path, "fukui", "KEY-SECRET", "test")


def _ingest_new_proposal(path: str) -> None:
    """A proposal first seen after the seed, as the nightly ingest adds it."""
    conn = sqlite3.connect(path)
    conn.create_aggregate("coord_repr", 2, CoordRepr)
    _insert_frames(conn, [("qhy600", "260110", "QHY_2601100001", "NEWTGT", 9.0, _NEW)])
    _rebuild_summaries(conn)
    conn.commit()
    conn.close()


def _summary_keys(path: str) -> list[tuple[str, str, str, str]]:
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT instrument, obsdate, object, proposal_id FROM summaries ORDER BY 1, 2, 3"
        ).fetchall()


def _visible(path: str, user: str | None) -> set[tuple[str, str, str, str]]:
    """Rows the web would show *user*, through the same helpers it uses."""
    denied = access.denied_proposal_ids_for(path, user)
    return {
        k
        for k in _summary_keys(path)
        if not access.is_denied(k[3], denied, instrument=k[0], obsdate=k[1], obj=k[2])
    }


def _visible_sql(path: str, user: str | None) -> set[tuple[str, str, str, str]]:
    clause, params = sql_not_denied(access.denied_proposal_ids_for(path, user))
    with sqlite3.connect(path) as conn:
        return set(
            conn.execute(
                "SELECT instrument, obsdate, object, proposal_id FROM summaries "
                f"WHERE {clause}",
                params,
            ).fetchall()
        )


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "muscat.db")
    _build_db(path)
    monkeypatch.setenv("MUSCAT_DB_PATH", path)
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path / "no-prose-output"))
    monkeypatch.setenv("MUSCAT_TIMER_DIR", str(tmp_path / "no-timer-output"))
    monkeypatch.setenv("MUSCAT_TMPDIR", str(tmp_path / "muscat-tmp"))
    return path


@pytest.fixture
def seeded(db):
    access_groups.seed_from_blocklist(db)
    return db


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("MUSCAT_PROXY_SECRET", _PROXY_SECRET)
    web._index_cache.clear()
    return TestClient(web.app, client=("127.0.0.1", 12345))


# ── which model decides ─────────────────────────────────────────────────


def _bare_db(tmp_path) -> str:
    """The fixture database with no access rule written yet."""
    path = str(tmp_path / "bare.db")
    conn = sqlite3.connect(path)
    conn.create_aggregate("coord_repr", 2, CoordRepr)
    conn.executescript(SCHEMA)
    _insert_frames(conn, [
        ("sinistro", "260101", "SIN_2601010001", "OPENTGT", 1.0, "OPEN-001"),
        ("qhy600", "260104", "QHY_2601040001", "HIDDENTGT", 4.0, "KEY-SECRET"),
    ])
    _rebuild_summaries(conn)
    conn.executemany(
        "INSERT INTO users (username, display_name, is_admin) VALUES (?, ?, ?)",
        [("alice", "alice", 0), ("boss", "boss", 1)],
    )
    conn.commit()
    conn.close()
    return path


def test_the_first_access_write_seeds_the_groups(db):
    with sqlite3.connect(db) as conn:
        assert access_groups.is_active(conn)
    denied = access.denied_proposal_ids_for(db, "alice")
    assert isinstance(denied, ScopedDenied)
    assert ("qhy600", "260104", "HIDDENTGT", "KEY-SECRET") not in _visible(db, "alice")
    assert ("sinistro", "260101", "OPENTGT", "OPEN-001") in _visible(db, "alice")


def test_a_database_without_any_rule_hides_nothing(tmp_path):
    path = _bare_db(tmp_path)
    assert access.denied_proposal_ids_for(path, "alice") == frozenset()
    assert len(_visible(path, None)) == 2


@pytest.mark.parametrize(
    "legacy_sql",
    [
        "INSERT INTO restricted_proposals (proposal_id) VALUES ('KEY-SECRET')",
        "INSERT INTO user_proposal_access (username, proposal_id) VALUES ('alice', 'KEY-SECRET')",
        "UPDATE users SET scoped = 1 WHERE username = 'alice'",
    ],
)
def test_an_unmigrated_blocklist_database_fails_closed(tmp_path, legacy_sql):
    # Its rules are no longer read, so it must not fall open to "nothing is
    # restricted" before `access groups-seed` migrates them.
    path = _bare_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(legacy_sql)
    assert _visible(path, "alice") == set()
    assert _visible_sql(path, None) == set()
    assert len(_visible(path, "boss")) == 2


def test_groups_seed_migrates_an_unmigrated_blocklist_database(tmp_path):
    path = _bare_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO restricted_proposals (proposal_id) VALUES ('KEY-SECRET')")
    access_groups.seed_from_blocklist(path)
    assert _visible(path, "alice") == {("sinistro", "260101", "OPENTGT", "OPEN-001")}


def test_seeded_database_decides_from_the_groups(seeded):
    for user in ("alice", "fukui", "boss", None):
        groups = access_groups.load_access(seeded, user)
        expected = {k for k in _summary_keys(seeded) if groups.frame_visible(*k)}
        assert _visible(seeded, user) == expected, user
        assert _visible_sql(seeded, user) == expected, user


def test_reseeding_changes_nothing_anyone_sees(db):
    # A proposal restricted through the groups is not in the legacy table, so
    # a reseed must read the revoke history to keep it hidden.
    before = {u: _visible(db, u) for u in ("alice", "fukui", "boss", None)}
    access_groups.seed_from_blocklist(db)
    after = {u: _visible(db, u) for u in ("alice", "fukui", "boss", None)}
    assert after == before


def test_admin_on_a_seeded_database_is_denied_nothing(seeded):
    assert access.denied_proposal_ids_for(seeded, "boss") == frozenset()


def test_no_propid_instrument_stays_visible_through_its_instrument_grant(seeded):
    row = ("muscat2", "260105", "M2TGT", "")
    assert row in _visible(seeded, "alice")
    assert row in _visible_sql(seeded, "alice")


# ── default deny ────────────────────────────────────────────────────────


def test_proposal_ingested_after_the_seed_is_hidden_from_non_admins(seeded):
    _ingest_new_proposal(seeded)
    row = ("qhy600", "260110", "NEWTGT", _NEW)
    for user in ("alice", "fukui", None):
        assert row not in _visible(seeded, user), user
        assert row not in _visible_sql(seeded, user), user
    assert row in _visible(seeded, "boss")


def test_unrestrict_shares_a_new_proposal_with_everyone(seeded):
    _ingest_new_proposal(seeded)
    assert access.unrestrict(seeded, _NEW) is True
    row = ("qhy600", "260110", "NEWTGT", _NEW)
    assert row in _visible(seeded, "alice")
    assert row in _visible_sql(seeded, None)


def test_access_writers_leave_the_legacy_tables_untouched(seeded):
    access.restrict(seeded, "OPEN-001")
    access.grant(seeded, "alice", "OPEN-001", "test")
    access.set_scoped(seeded, "alice", True)
    with sqlite3.connect(seeded) as conn:
        assert conn.execute("SELECT COUNT(*) FROM restricted_proposals").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM user_proposal_access").fetchone()[0] == 0


# ── the access CLI writes the groups ────────────────────────────────────


def test_restrict_on_a_seeded_database_hides_the_proposal(seeded):
    access.restrict(seeded, "OPEN-001")
    assert not any(k[3] == "OPEN-001" for k in _visible(seeded, "alice"))
    assert not any(k[3] == "OPEN-001" for k in _visible_sql(seeded, "alice"))


def test_unrestrict_restores_a_restricted_proposal(seeded):
    assert access.unrestrict(seeded, "KEY-SECRET") is True
    assert ("qhy600", "260104", "HIDDENTGT", "KEY-SECRET") in _visible(seeded, "alice")


def test_unrestrict_is_false_when_nothing_changes(seeded):
    assert access.unrestrict(seeded, "OPEN-001") is False


def test_grant_and_revoke_on_a_seeded_database(seeded):
    row = ("qhy600", "260104", "HIDDENTGT", "KEY-SECRET")
    assert row not in _visible(seeded, "alice")
    access.grant(seeded, "alice", "KEY-SECRET", "test")
    assert row in _visible(seeded, "alice")
    access.revoke(seeded, "alice", "KEY-SECRET")
    assert row not in _visible(seeded, "alice")


def test_scope_on_a_seeded_database_limits_the_user_to_their_grants(seeded):
    access.grant(seeded, "alice", "KEY-SECRET", "test")
    access.set_scoped(seeded, "alice", True)
    assert {k[3] for k in _visible(seeded, "alice")} == {"KEY-SECRET"}
    access.set_scoped(seeded, "alice", False)
    assert "OPEN-001" in {k[3] for k in _visible(seeded, "alice")}


def test_restrict_keeps_a_users_direct_grant(seeded):
    access.grant(seeded, "alice", "OPEN-001", "test")
    access.restrict(seeded, "OPEN-001")
    assert "OPEN-001" in {k[3] for k in _visible(seeded, "alice")}


# ── groups-seed after the switch ────────────────────────────────────────


def test_groups_seed_refuses_on_a_seeded_database(seeded):
    _ingest_new_proposal(seeded)
    result = CliRunner().invoke(cli_app, ["access", "groups-seed"])
    assert result.exit_code == 1
    assert "already seeded" in result.output
    assert ("qhy600", "260110", "NEWTGT", _NEW) not in _visible(seeded, "alice")


def test_groups_seed_force_reseeds(seeded):
    _ingest_new_proposal(seeded)
    result = CliRunner().invoke(cli_app, ["access", "groups-seed", "--force"])
    assert result.exit_code == 0, result.output
    assert ("qhy600", "260110", "NEWTGT", _NEW) in _visible(seeded, "alice")


def test_groups_seed_dry_run_is_allowed_on_a_seeded_database(seeded):
    _ingest_new_proposal(seeded)
    result = CliRunner().invoke(cli_app, ["access", "groups-seed", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert _NEW in result.output
    assert ("qhy600", "260110", "NEWTGT", _NEW) not in _visible(seeded, "alice")


# ── through the web ─────────────────────────────────────────────────────


def test_targets_page_hides_an_ungranted_new_proposal(client, seeded):
    _ingest_new_proposal(seeded)
    web._index_cache.clear()
    html = client.get("/targets", headers=_headers("alice")).text
    assert "NEWTGT" not in html
    assert "OPENTGT" in html and "M2TGT" in html
    assert "NEWTGT" in client.get("/targets", headers=_headers("boss")).text


def test_targets_page_shows_a_new_proposal_once_unrestricted(client, seeded):
    _ingest_new_proposal(seeded)
    access.unrestrict(seeded, _NEW)
    web._index_cache.clear()
    assert "NEWTGT" in client.get("/targets", headers=_headers("alice")).text
