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


def _snapshots(d: Path) -> list[Path]:
    return sorted(d.glob("*.nightly-*.sqlite"))


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
    assert len(list(backups.glob("*.CORRUPT"))) == 1
    assert list(backups.glob("*.part")) == []


def test_prune_keeps_newest_and_ignores_manual_backups(tmp_path, backups):
    db = tmp_path / "muscat.db"
    _make_db(db)
    base = datetime.datetime(2026, 9, 1, 17, 30)
    for day in range(4):
        db_backup.snapshot(db, now=base + datetime.timedelta(days=day))
    manual = backups / "muscat.db.backup-2026-08-28-pre-81-cleanup.sqlite"
    manual.write_bytes(b"manual")
    forensic = backups / "muscat.db.nightly-20260101-000000.sqlite.CORRUPT"
    forensic.write_bytes(b"bad")

    removed = db_backup.prune(db, keep=2)

    kept = [p.name for p in _snapshots(backups)]
    assert kept == ["muscat.db.nightly-20260903-173000.sqlite",
                    "muscat.db.nightly-20260904-173000.sqlite"]
    assert len(removed) == 2
    assert manual.exists() and forensic.exists()


def test_backup_keep_env(monkeypatch):
    monkeypatch.setenv("MUSCAT_DB_BACKUP_KEEP", "5")
    assert db_backup.backup_keep() == 5
    monkeypatch.setenv("MUSCAT_DB_BACKUP_KEEP", "0")
    assert db_backup.backup_keep() == 1
    monkeypatch.setenv("MUSCAT_DB_BACKUP_KEEP", "junk")
    assert db_backup.backup_keep() == db_backup.DEFAULT_KEEP


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
