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



def _wal_db_with_stale_rows(target):
    """A WAL-mode database held open by a live connection whose un-checkpointed
    WAL carries 200 db_meta rows the rebuild must not resurrect."""
    live = sqlite3.connect(str(target))
    live.execute("PRAGMA journal_mode=WAL;")
    live.execute("PRAGMA wal_autocheckpoint=0;")
    live.execute("CREATE TABLE db_meta (key TEXT PRIMARY KEY, value TEXT)")
    live.executemany(
        "INSERT INTO db_meta (key, value) VALUES (?, ?)",
        [(f"k{i}", "stale") for i in range(200)],
    )
    live.commit()
    return live


def _stale_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM db_meta WHERE value = 'stale'").fetchone()[0]


def test_build_db_with_live_connection_every_reader_sees_rebuilt_data(
    tmp_path, monkeypatch, no_real_obslog_scan,
):
    """With a server connection holding the destination open (production), the
    rebuilt content must be what every connection sees afterwards -- the open
    one included -- and the file must pass integrity_check (issue #182,
    finding 2). The old delete-sidecars-then-rename swap left the open
    connection on the detached pre-rebuild inode."""
    from muscat_db.database import build_db

    target = tmp_path / "muscat.db"
    monkeypatch.setenv("MUSCAT_DB_PATH", str(target))
    live = _wal_db_with_stale_rows(target)
    try:
        build_db(str(target))
        assert _stale_count(live) == 0
        with sqlite3.connect(str(target)) as fresh:
            assert _stale_count(fresh) == 0
            assert fresh.execute(
                "SELECT COUNT(*) FROM db_meta WHERE key = 'last_build_at'"
            ).fetchone()[0] == 1
            assert fresh.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        live.close()


# -- connection policy (issue #182, finding 5) --------------------------------

_SYNCHRONOUS_NORMAL = 1


def _policy(conn):
    return (
        conn.execute("PRAGMA journal_mode").fetchone()[0],
        conn.execute("PRAGMA synchronous").fetchone()[0],
        conn.execute("PRAGMA foreign_keys").fetchone()[0],
    )


def test_get_conn_applies_wal_and_synchronous_normal(tmp_path):
    with get_conn(str(tmp_path / "fresh.db")) as conn:
        assert _policy(conn) == ("wal", _SYNCHRONOUS_NORMAL, 0)


def test_connect_applies_the_same_policy(tmp_path):
    from muscat_db.database import connect

    conn = connect(str(tmp_path / "fresh.db"))
    try:
        assert _policy(conn) == ("wal", _SYNCHRONOUS_NORMAL, 0)
    finally:
        conn.close()


def test_exposure_connection_uses_the_policy(tmp_path, monkeypatch):
    from muscat_db import exposure

    monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "exp.db"))
    conn = exposure._conn()
    try:
        assert _policy(conn) == ("wal", _SYNCHRONOUS_NORMAL, 0)
    finally:
        conn.close()


def test_migrations_tolerate_already_applied_columns(tmp_path):
    from muscat_db.database import _apply_schema

    with get_conn(str(tmp_path / "m.db")) as conn:
        _apply_schema(conn)
        _apply_schema(conn)  # every ALTER now hits "duplicate column name"


def test_migrations_surface_real_failures(tmp_path, monkeypatch):
    """A failing migration used to be swallowed as "column already exists",
    silently leaving the schema unmigrated."""
    import muscat_db.database as database

    monkeypatch.setattr(
        database, "_MIGRATIONS",
        [*database._MIGRATIONS, "ALTER TABLE no_such_table ADD COLUMN x TEXT"],
    )
    with get_conn(str(tmp_path / "m.db")) as conn:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            database._apply_schema(conn)


def test_build_db_writes_on_a_connection_open_across_the_swap_are_kept(
    tmp_path, monkeypatch, no_real_obslog_scan,
):
    """A long-lived server connection that writes after the rebuild must write
    into the live database. Under the old swap it wrote into the unlinked
    pre-rebuild inode and the row silently vanished for everyone else."""
    from muscat_db.database import SCHEMA, build_db

    target = tmp_path / "muscat.db"
    monkeypatch.setenv("MUSCAT_DB_PATH", str(target))
    live = sqlite3.connect(str(target))
    live.execute("PRAGMA journal_mode=WAL;")
    live.executescript(SCHEMA)
    live.commit()
    try:
        build_db(str(target))
        live.execute(
            "INSERT INTO target_notes(object, obsdate, instrument, note) "
            "VALUES ('AFTER-SWAP', '', '', 'x')"
        )
        live.commit()
        with sqlite3.connect(str(target)) as fresh:
            assert fresh.execute(
                "SELECT COUNT(*) FROM target_notes WHERE object = 'AFTER-SWAP'"
            ).fetchone()[0] == 1
    finally:
        live.close()


def test_build_db_keeps_app_writes_made_while_the_build_runs(
    tmp_path, monkeypatch, no_real_obslog_scan,
):
    """App-owned rows written by the server mid-rebuild (job state, chat,
    notes) must survive. The old code snapshotted them at the start of a
    ~15-minute build, silently dropping everything written in between."""
    import muscat_db.database as database
    from muscat_db.database import SCHEMA, build_db

    target = tmp_path / "muscat.db"
    monkeypatch.setenv("MUSCAT_DB_PATH", str(target))
    with sqlite3.connect(str(target)) as c:
        c.executescript(SCHEMA)

    real_populate = database._populate_targets

    def populate_then_server_writes(conn):
        real_populate(conn)
        with sqlite3.connect(str(target)) as server:
            server.execute(
                "INSERT INTO target_notes(object, obsdate, instrument, note) "
                "VALUES ('MID-BUILD', '', '', 'x')"
            )

    monkeypatch.setattr(database, "_populate_targets", populate_then_server_writes)

    build_db(str(target))

    with sqlite3.connect(str(target)) as c:
        assert c.execute(
            "SELECT COUNT(*) FROM target_notes WHERE object = 'MID-BUILD'"
        ).fetchone()[0] == 1


def test_build_db_matches_a_non_default_page_size(tmp_path, monkeypatch, no_real_obslog_scan):
    """The backup API cannot change a WAL destination's page size, so the
    rebuilt image has to be built with the live file's."""
    from muscat_db.database import build_db

    target = tmp_path / "muscat.db"
    monkeypatch.setenv("MUSCAT_DB_PATH", str(target))
    with sqlite3.connect(str(target)) as c:
        c.execute("PRAGMA page_size=8192;")
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("CREATE TABLE db_meta (key TEXT PRIMARY KEY, value TEXT)")

    build_db(str(target))

    with sqlite3.connect(str(target)) as c:
        assert c.execute("PRAGMA page_size").fetchone()[0] == 8192
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("SELECT COUNT(*) FROM db_meta WHERE key='last_build_at'").fetchone()[0] == 1


def test_build_db_leaves_no_tmp_image_behind(tmp_path, monkeypatch, no_real_obslog_scan):
    from muscat_db.database import build_db

    target = tmp_path / "muscat.db"
    monkeypatch.setenv("MUSCAT_DB_PATH", str(target))

    build_db(str(target))

    assert sorted(p.name for p in tmp_path.iterdir() if ".tmp" in p.name) == []


def test_build_db_copies_into_live_through_the_connection_policy(
    tmp_path, monkeypatch, no_real_obslog_scan,
):
    """The copy writes the whole database into the live file, so it must use
    the same WAL + synchronous=NORMAL connection as every other writer."""
    import muscat_db.database as database

    target = tmp_path / "muscat.db"
    monkeypatch.setenv("MUSCAT_DB_PATH", str(target))
    opened = []
    real_connect = database.connect

    def recording_connect(path=None, **kwargs):
        conn = real_connect(path, **kwargs)
        opened.append((path, _policy(conn)))
        return conn

    monkeypatch.setattr(database, "connect", recording_connect)
    database.build_db(str(target))

    assert (str(target), ("wal", _SYNCHRONOUS_NORMAL, 0)) in opened
