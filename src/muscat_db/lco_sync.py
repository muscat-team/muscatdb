"""Download a proposal's new LCO archive frames and register them in muscat-db.

The cron-friendly counterpart of the interactive archive download (``lco.py``)
and the per-request monitor (``lco_monitor.py``). Those only follow requests
submitted through the UI; this asks the archive for every BANZAI final product
(RLEVEL 91, OBSTYPE EXPOSE) a proposal accumulated in a recent DATE_OBS window,
so observations scheduled any other way still reach the database.

Each run fetches only frames not yet unpacked on disk, funpacks
them, and rescans every night that gained a frame, so the obslog CSVs are
current before ``build-db`` runs. Re-running is idempotent: an overlapping
lookback window costs metadata queries, not downloads, and that overlap is what
picks up reductions BANZAI publishes a day or more after the night.
"""

from __future__ import annotations

import concurrent.futures
import datetime
import fcntl
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from muscat_db import instruments, lco

DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_WORKERS = 4
# Concurrent archive downloads per run. The archive serves the bytes from S3,
# but the frame metadata and presigned URLs come from the shared archive API,
# so the ceiling stays modest rather than tracking the host's core count.
MAX_WORKERS = 16
_ARCHIVE_PAGE_SIZE = "1000"
_LOCK_NAME = ".lco-sync.lock"
_PROGRESS_EVERY = 50
# LCO proposal IDs look like KEY2026B-001 or LCO2026A-012. Anything outside
# this shape is a typo, and is refused before it is sent to the archive.
_PROPOSAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")

Log = Callable[[str], None]


class SyncError(Exception):
    """A sync could not start: bad arguments, no download root, or a held lock."""


@dataclass(frozen=True)
class PlannedFrame:
    frame: dict
    instrument: str
    obsdate: str
    dest: Path

    @property
    def filename(self) -> str:
        return self.dest.name


@dataclass(frozen=True)
class FramePlan:
    to_download: tuple[PlannedFrame, ...]
    present: tuple[PlannedFrame, ...]
    engineering: int
    errors: tuple[str, ...]


@dataclass(frozen=True)
class DatasetResult:
    instrument: str
    obsdate: str
    scanned: int
    ingested: int | None
    error: str = ""


@dataclass(frozen=True)
class SyncReport:
    proposal_id: str
    start: str
    end: str
    archive_count: int
    plan: FramePlan
    downloaded: tuple[str, ...]
    download_errors: tuple[str, ...]
    datasets: tuple[DatasetResult, ...]
    deferred: int

    @property
    def ok(self) -> bool:
        return not (
            self.plan.errors
            or self.download_errors
            or any(d.error for d in self.datasets)
        )


def validate_proposal_id(proposal_id: str) -> str:
    value = (proposal_id or "").strip()
    if not _PROPOSAL_RE.match(value):
        raise SyncError(f"not a valid LCO proposal ID: {proposal_id!r}")
    return value


def _parse_utc(value: str, name: str) -> datetime.datetime:
    try:
        parsed = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise SyncError(f"--{name} must be an ISO date or datetime, got {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def archive_window(
    days: int,
    start: str = "",
    end: str = "",
    now: datetime.datetime | None = None,
) -> tuple[str, str]:
    """Return the UTC ``(start, end)`` DATE_OBS window to query.

    An explicit ``start`` overrides the lookback, for one-off backfills; ``end``
    defaults to now.
    """
    if days < 1:
        raise SyncError("--days must be at least 1")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    end_dt = _parse_utc(end, "end") if end else now
    start_dt = _parse_utc(start, "start") if start else end_dt - datetime.timedelta(days=days)
    if start_dt >= end_dt:
        raise SyncError(f"empty window: start {start_dt:%Y-%m-%d %H:%M} is not before end {end_dt:%Y-%m-%d %H:%M}")
    fmt = "%Y-%m-%d %H:%M"
    return start_dt.strftime(fmt), end_dt.strftime(fmt)


def query_frames(
    proposal_id: str, start: str, end: str, user_name: str | None = None
) -> tuple[int, list[dict]]:
    """Return ``(archive count, frames)`` for the proposal's science products.

    Same server-side filters as the archive page's target search
    (``OBSTYPE=EXPOSE`` at RLEVEL 91). A truncated listing is refused rather
    than partially synced, so the caller narrows the window instead of silently
    missing frames.
    """
    filters = {
        "proposal_id": proposal_id,
        "reduction_level": 91,
        "OBSTYPE": "EXPOSE",
        "start": start,
        "end": end,
        "limit": _ARCHIVE_PAGE_SIZE,
    }
    page = lco.archive_search_all(filters, user_name=user_name)
    if page.get("truncated"):
        raise SyncError(
            f"archive returned more than {lco._ARCHIVE_MAX_FRAMES} frames for "
            f"{proposal_id} between {start} and {end}; narrow the window with --start/--end"
        )
    frames = [f for f in page.get("results") or [] if isinstance(f, dict)]
    return int(page.get("count") or len(frames)), frames


def _is_local(dest: Path) -> bool:
    """True once the frame is usable by the scanner, i.e. unpacked.

    A ``.fz`` without its ``.fits`` (a funpack that failed last run) is not
    local: it is queued again, the download step reports it as ``exists``
    without refetching, and only the funpack is retried.
    """
    unpacked = lco._funpack_dest(dest)
    return (unpacked or dest).exists()


def plan_frames(frames: list[dict]) -> FramePlan:
    """Split archive frames into those to fetch and those already on disk."""
    to_download: list[PlannedFrame] = []
    present: list[PlannedFrame] = []
    errors: list[str] = []
    engineering = 0
    seen: set[Path] = set()
    for frame in frames:
        if lco.is_engineering_object(str(frame.get("OBJECT") or "")):
            engineering += 1
            continue
        name = str(frame.get("filename") or frame.get("basename") or "?")
        try:
            instrument, obsdate, dest = lco.frame_destination(frame)
        except lco.LcoError as exc:
            errors.append(f"{name}: {exc.message}" + (f" ({exc.detail})" if exc.detail else ""))
            continue
        if dest in seen:
            continue
        seen.add(dest)
        planned = PlannedFrame(frame, instrument, obsdate, dest)
        (present if _is_local(dest) else to_download).append(planned)
    to_download.sort(key=lambda p: (p.obsdate, p.instrument, p.filename))
    return FramePlan(tuple(to_download), tuple(present), engineering, tuple(errors))


def _fetch_one(planned: PlannedFrame) -> str:
    """Download and funpack one frame; return an error message, or ``""``."""
    result = lco._download_frame_with_retry(planned.frame)
    if result.get("status") not in {"downloaded", "exists"}:
        return str(result.get("error") or "download failed")
    unpacked = lco._funpack_file(planned.dest)
    if unpacked.get("status") not in {"unpacked", "exists", "skipped"}:
        return "funpack: " + str(unpacked.get("error") or "failed")
    return ""


def fetch_frames(
    frames: tuple[PlannedFrame, ...], workers: int, log: Log
) -> tuple[tuple[PlannedFrame, ...], tuple[str, ...]]:
    """Fetch *frames* concurrently; return ``(succeeded, error messages)``."""
    if not frames:
        return (), ()
    succeeded: list[PlannedFrame] = []
    errors: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(workers, len(frames)), thread_name_prefix="lco-sync"
    ) as pool:
        futures = {pool.submit(_fetch_one, planned): planned for planned in frames}
        for done, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            planned = futures[future]
            try:
                error = future.result()
            except Exception as exc:  # one bad frame must not abort the batch
                error = str(exc) or type(exc).__name__
            if error:
                errors.append(f"{planned.filename}: {error}")
            else:
                succeeded.append(planned)
            if done % _PROGRESS_EVERY == 0 or done == len(frames):
                log(f"  {done}/{len(frames)} frames processed ({len(errors)} failed)")
    return tuple(succeeded), tuple(errors)


def _obslog_mtime(instrument: str, obsdate: str) -> float | None:
    """Newest obslog CSV mtime for the night, or ``None`` if it has none."""
    logdir = Path(instruments.OBSLOG_BASE) / instrument / obsdate
    mtimes = [p.stat().st_mtime for p in logdir.glob(f"obslog-{instrument}-{obsdate}-ccd*.csv")]
    return max(mtimes, default=None)


def _local_mtime(planned: PlannedFrame) -> float:
    return (lco._funpack_dest(planned.dest) or planned.dest).stat().st_mtime


def datasets_to_scan(
    fetched: tuple[PlannedFrame, ...], present: tuple[PlannedFrame, ...]
) -> list[tuple[str, str]]:
    """Nights that gained a frame, plus on-disk nights whose obslog is stale.

    The second set recovers a run that downloaded frames but died, or failed,
    before scanning them: those frames read as present next time, so only a
    missing obslog, or one older than a frame it should list, reveals the gap.
    """
    pairs = {(p.instrument, p.obsdate) for p in fetched}
    newest: dict[tuple[str, str], float] = {}
    for p in present:
        key = (p.instrument, p.obsdate)
        newest[key] = max(newest.get(key, 0.0), _local_mtime(p))
    for (instrument, obsdate), frame_mtime in newest.items():
        log_mtime = _obslog_mtime(instrument, obsdate)
        if log_mtime is None or log_mtime < frame_mtime:
            pairs.add((instrument, obsdate))
    return sorted(pairs, key=lambda pair: (pair[1], pair[0]))


def scan_and_ingest(
    datasets: list[tuple[str, str]],
    data_root: Path,
    db: str | None,
    log: Log,
) -> tuple[DatasetResult, ...]:
    """Rescan each night's obslog CSVs and, when *db* is given, ingest them."""
    from muscat_db.database import ingest_date
    from muscat_db.scanner import scan_date

    results: list[DatasetResult] = []
    for instrument, obsdate in datasets:
        try:
            scan = scan_date(instrument, obsdate, max_workers=1, data_root=str(data_root))
            scanned = int((scan or {}).get("total") or 0)
            if not scanned:
                raise RuntimeError("scan found no reduced FITS files")
            ingested = int(ingest_date(db, instrument, obsdate) or 0) if db else None
        except Exception as exc:
            results.append(DatasetResult(instrument, obsdate, 0, None, str(exc) or type(exc).__name__))
            log(f"  {instrument} {obsdate}: FAILED ({exc})")
            continue
        results.append(DatasetResult(instrument, obsdate, scanned, ingested))
        suffix = f", {ingested} ingested" if ingested is not None else ""
        log(f"  {instrument} {obsdate}: {scanned} frames scanned{suffix}")
    return tuple(results)


@contextmanager
def sync_lock(root: Path) -> Iterator[None]:
    """Hold an exclusive, non-blocking lock so overlapping cron runs bail out."""
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / _LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o664)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyncError(f"another lco-sync run holds {root / _LOCK_NAME}") from exc
        yield
    finally:
        os.close(fd)


def require_download_root() -> Path:
    root = lco.download_root()
    if root is None:
        raise SyncError("MUSCAT_LCO_DIR or MUSCAT_DATA_DIR must be set")
    return root


def sync_proposal(
    proposal_id: str,
    *,
    start: str,
    end: str,
    data_root: Path,
    db: str | None,
    user_name: str | None = None,
    workers: int = DEFAULT_WORKERS,
    max_frames: int = 0,
    dry_run: bool = False,
    log: Log = print,
) -> SyncReport:
    """Bring one proposal's frames in ``[start, end)`` onto disk and into the DB.

    ``db=None`` stops after the obslog scan, leaving ingestion to ``build-db``.
    ``max_frames`` (0 = no cap) bounds one run's downloads; the remainder is
    picked up by the next run, oldest nights first.
    """
    proposal_id = validate_proposal_id(proposal_id)
    # Large windows paginate for a minute or more with nothing else to show.
    log(f"{proposal_id}: querying the LCO archive for {start} .. {end} UTC ...")
    count, frames = query_frames(proposal_id, start, end, user_name=user_name)
    plan = plan_frames(frames)
    log(
        f"{proposal_id} [{start} .. {end} UTC]: {count} archive frames, "
        f"{len(plan.present)} already local, {len(plan.to_download)} to download, "
        f"{plan.engineering} engineering skipped, {len(plan.errors)} unplaceable"
    )
    for error in plan.errors[:10]:
        log(f"  unplaceable: {error}")

    batch = plan.to_download[:max_frames] if max_frames > 0 else plan.to_download
    deferred = len(plan.to_download) - len(batch)
    if dry_run:
        for (instrument, obsdate), n in _count_by_night(batch).items():
            log(f"  would download {n} frames for {instrument} {obsdate}")
        return SyncReport(proposal_id, start, end, count, plan, (), (), (), deferred)

    fetched, download_errors = fetch_frames(batch, workers, log)
    for error in download_errors[:10]:
        log(f"  failed: {error}")
    if deferred:
        log(f"  {deferred} frames deferred to the next run (--max-frames {max_frames})")
    datasets = scan_and_ingest(datasets_to_scan(fetched, plan.present), data_root, db, log)
    return SyncReport(
        proposal_id,
        start,
        end,
        count,
        plan,
        tuple(p.filename for p in fetched),
        download_errors,
        datasets,
        deferred,
    )


def _count_by_night(frames: tuple[PlannedFrame, ...]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for planned in frames:
        key = (planned.instrument, planned.obsdate)
        counts[key] = counts.get(key, 0) + 1
    return counts
