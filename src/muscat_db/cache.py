import time
from collections.abc import Callable
import threading
from collections import OrderedDict
from functools import wraps


class LRUCache:
    """Thread-safe mapping bounded to ``maxsize`` entries with LRU eviction.

    Backs the manually-managed web caches (rendered-page HTML, catalog
    lookups) that used to be plain module-level dicts: unbounded — a slow
    memory leak over a long-lived server — and mutated from FastAPI's
    threadpool (``def`` routes run off-thread) with no lock, so concurrent
    requests could corrupt the dict.

    Each method takes the lock for its whole critical section, so reads and
    writes are atomic; ``get`` distinguishes "absent" from a cached ``None``
    via the ``default`` sentinel rather than the racey ``key in cache`` /
    ``cache[key]`` two-step the callers previously used.
    """

    def __init__(self, maxsize: int = 128):
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self.maxsize = maxsize
        self._data: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key, default=None):
        """Return the value for *key*, marking it most-recently-used; *default*
        (any sentinel the caller picks) when the key is absent."""
        with self._lock:
            try:
                self._data.move_to_end(key)
                return self._data[key]
            except KeyError:
                return default

    def __contains__(self, key) -> bool:
        with self._lock:
            return key in self._data

    def __getitem__(self, key):
        with self._lock:
            self._data.move_to_end(key)
            return self._data[key]

    def __setitem__(self, key, value) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)  # evict least-recently-used

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


# Longest a caller waits for another caller's lookup of the same key before it
# runs its own, so a hung lookup cannot block every caller of that key.
_FLIGHT_WAIT_S = 120.0


class _Flight:
    """One in-progress lookup that other callers of the same key can wait on."""

    __slots__ = ("generation", "leader", "done", "value", "shared")

    def __init__(self, generation: int):
        self.generation = generation
        self.leader = threading.get_ident()
        self.done = threading.Event()
        self.value = None
        self.shared = False  # set once the leader has stored its result


class TTLCache:
    """Time-bounded memoizing decorator with an LRU size cap.

    Entries expire ``ttl`` seconds after insertion; independently, the cache
    never holds more than ``maxsize`` live entries (least-recently-used evicted
    first), so a long-running server can't accumulate stale keys for every
    distinct (inst, date, target) ever queried before any of them expire.
    Optional ``cache_if`` selects which results to store, allowing callers to
    retry transient failures immediately while retaining successful results.

    The wrapped function runs outside the lock, so a lookup that started
    before ``clear()`` can finish after it. Each ``clear()`` bumps an
    invalidation generation; a result computed against an older generation
    is returned to its caller but not stored, otherwise a slow lookup would
    repopulate the cache with the pre-invalidation value. Because every
    database write fires ``clear_all_caches()``, a slow lookup that overlaps
    a write is never stored; under write-heavy load the hit rate of an
    expensive cached function drops, which is the price of never serving a
    value computed against superseded state.

    Concurrent misses for one key share a single lookup: the first caller
    computes it and the rest wait for that result (#325), so a burst of
    identical requests costs one lookup, not one each. Only a result that was
    stored is shared. If the leader raises, or its result is rejected by
    ``cache_if`` or predates a ``clear()``, each waiting caller looks the key
    up again, so a transient failure is never fanned out and #366's guarantee
    holds.
    """

    def __init__(
        self, ttl: float = 300.0, maxsize: int = 2048,
        *, cache_if: Callable[[object], bool] | None = None,
    ):
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self.ttl = ttl
        self.cache_if = cache_if
        self.maxsize = maxsize
        self.cache: OrderedDict = OrderedDict()
        self.lock = threading.Lock()
        self._generation = 0
        self._in_flight: dict = {}

    def __call__(self, func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            while True:
                now = time.time()
                with self.lock:
                    if key in self.cache:
                        val, expiry = self.cache[key]
                        if now < expiry:
                            self.cache.move_to_end(key)
                            return val
                        del self.cache[key]
                    generation = self._generation
                    flight = self._in_flight.get(key)
                    if flight is not None and (
                        flight.generation != generation or flight.leader == threading.get_ident()
                    ):
                        flight = None  # superseded by a clear(), or this thread's own recursion
                    if flight is None:
                        mine = _Flight(generation)
                        self._in_flight[key] = mine
                if flight is not None:
                    # Another caller is already looking this key up: wait for it
                    # instead of repeating an expensive lookup. Its result is
                    # shared only if it was stored; a failed, rejected or
                    # pre-clear() result sends this caller round again.
                    if not flight.done.wait(_FLIGHT_WAIT_S):
                        # The leader is hung: one stuck lookup must not stall
                        # every caller of the key. Look it up independently.
                        return func(*args, **kwargs)
                    if flight.shared:
                        return flight.value
                    continue
                return self._lead(mine, key, now, func, args, kwargs)

        wrapper.cache_clear = self.clear
        return wrapper

    def _lead(self, flight, key, now, func, args, kwargs):
        """Run the lookup for *key* as its single leader and publish the
        outcome to any callers waiting on *flight*."""
        stored = False
        try:
            val = func(*args, **kwargs)
            flight.value = val
            if self.cache_if is None or self.cache_if(val):
                with self.lock:
                    if flight.generation == self._generation:
                        self.cache[key] = (val, now + self.ttl)
                        self.cache.move_to_end(key)
                        while len(self.cache) > self.maxsize:
                            self.cache.popitem(last=False)  # evict least-recently-used
                        stored = True
            return val
        finally:
            flight.shared = stored
            with self.lock:
                if self._in_flight.get(key) is flight:
                    del self._in_flight[key]
            flight.done.set()

    def clear(self):
        with self.lock:
            self.cache.clear()
            self._generation += 1

_registered_caches: list[TTLCache] = []

def register_cache(
    ttl: float = 300.0, maxsize: int = 2048,
    *, cache_if: Callable[[object], bool] | None = None,
) -> TTLCache:
    cache = TTLCache(ttl, maxsize, cache_if=cache_if)
    _registered_caches.append(cache)
    return cache

def clear_all_caches() -> None:
    for cache in _registered_caches:
        cache.clear()
