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
import time

import pytest

from muscat_db import job_store
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


class TestNotifyDispatch:
    """Instant dispatch (architecture issue #51, "Signalling & live logs").

    The shared contract suite above (test_wait_for_work_returns_true_after_an_enqueue
    etc.) already passes against this backend, but it uses ONE store instance
    for both the arm/wait and the enqueue -- which a purely in-process
    mechanism (a threading.Event, say) would also satisfy. The tests here are
    what actually prove the signal crosses a connection: a second, independent
    PostgresJobStore instance (a stand-in for a worker on a different host)
    gets woken by an enqueue through the first.
    """

    def test_notify_wakes_a_different_store_instance(self, store, monkeypatch):
        from muscat_db.job_store import PostgresJobStore

        monkeypatch.setattr(job_store, "_NOTIFY_ENABLED", True)
        other = PostgresJobStore(dsn=_DSN)
        try:
            results: list[bool] = []

            def waiter():
                results.append(other.wait_for_work(5.0))

            other.wait_for_work(0.0)  # arm before the signal is sent
            t = threading.Thread(target=waiter)
            t.start()
            time.sleep(0.2)  # give the waiter thread time to actually block
            store.enqueue(
                type_="photometry", inst="muscat4", date="260101", target="HIP1",
                started_at=0.0,
            )
            t.join(timeout=3)
            assert not t.is_alive()
            assert results == [True]
        finally:
            other.close()

    def test_wait_for_work_survives_a_locally_closed_listen_connection(
        self, store, monkeypatch,
    ):
        """A dropped connection is detected and replaced on the *next* call
        -- a signal sent into the gap between the drop and that reconnect is
        genuinely lost (real Postgres LISTEN/NOTIFY semantics: only sessions
        actively LISTENing when NOTIFY commits are woken), exactly the
        window the fallback poll exists to cover. What must actually hold is
        that the store keeps working afterward: reconnect, then a
        subsequent enqueue/wait pair still succeeds."""
        monkeypatch.setattr(job_store, "_NOTIFY_ENABLED", True)
        store.wait_for_work(0.0)  # forces the LISTEN connection to open
        store._listen_conn.close()
        store.wait_for_work(0.0)  # detects the closed connection, reconnects + re-LISTENs
        store.enqueue(
            type_="photometry", inst="muscat4", date="260101", target="HIP1",
            started_at=0.0,
        )
        assert store.wait_for_work(5.0) is True

    def test_wait_for_work_reconnects_after_the_listen_connection_is_terminated(
        self, store, monkeypatch,
    ):
        """The truer failure than a locally-closed connection: the server
        drops the session (e.g. an admin restart) while the client-side
        object still looks open. Same reconnect-then-it-works shape as
        test_wait_for_work_survives_a_locally_closed_listen_connection."""
        monkeypatch.setattr(job_store, "_NOTIFY_ENABLED", True)
        store.wait_for_work(0.0)
        backend_pid = store._listen_conn.info.backend_pid
        with store._pool.connection() as conn:
            conn.execute("SELECT pg_terminate_backend(%s)", (backend_pid,))
        time.sleep(0.2)  # let the termination actually land
        # A server-side kill isn't discovered until the client actually
        # polls the socket -- unlike a locally .close()'d connection, where
        # .closed is already true beforehand, psycopg's notifies(timeout=0)
        # returns instantly without ever touching the socket (confirmed
        # against psycopg 3.3.4's source: a zero interval short-circuits the
        # wait), so it can't surface this break at all. A small positive
        # timeout is what a real wait_for_work_or_sleep(interval) call
        # always uses in production, and is what's needed here too: the
        # first call is what surfaces the break (notifies() raising) and
        # discards the dead connection; the second reconnects and
        # re-registers LISTEN.
        store.wait_for_work(0.3)
        store.wait_for_work(0.3)
        store.enqueue(
            type_="photometry", inst="muscat4", date="260101", target="HIP1",
            started_at=0.0,
        )
        assert store.wait_for_work(5.0) is True

    def test_wait_for_work_does_not_take_much_longer_than_timeout_when_reconnecting_slowly(
        self, store, monkeypatch,
    ):
        """A (re)connect that succeeds, but slowly (a congested/degraded
        network path, not an instant refusal), must not make wait_for_work
        take connect_time + timeout: the notifies() wait after connecting
        has to be budgeted from what's left of *timeout*, not the full
        *timeout* again -- otherwise every reconnecting pass pays up to
        ~2x its configured interval, worse for callers using a sub-1s
        interval (connect_timeout's `max(1, int(timeout))` floor makes a
        slow connect proportionally larger relative to a small timeout)."""
        import psycopg

        monkeypatch.setattr(job_store, "_NOTIFY_ENABLED", True)
        store._close_listen_connection()  # force the next call to (re)connect
        real_connect = psycopg.connect

        def slow_connect(*args, **kwargs):
            time.sleep(0.6)
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(psycopg, "connect", slow_connect)
        start = time.monotonic()
        result = store.wait_for_work(1.0)
        elapsed = time.monotonic() - start
        assert result is False
        assert elapsed < 1.5  # not ~0.6s (connect) + 1.0s (notify) = ~1.6s

    def test_wait_for_work_does_not_return_early_when_postgres_is_unreachable(
        self, monkeypatch,
    ):
        from muscat_db.job_store import PostgresJobStore

        monkeypatch.setattr(job_store, "_NOTIFY_ENABLED", True)
        dead = PostgresJobStore(dsn=_DSN)
        try:
            dead._dsn = "postgresql://muscatdb@127.0.0.1:1/nope"
            dead._close_listen_connection()
            original = job_store.get_job_store()
            job_store.set_job_store(dead)
            try:
                start = time.monotonic()
                assert job_store.wait_for_work_or_sleep(0.3) is False
                assert time.monotonic() - start >= 0.28
            finally:
                job_store.set_job_store(original)
        finally:
            dead.close()

    def test_close_releases_the_listen_connection(self, monkeypatch):
        from muscat_db.job_store import PostgresJobStore

        monkeypatch.setattr(job_store, "_NOTIFY_ENABLED", True)
        s = PostgresJobStore(dsn=_DSN)
        s.wait_for_work(0.0)
        assert s._listen_conn is not None
        s.close()
        assert s._listen_conn is None

    def test_enqueue_records_pending_even_when_notify_fails(self, store, monkeypatch):
        monkeypatch.setattr(job_store, "_NOTIFY_ENABLED", True)
        monkeypatch.setattr(
            store, "_signal_work",
            lambda type_: (_ for _ in ()).throw(RuntimeError("simulated notify failure")),
        )
        store.enqueue(
            type_="photometry", inst="muscat4", date="260101", target="HIP1",
            started_at=0.0,
        )
        rows = store.all()
        assert len(rows) == 1
        assert rows[0]["state"] == "pending"


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


class TestHostCapConcurrency:
    """The per-host cap (architecture issue #51's rejected os.getloadavg()
    replacement) spans ALL pipelines combined on one host, so claim_slot must
    take a *second* advisory lock keyed on host, independent of the existing
    pipeline-keyed one -- otherwise two different pipelines claiming on the
    same host at the same instant take different locks, aren't mutually
    exclusive, and can both read the same pre-commit host COUNT and
    over-grant. A single-threaded test cannot exercise the race; this fires
    real concurrent claims across three different pipeline names on one host."""

    def test_concurrent_claims_across_pipelines_never_exceed_host_cap(
        self, store, monkeypatch,
    ):
        from muscat_db import job_store

        monkeypatch.setattr(job_store, "_WORKER_MAX_SLOTS", 3)
        pipelines = ["photometry", "transit_fit", "ttv_fit"]
        attempts = 30
        results: list[bool] = []
        lock = threading.Lock()

        def attempt(i: int) -> None:
            pipeline = pipelines[i % len(pipelines)]
            granted = store.claim_slot(pipeline, f"inst/date/T{i}", 10)
            with lock:
                results.append(granted)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(attempts)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(results) == 3
        total_claimed = sum(store.count_claimed(p) for p in pipelines)
        assert total_claimed == 3
