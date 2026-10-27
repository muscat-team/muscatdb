"""Publishing a rebuilt catalog must preserve concurrent application commits."""

import sqlite3

import pytest

from muscat_db import database


@pytest.fixture
def live_db(tmp_path, monkeypatch):
    path = str(tmp_path / "muscat.db")
    monkeypatch.setenv("MUSCAT_DB_PATH", path)
    monkeypatch.setattr(database, "_discover_csv_jobs", lambda: [])
    with database.get_conn(path) as conn:
        database._apply_schema(conn)
        conn.execute(
            "INSERT INTO target_notes(object, note) VALUES ('EXISTING', 'before')"
        )
        conn.commit()
    return path


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "INSERT INTO target_notes(object, note) VALUES ('NEW', 'committed')",
            [("EXISTING", "before"), ("NEW", "committed")],
        ),
        (
            "UPDATE target_notes SET note = 'committed' WHERE object = 'EXISTING'",
            [("EXISTING", "committed")],
        ),
        ("DELETE FROM target_notes WHERE object = 'EXISTING'", []),
    ],
    ids=["insert", "update", "delete"],
)
def test_rebuild_keeps_app_commit_after_staged_integrity_check(live_db, monkeypatch, sql, expected):
    """A commit after staging has finished must not be overwritten by its snapshot.

    Exercise build_db end to end, injecting a separate server connection at
    the last integrity check. The server commits before publication begins;
    no timing assumption or connection-policy cooperation is involved.
    """
    checked = []
    real_check = database.db_backup.integrity_ok

    def check_then_commit(path):
        ok = real_check(path)
        assert ok
        with sqlite3.connect(live_db) as server:
            server.execute(sql)
        checked.append(path)
        return ok

    monkeypatch.setattr(database.db_backup, "integrity_ok", check_then_commit)

    database.build_db(live_db)

    assert checked == [live_db + ".tmp"]
    with sqlite3.connect(live_db) as conn:
        rows = conn.execute("SELECT object, note FROM target_notes ORDER BY object").fetchall()
    assert rows == expected


def _seed_catalog(conn, name):
    conn.execute(
        "INSERT INTO frames(instrument, obsdate, ccd, filename, object) "
        "VALUES ('muscat3', '261027', 0, 'frame.fits', ?)", (name,),
    )
    conn.execute(
        "INSERT INTO summaries(instrument, obsdate, ccd, object) "
        "VALUES ('muscat3', '261027', 0, ?)", (name,),
    )
    conn.execute(
        "INSERT INTO targets(object, n_dates, n_frames) VALUES (?, 1, 1)", (name,),
    )
    conn.execute("INSERT INTO db_meta(key, value) VALUES ('last_build_at', ?)", (name,))


@pytest.fixture
def catalogs(live_db, tmp_path):
    staged = str(tmp_path / 'rebuilt.db')
    with database.get_conn(live_db) as conn:
        _seed_catalog(conn, 'OLD')
        database.target_keys.refresh(conn)
        conn.commit()
    with database.get_conn(staged) as conn:
        database._apply_schema(conn)
        _seed_catalog(conn, 'TOI-1234')
        conn.commit()
    return live_db, staged


def _catalog_values(conn):
    return [conn.execute(f'SELECT object FROM {table}').fetchone()[0]
            for table in ('frames', 'summaries', 'targets')] + [
        conn.execute("SELECT value FROM db_meta WHERE key = 'last_build_at'").fetchone()[0]
    ]


def test_publication_failure_rolls_back_catalog_indexes_and_keys(catalogs, monkeypatch):
    live, staged = catalogs
    real_refresh = database.target_keys.refresh

    def fail_after_refresh(conn):
        assert _catalog_values(conn) == ['TOI-1234'] * 4
        real_refresh(conn)
        raise RuntimeError('injected publication failure')

    monkeypatch.setattr(database.target_keys, 'refresh', fail_after_refresh)
    with pytest.raises(RuntimeError, match='injected publication failure'):
        database._publish_catalog(staged, live)

    with database.get_conn(live) as conn:
        assert _catalog_values(conn) == ['OLD'] * 4
        assert conn.execute('SELECT object FROM object_keys').fetchall() == [('OLD',)]
        assert conn.execute(
            'SELECT object FROM frames INDEXED BY idx_frames_object'
        ).fetchall() == [('OLD',)]
        assert conn.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        # The failed publisher released its writer lock.
        conn.execute("UPDATE target_notes SET note = 'after failure'")
        conn.commit()


def test_publication_keeps_reader_snapshot_and_serializes_writer(catalogs, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    live, staged = catalogs
    attempted = Event()
    real_refresh = database.target_keys.refresh

    def write_during_publication():
        with sqlite3.connect(live, timeout=0) as writer:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                writer.execute("UPDATE target_notes SET note = 'during publication'")
        attempted.set()
        with sqlite3.connect(live, timeout=10) as writer:
            writer.execute("UPDATE target_notes SET note = 'during publication'")

    with database.get_conn(live) as reader, ThreadPoolExecutor(max_workers=1) as pool:
        reader.execute('BEGIN')
        assert _catalog_values(reader) == ['OLD'] * 4
        pending = []

        def refresh_with_concurrent_clients(conn):
            assert _catalog_values(conn) == ['TOI-1234'] * 4
            with database.get_conn(live, readonly=True) as fresh_reader:
                assert _catalog_values(fresh_reader) == ['OLD'] * 4
            pending.append(pool.submit(write_during_publication))
            assert attempted.wait(timeout=5), 'concurrent writer did not attempt its commit'
            return real_refresh(conn)

        monkeypatch.setattr(database.target_keys, 'refresh', refresh_with_concurrent_clients)
        database._publish_catalog(staged, live)
        pending[0].result(timeout=10)
        assert _catalog_values(reader) == ['OLD'] * 4
        reader.commit()
        assert _catalog_values(reader) == ['TOI-1234'] * 4
        assert reader.execute('SELECT note FROM target_notes').fetchone() == ('during publication',)


def test_publication_keeps_unlisted_app_tables_and_sequences(catalogs):
    live, staged = catalogs
    with database.get_conn(live) as conn:
        conn.execute('CREATE TABLE future_app_state(id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT)')
        conn.execute("INSERT INTO future_app_state(id, value) VALUES (99, 'deleted')")
        conn.execute('DELETE FROM future_app_state')
        conn.commit()
    database._publish_catalog(staged, live)
    with database.get_conn(live) as conn:
        conn.execute("INSERT INTO future_app_state(value) VALUES ('kept')")
        assert conn.execute('SELECT id, value FROM future_app_state').fetchone() == (100, 'kept')


def test_publication_keys_current_live_grants_against_new_catalog(catalogs):
    live, staged = catalogs
    with database.get_conn(live) as conn:
        conn.execute("INSERT INTO group_grants(groupname, target) VALUES ('team', 'TOI 1234.01')")
        conn.commit()
    database._publish_catalog(staged, live)
    with database.get_conn(live) as conn:
        expected = database.target_keys.target_key('TOI-1234')
        assert conn.execute('SELECT object, target_key FROM object_keys').fetchall() == [
            ('TOI-1234', expected)
        ]
        assert conn.execute('SELECT target_key FROM group_grants').fetchall() == [(expected,)]


def test_rebuild_keeps_commit_during_legacy_notes_migration(tmp_path, monkeypatch):
    live = str(tmp_path / 'legacy.db')
    monkeypatch.setattr(database, '_discover_csv_jobs', lambda: [])
    with database.get_conn(live) as conn:
        conn.execute('CREATE TABLE target_notes(object TEXT PRIMARY KEY, note TEXT, updated_at TEXT)')
        conn.execute("INSERT INTO target_notes VALUES ('OLD', 'before', '2020-01-01')")
        conn.commit()
    real_migrate = database._migrate_target_notes
    injected = []

    def migrate_with_concurrent_commit(conn):
        path = conn.execute('PRAGMA database_list').fetchone()[2]
        if path != live:
            return real_migrate(conn)

        def commit_before_drop(sql):
            if not injected and sql.startswith('DROP TABLE IF EXISTS target_notes'):
                with sqlite3.connect(live) as writer:
                    writer.execute("INSERT INTO target_notes VALUES ('NEW', 'committed', '2026-10-27')")
                injected.append(True)

        conn.set_trace_callback(commit_before_drop)
        try:
            return real_migrate(conn)
        finally:
            conn.set_trace_callback(None)

    monkeypatch.setattr(database, '_migrate_target_notes', migrate_with_concurrent_commit)
    try:
        database.build_db(live)
    except sqlite3.OperationalError as exc:
        # A read snapshot invalidated by another writer must abort, not replace
        # the newer table contents with the rows read before that commit.
        assert 'locked' in str(exc)
    assert injected == [True]
    with database.get_conn(live) as conn:
        assert conn.execute('SELECT object, note FROM target_notes ORDER BY object').fetchall() == [
            ('NEW', 'committed'), ('OLD', 'before')
        ]
        assert conn.execute('PRAGMA integrity_check').fetchone() == ('ok',)
