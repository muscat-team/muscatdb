"""Per-user caps on running and queued jobs (issue #265).

Two caps apply to every user, counted across photometry, transit fit and TTV
fit together:

* **running** -- durable concurrency slots a user holds. A launch that would
  exceed it is queued instead of started, and the pending drain skips the
  user's queued rows until a slot frees (see ``claim_slot``'s ``max_user_slots``).
* **queued** -- ``pending`` rows a user owns. An enqueue that would exceed it is
  refused with :class:`JobLimitExceeded` (see ``JobQueue.enqueue``'s
  ``max_queued``).

Both default to the site values below and can be overridden per user in
``users.max_running_jobs`` / ``users.max_queued_jobs`` (NULL = site default).
``0`` means unlimited, in either place. Test runs take no durable slot and are
not counted.

The user rows live in SQLite while production's job store may be PostgreSQL, so
the limit is read here first and handed to the job store, which does the count
and the claim/insert atomically.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass

logger = logging.getLogger(__name__)

UNLIMITED = 0
_DEFAULT_MAX_RUNNING = 1
_DEFAULT_MAX_QUEUED = 5


# Database paths whose users table has been migrated by this process: limits_for
# runs on every pending-drain pass, so the schema probe must not.
_migrated_paths: set[str] = set()


class JobLimitExceeded(Exception):
    """Raised by an enqueue that would take a user over their queued-job cap."""


@dataclass(frozen=True)
class UserLimits:
    """Resolved caps for one user; ``0`` (:data:`UNLIMITED`) means no cap."""

    running: int
    queued: int


def _env_cap(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(UNLIMITED, int(raw))
    except ValueError:
        logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default


def site_defaults() -> UserLimits:
    """The caps a user gets when their row carries no override."""
    return UserLimits(
        running=_env_cap("MUSCAT_USER_MAX_RUNNING", _DEFAULT_MAX_RUNNING),
        queued=_env_cap("MUSCAT_USER_MAX_QUEUED", _DEFAULT_MAX_QUEUED),
    )


def limits_for(user_name: str | None) -> UserLimits | None:
    """Caps for *user_name*, or ``None`` for a job with no user.

    A job with no user (an instance without an authenticating proxy) has no
    identity to count against, so it is not capped. A failed lookup falls back
    to the site defaults rather than lifting the cap.
    """
    if not user_name:
        return None
    from muscat_db import database

    defaults = site_defaults()
    path = database.db_path()
    try:
        with database.get_conn(path) as conn:
            if path not in _migrated_paths:
                database._apply_schema(conn)
                _migrated_paths.add(path)
            row = conn.execute(
                "SELECT max_running_jobs, max_queued_jobs FROM users WHERE username = ?",
                (user_name,),
            ).fetchone()
    except sqlite3.Error:
        logger.warning("could not read job limits for %s; using site defaults", user_name, exc_info=True)
        return defaults
    if row is None:
        return defaults
    running, queued = row
    return UserLimits(
        running=defaults.running if running is None else running,
        queued=defaults.queued if queued is None else queued,
    )


def set_limits(
    db_path: str,
    username: str,
    *,
    running: int | None = None,
    queued: int | None = None,
    reset: bool = False,
) -> UserLimits | None:
    """Store per-user overrides and return the user's resolved caps.

    With *reset* both overrides are cleared (back to the site defaults);
    otherwise only the values passed are changed. The user row is created if
    missing, the same way ``htpasswd promote`` and ``access scope`` do.
    """
    from muscat_db import database

    for name, value in (("running", running), ("queued", queued)):
        if value is not None and value < UNLIMITED:
            raise ValueError(f"{name} limit must be >= 0 (0 means unlimited), got {value}")
    with database.get_conn(db_path) as conn:
        database._apply_schema(conn)
        conn.execute(
            "INSERT OR IGNORE INTO users (username, display_name) VALUES (?, ?)",
            (username, username),
        )
        if reset:
            conn.execute(
                "UPDATE users SET max_running_jobs = NULL, max_queued_jobs = NULL WHERE username = ?",
                (username,),
            )
        if running is not None:
            conn.execute("UPDATE users SET max_running_jobs = ? WHERE username = ?", (running, username))
        if queued is not None:
            conn.execute("UPDATE users SET max_queued_jobs = ? WHERE username = ?", (queued, username))
        conn.commit()
    return limits_for(username)


def describe(cap: int) -> str:
    return "unlimited" if cap == UNLIMITED else str(cap)


def queued_limit_message(user_name: str, cap: int) -> str:
    """The text a refused enqueue carries back to the caller (HTTP 429)."""
    return (
        f"{user_name} already has {cap} queued job(s), the per-user limit; "
        "wait for one to start or cancel one"
    )


def claim_caps(user_name: str | None) -> dict:
    """``claim_slot`` keyword arguments carrying *user_name*'s running cap."""
    limits = limits_for(user_name)
    return {"user_name": user_name or None, "max_user_slots": limits.running if limits else None}


def enqueue_caps(user_name: str | None) -> dict:
    """``enqueue`` keyword argument carrying *user_name*'s queued cap."""
    limits = limits_for(user_name)
    return {"max_queued": limits.queued if limits else None}


def limit_refusal(exc: JobLimitExceeded) -> dict:
    """The launch result for a refused enqueue; ``limit_exceeded`` lets the web
    layer answer 429 instead of 400."""
    return {"ok": False, "error": str(exc), "limit_exceeded": True}
