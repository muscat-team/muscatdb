"""Durable archive queue on the existing PostgreSQL jobs table.

The archive has independent I/O capacity; science concurrency slots are never
claimed here. All admissions/claims/reclaims serialize on one transaction lock.
Every update from an executor is fenced by a fresh, per-attempt lease token.
Only public progress lives in params. Signed URLs and credentials live in the
private shared spool (see lco_download), never in a jobs row.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager

from muscat_db import job_store

TYPE = "lco_archive_download"
OWNER = "archive_queue"
LEASE_S = 300
MAX_ATTEMPTS = 3
# Hard fleet ceilings, independent of the older per-web-process settings.
MAX_JOBS = 2
FRAME_WORKERS = 4
_LOCK = 0x6D75_7363_6C63_6F31
TERMINAL = {"done", "error", "cancelled"}


def enabled() -> bool:
    return job_store.uses_shared_control_plane()


def key(job_id: str) -> str:
    return f"{TYPE}:{job_id}"


class ArchiveQueue:
    """Postgres-only adapter sharing the configured JobStore connection pool."""

    def __init__(self, store=None):
        self.store = store or job_store.get_job_store()
        if not isinstance(self.store, job_store.PostgresJobStore):
            raise RuntimeError("Fleet archive downloads require PostgreSQL")

    @contextmanager
    def _connection(self):
        # Bound failed renewals well below the lease. SET LOCAL does not alter
        # the science pipelines using other connections in this same pool.
        with self.store._pool.connection(timeout=10) as conn:
            conn.execute("SET LOCAL statement_timeout = '10s'")
            conn.execute("SET LOCAL lock_timeout = '5s'")
            yield conn

    @staticmethod
    def _now(conn) -> float:
        return float(conn.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp())").fetchone()[0])

    @staticmethod
    def _lock(conn):
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK,))

    @staticmethod
    def _read(conn, job_id: str, *, lock=False):
        row = conn.execute(
            "SELECT params FROM jobs WHERE key=%s AND owner=%s" + (" FOR UPDATE" if lock else ""),
            (key(job_id), OWNER),
        ).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _write(conn, job: dict, now: float):
        conn.execute(
            "UPDATE jobs SET params=%s, state=%s, heartbeat_at=%s, instance_id=%s, "
            "attempts=%s, elapsed=%s, returncode=%s, error_desc=%s, run_name=%s "
            "WHERE key=%s AND owner=%s",
            (json.dumps(job), job["state"], now,
             f"{job['host']}:{job['lease_token']}" if job.get("lease_token") else "",
             job.get("download_attempts", 0), int(now - job["started_at"]),
             (0 if job["state"] == "done" else 1) if job["state"] in TERMINAL else None,
             job.get("error") or "", f"{job['frames_done']}/{job['frames_total']} frames",
             key(job["job_id"]), OWNER),
        )

    def submit(self, snapshot: dict, *, request_id, max_user: int, max_jobs: int) -> dict:
        from muscat_db.lco import LcoError

        with self._connection() as conn:
            self._lock(conn)
            rows = conn.execute(
                "SELECT params FROM jobs WHERE owner=%s AND state IN ('pending','running','cancelling')",
                (OWNER,),
            ).fetchall()
            active = [json.loads(r[0]) for r in rows]
            owned = [j for j in active if j["user_name"] == snapshot["user_name"]]
            if request_id and any(j.get("request_id") == request_id for j in owned):
                raise LcoError(f"Request {request_id} is already downloading", status=409)
            if len(owned) >= max_user or len(active) >= max_jobs:
                raise LcoError("Too many active LCO archive downloads", status=429)
            now = self._now(conn)
            job = dict(snapshot, durable=True, stage="download", request_id=request_id,
                       started_at=now, download_attempts=0, ingest_attempts=0,
                       lease_token="", lease_until=0, cancel_requested=False)
            conn.execute(
                "INSERT INTO jobs (key,type,instrument,obsdate,target,state,elapsed,started_at,"
                "run_type,params,run_id,user_name,owner) VALUES (%s,%s,%s,%s,%s,'pending',0,%s,"
                "'archive',%s,%s,%s,%s)",
                (key(job["job_id"]), TYPE, ",".join(job["instruments"]) or "lco",
                 ",".join(job["obsdates"]) or "mixed", ", ".join(job["objects"]) or "LCO archive",
                 now, json.dumps(job), job["job_id"], job["user_name"], OWNER),
            )
        try:
            self.store._signal_work(TYPE)
        except Exception:
            pass  # Polling is authoritative; notification is only a wake-up.
        return job

    def get(self, job_id: str) -> dict | None:
        with self._connection() as conn:
            return self._read(conn, job_id)

    def all(self) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT params FROM jobs WHERE owner=%s ORDER BY started_at DESC", (OWNER,),
            ).fetchall()
            return [json.loads(r[0]) for r in rows]

    def claim(self, stage: str, host: str) -> dict | None:
        with self._connection() as conn:
            self._lock(conn)
            now = self._now(conn)
            rows = conn.execute(
                "SELECT params FROM jobs WHERE owner=%s AND state IN ('pending','running','cancelling') "
                "ORDER BY started_at, key FOR UPDATE", (OWNER,),
            ).fetchall()
            active = [json.loads(r[0]) for r in rows]
            for job in active:
                if job.get("lease_token") and job["lease_until"] <= now:
                    job.update(lease_token="", lease_until=0)
                    if job.get("cancel_requested"):
                        job.update(state="cancelled", phase="cancelled", finished_at=now)
                    elif job[job["stage"] + "_attempts"] >= MAX_ATTEMPTS:
                        job.update(state="error", phase="error", finished_at=now,
                                   error="Archive worker lease expired; retry limit reached")
                    else:
                        job.update(state="pending", phase="retrying")
                    self._write(conn, job, now)
            running = [j for j in active if j.get("lease_token") and j["stage"] == stage]
            limit = MAX_JOBS if stage == "download" else 1
            if len(running) >= limit or any(j.get("host") == host for j in running):
                return None
            for job in active:
                if (job["state"] != "pending" or job["stage"] != stage
                        or job.get("retry_after", 0) > now):
                    continue
                job.update(state="running", phase="downloading" if stage == "download" else "scanning",
                           lease_token=uuid.uuid4().hex, lease_until=now + LEASE_S, host=host)
                job[stage + "_attempts"] += 1
                self._write(conn, job, now)
                return job
        return None

    def update(self, job_id: str, token: str, changes: dict | None = None) -> dict | None:
        """Renew and/or checkpoint only while this attempt still owns its lease."""
        with self._connection() as conn:
            job = self._read(conn, job_id, lock=True)
            now = self._now(conn)
            if (not job or job.get("lease_token") != token or job["lease_until"] <= now
                    or job["state"] not in {"running", "cancelling"}):
                return None
            # Cancellation wins any racing completion/checkpoint.
            if changes and not job.get("cancel_requested"):
                job.update(changes)
            job["lease_until"] = now + LEASE_S
            self._write(conn, job, now)
            return job

    def finish(self, job_id: str, token: str, *, error: str | None = None,
               retryable: bool = False, auto_ingest: bool = False) -> dict | None:
        with self._connection() as conn:
            job = self._read(conn, job_id, lock=True)
            now = self._now(conn)
            if not job or job.get("lease_token") != token or job["lease_until"] <= now:
                return None
            if job.get("cancel_requested"):
                job.update(state="cancelled", phase="cancelled", error=None)
            elif error:
                retry = retryable and job[job["stage"] + "_attempts"] < MAX_ATTEMPTS
                job.update(state="pending" if retry else "error", phase="retrying" if retry else "error",
                           error=error, retry_after=now + 30 * job[job["stage"] + "_attempts"])
            elif job["stage"] == "download" and auto_ingest:
                job.update(state="pending", stage="ingest", phase="awaiting_ingest", error=None)
            else:
                job.update(state="done", phase="done", error=None)
            job.update(lease_token="", lease_until=0)
            if job["state"] in TERMINAL:
                job["finished_at"] = now
            self._write(conn, job, now)
            return job

    def cancel(self, job_id: str) -> dict | None:
        from muscat_db.lco import LcoError

        with self._connection() as conn:
            job = self._read(conn, job_id, lock=True)
            if not job:
                return None
            if job["state"] in TERMINAL:
                raise LcoError(f"LCO archive download job already {job['state']}", status=409)
            now = self._now(conn)
            job["cancel_requested"] = True
            if job["state"] == "pending":
                job.update(state="cancelled", phase="cancelled", finished_at=now)
            else:
                job["state"] = "cancelling"
            self._write(conn, job, now)
            return job

    def delete(self, job_id: str) -> None:
        from muscat_db.lco import LcoError

        with self._connection() as conn:
            job = self._read(conn, job_id, lock=True)
            if job and job["state"] not in TERMINAL:
                raise LcoError(f"LCO archive download job is still {job['state']}", status=409)
            conn.execute("DELETE FROM jobs WHERE key=%s AND owner=%s", (key(job_id), OWNER))

    def take_notification(self, job_id: str) -> dict | None:
        """Claim a best-effort completion hook once, including across web restarts."""
        with self._connection() as conn:
            job = self._read(conn, job_id, lock=True)
            if not job or job["state"] not in {"done", "error"} or job.get("notified"):
                return None
            job["notified"] = True
            self._write(conn, job, self._now(conn))
            return job
