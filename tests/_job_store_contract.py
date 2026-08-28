"""Shared JobRepository/JobQueue/JobConcurrency conformance tests.

Every job_store.py backend (SQLite's DatabaseJobStore; PostgreSQL's
PostgresJobStore, architecture issue #51 step 2) must satisfy the identical
contract, since callers (the pipelines, the web layer) hold whichever one
``get_job_store()`` hands back and never branch on which it is. Per
notes/MUSCATDB-LITE.md §15 ("the WorkQueue runs against both the SQLite and
Postgres adapters"), the same test bodies run against both -- this module
defines them once; a per-backend test module supplies its own ``store``
fixture and inherits :class:`JobStoreContractTests`.

Not collected directly by pytest: this file's name doesn't match
``test_*.py``.
"""

from __future__ import annotations

import time


def save(store, *, target, state, started_at, type_="photometry", run_id="", **kw):
    store.save(
        type_=type_, inst="muscat4", date="260101", target=target,
        state=state, returncode=None, elapsed=0, started_at=started_at,
        run_id=run_id, **kw,
    )


class JobStoreContractTests:
    # --- JobRepository -----------------------------------------------

    def test_save_then_all_and_get(self, store):
        save(store, target="HIP1", state="running", started_at=100.0)
        rows = store.all()
        assert len(rows) == 1
        assert rows[0]["state"] == "running"
        assert rows[0]["inst"] == "muscat4"  # database aliases instrument->inst

        got = store.get("photometry:muscat4/260101/HIP1")
        assert got is not None and got["target"] == "HIP1"
        assert store.get("photometry:muscat4/260101/NOPE") is None

    def test_save_upserts_by_key(self, store):
        save(store, target="HIP1", state="running", started_at=100.0)
        save(store, target="HIP1", state="done", started_at=100.0)
        rows = store.all()
        assert len(rows) == 1  # same key -> one row
        assert rows[0]["state"] == "done"

    def test_all_is_newest_first(self, store):
        save(store, target="OLD", state="done", started_at=100.0)
        save(store, target="NEW", state="done", started_at=200.0)
        assert [r["target"] for r in store.all()] == ["NEW", "OLD"]

    def test_delete_removes_only_that_key(self, store):
        save(store, target="A", state="done", started_at=100.0)
        save(store, target="B", state="done", started_at=101.0)
        store.delete("photometry:muscat4/260101/A")
        assert {r["target"] for r in store.all()} == {"B"}

    def test_delete_missing_key_is_noop(self, store):
        save(store, target="A", state="done", started_at=100.0)
        store.delete("photometry:muscat4/260101/GONE")  # must not raise
        assert len(store.all()) == 1

    def test_enqueue_records_pending(self, store):
        store.enqueue(
            type_="photometry", inst="muscat4", date="260101", target="HIP1",
            started_at=time.time(), run_type="full",
        )
        rows = store.all()
        assert rows[0]["state"] == "pending"

    def test_pending_is_fifo_and_type_filtered(self, store):
        save(store, target="P2", state="pending", started_at=200.0)
        save(store, target="P1", state="pending", started_at=100.0)
        save(store, target="R", state="running", started_at=150.0)
        save(store, type_="transit_fit", target="OTHER", state="pending", started_at=50.0)
        pend = store.pending("photometry")
        assert [r["target"] for r in pend] == ["P1", "P2"]  # oldest-first, photometry only

    # --- JobConcurrency ------------------------------------------------
    #
    # Cross-process (and, for PostgresJobStore, cross-host) job-concurrency
    # gate (architecture audit: _MAX_FULL_JOBS was an in-memory-only
    # per-process dict, already wrong under --workers N>1).

    def test_claim_slot_grants_up_to_capacity(self, store):
        assert store.claim_slot("photometry", "inst/date/A", 2) is True
        assert store.claim_slot("photometry", "inst/date/B", 2) is True
        assert store.count_claimed("photometry") == 2

    def test_claim_slot_rejects_beyond_capacity(self, store):
        assert store.claim_slot("photometry", "inst/date/A", 1) is True
        assert store.claim_slot("photometry", "inst/date/B", 1) is False
        assert store.count_claimed("photometry") == 1

    def test_claim_slot_is_not_idempotently_true(self, store):
        """A repeat claim for a key already held returns False, not True --
        this is what stops two racing callers from both thinking they won and
        launching the same job twice."""
        assert store.claim_slot("photometry", "inst/date/A", 2) is True
        assert store.claim_slot("photometry", "inst/date/A", 2) is False
        assert store.count_claimed("photometry") == 1

    def test_release_slot_frees_capacity(self, store):
        store.claim_slot("photometry", "inst/date/A", 1)
        store.release_slot("photometry", "inst/date/A")
        assert store.count_claimed("photometry") == 0
        assert store.claim_slot("photometry", "inst/date/B", 1) is True

    def test_release_slot_missing_key_is_noop(self, store):
        store.release_slot("photometry", "inst/date/gone")  # must not raise
        assert store.count_claimed("photometry") == 0

    def test_slots_are_isolated_per_pipeline(self, store):
        assert store.claim_slot("photometry", "inst/date/A", 1) is True
        # Same holder_key, different pipeline: its own independent capacity.
        assert store.claim_slot("transit_fit", "inst/date/A", 1) is True
        assert store.count_claimed("photometry") == 1
        assert store.count_claimed("transit_fit") == 1

    def test_reconcile_releases_claim_with_no_matching_job_row(self, store):
        """A claim whose launch attempt never reached the jobs table (e.g. it
        failed before the first store.save) is stale and must be released."""
        store.claim_slot("photometry", "inst/date/A", 1)
        released = store.reconcile_slots("photometry")
        assert released == 1
        assert store.count_claimed("photometry") == 0

    def test_reconcile_releases_claim_whose_job_finished(self, store):
        store.claim_slot("photometry", "muscat4/260101/HIP1", 1)
        save(store, target="HIP1", state="done", started_at=100.0)
        released = store.reconcile_slots("photometry")
        assert released == 1
        assert store.count_claimed("photometry") == 0

    def test_reconcile_keeps_claim_whose_job_is_still_running(self, store):
        store.claim_slot("photometry", "muscat4/260101/HIP1", 1)
        save(store, target="HIP1", state="running", started_at=100.0)
        released = store.reconcile_slots("photometry")
        assert released == 0
        assert store.count_claimed("photometry") == 1

    def test_reconcile_on_empty_pipeline_is_noop(self, store):
        assert store.reconcile_slots("photometry") == 0

    # --- owner tagging (architecture issue #51 step 1 follow-up) ------
    #
    # A row's `owner` records which role (job_store.current_owner(): "web" or
    # "worker") launched it, so a process reconciling orphaned running rows
    # can tell "another live role owns this" apart from "the owner is gone" --
    # see job_store.py's `_OWNER` docstring. Preserved on empty, same pattern
    # as run_name/user_name, so a later state-transition save() (which never
    # passes owner) does not erase it.

    def test_save_persists_owner(self, store):
        save(store, target="HIP1", state="running", started_at=100.0, owner="worker")
        assert store.all()[0]["owner"] == "worker"

    def test_save_without_owner_defaults_empty(self, store):
        save(store, target="HIP1", state="running", started_at=100.0)
        assert store.all()[0].get("owner", "") == ""

    def test_save_preserves_owner_when_later_save_omits_it(self, store):
        save(store, target="HIP1", state="running", started_at=100.0, owner="worker")
        save(store, target="HIP1", state="error", started_at=100.0)
        assert store.all()[0]["owner"] == "worker"
