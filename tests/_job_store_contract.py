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

from muscat_db import job_store


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

    # --- instant dispatch: wait_for_work (architecture issue #51,
    # "Signalling & live logs") -----------------------------------------
    #
    # Opt-in via MUSCAT_JOB_NOTIFY (job_store._NOTIFY_ENABLED, monkeypatched
    # directly below -- same pattern as _WORKER_MAX_SLOTS, since it is parsed
    # once at import time and setenv alone would not be observed). Every test
    # here arms the listener with a wait_for_work(0.0) call before enqueueing:
    # Postgres only delivers NOTIFY to sessions that were already LISTENing
    # when the enqueue's transaction committed, so a body that enqueues
    # before ever waiting would only prove the SQLite in-process path.

    def test_wait_for_work_returns_false_after_timeout_when_nothing_is_enqueued(
        self, store, monkeypatch,
    ):
        monkeypatch.setattr(job_store, "_NOTIFY_ENABLED", True)
        start = time.monotonic()
        assert store.wait_for_work(0.2) is False
        assert time.monotonic() - start >= 0.18

    def test_wait_for_work_returns_immediately_for_a_zero_timeout(self, store, monkeypatch):
        monkeypatch.setattr(job_store, "_NOTIFY_ENABLED", True)
        start = time.monotonic()
        store.wait_for_work(0.0)
        assert time.monotonic() - start < 0.5

    def test_wait_for_work_returns_true_after_an_enqueue(self, store, monkeypatch):
        monkeypatch.setattr(job_store, "_NOTIFY_ENABLED", True)
        store.wait_for_work(0.0)  # arm the listener before the signal is sent
        store.enqueue(
            type_="photometry", inst="muscat4", date="260101", target="HIP1",
            started_at=time.time(),
        )
        start = time.monotonic()
        assert store.wait_for_work(5.0) is True
        assert time.monotonic() - start < 2.0

    def test_wait_for_work_consumes_the_signal(self, store, monkeypatch):
        monkeypatch.setattr(job_store, "_NOTIFY_ENABLED", True)
        store.wait_for_work(0.0)
        store.enqueue(
            type_="photometry", inst="muscat4", date="260101", target="HIP1",
            started_at=time.time(),
        )
        assert store.wait_for_work(5.0) is True
        assert store.wait_for_work(0.2) is False  # already consumed, no second wake

    def test_wait_for_work_does_not_signal_when_disabled(self, store, monkeypatch):
        monkeypatch.setattr(job_store, "_NOTIFY_ENABLED", False)
        store.wait_for_work(0.0)
        store.enqueue(
            type_="photometry", inst="muscat4", date="260101", target="HIP1",
            started_at=time.time(),
        )
        start = time.monotonic()
        assert store.wait_for_work(0.2) is False
        assert time.monotonic() - start >= 0.18

    def test_enqueue_still_records_pending_when_signalling(self, store, monkeypatch):
        monkeypatch.setattr(job_store, "_NOTIFY_ENABLED", True)
        store.enqueue(
            type_="photometry", inst="muscat4", date="260101", target="HIP1",
            started_at=time.time(),
        )
        rows = store.all()
        assert len(rows) == 1
        assert rows[0]["state"] == "pending"

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

    # --- per-host concurrency cap (architecture issue #51's rejected
    # os.getloadavg() replacement) --------------------------------------
    #
    # MUSCAT_WORKER_MAX_SLOTS (job_store._WORKER_MAX_SLOTS) is an opt-in
    # second predicate on claim_slot: it caps total concurrent slots on
    # *this host* (job_store._HOST), summed across every pipeline combined
    # -- orthogonal to the per-pipeline cluster-wide cap tested above. Unset
    # (the default, and every other test in this file) must not change
    # claim_slot's behavior at all.

    def test_claim_slot_has_no_host_cap_by_default(self, store):
        for i in range(5):
            assert store.claim_slot("photometry", f"inst/date/P{i}", 10) is True
        for i in range(5):
            assert store.claim_slot("transit_fit", f"inst/date/T{i}", 10) is True

    def test_claim_slot_respects_host_cap_across_pipelines(self, store, monkeypatch):
        """The host cap is one shared budget across ALL pipelines, not a
        separate budget per pipeline: a claim for a different pipeline on the
        same host must still be denied once the host budget is spent, even
        though that pipeline's own max_slots has plenty of room."""
        monkeypatch.setattr(job_store, "_WORKER_MAX_SLOTS", 1)
        assert store.claim_slot("photometry", "inst/date/A", 10) is True
        assert store.claim_slot("transit_fit", "inst/date/B", 10) is False

    def test_claim_slot_host_cap_ignores_other_hosts_claims(self, store, monkeypatch):
        """A claim made under a different _HOST must not count against this
        host's budget -- proven here by spending this host's own budget (1)
        on a *second* claim after the other host's, then requiring a third
        claim to be denied. A no-op host predicate would let all three
        through (the pipeline cap alone is 10), so this fails loudly if the
        host-scoping is ever accidentally dropped."""
        monkeypatch.setattr(job_store, "_WORKER_MAX_SLOTS", 1)
        monkeypatch.setattr(job_store, "_HOST", "other-host")
        assert store.claim_slot("photometry", "inst/date/A", 10) is True
        monkeypatch.setattr(job_store, "_HOST", "this-host")
        assert store.claim_slot("photometry", "inst/date/B", 10) is True
        assert store.claim_slot("photometry", "inst/date/C", 10) is False

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

    # --- instance_id tagging + heartbeat (architecture issue #51 step 3) --
    #
    # instance_id records which *process* (job_store.current_instance_id())
    # launched a row -- distinct from owner's coarser per-role tag, see
    # job_store.py's `_INSTANCE_ID` docstring. Preserved on empty, same
    # pattern as owner/run_name/user_name. heartbeat() is the cheap
    # alternative to a full save() for refreshing a still-running job's
    # liveness without rewriting (and cache-invalidating) the rest of the row.

    def test_save_persists_instance_id(self, store):
        save(store, target="HIP1", state="running", started_at=100.0, instance_id="host:1:abc")
        assert store.all()[0]["instance_id"] == "host:1:abc"

    def test_save_without_instance_id_defaults_empty(self, store):
        save(store, target="HIP1", state="running", started_at=100.0)
        assert store.all()[0].get("instance_id", "") == ""

    def test_save_preserves_instance_id_when_later_save_omits_it(self, store):
        save(store, target="HIP1", state="running", started_at=100.0, instance_id="host:1:abc")
        save(store, target="HIP1", state="error", started_at=100.0)
        assert store.all()[0]["instance_id"] == "host:1:abc"

    # --- attempts (reclaim-with-attempt-limit, architecture issue #51 step 3) -
    #
    # attempts is the orphan-reconciliation retry counter -- see
    # jobs.next_reconcile_attempt. Unlike owner/instance_id/run_name above, it
    # is *not* preserved on omit: a caller that omits it means "this write is
    # not reconcile-retry bookkeeping" and 0 is the correct value (a fresh
    # launch must not inherit a stale count from a prior run of the same key).
    # Only the reconcile-retry write itself and the pending-drain relaunch
    # that carries an in-flight count forward ever pass a nonzero value.

    def test_save_persists_attempts(self, store):
        save(store, target="HIP1", state="pending", started_at=100.0, attempts=3)
        assert store.all()[0]["attempts"] == 3

    def test_save_without_attempts_defaults_zero(self, store):
        save(store, target="HIP1", state="running", started_at=100.0)
        assert store.all()[0]["attempts"] == 0

    def test_save_resets_attempts_when_later_save_omits_it(self, store):
        save(store, target="HIP1", state="pending", started_at=100.0, attempts=3)
        save(store, target="HIP1", state="running", started_at=100.0)
        assert store.all()[0]["attempts"] == 0

    def test_save_stamps_heartbeat_at_to_now(self, store):
        before = time.time()
        save(store, target="HIP1", state="running", started_at=100.0)
        after = time.time()
        heartbeat_at = store.all()[0]["heartbeat_at"]
        assert before <= heartbeat_at <= after

    def test_heartbeat_bumps_when_running_and_instance_matches(self, store):
        save(store, target="HIP1", state="running", started_at=100.0, instance_id="host:1:abc")
        key = "photometry:muscat4/260101/HIP1"
        original = store.get(key)["heartbeat_at"]
        time.sleep(0.02)
        store.heartbeat(key, "host:1:abc")
        assert store.get(key)["heartbeat_at"] > original

    def test_heartbeat_noop_when_instance_mismatch(self, store):
        """A different instance can never resurrect/steal another
        instance's heartbeat -- otherwise a stale caller racing a
        reconciliation pass that already reclaimed the row could make it
        look alive again."""
        save(store, target="HIP1", state="running", started_at=100.0, instance_id="host:1:abc")
        key = "photometry:muscat4/260101/HIP1"
        original = store.get(key)["heartbeat_at"]
        time.sleep(0.02)
        store.heartbeat(key, "host:2:xyz")
        assert store.get(key)["heartbeat_at"] == original

    def test_heartbeat_noop_when_not_running(self, store):
        save(store, target="HIP1", state="done", started_at=100.0, instance_id="host:1:abc")
        key = "photometry:muscat4/260101/HIP1"
        original = store.get(key)["heartbeat_at"]
        time.sleep(0.02)
        store.heartbeat(key, "host:1:abc")
        assert store.get(key)["heartbeat_at"] == original

    def test_heartbeat_missing_key_is_noop(self, store):
        store.heartbeat("photometry:muscat4/260101/GONE", "host:1:abc")  # must not raise
