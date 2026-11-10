"""Fleet archive execution and control-plane-only catalog registration."""

from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import stat
import threading
import time
import urllib.error
import urllib.parse

from muscat_db import database, job_store, lco, lco_queue, lco_rate

logger = logging.getLogger(__name__)

# The shortest allowance for one frame; large responses get longer (see
# lco._transfer_deadline).
_TRANSFER_DEADLINE_S = max(1.0, float(os.environ.get("MUSCAT_LCO_ARCHIVE_TRANSFER_DEADLINE_S", "120")))
# Progress lives in one row holding every frame's result, so a write per frame
# costs O(n^2). The heartbeat still notices cancellation between saves.
_CHECKPOINT_INTERVAL_S = 10.0
_CLEANUP_INTERVAL_S = 30.0
_COMPACT_INTERVAL_S = 3600.0
_TOKEN_RE = re.compile(r"[0-9a-f]{32}")
# How long a stopping process waits for its archive job to unwind and hand the
# job back. Socket reads are bounded at 10 s and rate-limit waits poll every
# 250 ms, so a transfer normally stops well inside this; one that does not is
# reclaimed when its lease expires, as before. Below systemd's 90 s stop limit.
_SHUTDOWN_GRACE_S = 20.0
# Set once by shutdown(): the running job stops at its next lease check and no
# new job is claimed.
_STOPPING = threading.Event()


def _describe(exc: BaseException) -> str:
    """Name a failure for logs. Network and subprocess text can hold secrets."""
    return exc.message if isinstance(exc, lco.LcoError) else type(exc).__name__


def _spool() -> Path:
    root = Path(os.environ.get("MUSCAT_LCO_DOWNLOAD_SPOOL", "")).expanduser()
    if not root.is_absolute():
        raise lco.LcoError("MUSCAT_LCO_DOWNLOAD_SPOOL must be an absolute shared path", status=503)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise lco.LcoError("Archive credential spool must be owned by the service user with mode 700", status=503)
    return root


def _path(job_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{16}", job_id):
        raise lco.LcoError("LCO archive download job not found", status=404)
    return _spool() / (job_id + ".json")


def config_warnings() -> list[str]:
    """Problems that will block archive access, named without their values."""
    warnings = []
    if importlib.util.find_spec("psycopg") is None:
        warnings.append("psycopg is not installed; archive requests need the cluster extra and will return 503")
    if not os.environ.get("MUSCAT_LCO_RATE_DSN"):
        warnings.append("MUSCAT_LCO_RATE_DSN is unset; archive search, monitoring and downloads will return 503")
    if lco_queue.accepting():
        try:
            _spool()
        except (lco.LcoError, OSError) as exc:
            warnings.append(f"MUSCAT_LCO_DOWNLOAD_SPOOL is unusable ({_describe(exc)}); fleet downloads will fail")
    return warnings


def _read_manifest(job_id: str) -> dict:
    fd = os.open(_path(job_id), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError("Archive credential file must have mode 600")
        return json.load(stream)


def public(job: dict) -> dict:
    """Keep the existing API shape; queue ownership is internal."""
    fields = {"job_id", "state", "frames_total", "frames_done", "results", "phase",
              "funpack_total", "funpack_done", "funpack_results", "processing_results",
              "photometry_url", "instruments", "obsdates", "objects", "dest_dirs",
              "started_at", "finished_at", "error", "user_name", "durable"}
    out = {k: v for k, v in job.items() if k in fields}
    # Let the monitor account for every failed frame, even after a worker crash
    # before its first checkpoint, instead of requeueing forever without using
    # the monitor's per-frame retry budget.
    if job["state"] == "error":
        results = list(out.get("results") or [])
        seen = {r["filename"] for r in results}
        results.extend({"filename": name, "status": "error", "error": job.get("error") or "Download failed"}
                       for name in job.get("filenames", []) if name not in seen)
        out["results"] = results
    return out


def submit(job: dict, *, credential_user: str | None) -> dict:
    if job_store.current_owner() == "worker":
        raise RuntimeError("Only the control plane may submit archive downloads")
    root = lco.download_root()
    if root is None or not root.is_absolute() or not root.is_dir():
        raise lco.LcoError("Archive downloads require an existing absolute shared data root", status=503)
    # Resolve under the caller's existing archive identity on ut2. Workers never
    # read the SQLite user settings or borrow a different user's credentials.
    token = lco._get_lco_api_token(credential_user)
    manifest = {"frames": job["frames"], "token": token, "root": str(root),
                "overwrite": job["overwrite"], "auto_ingest": job["auto_ingest"]}
    path = _path(job["job_id"])
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(manifest, stream)
            stream.flush()
            os.fsync(stream.fileno())
        snapshot = lco._archive_download_snapshot(job)
        snapshot["filenames"] = [str(f.get("filename") or f.get("basename")) for f in job["frames"]]
        result = lco_queue.ArchiveQueue().submit(
            snapshot, request_id=job["request_id"], max_user=lco._ARCHIVE_DOWNLOAD_MAX_PER_USER,
            max_jobs=lco._ARCHIVE_DOWNLOAD_MAX_JOBS,
        )
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return public(result)


class LeaseLost(RuntimeError):
    pass


class Stopping(LeaseLost):
    """This process is shutting down; the job goes back to the queue."""


class Lease:
    """Fail closed on lost ownership, cancellation, or unavailable Postgres.

    Socket reads are bounded; no new operation or rename starts after failure.
    A monotonic deadline also fences a suspended process when it wakes after a
    lease reclaim. Attempt-specific temporary paths isolate abandoned writers.
    """

    def __init__(self, queue, job):
        self.queue, self.job = queue, job
        self.stop = threading.Event()
        self.closed = threading.Event()
        self.ack = time.monotonic()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True, name="archive-lease")

    def __enter__(self):
        self.checkpoint({})
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.closed.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=2)

    def _heartbeat(self):
        while not self.closed.wait(15):
            try:
                self.checkpoint({})
            except Stopping:
                return  # the executor releases the job; the lease is still ours
            except Exception as exc:
                logger.info("archive job %s lease renewal stopped: %s", self.job["job_id"], _describe(exc))
                self.stop.set()
                return

    def check(self):
        if _STOPPING.is_set():
            raise Stopping("Archive execution stopped: this process is shutting down")
        if self.stop.is_set() or time.monotonic() - self.ack >= lco_queue.LEASE_S / 2:
            raise LeaseLost("Archive execution stopped: lease unavailable or cancellation requested")

    def checkpoint(self, changes):
        self.check()
        try:
            row = self.queue.update(self.job["job_id"], self.job["lease_token"], changes)
        except Exception as exc:
            logger.warning("archive job %s could not reach the control plane: %s",
                           self.job["job_id"], _describe(exc))
            self.stop.set()
            raise LeaseLost("Archive execution stopped: control plane unavailable") from None
        if not row or row.get("cancel_requested"):
            self.stop.set()
            raise LeaseLost("Archive execution stopped: lease lost or cancellation requested")
        self.ack = time.monotonic()


def _safe_result(result: dict, root: Path) -> dict:
    out = dict(result)
    # Exceptions can contain signed URLs, response bodies or Authorization
    # headers. Never persist exception text from network/subprocess calls.
    if out.get("error"):
        match = re.search(r"HTTP(?: Error)? (\d{3})", str(out["error"]))
        out["error"] = f"Archive HTTP {match[1]}" if match else "Archive frame processing failed"
    for field in ("src", "dest"):
        if out.get(field):
            out[field] = str(root / Path(out[field]).resolve().relative_to(root.resolve()))
    return out


def _frame(frame: dict, manifest: dict, lease: Lease) -> dict:
    lease.check()
    frame = dict(frame)
    filename = str(frame.get("filename") or frame.get("basename"))
    # Resume skips complete files even if the initial job requested overwrite.
    overwrite = manifest["overwrite"] and lease.job["download_attempts"] == 1
    max_attempts = min(3, max(1, lco._ARCHIVE_DOWNLOAD_FRAME_RETRIES))
    dest = None
    for attempt in range(max_attempts):
        lease.check()
        try:
            # Inside the retry: an NFS blip here must not end a whole dataset.
            _, _, dest = lco.frame_destination(frame)
            if dest.exists() and dest.stat().st_size > 0 and not overwrite:
                return {"filename": filename, "dest": str(dest), "status": "exists"}
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not frame.get("url"):
                # Refresh by exact filename only when needed. This also supports
                # old selections with no archive frame ID, without broad searches.
                basename = re.sub(r"\.(?:fits(?:\.fz)?|fz)$", "", filename, flags=re.IGNORECASE)
                url = "https://archive-api.lco.global/frames/?" + urllib.parse.urlencode({"basename_exact": basename, "limit": 2})
                page = lco._lco_api_request(url, token=manifest["token"], check=lease.check)
                matches = [f for f in page.get("results", []) if (f.get("filename") or f.get("basename")) == filename]
                if len(matches) != 1 or not matches[0].get("url"):
                    raise lco.LcoError("Archive frame URL could not be refreshed", status=404)
                frame["url"] = matches[0]["url"]
            lco._download_to_file(frame["url"], dest, timeout=10, check=lease.check,
                                  temp_suffix="." + lease.job["lease_token"],
                                  deadline_s=_TRANSFER_DEADLINE_S)
            return {"filename": filename, "dest": str(dest), "status": "downloaded"}
        except LeaseLost:
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, urllib.error.HTTPError) else getattr(exc, "status", None)
            transient = code in {401, 403, 429, 500, 502, 503, 504} or isinstance(exc, (OSError, TimeoutError))
            if code in {401, 403}:
                frame.pop("url", None)
            if not transient or attempt + 1 == max_attempts:
                return {"filename": filename, "dest": str(dest) if dest else None, "status": "error",
                        "error": f"Archive HTTP {code}" if code else "Archive transfer failed",
                        "retryable": transient}
            lco_rate.wait(lco_rate.backoff(attempt), lease.check)
    raise AssertionError("frame retry budget must be positive")


def _progress_saver(lease: Lease):
    """Checkpoint at most once per interval, or whenever forced."""
    last = time.monotonic()

    def save(changes: dict, *, force: bool = False) -> None:
        nonlocal last
        now = time.monotonic()
        if force or now - last >= _CHECKPOINT_INTERVAL_S:
            lease.checkpoint(changes)
            last = now
        else:
            lease.check()

    return save


def _discard_partial_files(job: dict) -> None:
    """Remove temporary files earlier attempts of this job left after a crash.

    An abandoned writer that wakes later only fails to publish: its rename finds
    no file, and its lease is already gone. Only this job's tokens are matched,
    so another job transferring into the same night directory is unaffected.
    """
    tokens = [token for token in job.get("attempt_tokens", [])
              if token != job.get("lease_token") and _TOKEN_RE.fullmatch(token)]
    for directory in job.get("dest_dirs") or []:
        for token in tokens:
            for path in Path(directory).glob(f"*.part.{token}"):
                path.unlink(missing_ok=True)


class _Unpacker:
    """Unpack each frame as soon as it is on disk, while later frames transfer.

    Unpacking is local CPU work and sends no archive request, so overlapping
    it with downloads leaves only the last few frames' unpack after the final
    transfer instead of a whole serial pass.
    """

    def __init__(self, lease: Lease, root: Path):
        self.lease, self.root = lease, root
        self.suffix = "." + lease.job["lease_token"]
        self.pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=lco._ARCHIVE_FUNPACK_WORKERS, thread_name_prefix="lco-archive-unpack")
        self.seen: set[str] = set()
        self.running: set[concurrent.futures.Future] = set()
        self.results: list[dict] = []

    def submit(self, results: list[dict]) -> None:
        # _funpack_paths keeps only downloaded/existing .fz frames.
        for path in lco._funpack_paths(results):
            if str(path) not in self.seen:
                self.seen.add(str(path))
                self.running.add(self.pool.submit(lco._funpack_file, path, check=self.lease.check,
                                                  temp_suffix=self.suffix))

    def collect(self, *, block: bool = False) -> None:
        done = (concurrent.futures.wait(self.running, return_when=concurrent.futures.FIRST_COMPLETED).done
                if block
                else {future for future in self.running if future.done()})
        self.running -= done
        # result() re-raises LeaseLost, which ends the job like a download would.
        self.results.extend(_safe_result(future.result(), self.root) for future in done)

    def close(self) -> None:
        self.pool.shutdown(wait=True, cancel_futures=True)


def _download(manifest: dict, lease: Lease):
    root = Path(manifest["root"])
    results = []
    save = _progress_saver(lease)
    unpacker = _Unpacker(lease, root)
    # Submit only a bounded window. A cancellation never leaves thousands of
    # pending network calls in an executor's internal queue.
    frames = iter(manifest["frames"])
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=lco_queue.FRAME_WORKERS) as pool:
            pending = set()
            for frame in frames:
                pending.add(pool.submit(_frame, frame, manifest, lease))
                if len(pending) >= lco_queue.FRAME_WORKERS:
                    break
            while pending:
                done, pending = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
                finished = [_safe_result(future.result(), root) for future in done]
                results.extend(finished)
                unpacker.submit(finished)
                unpacker.collect()
                # funpack_total travels with every save: a job cancelled before
                # its last transfer would otherwise report "n/0" unpacked.
                save({"results": results, "frames_done": len(results),
                      "funpack_results": unpacker.results, "funpack_done": len(unpacker.results),
                      "funpack_total": len(unpacker.seen)})
                for _ in done:
                    frame = next(frames, None)
                    if frame is not None:
                        lease.check()
                        pending.add(pool.submit(_frame, frame, manifest, lease))
        save({"results": results, "frames_done": len(results)}, force=True)
        lease.checkpoint({"phase": "funpacking", "funpack_total": len(unpacker.seen)})
        while unpacker.running:
            unpacker.collect(block=True)
            save({"funpack_results": unpacker.results, "funpack_done": len(unpacker.results)})
    finally:
        unpacker.close()
    unpacked = unpacker.results
    save({"funpack_results": unpacked, "funpack_done": len(unpacked)}, force=True)
    failures = [r for r in results + unpacked if r["status"] == "error"]
    return ("One or more archive frames failed" if failures else None,
            bool(failures) and all(r.get("retryable", False) for r in failures))


def _ingest(manifest: dict, lease: Lease):
    from muscat_db.scanner import scan_date

    if job_store.current_owner() == "worker":
        raise RuntimeError("Workers must never register archive data in the catalog")
    results = []
    datasets = lco._archive_dataset_pairs(manifest["frames"])
    for instrument, obsdate in datasets:
        lease.checkpoint({"phase": "scanning"})
        scanned = scan_date(instrument, obsdate, max_workers=1, data_root=manifest["root"])
        if not scanned or not scanned.get("total"):
            raise RuntimeError("Archive scan found no reduced files")
        lease.checkpoint({"phase": "ingesting"})
        count = database.ingest_date(database.db_path(), instrument, obsdate)
        results.append({"instrument": instrument, "obsdate": obsdate,
                        "scanned_count": int(scanned["total"]), "ingested_count": int(count or 0)})
        lease.checkpoint({"processing_results": results})
    lease.checkpoint({"photometry_url": lco._archive_photometry_url(datasets, manifest["frames"])})


def _execute(queue, job) -> None:
    stage = job["stage"]
    error, retryable, auto_ingest = None, False, False
    try:
        with Lease(queue, job) as lease:
            _discard_partial_files(job)
            manifest = _read_manifest(job["job_id"])
            if str(lco.download_root()) != manifest["root"]:
                raise RuntimeError("Archive data root differs between control plane and worker")
            auto_ingest = manifest["auto_ingest"]
            if stage == "download":
                error, retryable = _download(manifest, lease)
            else:
                _ingest(manifest, lease)
    except Stopping:
        # Hand the job straight back rather than leave its lease to expire.
        # The release is fenced like finish: a lease already lost releases nothing.
        try:
            queue.release(job["job_id"], job["lease_token"])
        except Exception as exc:
            logger.warning("archive job %s could not be released; its lease will expire: %s",
                           job["job_id"], _describe(exc))
        return
    except LeaseLost:
        # finish is fenced too: it acknowledges a cancellation when still owned,
        # but cannot overwrite a newly claimed attempt after a lost lease.
        error, retryable = "Archive execution interrupted", True
    except Exception as exc:
        logger.warning("archive job %s failed during %s: %s", job["job_id"], stage, _describe(exc))
        # Shared storage recovers; a programming or data error will not.
        error = "Archive configuration or processing failed; check shared paths and permissions"
        retryable = stage == "ingest" or isinstance(exc, OSError)
    completed = queue.finish(job["job_id"], job["lease_token"], error=error,
                             retryable=retryable, auto_ingest=auto_ingest)
    if completed and completed["state"] in lco_queue.TERMINAL:
        _path(job["job_id"]).unlink(missing_ok=True)


_EXECUTION_THREADS: dict[str, threading.Thread] = {}
_EXECUTION_LOCK = threading.Lock()
_LAST_CLEANUP = 0.0
_LAST_COMPACT = 0.0


def sync_jobs() -> None:
    """One independent I/O job per worker, or registration on the web host.

    Control-plane registration runs off the reconciliation thread so a scan
    cannot delay other pipelines' heartbeats. Its lease still lives in Postgres.
    """
    global _LAST_CLEANUP
    if not lco_queue.enabled() or _STOPPING.is_set():
        return
    queue = lco_queue.ArchiveQueue()
    stage = "download" if job_store.current_owner() == "worker" else "ingest"
    with _EXECUTION_LOCK:
        # Checked again under the lock shutdown() takes to set the flag: a claim
        # either registers its thread before shutdown looks, or never happens.
        if _STOPPING.is_set():
            return
        thread = _EXECUTION_THREADS.get(stage)
        if thread is not None and thread.is_alive():
            return
        if stage == "ingest" and time.monotonic() - _LAST_CLEANUP >= _CLEANUP_INTERVAL_S:
            # Advanced first: a broken spool must not retry on every 2 s pass,
            # and housekeeping must not stop jobs from being claimed.
            _LAST_CLEANUP = time.monotonic()
            try:
                cleanup()
            except Exception as exc:
                logger.warning("archive download cleanup failed: %s", _describe(exc))
        job = queue.claim(stage, job_store.current_host())
        if job:
            thread = threading.Thread(target=_execute, args=(queue, job),
                                      name=f"archive-{stage}", daemon=True)
            _EXECUTION_THREADS[stage] = thread
            thread.start()


def shutdown(timeout: float = _SHUTDOWN_GRACE_S) -> None:
    """Stop claiming, and hand any running archive job back before exit.

    Execution threads are daemons, so without this a stopped worker (every
    ``workers.py up`` and deploy) took its job down with it and the job sat
    claimed until its lease expired (canary 2026-11-10: 300 s idle). The job
    stops at its next lease check and ``_execute`` releases it.
    """
    with _EXECUTION_LOCK:
        _STOPPING.set()
        threads = [t for t in _EXECUTION_THREADS.values() if t.is_alive()]
    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            logger.warning("archive %s did not stop within %.0f s; its lease will expire",
                           thread.name, timeout)


def cleanup() -> None:
    """Fire completion hooks, compact old rows, and clear finished jobs' files.

    Terminal rows are durable history, so their credentials are no longer
    useful. Never prune active jobs by wall-clock TTL. Work scales with the spool
    and unfinished notifications, not with the length of the job history.
    """
    global _LAST_COMPACT
    from muscat_db import jobs

    queue = lco_queue.ArchiveQueue()
    for job in queue.unnotified():
        if queue.take_notification(job["job_id"]):
            jobs.fire_job_finished(
                job_key=lco_queue.key(job["job_id"]), type_="archive",
                target=", ".join(job["objects"]), inst=", ".join(job["instruments"]),
                date=", ".join(job["obsdates"]), state=job["state"],
                started_at=job["started_at"], owner="" if job["user_name"] == "anonymous" else job["user_name"],
                link=job.get("photometry_url") or "",
            )
    if time.monotonic() - _LAST_COMPACT >= _COMPACT_INTERVAL_S:
        _LAST_COMPACT = time.monotonic()
        queue.compact()
    if os.environ.get("MUSCAT_LCO_DOWNLOAD_SPOOL"):
        _sweep_spool(queue)


def _sweep_spool(queue) -> None:
    manifests = {path.stem: path for path in _spool().glob("*.json")
                 if re.fullmatch(r"[a-f0-9]{16}", path.stem)}
    states = queue.states(list(manifests))
    for job_id, path in manifests.items():
        state = states.get(job_id)
        if state is None:
            # A crash between writing a private manifest and committing the queue
            # row can leave an orphan. Allow a full day for an in-flight submission.
            if path.lstat().st_mtime < time.time() - 86400:
                path.unlink(missing_ok=True)
        elif state in lco_queue.TERMINAL:
            # A surviving manifest means no worker saw the end of this job, so
            # nothing has yet cleaned up after its last attempt.
            _discard_partial_files(queue.get(job_id) or {})
            path.unlink(missing_ok=True)
