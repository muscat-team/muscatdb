"""Shared archive request admission, independent of the job queue database.

Only fixed scope names, timestamps, policy numbers and an anonymous flag are
persisted. Credentials, URLs, users and profile names never enter this ledger.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from email.utils import parsedate_to_datetime
from functools import lru_cache
import hashlib
import json
import logging
import math
import os
import random
import time
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

DAY = 86400
# One bad header must not park every profile's archive traffic indefinitely.
MAX_RETRY_AFTER_S = DAY
# A shared cooldown stops dev, staging, production and every worker at once, so
# one client's Retry-After may park them only briefly. Peers re-probe after it,
# and a throttle that is still real publishes a fresh cooldown.
MAX_SHARED_COOLDOWN_S = 900
# A release's policy binds every peer only while that release is still running.
# A branch with lower limits stops tightening production this long after it stops.
POLICY_TTL_S = 3600
_LOCK = 389404
_CHECK = ContextVar("lco_request_check", default=None)
# Deliberately aggregates all identities, leaving headroom under LCO's per-user
# allowance: LCO does not say whether it counts per account, token or IP, and one
# aggregate budget is safe under all three. Staging and production run different
# releases between a merge and a release, so peers enforce the strictest policy
# among recently active releases (see Coordinator._effective_policy).
_POLICY = {"api_interval": 1.0, "api_daily": 4000, "anonymous_daily": 500,
           "storage_interval": 0.25}


def _strictest(stored, local):
    """Merge policies so no release can raise a limit another one still enforces.

    Budgets take the lowest value and pacing intervals the longest. A value a
    newer release adds is adopted. Anything malformed raises, which fails closed.
    """
    if not isinstance(stored, dict):
        raise ValueError("stored policy is not a mapping")
    merged = dict(stored)
    for name, value in local.items():
        current = stored.get(name, value)
        if (isinstance(current, bool) or not isinstance(current, (int, float))
                or not math.isfinite(current) or current < 0):
            raise ValueError(f"stored policy value {name} is invalid")
        merged[name] = max(current, value) if name.endswith("_interval") else min(current, value)
    return merged


def _error(message, status=503, retry_after=None):
    """Only a budget or cooldown refusal carries ``retry_after``.

    A missing DSN or an unreachable coordinator is not a rate limit: waiting
    cannot fix it, so callers must fail at once instead of sleeping on it.
    """
    from muscat_db.lco import LcoError

    return LcoError(message, status=status,
                    retry_after=None if retry_after is None else math.ceil(retry_after))


def retry_after_seconds(value, now):
    """Accept RFC Retry-After seconds or an HTTP date, clamped to one day.

    Malformed values use 60s.
    """
    try:
        value = str(value).strip()
        if value.isascii() and value.isdigit():
            seconds = float(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                raise ValueError("timezone missing")
            seconds = date.timestamp() - now
        if not math.isfinite(seconds):
            raise ValueError("not finite")
        if seconds > MAX_RETRY_AFTER_S:
            logger.warning("Clamped an LCO Retry-After of %.0f s to %d s", seconds, MAX_RETRY_AFTER_S)
        return min(MAX_RETRY_AFTER_S, max(1.0, seconds))
    except (TypeError, ValueError, OverflowError):
        return 60.0


def backoff(attempt):
    """Equal jitter, capped even if a caller supplies a huge attempt count."""
    ceiling = min(30.0, 2.0 ** min(max(attempt + 1, 1), 5))
    return random.uniform(ceiling / 2, ceiling)


def wait(seconds, check=None):
    """Never hold a DB connection while waiting; keep lease checks responsive."""
    end = time.monotonic() + seconds
    while True:
        if check:
            check()
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.25))


class Coordinator:
    def __init__(self, dsn, *, policy=None):
        self._dsn = dsn
        self._policy = dict(_POLICY if policy is None else policy)
        self._ready = False

    @staticmethod
    def _now(conn):
        return float(conn.execute("SELECT extract(epoch FROM clock_timestamp())").fetchone()[0])

    @contextmanager
    def _transaction(self):
        try:
            import psycopg

            with psycopg.connect(self._dsn, connect_timeout=3) as conn:
                conn.execute("SET LOCAL statement_timeout = '3s'")
                conn.execute("SET LOCAL lock_timeout = '3s'")
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK,))
                if not self._ready:
                    conn.execute("""CREATE TABLE IF NOT EXISTS lco_rate_state (
                        scope text PRIMARY KEY, policy jsonb NOT NULL,
                        next_at double precision NOT NULL DEFAULT 0,
                        cooldown_until double precision NOT NULL DEFAULT 0)""")
                    conn.execute("""CREATE TABLE IF NOT EXISTS lco_api_requests (
                        at double precision NOT NULL, anonymous boolean NOT NULL)""")
                    conn.execute("CREATE INDEX IF NOT EXISTS lco_api_requests_at ON lco_api_requests(at)")
                    conn.execute("""CREATE TABLE IF NOT EXISTS lco_rate_policies (
                        digest text PRIMARY KEY, policy jsonb NOT NULL,
                        seen_at double precision NOT NULL)""")
                yield conn
            self._ready = True
        except Exception as exc:
            # Connection errors can include passwords/DSNs. Never propagate or
            # log their text; the type still separates an outage from a bad row.
            logger.warning("LCO request coordinator failed: %s", type(exc).__name__)
            raise _error("Shared LCO request coordinator unavailable") from None

    def _effective_policy(self, conn, now):
        """The strictest policy among releases seen within ``POLICY_TTL_S``.

        This process's own policy always applies. Another release's tightening
        lapses an hour after its last request, so a stopped branch or laptop
        cannot lower production's limits permanently.
        """
        own = json.dumps(self._policy, sort_keys=True)
        conn.execute(
            "INSERT INTO lco_rate_policies(digest, policy, seen_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (digest) DO UPDATE SET seen_at=greatest(lco_rate_policies.seen_at, excluded.seen_at)",
            (hashlib.sha256(own.encode()).hexdigest(), own, now))
        conn.execute("DELETE FROM lco_rate_policies WHERE seen_at < %s", (now - POLICY_TTL_S,))
        policy = dict(self._policy)
        for (stored,) in conn.execute("SELECT policy FROM lco_rate_policies ORDER BY digest").fetchall():
            policy = _strictest(stored, policy)
        return policy

    def _state(self, conn, scope, now):
        if scope not in {"archive", "storage"}:
            raise ValueError("unknown request scope")
        policy = self._effective_policy(conn, now)
        conn.execute("INSERT INTO lco_rate_state(scope, policy) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                     (scope, json.dumps(policy)))
        stored, next_at, cooldown = conn.execute(
            "SELECT policy, next_at, cooldown_until FROM lco_rate_state WHERE scope=%s", (scope,)
        ).fetchone()
        if policy != stored:
            # Informational only: the effective policy is recomputed every time.
            conn.execute("UPDATE lco_rate_state SET policy=%s WHERE scope=%s", (json.dumps(policy), scope))
        if cooldown > now + MAX_SHARED_COOLDOWN_S:
            # A longer cooldown stored by an older release, or by hand.
            cooldown = now + MAX_SHARED_COOLDOWN_S
            conn.execute("UPDATE lco_rate_state SET cooldown_until=%s WHERE scope=%s", (cooldown, scope))
        return policy, next_at, cooldown

    def reserve(self, scope, *, anonymous=False):
        """Atomically debit one start, or return seconds until another attempt.

        Sliding 24h, not a UTC-day reset. Failed requests and abandoned permits
        still count. No future permits are issued ahead of a shared cooldown.
        """
        with self._transaction() as conn:
            now = self._now(conn)
            policy, next_at, cooldown = self._state(conn, scope, now)
            ready = max(next_at, cooldown)
            if scope == "archive":
                conn.execute("DELETE FROM lco_api_requests WHERE at <= %s", (now - DAY,))
                count, oldest, anon_count, anon_oldest = conn.execute(
                    "SELECT count(*), min(at), count(*) FILTER (WHERE anonymous), "
                    "min(at) FILTER (WHERE anonymous) FROM lco_api_requests"
                ).fetchone()
                if count >= policy["api_daily"]:
                    ready = max(ready, oldest + DAY)
                if anonymous and anon_count >= policy["anonymous_daily"]:
                    ready = max(ready, anon_oldest + DAY)
            if ready > now:
                return ready - now
            if scope == "archive":
                conn.execute("INSERT INTO lco_api_requests VALUES (%s, %s)", (now, anonymous))
            interval = policy["api_interval" if scope == "archive" else "storage_interval"]
            conn.execute("UPDATE lco_rate_state SET next_at=%s WHERE scope=%s", (now + interval, scope))
            return 0.0

    def cooldown(self, scope, header):
        with self._transaction() as conn:
            now = self._now(conn)
            self._state(conn, scope, now)
            seconds = retry_after_seconds(header, now)
            if seconds > MAX_SHARED_COOLDOWN_S:
                logger.warning("Shortened a shared LCO %s cooldown of %.0f s to %d s",
                               scope, seconds, MAX_SHARED_COOLDOWN_S)
                seconds = MAX_SHARED_COOLDOWN_S
            until = conn.execute(
                "UPDATE lco_rate_state SET cooldown_until=greatest(cooldown_until,%s) "
                "WHERE scope=%s RETURNING cooldown_until", (now + seconds, scope)
            ).fetchone()[0]
            return until - now


@lru_cache(maxsize=4)
def _coordinator(dsn):
    return Coordinator(dsn)


def coordinator():
    # Never fall back to the profile-specific queue DSN: that doubles budgets.
    dsn = os.environ.get("MUSCAT_LCO_RATE_DSN", "")
    if not dsn:
        raise _error("MUSCAT_LCO_RATE_DSN must name the shared LCO request coordinator")
    return _coordinator(dsn)


def acquire(scope, *, anonymous=False, check=None, max_wait=2.0):
    end = time.monotonic() + max_wait
    while True:
        if check:
            check()
        delay = coordinator().reserve(scope, anonymous=anonymous)
        if not delay:
            if check:
                check()
            return
        # Interactive/monitor calls return promptly. Durable workers can wait
        # for a day budget while their independent heartbeat renews the lease.
        if not check and time.monotonic() + delay > end:
            raise _error("Shared LCO request budget or cooldown active", status=429, retry_after=delay)
        wait(delay, check)


@contextmanager
def request_context(check):
    token = _CHECK.set(check)
    try:
        yield
    finally:
        _CHECK.reset(token)


def _scope(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host == "archive-api.lco.global":
        return "archive"
    if host.startswith("archive-lco-global.s3") or host.endswith(".amazonaws.com"):
        return "storage"
    return None  # Observation Portal uses its own limits; no archive debit.


def admit(url, *, authorized):
    """Wait for this hop's shared budget; every transport calls this per hop."""
    scope = _scope(url)
    if scope:
        acquire(scope, anonymous=not authorized, check=_CHECK.get())


def throttled(url, status, retry_after):
    """Publish a shared cooldown for a throttling reply; return its seconds."""
    scope = _scope(url)
    if scope and (status == 429 or (scope == "storage" and status == 503)):
        return coordinator().cooldown(scope, retry_after)
    return None


class ArchiveRateHandler(urllib.request.BaseHandler):
    """Intercept every HTTP hop, including redirects, before error processing."""

    handler_order = 490

    def http_request(self, req):
        admit(req.full_url, authorized=req.has_header("Authorization"))
        return req

    https_request = http_request

    def http_response(self, req, response):
        try:
            seconds = throttled(req.full_url, response.code, response.headers.get("Retry-After"))
        except Exception:
            response.close()
            raise
        if seconds is not None:
            # Preserve the coordinated wait for HTTPError/API consumers. Do
            # not copy response bodies or URLs into public error messages.
            del response.headers["Retry-After"]
            response.headers["Retry-After"] = str(math.ceil(seconds))
        return response

    https_response = http_response
