"""Tests for bounded, thread-safe caches (architecture audit M1).

LRUCache backs the web layer's previously-unbounded, unlocked module dicts
(_CATALOG_CACHE / _index_cache); TTLCache gains an LRU size cap so it can't
accumulate a stale entry per distinct (inst, date, target) before any expire.
"""

import threading
import time

import pytest

from muscat_db.cache import LRUCache, TTLCache, register_cache


class TestLRUCacheBounded:
    def test_get_set_roundtrip(self):
        c = LRUCache(maxsize=4)
        c["a"] = 1
        assert c.get("a") == 1
        assert c["a"] == 1
        assert "a" in c

    def test_evicts_least_recently_used_when_over_maxsize(self):
        c = LRUCache(maxsize=2)
        c["a"] = 1
        c["b"] = 2
        c["c"] = 3  # exceeds cap -> oldest ("a") evicted
        assert "a" not in c
        assert "b" in c and "c" in c
        assert len(c) == 2

    def test_access_refreshes_recency(self):
        c = LRUCache(maxsize=2)
        c["a"] = 1
        c["b"] = 2
        assert c.get("a") == 1  # "a" now most-recently-used
        c["c"] = 3  # evicts the now-oldest, "b"
        assert "a" in c
        assert "b" not in c
        assert "c" in c

    def test_setitem_refreshes_recency(self):
        c = LRUCache(maxsize=2)
        c["a"] = 1
        c["b"] = 2
        c["a"] = 10  # re-write bumps "a" to most-recent
        c["c"] = 3   # evicts "b"
        assert c.get("a") == 10
        assert "b" not in c

    def test_get_default_sentinel_distinguishes_absent_from_cached_none(self):
        c = LRUCache(maxsize=4)
        miss = object()
        assert c.get("absent", miss) is miss
        c["present"] = None  # legitimately cached None
        assert c.get("present", miss) is None  # not the miss sentinel

    def test_clear_empties(self):
        c = LRUCache(maxsize=4)
        c["a"] = 1
        c.clear()
        assert len(c) == 0
        assert "a" not in c

    def test_maxsize_must_be_positive(self):
        with pytest.raises(ValueError):
            LRUCache(maxsize=0)

    def test_concurrent_writes_stay_bounded_and_uncorrupted(self):
        c = LRUCache(maxsize=50)

        def worker(base):
            for i in range(500):
                c[f"{base}:{i}"] = i

        threads = [threading.Thread(target=worker, args=(b,)) for b in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Never exceeds the cap despite 4000 concurrent insertions.
        assert len(c) == 50


class TestTTLCacheBounded:
    def test_caches_within_ttl(self):
        calls = []

        @TTLCache(ttl=100.0, maxsize=8)
        def f(x):
            calls.append(x)
            return x * 2

        assert f(3) == 6
        assert f(3) == 6
        assert calls == [3]  # second call served from cache

    def test_expiry_recomputes(self):
        calls = []

        @TTLCache(ttl=0.05, maxsize=8)
        def f(x):
            calls.append(x)
            return x

        f(1)
        time.sleep(0.06)
        f(1)
        assert calls == [1, 1]  # expired -> recomputed

    def test_lru_size_cap_evicts_oldest(self):
        calls = []

        @TTLCache(ttl=1000.0, maxsize=2)
        def f(x):
            calls.append(x)
            return x

        f(1)
        f(2)
        f(3)          # evicts key for f(1)
        f(1)          # must recompute (was evicted)
        f(3)          # still cached
        assert calls == [1, 2, 3, 1]

    def test_hit_refreshes_recency(self):
        calls = []

        @TTLCache(ttl=1000.0, maxsize=2)
        def f(x):
            calls.append(x)
            return x

        f(1)
        f(2)
        f(1)          # bump 1 to most-recent
        f(3)          # evicts 2 (now oldest), not 1
        f(1)          # cached
        f(2)          # recompute
        assert calls == [1, 2, 3, 2]

    def test_cache_clear_attribute(self):
        @TTLCache(ttl=1000.0, maxsize=8)
        def f(x):
            return object()

        a = f(1)
        f.cache_clear()
        assert f(1) is not a  # cleared -> fresh value

    def test_register_cache_accepts_maxsize(self):
        c = register_cache(ttl=10.0, maxsize=3)
        assert isinstance(c, TTLCache)
        assert c.maxsize == 3


def test_cache_predicate_retries_failure_and_keeps_success():
    results = iter([{"ok": False}, {"ok": True}])
    cache = register_cache(cache_if=lambda result: result["ok"])

    @cache
    def evaluate():
        return next(results)

    assert evaluate() == {"ok": False}
    assert evaluate() == {"ok": True}
    assert evaluate() == {"ok": True}


def test_cache_without_predicate_still_caches_arbitrary_results():
    calls = []

    @TTLCache()
    def evaluate():
        calls.append(1)
        return {"ok": False}

    assert evaluate() == evaluate() == {"ok": False}
    assert calls == [1]


class TestTTLCacheInvalidationGeneration:
    """A lookup started before ``cache_clear()`` must not repopulate the
    cache with its stale result once the clear has run (audit #325 item 11)."""

    def test_in_flight_lookup_does_not_repopulate_after_clear(self):
        source = {"value": "old"}
        computing = threading.Event()
        resume = threading.Event()
        results = []

        @TTLCache(ttl=1000.0, maxsize=8)
        def read():
            snapshot = source["value"]
            computing.set()
            resume.wait(timeout=5)
            return snapshot

        worker = threading.Thread(target=lambda: results.append(read()))
        worker.start()
        assert computing.wait(timeout=5)
        # The underlying value changes and the cache is invalidated while the
        # older lookup is still paused inside the wrapped function.
        source["value"] = "new"
        read.cache_clear()
        resume.set()
        worker.join(timeout=5)
        assert results == ["old"]  # the paused caller keeps what it read
        # A later caller must see the new value, not the stale repopulation.
        assert read() == "new"

    def test_clear_before_lookup_still_caches_result(self):
        calls = []

        @TTLCache(ttl=1000.0, maxsize=8)
        def f(x):
            calls.append(x)
            return x

        f.cache_clear()
        assert f(1) == 1
        assert f(1) == 1
        assert calls == [1]  # a clear before the lookup does not poison it

    def test_cache_if_rejected_result_is_not_stored_across_clear(self):
        results = iter([{"ok": False}, {"ok": True}])
        computing = threading.Event()
        resume = threading.Event()
        cache = TTLCache(ttl=1000.0, maxsize=8, cache_if=lambda r: r["ok"])

        @cache
        def evaluate():
            value = next(results)
            computing.set()
            resume.wait(timeout=5)
            return value

        worker = threading.Thread(target=evaluate)
        worker.start()
        assert computing.wait(timeout=5)
        # The clear lands while a lookup that will be rejected is paused.
        evaluate.cache_clear()
        resume.set()
        worker.join(timeout=5)
        assert len(cache.cache) == 0  # the rejected value was not stored
        assert evaluate() == {"ok": True}
        assert evaluate() == {"ok": True}  # the accepted value is cached


# --- concurrent misses share one lookup (#325) --------------------------------


def _run_threads(target, n):
    results, errors = [None] * n, [None] * n

    def work(i):
        try:
            results[i] = target(i)
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            errors[i] = exc

    threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    return threads, results, errors


def test_concurrent_misses_for_one_key_run_the_lookup_once():
    calls = []
    release = threading.Event()
    started = threading.Event()

    @TTLCache(ttl=60)
    def lookup(x):
        calls.append(x)
        started.set()
        release.wait(5)
        return x * 2

    threads, results, errors = _run_threads(lambda i: lookup(21), 8)
    assert started.wait(5)
    time.sleep(0.1)  # let the other callers reach the wait
    release.set()
    for t in threads:
        t.join(5)
    assert errors == [None] * 8
    assert results == [42] * 8
    assert calls == [21]


def test_misses_for_different_keys_do_not_wait_for_each_other():
    both_in = threading.Barrier(2, timeout=5)

    @TTLCache(ttl=60)
    def lookup(x):
        both_in.wait()  # only passes if both lookups run at the same time
        return x

    threads, results, errors = _run_threads(lambda i: lookup(i), 2)
    for t in threads:
        t.join(10)
    assert errors == [None, None]
    assert sorted(results) == [0, 1]


def test_a_failed_lookup_is_not_shared_with_waiting_callers():
    calls = []
    release = threading.Event()

    @TTLCache(ttl=60)
    def lookup():
        calls.append(len(calls))
        if len(calls) == 1:
            release.wait(5)
            raise RuntimeError("upstream down")
        return "ok"

    threads, results, errors = _run_threads(lambda i: lookup(), 4)
    time.sleep(0.2)
    release.set()
    for t in threads:
        t.join(5)
    assert sum(isinstance(e, RuntimeError) for e in errors) == 1  # only the leader saw it
    assert [r for r in results if r is not None] == ["ok"] * 3


def test_a_result_the_predicate_rejects_is_not_shared():
    calls = []
    release = threading.Event()

    @TTLCache(ttl=60, cache_if=lambda v: v is not None)
    def lookup():
        calls.append(len(calls))
        if len(calls) == 1:
            release.wait(5)
            return None  # transient failure
        return "ok"

    threads, results, _ = _run_threads(lambda i: lookup(), 4)
    time.sleep(0.2)
    release.set()
    for t in threads:
        t.join(5)
    assert sorted(r for r in results if r is not None) == ["ok"] * 3
    assert lookup() == "ok"  # the good value was cached


def test_a_clear_during_the_lookup_is_not_shared_with_waiting_callers():
    """#366 still holds: callers waiting on a lookup that started before a
    clear() get a fresh value, not the one computed against the old state."""
    state = {"v": "old"}
    release = threading.Event()
    started = threading.Event()
    seen = []

    cache = TTLCache(ttl=60)

    @cache
    def lookup():
        value = state["v"]
        seen.append(value)
        if value == "old":
            started.set()
            release.wait(5)
        return value

    threads, results, _ = _run_threads(lambda i: lookup(), 4)
    assert started.wait(5)
    time.sleep(0.1)
    state["v"] = "new"
    cache.clear()
    release.set()
    for t in threads:
        t.join(5)
    assert results.count("old") == 1  # the leader returns its own result
    assert results.count("new") == 3
    assert lookup() == "new"


def test_a_lookup_that_calls_itself_does_not_wait_on_itself():
    @TTLCache(ttl=60)
    def fib(n):
        return n if n < 2 else fib(n - 1) + fib(n - 2)

    done = []
    t = threading.Thread(target=lambda: done.append(fib(10)))
    t.start()
    t.join(5)
    assert done == [55]


def test_a_hung_lookup_does_not_stall_other_callers(monkeypatch):
    from muscat_db import cache as cache_module

    monkeypatch.setattr(cache_module, "_FLIGHT_WAIT_S", 0.2)
    hang = threading.Event()
    calls = []

    @TTLCache(ttl=60)
    def lookup():
        calls.append(1)
        if len(calls) == 1:
            hang.wait(5)  # the leader never finishes in time
        return "ok"

    leader = threading.Thread(target=lookup)
    leader.start()
    time.sleep(0.1)
    assert lookup() == "ok"  # waits ~0.2 s, then runs its own lookup
    hang.set()
    leader.join(5)
