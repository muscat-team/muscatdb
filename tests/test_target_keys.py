"""Stable target keys for access grants (issue #228, target_keys.py)."""

from __future__ import annotations

import sqlite3

import pytest
from typer.testing import CliRunner

from muscat_db import access, access_groups, target_keys
from muscat_db.cli import app as cli_app
from muscat_db.database import _apply_schema

# A change to any of these keys changes which observations an existing target
# grant covers. Bump TARGET_KEY_VERSION with it, so `access target-keys` reports
# the re-keyed grants as a rule change rather than as routine refresh noise.
PINNED_KEYS = {
    "TOI-1234": "TOI1234",
    "TOI1234": "TOI1234",
    "toi 1234": "TOI1234",
    "TOI 01234.01": "TOI1234",
    "TOI-1234 b (.02)": "TOI1234",
    "TOI-179b_231015 J025710-560913": "TOI179",
    "TIC245728942.01(TOI5012.01)": "TOI5012",
    "HIP 67522": "HIP67522",
    "V1298Tau_b": "V1298TAU",
    "V1298Tauc": "V1298TAU",
    "AU Mic": "AUMIC",
    "WASP-12": "WASP12",
    "K2-25 b": "K225",
    "  ": "",
}


def test_pinned_keys_hold_for_this_rule_version():
    assert target_keys.TARGET_KEY_VERSION == 1
    assert {name: target_keys.target_key(name) for name in PINNED_KEYS} == PINNED_KEYS


def test_the_key_ignores_name_overrides(monkeypatch):
    # target_key() takes no overrides argument at all, so nothing a caller can
    # pass reaches the rule; this pins that it does not read the table either.
    from muscat_db import target_names

    seen = []
    real = target_names._normalize_target_name
    monkeypatch.setattr(
        target_keys, "_normalize_target_name", lambda t, o=None: seen.append(o) or real(t, o)
    )
    target_keys.target_key("HIDDENTGT")
    assert seen == [None]


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "muscat.db")
    with sqlite3.connect(path) as conn:
        _apply_schema(conn)
        conn.executemany(
            "INSERT INTO summaries (instrument, obsdate, ccd, object, proposal_id) "
            "VALUES (?, ?, 0, ?, ?)",
            [
                ("sinistro", "260101", "TOI-1234", "CON-002"),
                ("qhy600", "260102", "TOI 01234.01", "KEY-001"),
                ("sinistro", "260103", "OTHERTGT", "CON-002"),
            ],
        )
        # `everyone` exists (so the groups decide) but is granted nothing.
        access_groups.ensure_group(conn, access_groups.EVERYONE, builtin=True)
        access_groups.ensure_group(conn, "team")
        access_groups.add_member(conn, "team", "alice")
        conn.commit()
    return path


def _refresh(db):
    with sqlite3.connect(db) as conn:
        report = target_keys.refresh(conn)
        conn.commit()
    return report


def test_refresh_keys_every_object_and_is_idempotent(db):
    first = _refresh(db)
    assert first.objects_added == 3
    with sqlite3.connect(db) as conn:
        rows = dict(conn.execute("SELECT object, target_key FROM object_keys"))
    assert rows == {"TOI-1234": "TOI1234", "TOI 01234.01": "TOI1234", "OTHERTGT": "OTHERTGT"}
    assert _refresh(db).changes == 0


def test_refresh_drops_keys_of_objects_no_longer_observed(db):
    _refresh(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM summaries WHERE object = 'OTHERTGT'")
        conn.commit()
    assert _refresh(db).objects_removed == 1


def test_refresh_keys_a_grant_made_before_the_key_column(db):
    # Production's grants predate the column: NULL until the first refresh,
    # matched meanwhile by keying the stored target name on read.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO group_grants (groupname, proposal_id, target) VALUES ('team', 'CON-002', 'TOI1234')"
        )
        conn.commit()
    before = access_groups.load_access(db, "alice")
    assert before.frame_visible("sinistro", "260101", "TOI-1234", "CON-002")
    report = _refresh(db)
    assert [(g[2], g[3], g[4]) for g in report.grants_rekeyed] == [("TOI1234", None, "TOI1234")]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT target_key, target_key_version FROM group_grants").fetchone() == (
            "TOI1234",
            target_keys.TARGET_KEY_VERSION,
        )
    after = access_groups.load_access(db, "alice")
    assert after.frame_visible("sinistro", "260101", "TOI-1234", "CON-002")
    assert not after.frame_visible("sinistro", "260103", "OTHERTGT", "CON-002")


def test_refresh_reports_a_target_grant_matching_nothing(db):
    with sqlite3.connect(db) as conn:
        access_groups.add_grant(conn, "team", target="NOSUCHTGT")
        conn.commit()
    assert [g[2] for g in _refresh(db).dead_grants] == ["NOSUCHTGT"]


def test_a_second_spelling_of_the_same_target_is_the_same_grant(db):
    with sqlite3.connect(db) as conn:
        assert access_groups.add_grant(conn, "team", proposal_id="CON-002", target="TOI1234")
        assert not access_groups.add_grant(conn, "team", proposal_id="CON-002", target="TOI-1234")
        assert access_groups.add_grant(conn, "team", proposal_id="KEY-001", target="TOI-1234")
        conn.commit()


def test_revoking_by_another_spelling_revokes_the_grant(db):
    with sqlite3.connect(db) as conn:
        access_groups.add_grant(conn, "team", target="TOI1234")
        assert access_groups.remove_target_grants(conn, "team", "toi 01234") == 1
        conn.commit()
        assert conn.execute("SELECT target FROM group_grants_revoked").fetchone() == ("TOI1234",)


def test_a_target_with_no_name_left_once_normalized_is_refused():
    with pytest.raises(ValueError, match="no name left"):
        access_groups.check_selector(
            proposal_id=None, instrument=None, target="-", date=None,
            date_from=None, date_to=None, instruments=(),
        )


def test_grants_still_load_on_a_schema_without_the_key_column(db):
    # The web applies the schema at startup, but an old database opened by a
    # path that does not must keep resolving target grants, not fail open or 500.
    with sqlite3.connect(db) as conn:
        access_groups.add_grant(conn, "team", target="TOI-1234")
        conn.execute("ALTER TABLE group_grants DROP COLUMN target_key_version")
        conn.execute("ALTER TABLE group_grants DROP COLUMN target_key")
        conn.commit()
    acc = access_groups.load_access(db, "alice")
    assert acc.frame_visible("qhy600", "260102", "TOI 01234.01", "KEY-001")


def _fake_csv_ingest(monkeypatch, frames):
    """Run build_db/ingest_date for real, with the obslog CSV layer replaced by
    an insert of *frames*: (instrument, obsdate, filename, object, proposal_id)."""

    def ingest(conn, jobs, progress=None):
        conn.executemany(
            """INSERT INTO frames
               (instrument, obsdate, ccd, filename, object, jd_start, ut_start, exptime,
                read_mode, filter, ra, declination, airmass, focus, pa, proposal_id)
               VALUES (?, ?, 0, ?, ?, 1.0, '00:00:00', 10, 'fast', 'gp', '', '', 1, 0, 0, ?)""",
            frames,
        )
        return len(frames)

    monkeypatch.setattr("muscat_db.database._discover_csv_jobs", lambda *a, **k: ["fake.csv"])
    monkeypatch.setattr("muscat_db.database._ingest_csv_jobs", ingest)


def test_build_db_rebuilds_object_keys_and_keys_carried_over_grants(db, monkeypatch, tmp_path):
    from muscat_db.database import build_db

    monkeypatch.setenv("MUSCAT_DB_BACKUP_DIR", str(tmp_path / "backups"))
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO group_grants (groupname, target) VALUES ('team', 'TOI1234')")
        conn.commit()
    _fake_csv_ingest(monkeypatch, [("sinistro", "260201", "SIN_1", "TOI 1234 b", "CON-002")])
    build_db(db)
    with sqlite3.connect(db) as conn:
        assert dict(conn.execute("SELECT object, target_key FROM object_keys")) == {"TOI 1234 b": "TOI1234"}
        assert conn.execute("SELECT target_key FROM group_grants").fetchone() == ("TOI1234",)


def test_ingest_date_keys_a_newly_observed_object(db, monkeypatch):
    from muscat_db.database import ingest_date

    _refresh(db)
    _fake_csv_ingest(monkeypatch, [("sinistro", "260301", "SIN_2", "TOI-1234.02", "CON-002")])
    ingest_date(db, "sinistro", "260301")
    with sqlite3.connect(db) as conn:
        key = conn.execute("SELECT target_key FROM object_keys WHERE object = 'TOI-1234.02'").fetchone()
    assert key == ("TOI1234",)


def test_cli_target_keys_dry_run_reports_without_writing(db, monkeypatch):
    monkeypatch.setenv("MUSCAT_DB_PATH", db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO group_grants (groupname, target) VALUES ('team', 'TOI1234')")
        conn.commit()
    result = CliRunner().invoke(cli_app, ["access", "target-keys", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Would add 3" in result.output and "(none) -> TOI1234" in result.output
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM object_keys").fetchone()[0] == 0
    result = CliRunner().invoke(cli_app, ["access", "target-keys"])
    assert result.exit_code == 0 and "Did add 3" in result.output


def test_cli_grant_does_not_call_a_hyphenless_target_dead(db, monkeypatch):
    # The CLI used to check a new grant with spaces and case ignored only, while
    # the web matched through the normalizer, so `--target TOI1234` was reported
    # as matching nothing although the web showed its frames.
    monkeypatch.setenv("MUSCAT_DB_PATH", db)
    result = CliRunner().invoke(
        cli_app, ["access", "group", "grant", "team", "--proposal", "CON-002", "--target", "TOI1234"]
    )
    assert result.exit_code == 0, result.output
    assert "matches no observation" not in result.output
    rows = access.explain_target(db, "alice", "TOI1234")
    assert {(r["object"], r["visible"]) for r in rows} == {
        ("TOI-1234", True),
        ("TOI 01234.01", False),
    }
