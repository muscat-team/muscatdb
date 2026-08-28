"""Tests for PostgresJobStore (architecture issue #51 step 2).

Runs the same JobRepository/JobQueue/JobConcurrency conformance suite
DatabaseJobStore satisfies (tests/_job_store_contract.py) against a real
PostgreSQL server, plus a concurrency test claim_slot's SQLite counterpart
cannot exercise at all: SQLite serializes every writer against every other,
so a naive COUNT-then-INSERT capacity check is safe there almost by accident.
Postgres's default READ COMMITTED isolation is not that forgiving -- without
claim_slot's pg_advisory_xact_lock, concurrent claimants can all read the same
COUNT before any of them commits, over-granting the capacity cap it exists to
enforce. That is exactly the failure mode this module's threaded test is
built to catch, per this project's testing rule: a correctness-critical path
mocked instead of exercised for real proves nothing.

Skips cleanly (never fails) when MUSCAT_POSTGRES_DSN is unset or no server is
reachable there -- the same "skip off-host" pattern tests/conftest.py already
uses for the NASA/TOI catalog CSVs. CI's postgres job (.github/workflows/ci.yml)
sets MUSCAT_POSTGRES_DSN against a real service container so this suite
actually runs there; MUSCATDB-LITE.md's own §15 goal ("the WorkQueue runs
against both the SQLite and Postgres adapters") is otherwise just a doc claim.
"""

from __future__ import annotations

import os
import threading

import pytest

from tests._job_store_contract import JobStoreContractTests

_DSN = os.environ.get("MUSCAT_POSTGRES_DSN")


def _postgres_reachable(dsn: str | None) -> bool:
    if not dsn:
        return False
    try:
        import psycopg
    except ImportError:
        return False
    try:
        with psycopg.connect(dsn, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(_DSN),
    reason="MUSCAT_POSTGRES_DSN unset or PostgreSQL unreachable (skips off-host)",
)


@pytest.fixture
def store():
    from muscat_db.job_store import PostgresJobStore

    s = PostgresJobStore(dsn=_DSN)
    with s._pool.connection() as conn:
        conn.execute("TRUNCATE jobs, job_concurrency_slots")
    yield s
    s.close()


class TestPostgresJobStore(JobStoreContractTests):
    pass


class TestClaimSlotConcurrency:
    """claim_slot must cap grants at max_slots under real concurrent
    claimants, not just when called serially from one thread."""

    def test_concurrent_claims_never_exceed_capacity(self, store):
        max_slots = 3
        attempts = 20
        results: list[bool] = []
        lock = threading.Lock()

        def attempt(i: int) -> None:
            granted = store.claim_slot("photometry", f"inst/date/T{i}", max_slots)
            with lock:
                results.append(granted)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(attempts)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(results) == max_slots
        assert store.count_claimed("photometry") == max_slots

    def test_concurrent_claims_for_the_same_key_grant_exactly_once(self, store):
        attempts = 20
        results: list[bool] = []
        lock = threading.Lock()

        def attempt() -> None:
            granted = store.claim_slot("photometry", "inst/date/SAME", 5)
            with lock:
                results.append(granted)

        threads = [threading.Thread(target=attempt) for _ in range(attempts)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(results) == 1
        assert store.count_claimed("photometry") == 1
