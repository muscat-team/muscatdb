"""Tests for the job-store persistence seam (architecture audit C2).

DatabaseJobStore is exercised against a real temp SQLite DB. The
JobRepository/JobQueue/JobConcurrency conformance tests live in
tests/_job_store_contract.py and run against every backend (see also
tests/test_job_store_postgres.py); this file adds the seam's swap point
(set_job_store/get_job_store) checks, which are backend-agnostic.
"""

import re

import pytest

from muscat_db import database, job_store
from muscat_db.job_store import (
    DatabaseJobStore,
    JobConcurrency,
    JobQueue,
    JobRepository,
    get_job_store,
    set_job_store,
)
from tests._job_store_contract import JobStoreContractTests


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "muscat.db"))
    return DatabaseJobStore()


class TestDatabaseJobStore(JobStoreContractTests):
    pass


class TestSeamSwap:
    def test_get_returns_installed_store(self):
        original = get_job_store()
        try:
            sentinel = object()
            set_job_store(sentinel)
            assert get_job_store() is sentinel
        finally:
            set_job_store(original)

    def test_database_store_satisfies_protocols(self):
        s = DatabaseJobStore()
        assert isinstance(s, JobRepository)
        assert isinstance(s, JobQueue)
        assert isinstance(s, JobConcurrency)

    def test_default_store_is_database_backed(self):
        assert isinstance(job_store.get_job_store(), DatabaseJobStore)


_CONSTRAINT_KEYWORDS = {"PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"}


def _table_columns(schema_sql: str, table: str) -> set[str]:
    """Column names declared in `table`'s ``CREATE TABLE`` block within
    schema_sql. Splits the column-def body on top-level commas (nested parens,
    e.g. ``PRIMARY KEY (a, b)``, are not split) and takes each segment's first
    token as the column name, skipping table-level constraint clauses."""
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\s*\((.*?)\)\s*;",
        schema_sql,
        re.DOTALL,
    )
    assert match, f"no CREATE TABLE IF NOT EXISTS {table} found"
    body = match.group(1)
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in body:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))

    columns = set()
    for part in parts:
        tokens = part.strip().split()
        if not tokens or tokens[0].upper() in _CONSTRAINT_KEYWORDS:
            continue
        columns.add(tokens[0])
    return columns


class TestJobsSchemaParity:
    """database.SCHEMA (SQLite) and job_store._PG_SCHEMA (Postgres) must
    declare the same columns for `jobs` and `job_concurrency_slots`, or a
    column added to one backend silently drifts from the other -- the two
    control-plane implementations otherwise disagree about upgrades (issue
    #118). Runs unconditionally: it only parses the two DDL strings, so it
    does not need a reachable PostgreSQL server."""

    @pytest.mark.parametrize("table", ["jobs", "job_concurrency_slots"])
    def test_column_sets_match(self, table):
        sqlite_columns = _table_columns(database.SCHEMA, table)
        postgres_columns = _table_columns(job_store._PG_SCHEMA, table)
        assert sqlite_columns == postgres_columns


# Columns `jobs` declared at its initial release, before either backend's
# migration list existed. Anything declared in SCHEMA / _PG_SCHEMA beyond this
# set must appear in that backend's migration list, or a control plane created
# before the PR that added the column never gets it (issue #118).
_ORIGINAL_JOBS_COLUMNS = {
    "key", "type", "instrument", "obsdate", "target", "state",
    "returncode", "elapsed", "started_at", "error_desc",
}


class TestJobsColumnMigrations:
    """TestJobsSchemaParity only compares the two schema strings to each
    other: a column added to `jobs` in both SCHEMA and _PG_SCHEMA but wired
    into neither migration list still leaves the schemas agreeing with each
    other, so that test alone would not have caught it -- and a control plane
    created before the column existed would keep the old shape forever,
    exactly as in #118. These tests instead tie each backend's migration list
    to its own schema's post-baseline columns, and the two migration lists to
    each other directly, so a forgotten migration entry fails here even when
    the schemas still match."""

    def test_sqlite_migrations_cover_every_post_release_column(self):
        declared = _table_columns(database.SCHEMA, "jobs")
        migrated = {col for col, _ in database._JOBS_COLUMN_MIGRATIONS}
        assert declared - _ORIGINAL_JOBS_COLUMNS == migrated

    def test_pg_migrations_cover_every_post_release_column(self):
        declared = _table_columns(job_store._PG_SCHEMA, "jobs")
        migrated = {col for col, _ in job_store._PG_JOBS_COLUMN_MIGRATIONS}
        assert declared - _ORIGINAL_JOBS_COLUMNS == migrated

    def test_migration_lists_match_between_backends(self):
        assert database._JOBS_COLUMN_MIGRATIONS == job_store._PG_JOBS_COLUMN_MIGRATIONS
