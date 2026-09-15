"""Tests for the job-store persistence seam (architecture audit C2).

DatabaseJobStore is exercised against a real temp SQLite DB. The
JobRepository/JobQueue/JobConcurrency conformance tests live in
tests/_job_store_contract.py and run against every backend (see also
tests/test_job_store_postgres.py); this file adds the seam's swap point
(set_job_store/get_job_store) checks, which are backend-agnostic.
"""

import re
import threading
import time

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


class TestInProcessWakeup:
    """wait_for_work's SQLite backend is a threading.Event, scoped to one
    process -- see DatabaseJobStore.__init__'s docstring for why cross-process
    wakeup isn't in scope for this backend. These tests exercise the actual
    web-process scenario the contract suite's single-threaded
    test_wait_for_work_returns_true_after_an_enqueue doesn't reach: a waiter
    already *blocked* inside wait_for_work when the enqueue happens, not an
    enqueue that lands before anyone starts waiting."""

    def test_enqueue_sets_the_in_process_wakeup(self, store, monkeypatch):
        monkeypatch.setattr(job_store, "_NOTIFY_ENABLED", True)
        store.enqueue(
            type_="photometry", inst="muscat4", date="260101", target="HIP1",
            started_at=0.0,
        )
        assert store._wakeup.is_set()

    def test_enqueue_does_not_signal_when_disabled(self, store, monkeypatch):
        monkeypatch.setattr(job_store, "_NOTIFY_ENABLED", False)
        store.enqueue(
            type_="photometry", inst="muscat4", date="260101", target="HIP1",
            started_at=0.0,
        )
        assert not store._wakeup.is_set()

    def test_wait_for_work_wakes_a_waiter_already_blocked_in_this_process(
        self, store, monkeypatch,
    ):
        monkeypatch.setattr(job_store, "_NOTIFY_ENABLED", True)
        results: list[bool] = []

        def waiter():
            results.append(store.wait_for_work(5.0))

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.1)  # give the waiter a real chance to block first
        store.enqueue(
            type_="photometry", inst="muscat4", date="260101", target="HIP1",
            started_at=0.0,
        )
        t.join(timeout=2)
        assert not t.is_alive()
        assert results == [True]


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


class TestWaitForWorkOrSleep:
    """job_store.wait_for_work_or_sleep -- the one function every caller
    (web.py's background loop, worker.py's _loop) should call instead of a
    store's wait_for_work() directly. Its whole job is the floor documented
    in its own docstring: never return early without an actual signal, so a
    disabled flag, a store lacking the method, or a store that raises all
    degrade to exactly today's `time.sleep(timeout)` rather than a hot loop.
    A recorder replaces time.sleep so every case here is instant."""

    @pytest.fixture(autouse=True)
    def _recorder(self, monkeypatch):
        self.sleeps: list[float] = []
        monkeypatch.setattr(job_store.time, "sleep", lambda s: self.sleeps.append(s))

    def test_sleeps_the_full_interval_when_notify_is_disabled(self, monkeypatch):
        monkeypatch.setattr(job_store, "_NOTIFY_ENABLED", False)
        calls = []
        monkeypatch.setattr(
            job_store.get_job_store(), "wait_for_work", lambda t: calls.append(t) or True,
        )
        assert job_store.wait_for_work_or_sleep(2.0) is False
        assert self.sleeps == [2.0]
        assert calls == []  # never even asked -- disabled short-circuits before the store

    def test_sleeps_the_remaining_time_when_the_store_has_no_wait_for_work(self, monkeypatch):
        monkeypatch.setattr(job_store, "_NOTIFY_ENABLED", True)

        class _NoWaitStore:
            pass

        original = job_store.get_job_store()
        try:
            job_store.set_job_store(_NoWaitStore())
            assert job_store.wait_for_work_or_sleep(1.5) is False
            assert self.sleeps == pytest.approx([1.5], abs=0.01)
        finally:
            job_store.set_job_store(original)

    def test_sleeps_the_remaining_time_when_the_store_raises(self, monkeypatch):
        monkeypatch.setattr(job_store, "_NOTIFY_ENABLED", True)

        class _RaisingStore:
            def wait_for_work(self, timeout):
                raise RuntimeError("simulated store failure")

        original = job_store.get_job_store()
        try:
            job_store.set_job_store(_RaisingStore())
            assert job_store.wait_for_work_or_sleep(1.5) is False
            assert self.sleeps == pytest.approx([1.5], abs=0.01)
        finally:
            job_store.set_job_store(original)

    def test_returns_true_immediately_when_the_store_signals(self, monkeypatch):
        monkeypatch.setattr(job_store, "_NOTIFY_ENABLED", True)

        class _SignallingStore:
            def wait_for_work(self, timeout):
                return True

        original = job_store.get_job_store()
        try:
            job_store.set_job_store(_SignallingStore())
            assert job_store.wait_for_work_or_sleep(1.5) is True
            assert self.sleeps == []  # signalled -- no leftover sleep
        finally:
            job_store.set_job_store(original)


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
        # Column names/order only -- not (name, type) pairs. The two backends'
        # type vocabularies already diverge in this table (started_at is REAL
        # in database.SCHEMA but DOUBLE PRECISION in job_store._PG_SCHEMA), so
        # comparing full tuples fails on any correctly-typed non-TEXT column
        # even when the migration itself is right. Each backend's own type is
        # already covered by test_sqlite_/test_pg_migrations_cover_every_post_
        # release_column against its own schema.
        assert [col for col, _ in database._JOBS_COLUMN_MIGRATIONS] == [
            col for col, _ in job_store._PG_JOBS_COLUMN_MIGRATIONS
        ]


# `job_concurrency_slots`'s columns at initial release, before the per-host
# cap (architecture issue #51) added `host`. Same role as _ORIGINAL_JOBS_COLUMNS
# above, for the same reason (issue #118).
_ORIGINAL_JOB_CONCURRENCY_SLOTS_COLUMNS = {"pipeline", "holder_key", "claimed_at"}


class TestJobConcurrencySlotsColumnMigrations:
    """Same rationale as TestJobsColumnMigrations, for `job_concurrency_slots`."""

    def test_sqlite_migrations_cover_every_post_release_column(self):
        declared = _table_columns(database.SCHEMA, "job_concurrency_slots")
        migrated = {col for col, _ in database._JOB_CONCURRENCY_SLOTS_COLUMN_MIGRATIONS}
        assert declared - _ORIGINAL_JOB_CONCURRENCY_SLOTS_COLUMNS == migrated

    def test_pg_migrations_cover_every_post_release_column(self):
        declared = _table_columns(job_store._PG_SCHEMA, "job_concurrency_slots")
        migrated = {col for col, _ in job_store._PG_JOB_CONCURRENCY_SLOTS_COLUMN_MIGRATIONS}
        assert declared - _ORIGINAL_JOB_CONCURRENCY_SLOTS_COLUMNS == migrated

    def test_migration_lists_match_between_backends(self):
        assert [col for col, _ in database._JOB_CONCURRENCY_SLOTS_COLUMN_MIGRATIONS] == [
            col for col, _ in job_store._PG_JOB_CONCURRENCY_SLOTS_COLUMN_MIGRATIONS
        ]
