"""Nightly pg_dump of the Postgres control plane (issue #51 step C).

The unit tests replace pg_dump/pg_restore with a fake ``subprocess.run``, so
they run anywhere. ``TestRealDump`` dumps the real test database and skips
cleanly when MUSCAT_POSTGRES_DSN is unset or unreachable, the same pattern as
tests/test_job_store_postgres.py.
"""

from __future__ import annotations

import datetime
import os
import subprocess

import pytest

from muscat_db import pg_backup

_DSN = "postgresql://muscatdb:s3cret@db.example:5432/muscatdb_prod"
_NOW = datetime.datetime(2026, 10, 21, 17, 30, 0)


class _FakeRun:
    """Stands in for subprocess.run: records each call, writes the archive
    pg_dump would write, and fails whichever tool it is told to."""

    def __init__(self, fail: str | None = None):
        self.calls: list[tuple[list[str], dict]] = []
        self.fail = fail

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        tool = os.path.basename(argv[0])
        if tool == "pg_dump":
            out = argv[argv.index("--file") + 1]
            with open(out, "wb") as fh:
                fh.write(b"PGDMP")
        rc = 1 if tool == self.fail else 0
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr=f"{tool} broke" if rc else "")


def _dump(tmp_path, monkeypatch, fake, **kw):
    monkeypatch.setattr(pg_backup.subprocess, "run", fake)
    return pg_backup.dump_control_plane(_DSN, dest_dir=tmp_path, now=_NOW, **kw)


def test_dump_is_named_after_the_database_and_verified(tmp_path, monkeypatch):
    fake = _FakeRun()
    dest = _dump(tmp_path, monkeypatch, fake)

    assert dest == tmp_path / "muscatdb_prod.nightly-20261021-173000.dump"
    assert dest.read_bytes() == b"PGDMP"
    assert dest.stat().st_mode & 0o777 == 0o600
    tools = [os.path.basename(argv[0]) for argv, _ in fake.calls]
    assert tools == ["pg_dump", "pg_restore"]
    assert "--list" in fake.calls[1][0]


def test_password_is_passed_by_environment_never_argv(tmp_path, monkeypatch):
    """argv is readable by every user on the host through ps."""
    fake = _FakeRun()
    _dump(tmp_path, monkeypatch, fake)

    argv, kwargs = fake.calls[0]
    assert not any("s3cret" in a for a in argv)
    assert kwargs["env"]["PGPASSWORD"] == "s3cret"
    conninfo = argv[argv.index("--dbname") + 1]
    assert conninfo == "postgresql://muscatdb@db.example:5432/muscatdb_prod"


@pytest.mark.parametrize(
    ("dsn", "conninfo", "password", "dbname"),
    [
        # Percent-encoded password, and a password in the query string.
        ("postgresql://u:p%40ss%2Fw@h:5432/db", "postgresql://u@h:5432/db", "p@ss/w", "db"),
        ("postgres://u@h/db?sslmode=require&password=pw", "postgres://u@h/db?sslmode=require", "pw", "db"),
        # Multi-host lists and an '@' inside the user name pass through.
        ("postgresql://a%40b:pw@h1:5432,h2:5433/db", "postgresql://a%40b@h1:5432,h2:5433/db", "pw", "db"),
        ("postgresql:///db?host=/run/postgresql", "postgresql:///db?host=/run/postgresql", "", "db"),
        # Keyword form, quoted values with escapes.
        (
            "host=h dbname = db password='it\\'s a \\\\secret' user=u",
            "host='h' dbname='db' user='u'",
            "it's a \\secret",
            "db",
        ),
    ],
)
def test_split_dsn_cuts_out_only_the_password(dsn, conninfo, password, dbname):
    assert pg_backup._split_dsn(dsn) == (conninfo, password, dbname)


@pytest.mark.parametrize("dsn", ["host=h dbname", "host=h password='unterminated dbname=db"])
def test_malformed_keyword_dsn_is_an_error(dsn):
    with pytest.raises(pg_backup.ControlPlaneBackupError, match="not a valid connection string"):
        pg_backup._split_dsn(dsn)


def test_encoded_password_is_redacted_too(tmp_path, monkeypatch):
    dsn = "postgresql://u:p%40ss@h/db"

    class _Leaky(_FakeRun):
        def __call__(self, argv, **kwargs):
            res = super().__call__(argv, **kwargs)
            res.stderr = f"connection to {dsn} failed (password p@ss)"
            return res

    monkeypatch.setattr(pg_backup.subprocess, "run", _Leaky(fail="pg_dump"))
    with pytest.raises(pg_backup.ControlPlaneBackupError) as info:
        pg_backup.dump_control_plane(dsn, dest_dir=tmp_path, now=_NOW)
    assert "p@ss" not in str(info.value) and "p%40ss" not in str(info.value)


def test_failed_pg_dump_leaves_no_archive(tmp_path, monkeypatch):
    with pytest.raises(pg_backup.ControlPlaneBackupError, match="pg_dump broke"):
        _dump(tmp_path, monkeypatch, _FakeRun(fail="pg_dump"))
    assert list(tmp_path.iterdir()) == []


def test_unreadable_archive_is_not_kept(tmp_path, monkeypatch):
    with pytest.raises(pg_backup.ControlPlaneBackupError, match="pg_restore broke"):
        _dump(tmp_path, monkeypatch, _FakeRun(fail="pg_restore"))
    assert list(tmp_path.iterdir()) == []


def test_error_messages_never_carry_the_password(tmp_path, monkeypatch):
    class _Leaky(_FakeRun):
        def __call__(self, argv, **kwargs):
            res = super().__call__(argv, **kwargs)
            res.stderr = f"connection to {_DSN} failed"
            return res

    with pytest.raises(pg_backup.ControlPlaneBackupError) as info:
        _dump(tmp_path, monkeypatch, _Leaky(fail="pg_dump"))
    assert "s3cret" not in str(info.value)


def test_keeps_only_the_newest_dumps_of_this_database(tmp_path, monkeypatch):
    for stamp in ("20261018-173000", "20261019-173000", "20261020-173000"):
        (tmp_path / f"muscatdb_prod.nightly-{stamp}.dump").write_bytes(b"old")
    other = tmp_path / "muscatdb_dev.nightly-20261001-000000.dump"
    other.write_bytes(b"dev")
    manual = tmp_path / "muscatdb_prod.before-upgrade.dump"
    manual.write_bytes(b"manual")

    _dump(tmp_path, monkeypatch, _FakeRun(), keep=2)

    kept = sorted(p.name for p in tmp_path.glob("muscatdb_prod.nightly-*.dump"))
    assert kept == [
        "muscatdb_prod.nightly-20261020-173000.dump",
        "muscatdb_prod.nightly-20261021-173000.dump",
    ]
    assert other.exists() and manual.exists()


def test_missing_dsn_is_an_error(monkeypatch):
    monkeypatch.delenv("MUSCAT_POSTGRES_DSN", raising=False)
    with pytest.raises(pg_backup.ControlPlaneBackupError, match="MUSCAT_POSTGRES_DSN"):
        pg_backup.dump_control_plane()


def test_dsn_without_a_database_is_an_error(tmp_path):
    with pytest.raises(pg_backup.ControlPlaneBackupError, match="database"):
        pg_backup.dump_control_plane("postgresql://u:p@h:5432", dest_dir=tmp_path)


def _postgres_reachable(dsn: str | None) -> bool:
    if not dsn:
        return False
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=2):
            return True
    except Exception:
        return False


@pytest.mark.skipif(
    not _postgres_reachable(os.environ.get("MUSCAT_POSTGRES_DSN")),
    reason="MUSCAT_POSTGRES_DSN unset or PostgreSQL unreachable (skips off-host)",
)
class TestRealDump:
    def test_dump_of_the_test_database_lists_the_jobs_table(self, tmp_path):
        from muscat_db.job_store import PostgresJobStore

        PostgresJobStore().close()  # make sure the tables exist
        dest = pg_backup.dump_control_plane(dest_dir=tmp_path)

        listing = subprocess.run(
            ["pg_restore", "--list", str(dest)], capture_output=True, text=True, check=True,
        ).stdout
        assert "TABLE public jobs" in listing
        assert "TABLE public job_concurrency_slots" in listing
