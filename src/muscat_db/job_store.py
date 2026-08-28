"""Persistence seam for background jobs (architecture audit C2).

Today the authoritative *live* state of a job lives in the web process's RAM
(``photometry._JOBS`` / ``transit_fit._FIT_JOBS``) while its *durable* state lives
in the ``jobs`` table, reached through ad-hoc ``save_job`` / ``get_persisted_jobs``
calls and inline ``DELETE FROM jobs`` SQL scattered across both pipelines and the
web layer. None of that survives the move to the planned multi-server (Celery /
Redis) setup.

This module introduces a single interface — :class:`JobRepository` (durable CRUD),
:class:`JobQueue` (pending-work ordering), and :class:`JobConcurrency` (the
cross-process full-job cap, see ``job_concurrency_slots`` in database.py) —
that both pipelines and the web layer hold instead of touching the database
directly. The concrete :class:`DatabaseJobStore` keeps using the existing
``jobs`` table, so single-host behaviour is unchanged.

:class:`PostgresJobStore` (architecture issue #51 step 2) implements the same
three Protocols against PostgreSQL instead of SQLite, so callers never branch
on which store is installed. It is selected via ``MUSCAT_CONTROL_PLANE=postgres``
(default: ``sqlite``, i.e. :class:`DatabaseJobStore`, unchanged) and needs the
optional ``muscatdb[cluster]`` extra (``psycopg``). :func:`get_job_store`
resolves the backend lazily on first call and caches it, so importing this
module never requires ``psycopg`` or a reachable Postgres unless the control
plane is actually configured for it. :func:`set_job_store` remains the manual
swap point for tests or a future backend.

The repository read methods intentionally return plain ``dict`` rows in the exact
shape ``get_persisted_jobs`` already produces, so callers keep reading
``entry["state"]`` etc. without a data-model rewrite.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Protocol, runtime_checkable

# Imported as a module (not by name) so the concrete store sees monkeypatched
# muscat_db.database.save_job / get_persisted_jobs in tests and any runtime swap.
from muscat_db import database

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)


@runtime_checkable
class JobRepository(Protocol):
    """Durable CRUD over job records, keyed by the canonical job key
    ``"<type>:<inst>/<date>/<target>[/<run_id>]"``."""

    def all(self) -> list[dict]:
        """All job rows, newest-first (by ``started_at``)."""
        ...

    def active(self) -> list[dict]:
        """Only running, cancelling, or pending rows, newest-first."""
        ...

    def get(self, key: str) -> dict | None:
        """The single row for *key* (the table key is unique), or ``None``."""
        ...

    def save(
        self,
        *,
        type_: str,
        inst: str,
        date: str,
        target: str,
        state: str,
        returncode: int | None,
        elapsed: int,
        started_at: float,
        error_desc: str = "",
        run_type: str = "",
        params: str = "",
        run_id: str = "",
        run_name: str = "",
        user_name: str | None = None,
    ) -> None:
        """Upsert one job record (same fields as the legacy ``save_job``)."""
        ...

    def delete(self, key: str) -> None:
        """Remove the job row for *key* if present (no-op when absent)."""
        ...


@runtime_checkable
class JobQueue(Protocol):
    """Pending-work ordering. A future broker-backed implementation replaces the
    ``state='pending'`` row convention without changing callers."""

    def enqueue(
        self,
        *,
        type_: str,
        inst: str,
        date: str,
        target: str,
        started_at: float,
        run_type: str = "",
        params: str = "",
        run_id: str = "",
        run_name: str = "",
        user_name: str | None = None,
    ) -> None:
        """Record a job as pending (queued, not yet launched)."""
        ...

    def pending(self, type_: str) -> list[dict]:
        """Pending jobs of *type_*, oldest-first (FIFO drain order)."""
        ...


@runtime_checkable
class JobConcurrency(Protocol):
    """Cross-process/cross-server concurrency gate for a pipeline's full-job
    cap (architecture audit finding: ``_MAX_FULL_JOBS`` was enforced via an
    in-memory-only per-process dict, already wrong under ``--workers N>1``
    and not viable once multiple servers share one database). A future
    Celery/Redis backend replaces the SQLite-backed slot claim with
    broker-native concurrency limits without changing callers."""

    def claim_slot(self, pipeline: str, holder_key: str, max_slots: int) -> bool:
        """Atomically claim one of *max_slots* concurrency slots for
        *pipeline* under *holder_key* (the pipeline's own job key, unprefixed).
        Returns True only if this call newly claimed the slot; False if
        *holder_key* is already claimed (by this or another caller) or all
        slots are taken. Never silently double-claims the same key, so two
        processes racing to launch the same job never both proceed."""
        ...

    def release_slot(self, pipeline: str, holder_key: str) -> None:
        """Release the slot held by *holder_key* for *pipeline*, if any.
        Best-effort: never raises."""
        ...

    def count_claimed(self, pipeline: str) -> int:
        """Number of currently-claimed slots for *pipeline*, across every
        process/server sharing this database."""
        ...

    def reconcile_slots(self, pipeline: str) -> int:
        """Release any claimed slot whose holder_key's persisted job row is
        no longer ``state='running'`` (the claimant finished, crashed, or was
        restarted without releasing cleanly). Checks the durable ``jobs``
        table rather than any one process's in-memory state, so it is safe to
        call from any process. Returns the number of slots released."""
        ...


class DatabaseJobStore(JobRepository, JobQueue, JobConcurrency):
    """``jobs``-table-backed store. Delegates record writes/reads to
    :mod:`muscat_db.database` (so the daily-build and migration paths stay the
    single owner of the schema) and owns the row-delete SQL that previously lived
    inline in the pipelines."""

    def all(self) -> list[dict]:
        return database.get_persisted_jobs()

    def active(self) -> list[dict]:
        return database.get_active_persisted_jobs()

    def get(self, key: str) -> dict | None:
        return next((j for j in database.get_persisted_jobs() if j.get("key") == key), None)

    def save(
        self,
        *,
        type_: str,
        inst: str,
        date: str,
        target: str,
        state: str,
        returncode: int | None,
        elapsed: int,
        started_at: float,
        error_desc: str = "",
        run_type: str = "",
        params: str = "",
        run_id: str = "",
        run_name: str = "",
        user_name: str | None = None,
    ) -> None:
        database.save_job(
            type_=type_,
            inst=inst,
            date=date,
            target=target,
            state=state,
            returncode=returncode,
            elapsed=elapsed,
            started_at=started_at,
            error_desc=error_desc,
            run_type=run_type,
            params=params,
            run_id=run_id,
            run_name=run_name,
            user_name=user_name,
        )

    def delete(self, key: str) -> None:
        # Best-effort, matching the prior inline behaviour: a failed delete must
        # not break the surrounding delete-reduction / delete-fit flow.
        try:
            with database.get_conn() as conn:
                conn.execute("DELETE FROM jobs WHERE key = ?", (key,))
                conn.commit()
            database.clear_all_caches()
        except Exception:
            logger.debug("failed to delete job row %s", key, exc_info=True)

    def enqueue(
        self,
        *,
        type_: str,
        inst: str,
        date: str,
        target: str,
        started_at: float,
        run_type: str = "",
        params: str = "",
        run_id: str = "",
        run_name: str = "",
        user_name: str | None = None,
    ) -> None:
        self.save(
            type_=type_,
            inst=inst,
            date=date,
            target=target,
            state="pending",
            returncode=None,
            elapsed=0,
            started_at=started_at,
            run_type=run_type,
            params=params,
            run_id=run_id,
            run_name=run_name,
            user_name=user_name,
        )

    def pending(self, type_: str) -> list[dict]:
        rows = [
            j
            for j in database.get_persisted_jobs()
            if j.get("type") == type_ and j.get("state") == "pending"
        ]
        rows.sort(key=lambda j: j.get("started_at") or 0)
        return rows

    def claim_slot(self, pipeline: str, holder_key: str, max_slots: int) -> bool:
        # A single INSERT ... SELECT ... WHERE statement is one atomic write:
        # SQLite serializes writers even in WAL mode, so the capacity check
        # (the COUNT subquery) and the claim (the INSERT) can never interleave
        # with another connection's claim/release. OR IGNORE makes a repeat
        # claim for the same (pipeline, holder_key) a no-op (PRIMARY KEY
        # conflict) rather than an error, and rowcount then distinguishes "I
        # newly claimed it" (1) from "already claimed, by anyone" or "no free
        # slot" (0 either way) -- the caller only ever gets True from the
        # single claim that actually won, so two racing launches for the same
        # key never both proceed.
        with database.get_conn() as conn:
            conn.executescript(database.SCHEMA)
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO job_concurrency_slots (pipeline, holder_key, claimed_at)
                SELECT ?, ?, ?
                WHERE (SELECT COUNT(*) FROM job_concurrency_slots WHERE pipeline = ?) < ?
                """,
                (pipeline, holder_key, time.time(), pipeline, max_slots),
            )
            conn.commit()
        return cur.rowcount > 0

    def release_slot(self, pipeline: str, holder_key: str) -> None:
        try:
            with database.get_conn() as conn:
                conn.executescript(database.SCHEMA)
                conn.execute(
                    "DELETE FROM job_concurrency_slots WHERE pipeline = ? AND holder_key = ?",
                    (pipeline, holder_key),
                )
                conn.commit()
        except Exception:
            logger.debug(
                "failed to release concurrency slot pipeline=%s holder_key=%s",
                pipeline, holder_key, exc_info=True,
            )

    def count_claimed(self, pipeline: str) -> int:
        with database.get_conn() as conn:
            conn.executescript(database.SCHEMA)
            row = conn.execute(
                "SELECT COUNT(*) FROM job_concurrency_slots WHERE pipeline = ?",
                (pipeline,),
            ).fetchone()
        return int(row[0]) if row else 0

    def reconcile_slots(self, pipeline: str) -> int:
        with database.get_conn() as conn:
            conn.executescript(database.SCHEMA)
            holder_keys = [
                r[0] for r in conn.execute(
                    "SELECT holder_key FROM job_concurrency_slots WHERE pipeline = ?",
                    (pipeline,),
                ).fetchall()
            ]
            if not holder_keys:
                return 0
            db_keys = [f"{pipeline}:{hk}" for hk in holder_keys]
            placeholders = ",".join("?" for _ in db_keys)
            running_db_keys = {
                r[0] for r in conn.execute(
                    f"SELECT key FROM jobs WHERE key IN ({placeholders}) AND state = 'running'",
                    db_keys,
                ).fetchall()
            }
            stale = [hk for hk in holder_keys if f"{pipeline}:{hk}" not in running_db_keys]
            for hk in stale:
                conn.execute(
                    "DELETE FROM job_concurrency_slots WHERE pipeline = ? AND holder_key = ?",
                    (pipeline, hk),
                )
            if stale:
                conn.commit()
        return len(stale)


# DDL for PostgresJobStore, scoped to only the two tables this module owns.
# Deliberately not the rest of the multi-host control plane MUSCATDB-LITE.md
# §12 describes (settings/tokens, notes, lco_observation_*, ephemeris_views)
# -- those stay out of scope until something other than job_store.py needs
# them on Postgres too.
_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    key          TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    instrument   TEXT NOT NULL,
    obsdate      TEXT NOT NULL,
    target       TEXT NOT NULL,
    state        TEXT NOT NULL,
    returncode   INTEGER,
    elapsed      INTEGER NOT NULL,
    started_at   DOUBLE PRECISION NOT NULL,
    error_desc   TEXT,
    run_type     TEXT NOT NULL DEFAULT '',
    params       TEXT NOT NULL DEFAULT '',
    run_id       TEXT NOT NULL DEFAULT '',
    run_name     TEXT NOT NULL DEFAULT '',
    user_name    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_state_started ON jobs(state, started_at DESC);

CREATE TABLE IF NOT EXISTS job_concurrency_slots (
    pipeline    TEXT NOT NULL,
    holder_key  TEXT NOT NULL,
    claimed_at  DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (pipeline, holder_key)
);
"""


def _pg_rows_to_dicts(columns: list[str], rows: list[tuple]) -> list[dict]:
    """Row-shaping shared by every PostgresJobStore read method: alias
    instrument/obsdate to the inst/date keys callers already use, and fall
    back run_name to run_id -- exactly what database.get_persisted_jobs does
    for DatabaseJobStore, so the two backends hand callers identical dicts."""
    result = []
    for row in rows:
        d = dict(zip(columns, row))
        d["inst"] = d["instrument"]
        d["date"] = d["obsdate"]
        if not str(d.get("run_name") or "").strip():
            d["run_name"] = str(d.get("run_id") or "").strip()
        result.append(d)
    return result


class PostgresJobStore(JobRepository, JobQueue, JobConcurrency):
    """PostgreSQL-backed control-plane adapter (``MUSCATDB-LITE.md`` §12),
    selected via ``MUSCAT_CONTROL_PLANE=postgres``. Implements the same
    Protocol as :class:`DatabaseJobStore` so callers never branch on which
    store is installed; the only behavioural difference is that this one is
    safe for concurrent writers across hosts, which SQLite-over-NFS is not.

    Requires the optional ``muscatdb[cluster]`` extra (``psycopg``); importing
    *this module* never requires it -- only constructing this class does, so a
    base install is unaffected. Connects lazily via a small pool, sized for
    "a handful of workers claiming jobs", not high-throughput OLTP.
    """

    def __init__(self, dsn: str | None = None):
        try:
            import psycopg  # noqa: F401  (import-checked here, used via the pool)
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise RuntimeError(
                "PostgresJobStore requires the 'cluster' extra: "
                "install with `uv sync --extra cluster` (adds psycopg)."
            ) from exc
        if dsn is None:
            dsn = os.environ.get("MUSCAT_POSTGRES_DSN")
            if not dsn:
                raise RuntimeError(
                    "MUSCAT_CONTROL_PLANE=postgres requires MUSCAT_POSTGRES_DSN "
                    "(a postgresql:// connection string)."
                )
        self._pool: ConnectionPool = ConnectionPool(dsn, min_size=1, max_size=5, open=True)
        with self._pool.connection() as conn:
            conn.execute(_PG_SCHEMA)

    def close(self) -> None:
        """Release the connection pool. Not part of the Protocol -- callers
        that install this store for a process lifetime never need it; tests
        that construct one per case do, to avoid leaking pool threads."""
        self._pool.close()

    def all(self) -> list[dict]:
        with self._pool.connection() as conn:
            cur = conn.execute("SELECT * FROM jobs ORDER BY started_at DESC")
            columns = [d.name for d in cur.description]
            rows = cur.fetchall()
        return _pg_rows_to_dicts(columns, rows)

    def active(self) -> list[dict]:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "SELECT * FROM jobs WHERE state IN ('running', 'cancelling', 'pending') "
                "ORDER BY started_at DESC"
            )
            columns = [d.name for d in cur.description]
            rows = cur.fetchall()
        return _pg_rows_to_dicts(columns, rows)

    def get(self, key: str) -> dict | None:
        with self._pool.connection() as conn:
            cur = conn.execute("SELECT * FROM jobs WHERE key = %s", (key,))
            columns = [d.name for d in cur.description]
            row = cur.fetchone()
        if row is None:
            return None
        return _pg_rows_to_dicts(columns, [row])[0]

    def save(
        self,
        *,
        type_: str,
        inst: str,
        date: str,
        target: str,
        state: str,
        returncode: int | None,
        elapsed: int,
        started_at: float,
        error_desc: str = "",
        run_type: str = "",
        params: str = "",
        run_id: str = "",
        run_name: str = "",
        user_name: str | None = None,
    ) -> None:
        if user_name is None:
            user_name = ""
        key = f"{type_}:{inst}/{date}/{target.replace(' ', '')}"
        if run_id:
            key = f"{key}/{run_id}"
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO jobs(key, type, instrument, obsdate, target, state, returncode,
                                  elapsed, started_at, error_desc, run_type, params, run_id,
                                  run_name, user_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (key) DO UPDATE SET
                    state      = EXCLUDED.state,
                    returncode = EXCLUDED.returncode,
                    elapsed    = EXCLUDED.elapsed,
                    started_at = EXCLUDED.started_at,
                    error_desc = EXCLUDED.error_desc,
                    run_type   = CASE WHEN EXCLUDED.run_type  != '' THEN EXCLUDED.run_type  ELSE jobs.run_type  END,
                    params     = CASE WHEN EXCLUDED.params    != '' THEN EXCLUDED.params    ELSE jobs.params    END,
                    run_id     = EXCLUDED.run_id,
                    run_name   = CASE WHEN EXCLUDED.run_name  != '' THEN EXCLUDED.run_name  ELSE jobs.run_name  END,
                    user_name  = CASE WHEN EXCLUDED.user_name != '' THEN EXCLUDED.user_name ELSE jobs.user_name END
                """,
                (key, type_, inst, date, target, state, returncode, elapsed, started_at,
                 error_desc, run_type, params, run_id, run_name, user_name),
            )

    def delete(self, key: str) -> None:
        try:
            with self._pool.connection() as conn:
                conn.execute("DELETE FROM jobs WHERE key = %s", (key,))
        except Exception:
            logger.debug("failed to delete job row %s", key, exc_info=True)

    def enqueue(
        self,
        *,
        type_: str,
        inst: str,
        date: str,
        target: str,
        started_at: float,
        run_type: str = "",
        params: str = "",
        run_id: str = "",
        run_name: str = "",
        user_name: str | None = None,
    ) -> None:
        self.save(
            type_=type_,
            inst=inst,
            date=date,
            target=target,
            state="pending",
            returncode=None,
            elapsed=0,
            started_at=started_at,
            run_type=run_type,
            params=params,
            run_id=run_id,
            run_name=run_name,
            user_name=user_name,
        )

    def pending(self, type_: str) -> list[dict]:
        rows = [j for j in self.all() if j.get("type") == type_ and j.get("state") == "pending"]
        rows.sort(key=lambda j: j.get("started_at") or 0)
        return rows

    def claim_slot(self, pipeline: str, holder_key: str, max_slots: int) -> bool:
        with self._pool.connection() as conn:
            # Unlike SQLite (whole-database writer serialization even in WAL
            # mode), Postgres's default READ COMMITTED isolation lets two
            # concurrent transactions both see the same COUNT before either
            # commits its INSERT -- the capacity check below would not be
            # atomic without this. pg_advisory_xact_lock, keyed on a hash of
            # the pipeline name, serializes claims for that pipeline across
            # every connection/host and releases automatically at this
            # transaction's COMMIT/ROLLBACK, so it never needs an explicit
            # unlock or leaks past a crash.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (pipeline,))
            cur = conn.execute(
                """
                INSERT INTO job_concurrency_slots (pipeline, holder_key, claimed_at)
                SELECT %s, %s, %s
                WHERE (SELECT COUNT(*) FROM job_concurrency_slots WHERE pipeline = %s) < %s
                ON CONFLICT (pipeline, holder_key) DO NOTHING
                """,
                (pipeline, holder_key, time.time(), pipeline, max_slots),
            )
            return cur.rowcount > 0

    def release_slot(self, pipeline: str, holder_key: str) -> None:
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    "DELETE FROM job_concurrency_slots WHERE pipeline = %s AND holder_key = %s",
                    (pipeline, holder_key),
                )
        except Exception:
            logger.debug(
                "failed to release concurrency slot pipeline=%s holder_key=%s",
                pipeline, holder_key, exc_info=True,
            )

    def count_claimed(self, pipeline: str) -> int:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM job_concurrency_slots WHERE pipeline = %s",
                (pipeline,),
            ).fetchone()
        return int(row[0]) if row else 0

    def reconcile_slots(self, pipeline: str) -> int:
        with self._pool.connection() as conn:
            holder_keys = [
                r[0] for r in conn.execute(
                    "SELECT holder_key FROM job_concurrency_slots WHERE pipeline = %s",
                    (pipeline,),
                ).fetchall()
            ]
            if not holder_keys:
                return 0
            db_keys = [f"{pipeline}:{hk}" for hk in holder_keys]
            running_db_keys = {
                r[0] for r in conn.execute(
                    "SELECT key FROM jobs WHERE key = ANY(%s) AND state = 'running'",
                    (db_keys,),
                ).fetchall()
            }
            stale = [hk for hk in holder_keys if f"{pipeline}:{hk}" not in running_db_keys]
            for hk in stale:
                conn.execute(
                    "DELETE FROM job_concurrency_slots WHERE pipeline = %s AND holder_key = %s",
                    (pipeline, hk),
                )
        return len(stale)


# Active store singleton. Resolved lazily (not at import time, so importing
# this module never requires psycopg or a reachable Postgres) from
# MUSCAT_CONTROL_PLANE on first get_job_store() call, then cached -- swap it
# explicitly with set_job_store() for tests or a future backend.
_STORE: JobRepository | JobQueue | JobConcurrency | None = None


def _make_default_store() -> JobRepository:
    backend = os.environ.get("MUSCAT_CONTROL_PLANE", "sqlite").strip().lower()
    if backend == "sqlite":
        return DatabaseJobStore()
    if backend == "postgres":
        return PostgresJobStore()
    raise ValueError(
        f"unknown MUSCAT_CONTROL_PLANE={backend!r}; expected 'sqlite' or 'postgres'"
    )


def get_job_store() -> DatabaseJobStore:
    """Return the process-wide job store (the C2 seam)."""
    global _STORE
    if _STORE is None:
        _STORE = _make_default_store()
    return _STORE  # type: ignore[return-value]


def set_job_store(store) -> None:
    """Install a different job store implementation (Postgres backend, test double)."""
    global _STORE
    _STORE = store
