"""Tests for the get_conn() connection abstraction (architecture audit M3).

The previous open-coded `connect(...) ... close()` helpers leaked the handle
whenever the body raised between the two. get_conn() is a contextmanager that
guarantees close on every path and standardizes timeout/row_factory.
"""

import os
import sqlite3

import pytest

from muscat_db.database import get_conn


@pytest.fixture
def dbfile(tmp_path):
    path = str(tmp_path / "t.db")
    with get_conn(path) as conn:
        conn.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v INTEGER)")
        conn.execute("INSERT INTO t VALUES ('a', 1)")
        conn.commit()
    return path


def test_yields_usable_connection_and_commits(dbfile):
    with get_conn(dbfile) as conn:
        (v,) = conn.execute("SELECT v FROM t WHERE k = 'a'").fetchone()
    assert v == 1


def test_closes_connection_on_normal_exit(dbfile):
    with get_conn(dbfile) as conn:
        pass
    # Operating on a closed connection raises ProgrammingError.
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_closes_connection_when_body_raises(dbfile):
    captured = {}
    with pytest.raises(ValueError):
        with get_conn(dbfile) as conn:
            captured["conn"] = conn
            raise ValueError("boom")
    # Even though the body raised, the connection was closed (no leak).
    with pytest.raises(sqlite3.ProgrammingError):
        captured["conn"].execute("SELECT 1")


def test_row_factory_applied(dbfile):
    with get_conn(dbfile, row_factory=sqlite3.Row) as conn:
        row = conn.execute("SELECT k, v FROM t WHERE k = 'a'").fetchone()
    assert row["k"] == "a"
    assert row["v"] == 1


def test_defaults_to_env_db_path(monkeypatch, tmp_path):
    target = tmp_path / "muscat.db"
    monkeypatch.setenv("MUSCAT_DB_PATH", str(target))
    with get_conn() as conn:  # no explicit path -> db_path() from env
        conn.execute("CREATE TABLE x (a)")
        conn.commit()
    assert target.exists()


@pytest.fixture
def no_real_obslog_scan(monkeypatch):
    """Keep build_db() off the real production obslog tree.

    ``MUSCAT_OBSLOG_DIR`` (from .env) points ``database.OBSLOG_BASE`` at the
    real, shared obslog tree on a configured MuSCAT host. These tests only
    exercise build_db()'s destination-file/sidecar swap, but without this the
    unmocked ``_discover_csv_jobs()`` walks and ingests the entire real tree
    (thousands of CSVs) on every call, which is both slow and irrelevant to
    what's under test.
    """
    monkeypatch.setattr("muscat_db.database._discover_csv_jobs", lambda *a, **k: [])


def test_build_db_preserves_destination_file(tmp_path, monkeypatch, no_real_obslog_scan):
    from muscat_db.database import build_db
    target = tmp_path / "muscat.db"
    monkeypatch.setenv("MUSCAT_DB_PATH", str(target))
    
    # Initialize empty target DB with valid schema
    with get_conn(str(target)) as conn:
        conn.execute("CREATE TABLE db_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
    
    # Pre-create a sidecar file
    sidecar = tmp_path / "muscat.db-wal"
    sidecar.write_text("dummy")

    assert target.exists()
    assert sidecar.exists()

    build_db(str(target))

    assert target.exists()
    assert not sidecar.exists()


def test_build_db_never_removes_destination_file_itself(tmp_path, monkeypatch, no_real_obslog_scan):
    """The pre-swap sidecar cleanup must only ever touch -wal/-shm, never the
    destination path itself.

    ``os.replace`` is what makes the swap atomic -- a reader always sees the
    old inode or the new one, never neither. Removing the destination file
    ahead of the replace (as ``_remove_sqlite_tmp``'s ``("", "-wal", "-shm",
    "-journal")`` suffix list would, since "" is the bare path) throws that
    guarantee away and opens a window where the database doesn't exist at
    all. A before/after existence check can't catch that window because the
    file exists again by the time the assertion runs; asserting the bare
    path is never passed to ``os.remove`` can.
    """
    from muscat_db.database import build_db

    target = tmp_path / "muscat.db"
    monkeypatch.setenv("MUSCAT_DB_PATH", str(target))

    with get_conn(str(target)) as conn:
        conn.execute("CREATE TABLE db_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()

    removed_paths = []
    real_remove = os.remove

    def tracking_remove(path, *args, **kwargs):
        removed_paths.append(str(path))
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr("muscat_db.database.os.remove", tracking_remove)

    build_db(str(target))

    assert str(target) not in removed_paths



def test_build_db_clears_sidecars_held_by_a_live_connection(tmp_path, monkeypatch, no_real_obslog_scan):
    """A stale ``-wal`` at the destination silently masks the rebuilt database.

    The other two build_db tests here pass whether or not the sidecar removal in
    ``build_db`` exists, because the preserve-step connection checkpoints and
    deletes the WAL when it closes, so a sidecar with no live owner disappears on
    its own. Production is the case where another connection still holds the
    destination open: the WAL then survives into ``os.replace``, SQLite replays
    it over the freshly built file, and every reader keeps seeing pre-rebuild
    data with ``PRAGMA integrity_check`` still reporting ``ok``.
    """
    import sqlite3
    from muscat_db.database import build_db

    target = tmp_path / "muscat.db"
    monkeypatch.setenv("MUSCAT_DB_PATH", str(target))

    live = sqlite3.connect(str(target))
    live.execute("PRAGMA journal_mode=WAL;")
    live.execute("PRAGMA wal_autocheckpoint=0;")
    live.execute("CREATE TABLE db_meta (key TEXT PRIMARY KEY, value TEXT)")
    live.executemany(
        "INSERT INTO db_meta (key, value) VALUES (?, ?)",
        [(f"k{i}", "stale") for i in range(200)],
    )
    live.commit()
    try:
        assert (tmp_path / "muscat.db-wal").exists(), "setup: the WAL must be live"
        build_db(str(target))
        assert not (tmp_path / "muscat.db-wal").exists()
        assert not (tmp_path / "muscat.db-shm").exists()
    finally:
        live.close()
