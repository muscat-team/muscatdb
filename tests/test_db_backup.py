"""Pre-rebuild snapshot + integrity gate around build_db (issue #182, finding 1)."""

from __future__ import annotations

import datetime
import os
import sqlite3
from pathlib import Path

import pytest

from muscat_db import db_backup
from muscat_db.database import SCHEMA, build_db


@pytest.fixture
def no_real_obslog_scan(monkeypatch):
    """Keep build_db() off the real obslog tree (see test_db_conn.py)."""
    monkeypatch.setattr("muscat_db.database._discover_csv_jobs", lambda *a, **k: [])


@pytest.fixture
def backups(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "backups"
    monkeypatch.setenv("MUSCAT_DB_BACKUP_DIR", str(d))
    return d


def _make_db(path: Path, notes: int = 1) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO target_notes(object, obsdate, instrument, note) VALUES (?, '', '', ?)",
        [(f"TOI-{i}", "keep me") for i in range(notes)],
    )
    conn.commit()
    conn.close()


def _corrupt(path: Path) -> None:
    """Overwrite interior pages so the b-trees no longer parse, while leaving
    the 100-byte header intact so SQLite still opens the file."""
    size = path.stat().st_size
    assert size > 8 * 4096, "setup: need a multi-page database to corrupt"
    with open(path, "r+b") as f:
        f.seek(2 * 4096)
        f.write(b"\xa5" * (size - 3 * 4096))


def _leak_pages(path: Path, pages: int = 2) -> None:
    """Append pages no b-tree references and grow the header's page count to
    cover them: ``integrity_check`` then reports ``Page N: never used`` and
    nothing else (issue #237), with every row intact."""
    page_size = sqlite3.connect(str(path)).execute("PRAGMA page_size").fetchone()[0]
    count = path.stat().st_size // page_size
    with open(path, "r+b") as f:
        f.seek(0, os.SEEK_END)
        f.write(b"\0" * page_size * pages)
        f.seek(28)
        f.write((count + pages).to_bytes(4, "big"))


def _make_wal_db(path: Path, notes: int = 1) -> None:
    """Like ``_make_db`` but persistently in WAL mode, as the live muscat.db
    is: the backup API copies page 1, so only a WAL source yields a WAL copy."""
    _make_db(path, notes=notes)
    with sqlite3.connect(str(path)) as c:
        assert c.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"


def _snapshots(d: Path) -> list[Path]:
    return sorted(d.glob("*.nightly-*.sqlite"))


def _names(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir())


# -- snapshot / prune --------------------------------------------------------


def test_snapshot_is_a_verified_copy(tmp_path, backups):
    db = tmp_path / "muscat.db"
    _make_db(db, notes=3)

    snap = db_backup.snapshot(db)

    assert snap.parent == backups
    assert snap.name.startswith("muscat.db.nightly-")
    with sqlite3.connect(str(snap)) as c:
        assert c.execute("SELECT COUNT(*) FROM target_notes").fetchone()[0] == 3


def test_snapshot_captures_uncheckpointed_wal_rows(tmp_path, backups):
    """A byte copy of the main file would miss rows still in -wal; the backup
    API reads through the WAL, which is why it is safe against a live server."""
    db = tmp_path / "muscat.db"
    _make_db(db)
    live = sqlite3.connect(str(db))
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("PRAGMA wal_autocheckpoint=0")
    live.execute("INSERT INTO target_notes(object, obsdate, instrument, note) VALUES ('WAL-ONLY', '', '', 'x')")
    live.commit()
    try:
        snap = db_backup.snapshot(db)
    finally:
        live.close()
    with sqlite3.connect(str(snap)) as c:
        assert c.execute("SELECT COUNT(*) FROM target_notes WHERE object='WAL-ONLY'").fetchone()[0] == 1


def test_snapshot_of_corrupt_db_raises_and_keeps_forensic_copy(tmp_path, backups):
    db = tmp_path / "muscat.db"
    _make_db(db, notes=5000)
    _corrupt(db)

    with pytest.raises(db_backup.IntegrityError):
        db_backup.snapshot(db)

    assert _snapshots(backups) == []
    assert len(list(backups.glob("*.INTEGRITY-FAILED"))) == 1
    assert list(backups.glob("*.part")) == []


def test_prune_keeps_newest_and_ignores_manual_backups(tmp_path, backups):
    db = tmp_path / "muscat.db"
    _make_db(db)
    base = datetime.datetime(2026, 9, 1, 17, 30)
    for day in range(4):
        db_backup.snapshot(db, now=base + datetime.timedelta(days=day))
    manual = backups / "muscat.db.backup-2026-08-28-pre-81-cleanup.sqlite"
    manual.write_bytes(b"manual")
    forensic = backups / "muscat.db.nightly-20260101-000000.sqlite.INTEGRITY-FAILED"
    forensic.write_bytes(b"bad")

    removed = db_backup.prune(db, keep=2)

    kept = [p.name for p in _snapshots(backups)]
    assert kept == ["muscat.db.nightly-20260903-173000.sqlite",
                    "muscat.db.nightly-20260904-173000.sqlite"]
    assert len(removed) == 2
    assert manual.exists() and forensic.exists()


def test_snapshot_and_prune_of_a_wal_db_leave_no_sidecars(tmp_path, backups):
    """Issue #194: the copy inherited WAL mode, so the read-only integrity
    check left ``.part-wal``/``.part-shm`` that the rename and prune never
    touched -- two orphans per night."""
    db = tmp_path / "muscat.db"
    _make_wal_db(db)
    base = datetime.datetime(2026, 10, 1, 17, 30)
    for day in range(4):
        db_backup.snapshot(db, now=base + datetime.timedelta(days=day))

    db_backup.prune(db, keep=2)

    assert _names(backups) == ["muscat.db.nightly-20261003-173000.sqlite",
                               "muscat.db.nightly-20261004-173000.sqlite"]
    # Self-contained: a rollback-journal header (bytes 18-19 == 1), so opening
    # the snapshot later, e.g. to restore from it, needs no sidecar either.
    assert all(p.read_bytes()[18:20] == b"\x01\x01" for p in _snapshots(backups))


def test_corrupt_wal_snapshot_leaves_only_the_forensic_copy(tmp_path, backups):
    db = tmp_path / "muscat.db"
    _make_wal_db(db, notes=5000)
    _corrupt(db)

    with pytest.raises(db_backup.IntegrityError):
        db_backup.snapshot(db)

    assert [n for n in _names(backups) if not n.endswith(".INTEGRITY-FAILED")] == []


def test_snapshot_removes_part_when_the_check_fails_unexpectedly(tmp_path, backups, monkeypatch):
    """Cleanup used to wrap only ``backup()``; any other error after it (an
    OSError opening the copy, a failed rename) stranded ``.part``."""
    db = tmp_path / "muscat.db"
    _make_wal_db(db)

    def boom(path):
        raise OSError("disk went away")

    monkeypatch.setattr(db_backup, "integrity_problems", boom)

    with pytest.raises(OSError, match="disk went away"):
        db_backup.snapshot(db)

    assert _names(backups) == []


def test_prune_removes_sidecars_and_sweeps_orphans(tmp_path, backups):
    db = tmp_path / "muscat.db"
    _make_db(db)
    base = datetime.datetime(2026, 10, 1, 17, 30)
    for day in range(3):
        db_backup.snapshot(db, now=base + datetime.timedelta(days=day))
    old, _, newest = (p.name for p in _snapshots(backups))
    leftovers = [
        old + "-wal", old + "-shm",                 # of a snapshot being pruned
        old + ".part-wal", old + ".part-shm",       # pre-fix orphans (#194)
        "muscat.db.nightly-20260901-000000.sqlite.part-shm",  # main file long gone
    ]
    # Sidecars of a file that still exists may belong to an open connection
    # (someone inspecting the newest snapshot, a snapshot still in progress).
    in_use = [newest + "-shm", "muscat.db.nightly-20261009-000000.sqlite.part",
              "muscat.db.nightly-20261009-000000.sqlite.part-shm"]
    for name in leftovers + in_use:
        (backups / name).write_bytes(b"")

    db_backup.prune(db, keep=2)

    assert _names(backups) == sorted(
        [p.name for p in _snapshots(backups)] + in_use
    )
    assert len(_snapshots(backups)) == 2


def test_backup_keep_env(monkeypatch):
    monkeypatch.setenv("MUSCAT_DB_BACKUP_KEEP", "5")
    assert db_backup.backup_keep() == 5
    monkeypatch.setenv("MUSCAT_DB_BACKUP_KEEP", "0")
    assert db_backup.backup_keep() == 1
    monkeypatch.setenv("MUSCAT_DB_BACKUP_KEEP", "junk")
    assert db_backup.backup_keep() == db_backup.DEFAULT_KEEP


# -- leaked pages are not corruption (issue #237) ----------------------------

_LEAK = "*** in database main ***\nPage 11: never used\nPage 12: never used"


@pytest.mark.parametrize("lines,expected", [
    (["ok"], False),
    ([], False),
    (_LEAK.splitlines(), True),
    (["Page 7: never used"], True),
    (_LEAK.splitlines() + ["Tree 3 page 3 cell 0: Rowid 1 out of order"], False),
    (["*** in database main ***"], False),
    (["Page 5: btreeInitPage() returns error code 11"], False),
    ([f"Page {i}: never used" for i in range(db_backup.INTEGRITY_REPORT_LIMIT)], False),
])
def test_only_unreferenced_pages_count_as_leaked(lines, expected):
    assert db_backup.leaked_pages_only(lines) is expected


def test_integrity_problems_splits_the_report_into_lines(tmp_path):
    db = tmp_path / "muscat.db"
    _make_db(db, notes=500)
    assert db_backup.integrity_problems(db) == []
    _leak_pages(db)
    problems = db_backup.integrity_problems(db)
    assert problems[0] == "*** in database main ***"
    assert len(problems) == 3 and all(p.endswith(": never used") for p in problems[1:])
    assert db_backup.leaked_pages_only(problems)
    assert db_backup.integrity_ok(db) is False


def test_integrity_problems_of_an_unreadable_file_is_not_a_leak(tmp_path):
    db = tmp_path / "muscat.db"
    _make_db(db, notes=5000)
    _corrupt(db)
    problems = db_backup.integrity_problems(db)
    assert problems and not db_backup.leaked_pages_only(problems)


def test_snapshot_of_a_db_with_only_leaked_pages_is_a_normal_snapshot(tmp_path, backups, caplog):
    db = tmp_path / "muscat.db"
    _make_db(db, notes=500)
    _leak_pages(db)

    with caplog.at_level("WARNING", logger=db_backup.logger.name):
        snap = db_backup.snapshot(db)

    assert snap in _snapshots(backups)
    assert list(backups.glob("*.INTEGRITY-FAILED")) == []
    assert any("unreferenced page" in r.message for r in caplog.records)
    with sqlite3.connect(str(snap)) as c:
        assert c.execute("SELECT COUNT(*) FROM target_notes").fetchone()[0] == 500


def test_leaked_pages_plus_real_damage_is_still_refused(tmp_path, backups):
    db = tmp_path / "muscat.db"
    _make_db(db, notes=5000)
    _corrupt(db)
    _leak_pages(db)

    with pytest.raises(db_backup.IntegrityError):
        db_backup.snapshot(db)

    assert len(list(backups.glob("*.INTEGRITY-FAILED"))) == 1


def test_refusal_names_the_problem_the_kept_copy_and_the_runbook(tmp_path, backups):
    db = tmp_path / "muscat.db"
    _make_db(db, notes=5000)
    _corrupt(db)

    with pytest.raises(db_backup.IntegrityError) as exc:
        db_backup.snapshot(db)

    kept = next(backups.glob("*.INTEGRITY-FAILED"))
    msg = str(exc.value)
    assert str(kept) in msg
    assert "notes/DEPLOYMENT.md" in msg
    assert "restore from an earlier backup" in msg


def test_snapshot_logs_how_long_the_copy_and_the_check_took(tmp_path, backups, caplog):
    db = tmp_path / "muscat.db"
    _make_db(db)
    with caplog.at_level("INFO", logger=db_backup.logger.name):
        db_backup.snapshot(db)
    done = [r.message for r in caplog.records if "written to" in r.message]
    assert done and "copy" in done[0] and "check" in done[0]


def test_a_slow_snapshot_is_warned_about(tmp_path, backups, caplog, monkeypatch):
    """On 2026-10-20 the copy and check took 3 h 11 min on a disk shared with a
    RAID consistency check, and nothing said so until the verdict."""
    db = tmp_path / "muscat.db"
    _make_db(db)
    monkeypatch.setattr(db_backup, "SLOW_SNAPSHOT_S", 0.0)
    with caplog.at_level("WARNING", logger=db_backup.logger.name):
        db_backup.snapshot(db)
    assert any("slow" in r.message.lower() for r in caplog.records)


def test_build_db_rebuilds_over_leaked_pages_and_drops_them(tmp_path, backups, no_real_obslog_scan):
    """The rebuild copies a fresh image over the live file page for page, so
    unreferenced pages go with the old image and no row is at risk."""
    db = tmp_path / "muscat.db"
    _make_db(db, notes=500)
    _leak_pages(db)

    build_db(str(db))

    assert db_backup.integrity_ok(db)
    with sqlite3.connect(str(db)) as c:
        assert c.execute("SELECT COUNT(*) FROM target_notes").fetchone()[0] == 500
    assert len(_snapshots(backups)) == 1


# -- build_db integration ----------------------------------------------------


def test_build_db_snapshots_the_pre_rebuild_database(tmp_path, backups, no_real_obslog_scan):
    db = tmp_path / "muscat.db"
    _make_db(db, notes=2)

    build_db(str(db))

    snaps = _snapshots(backups)
    assert len(snaps) == 1
    with sqlite3.connect(str(snaps[0])) as c:
        assert c.execute("SELECT COUNT(*) FROM target_notes").fetchone()[0] == 2


def test_build_db_refuses_to_rebuild_over_a_corrupt_database(tmp_path, backups, no_real_obslog_scan):
    db = tmp_path / "muscat.db"
    _make_db(db, notes=5000)
    _corrupt(db)
    before = db.read_bytes()

    with pytest.raises(db_backup.IntegrityError):
        build_db(str(db))

    assert db.read_bytes() == before
    assert not os.path.exists(str(db) + ".tmp")


def test_build_db_does_not_swap_in_a_corrupt_rebuild(tmp_path, backups, monkeypatch, no_real_obslog_scan):
    db = tmp_path / "muscat.db"
    _make_db(db, notes=2)
    tmp_image = str(db) + ".tmp"
    real_ok = db_backup.integrity_ok
    monkeypatch.setattr(
        db_backup, "integrity_ok",
        lambda p: False if os.fspath(p) == tmp_image else real_ok(p),
    )
    # A table build_db neither rebuilds nor preserves: it survives only if the
    # live file was never swapped. (Byte equality is too strict here -- the
    # preserve step legitimately runs schema migrations on the live file.)
    with sqlite3.connect(str(db)) as c:
        c.execute("CREATE TABLE sentinel (x)")

    with pytest.raises(db_backup.IntegrityError, match="rebuilt database"):
        build_db(str(db))

    with sqlite3.connect(str(db)) as c:
        assert c.execute("SELECT name FROM sqlite_master WHERE name='sentinel'").fetchone()
    assert not os.path.exists(tmp_image)


def test_build_db_on_fresh_path_needs_no_snapshot(tmp_path, backups, no_real_obslog_scan):
    db = tmp_path / "muscat.db"

    build_db(str(db))

    assert db.exists()
    assert not backups.exists() or _snapshots(backups) == []
