"""Shared archive request admission, independent of the job queue database.

Only fixed scope names, timestamps, policy numbers and an anonymous flag are
persisted. Credentials, URLs, users and profile names never enter this ledger.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from email.utils import parsedate_to_datetime
from functools import lru_cache
import json
import math
import os
import random
import time
import urllib.parse
import urllib.request

DAY = 86400
_LOCK = 389404
_CHECK = ContextVar("lco_request_check", default=None)
# Fixed across releases/profiles; a mixed policy fails closed. Deliberately
# aggregate all identities, leaving headroom under LCO's per-user allowance.
_POLICY = {"api_interval": 1.0, "api_daily": 20000, "anonymous_daily": 500,
           "storage_interval": 0.25}


def _error(message, status=503, retry_after=60):
    from muscat_db.lco import LcoError

    return LcoError(message, status=status, retry_after=math.ceil(retry_after))


def retry_after_seconds(value, now):
    """Accept RFC Retry-After seconds or an HTTP date; malformed uses 60s."""
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
        return max(1.0, seconds)
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
                yield conn
            self._ready = True
        except Exception:
            # Connection errors can include passwords/DSNs. Never propagate them.
            raise _error("Shared LCO request coordinator unavailable or policy mismatched") from None

    def _state(self, conn, scope):
        if scope not in {"archive", "storage"}:
            raise ValueError("unknown request scope")
        conn.execute("INSERT INTO lco_rate_state(scope, policy) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                     (scope, json.dumps(self._policy)))
        policy, next_at, cooldown = conn.execute(
            "SELECT policy, next_at, cooldown_until FROM lco_rate_state WHERE scope=%s", (scope,)
        ).fetchone()
        if policy != self._policy:
            raise ValueError("request policy mismatch")
        return next_at, cooldown

    def reserve(self, scope, *, anonymous=False):
        """Atomically debit one start, or return seconds until another attempt.

        Sliding 24h, not a UTC-day reset. Failed requests and abandoned permits
        still count. No future permits are issued ahead of a shared cooldown.
        """
        with self._transaction() as conn:
            next_at, cooldown = self._state(conn, scope)
            now = self._now(conn)
            ready = max(next_at, cooldown)
            if scope == "archive":
                conn.execute("DELETE FROM lco_api_requests WHERE at <= %s", (now - DAY,))
                count, oldest, anon_count, anon_oldest = conn.execute(
                    "SELECT count(*), min(at), count(*) FILTER (WHERE anonymous), "
                    "min(at) FILTER (WHERE anonymous) FROM lco_api_requests"
                ).fetchone()
                if count >= self._policy["api_daily"]:
                    ready = max(ready, oldest + DAY)
                if anonymous and anon_count >= self._policy["anonymous_daily"]:
                    ready = max(ready, anon_oldest + DAY)
            if ready > now:
                return ready - now
            if scope == "archive":
                conn.execute("INSERT INTO lco_api_requests VALUES (%s, %s)", (now, anonymous))
            interval = self._policy["api_interval" if scope == "archive" else "storage_interval"]
            conn.execute("UPDATE lco_rate_state SET next_at=%s WHERE scope=%s", (now + interval, scope))
            return 0.0

    def cooldown(self, scope, header):
        with self._transaction() as conn:
            self._state(conn, scope)
            now = self._now(conn)
            seconds = retry_after_seconds(header, now)
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


class ArchiveRateHandler(urllib.request.BaseHandler):
    """Intercept every HTTP hop, including redirects, before error processing."""

    handler_order = 490

    def http_request(self, req):
        scope = _scope(req.full_url)
        if scope:
            acquire(scope, anonymous=not req.has_header("Authorization"), check=_CHECK.get())
        return req

    https_request = http_request

    def http_response(self, req, response):
        scope = _scope(req.full_url)
        if scope and (response.code == 429 or (scope == "storage" and response.code == 503)):
            try:
                seconds = coordinator().cooldown(scope, response.headers.get("Retry-After"))
            except Exception:
                response.close()
                raise
            # Preserve the coordinated wait for HTTPError/API consumers. Do
            # not copy response bodies or URLs into public error messages.
            del response.headers["Retry-After"]
            response.headers["Retry-After"] = str(math.ceil(seconds))
        return response

    https_response = http_response
