"""Download a proposal's LCO archive datasets and register them in muscat-db.

The cron-friendly counterpart of the interactive archive download (``lco.py``)
and the per-request monitor (``lco_monitor.py``). Those only follow requests
submitted through the UI; this asks the archive for every BANZAI final product
(RLEVEL 91, OBSTYPE EXPOSE) of a proposal, so observations scheduled any other
way still reach the database.

The unit of work is a *dataset*: one instrument night, i.e. the
``<Instrument>/<DAY-OBS>`` directory the scanner and ``ingest_date`` operate
on. A dataset is selected when any of its frames falls in the requested
DATE_OBS window, and is then synced whole, even where the window boundary
cuts through the night. Datasets are completed one at a time (download,
funpack, scan, ingest) so an interrupted run leaves at most one partial night.

Every run re-derives each dataset's state from the archive listing and the
files on disk -- nothing else is persisted -- so recovering from a killed run
is just running again: missing frames are fetched, a ``.fz`` whose funpack
never finished is unpacked, and a night whose obslog predates its newest frame
is rescanned.
"""

from __future__ import annotations

import concurrent.futures
import datetime
import fcntl
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterator

from muscat_db import instruments, lco

DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_WORKERS = 4
# Concurrent archive downloads per dataset. The bytes come from S3, but the
# presigned URLs come from the shared archive API, so the ceiling stays modest
# rather than tracking the host's core count.
MAX_WORKERS = 16
_ARCHIVE_PAGE_SIZE = "1000"
_LOCK_NAME = ".lco-sync.lock"
_PROGRESS_EVERY = 100
# A night's frames carry DATE_OBS between ~9 h and ~36 h after 00:00 UTC of
# its DAY-OBS (measured across KEY2026B-001's sites, Aug-Oct 2026). Padding
# the archive query by two days on each side therefore returns every frame of
# any night that overlaps the requested window.
_DATASET_PAD = datetime.timedelta(days=2)
# One archive query per day keeps each under the pager's 10,000-frame safety
# cap; a single busy 0.4m night has reached ~4,900 frames.
_QUERY_CHUNK = datetime.timedelta(days=1)
# A ``.part`` left by a killed run is swept once it is clearly not an active
# download (the web UI may be writing into the same night directory).
_STALE_PART_S = 3600.0
# LCO proposal IDs look like KEY2026B-001 or LCO2026A-012. Anything outside
# this shape is a typo, and is refused before it is sent to the archive.
_PROPOSAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")
_FMT = "%Y-%m-%d %H:%M"

Log = Callable[[str], None]


class SyncError(Exception):
    """A sync could not start: bad arguments, no download root, or a held lock."""


@dataclass(frozen=True)
class PlannedFrame:
    frame: dict
    dest: Path

    @property
    def filename(self) -> str:
        return self.dest.name

    @property
    def local_path(self) -> Path:
        """The file the scanner reads: the unpacked FITS for an fpacked frame."""
        return lco._funpack_dest(self.dest) or self.dest

    @property
    def is_local(self) -> bool:
        """True once unpacked. A ``.fz`` alone (funpack never finished) is not:
        it is queued again, the download step reports it as ``exists`` without
        refetching, and only the funpack is retried."""
        return self.local_path.exists()


@dataclass(frozen=True)
class Dataset:
    instrument: str
    obsdate: str
    frames: tuple[PlannedFrame, ...]

    @property
    def label(self) -> str:
        return f"{self.instrument} {self.obsdate}"

    @property
    def missing(self) -> tuple[PlannedFrame, ...]:
        return tuple(p for p in self.frames if not p.is_local)

    @property
    def directory(self) -> Path:
        return self.frames[0].dest.parent


@dataclass(frozen=True)
class DatasetResult:
    instrument: str
    obsdate: str
    total: int
    missing_before: int
    downloaded: int = 0
    failures: tuple[str, ...] = ()
    scanned: int | None = None
    ingested: int | None = None
    error: str = ""
    deferred: bool = False

    @property
    def complete(self) -> bool:
        return not self.deferred and self.missing_before == self.downloaded

    @property
    def ok(self) -> bool:
        return not (self.failures or self.error)


@dataclass(frozen=True)
class SyncReport:
    proposal_id: str
    start: str
    end: str
    archive_frames: int
    engineering: int
    unplaceable: tuple[str, ...]
    datasets: tuple[DatasetResult, ...]

    @property
    def ok(self) -> bool:
        return not self.unplaceable and all(d.ok for d in self.datasets)

    @property
    def downloaded(self) -> int:
        return sum(d.downloaded for d in self.datasets)

    @property
    def failed(self) -> int:
        return sum(len(d.failures) for d in self.datasets)

    @property
    def deferred(self) -> int:
        return sum(1 for d in self.datasets if d.deferred)


# --- arguments --------------------------------------------------------------

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
    """Return the UTC ``(start, end)`` DATE_OBS window that selects datasets.

    An explicit ``start`` overrides the lookback, for one-off backfills; ``end``
    defaults to now.
    """
    if days < 1:
        raise SyncError("--days must be at least 1")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    end_dt = _parse_utc(end, "end") if end else now
    start_dt = _parse_utc(start, "start") if start else end_dt - datetime.timedelta(days=days)
    if start_dt >= end_dt:
        raise SyncError(f"empty window: start {start_dt:{_FMT}} is not before end {end_dt:{_FMT}}")
    return start_dt.strftime(_FMT), end_dt.strftime(_FMT)


# --- archive query ----------------------------------------------------------

def _query_chunk(proposal_id: str, start: str, end: str, user_name: str | None) -> list[dict]:
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
            f"{proposal_id} between {start} and {end}; refusing a partial listing"
        )
    return [f for f in page.get("results") or [] if isinstance(f, dict)]


def query_frames(
    proposal_id: str, start: str, end: str, user_name: str | None = None, log: Log = print
) -> list[dict]:
    """Every science frame of every dataset overlapping ``[start, end)``.

    Queries the window padded by :data:`_DATASET_PAD` so nights cut by the
    boundary come back whole, one day at a time so no single listing nears the
    pager's cap. Frames are de-duplicated by archive id across chunks.
    """
    lo = _parse_utc(start, "start") - _DATASET_PAD
    hi = _parse_utc(end, "end") + _DATASET_PAD
    log(f"{proposal_id}: querying the LCO archive for {lo:{_FMT}} .. {hi:{_FMT}} UTC "
        f"(window +/- {_DATASET_PAD.days} days, to keep nights whole) ...")
    frames: dict[object, dict] = {}
    chunk_start = lo
    while chunk_start < hi:
        chunk_end = min(chunk_start + _QUERY_CHUNK, hi)
        for frame in _query_chunk(proposal_id, f"{chunk_start:{_FMT}}", f"{chunk_end:{_FMT}}", user_name):
            frames.setdefault(frame.get("id") or frame.get("filename"), frame)
        chunk_start = chunk_end
    return list(frames.values())


def _frame_time(frame: dict) -> datetime.datetime | None:
    raw = str(frame.get("DATE_OBS") or frame.get("observation_date") or "")
    if not raw:
        return None
    try:
        return _parse_utc(raw, "DATE_OBS")
    except SyncError:
        return None


def group_datasets(
    frames: list[dict], start: str, end: str
) -> tuple[tuple[Dataset, ...], int, tuple[str, ...]]:
    """Group frames into datasets that overlap ``[start, end)``.

    Returns ``(datasets oldest first, engineering frames skipped, unplaceable
    frame messages)``. A dataset is kept whole when *any* of its frames is in
    the window; one seen only through the query padding is dropped.
    """
    lo, hi = _parse_utc(start, "start"), _parse_utc(end, "end")
    by_night: dict[tuple[str, str], dict[Path, PlannedFrame]] = {}
    in_window: set[tuple[str, str]] = set()
    engineering = 0
    unplaceable: list[str] = []
    for frame in frames:
        if lco.is_engineering_object(str(frame.get("OBJECT") or "")):
            engineering += 1
            continue
        try:
            instrument, obsdate, dest = lco.frame_destination(frame)
        except lco.LcoError as exc:
            name = str(frame.get("filename") or frame.get("basename") or "?")
            unplaceable.append(f"{name}: {exc.message}" + (f" ({exc.detail})" if exc.detail else ""))
            continue
        key = (instrument, obsdate)
        by_night.setdefault(key, {}).setdefault(dest, PlannedFrame(frame, dest))
        when = _frame_time(frame)
        if when is not None and lo <= when < hi:
            in_window.add(key)
    datasets = tuple(
        Dataset(inst, date, tuple(sorted(by_night[(inst, date)].values(), key=lambda p: p.filename)))
        for inst, date in sorted(in_window, key=lambda k: (k[1], k[0]))
    )
    return datasets, engineering, tuple(unplaceable)


# --- per-dataset work -------------------------------------------------------

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
    frames: tuple[PlannedFrame, ...], workers: int, label: str, log: Log
) -> tuple[int, tuple[str, ...]]:
    """Fetch *frames* concurrently; return ``(succeeded, error messages)``.

    On an interrupt (Ctrl-C) the queued frames are cancelled instead of being
    drained, so the process stops after the in-flight downloads, each of which
    is atomic.
    """
    if not frames:
        return 0, ()
    succeeded = 0
    errors: list[str] = []
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=min(workers, len(frames)), thread_name_prefix="lco-sync"
    )
    try:
        futures = {pool.submit(_fetch_one, planned): planned for planned in frames}
        for done, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            try:
                error = future.result()
            except Exception as exc:  # one bad frame must not abort the night
                error = str(exc) or type(exc).__name__
            if error:
                errors.append(f"{futures[future].filename}: {error}")
            else:
                succeeded += 1
            if done % _PROGRESS_EVERY == 0 or done == len(frames):
                log(f"  {label}: {done}/{len(frames)} fetched ({len(errors)} failed)")
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return succeeded, tuple(errors)


def sweep_stale_parts(directory: Path, now: float | None = None) -> int:
    """Remove ``.part`` files a killed run left behind; return how many."""
    now = time.time() if now is None else now
    removed = 0
    for part in directory.glob("*.part"):
        try:
            if now - part.stat().st_mtime >= _STALE_PART_S:
                part.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    return removed


def _obslog_mtime(instrument: str, obsdate: str) -> float | None:
    """Newest obslog CSV mtime for the night, or ``None`` if it has none."""
    logdir = Path(instruments.OBSLOG_BASE) / instrument / obsdate
    mtimes = [p.stat().st_mtime for p in logdir.glob(f"obslog-{instrument}-{obsdate}-ccd*.csv")]
    return max(mtimes, default=None)


def needs_scan(dataset: Dataset) -> bool:
    """True when the night's obslog is missing or older than a local frame.

    This is how a run that died between download and scan is noticed: its
    frames are all local by now, so only the obslog's age reveals the gap.
    """
    local = [p.local_path.stat().st_mtime for p in dataset.frames if p.is_local]
    if not local:
        return False
    log_mtime = _obslog_mtime(dataset.instrument, dataset.obsdate)
    return log_mtime is None or log_mtime < max(local)


def scan_and_ingest(
    dataset: Dataset, data_root: Path, db: str | None
) -> tuple[int, int | None]:
    """Rescan the night's obslog CSVs and, when *db* is given, ingest them."""
    from muscat_db.database import ingest_date
    from muscat_db.scanner import scan_date

    scan = scan_date(dataset.instrument, dataset.obsdate, max_workers=1, data_root=str(data_root))
    scanned = int((scan or {}).get("total") or 0)
    if not scanned:
        raise RuntimeError("scan found no reduced FITS files")
    ingested = int(ingest_date(db, dataset.instrument, dataset.obsdate) or 0) if db else None
    return scanned, ingested


def sync_dataset(
    dataset: Dataset, *, data_root: Path, db: str | None, workers: int, log: Log
) -> DatasetResult:
    """Make one night complete on disk, then rescan (and ingest) it."""
    missing = dataset.missing
    base = DatasetResult(dataset.instrument, dataset.obsdate, len(dataset.frames), len(missing))
    if missing:
        swept = sweep_stale_parts(dataset.directory)
        if swept:
            log(f"  {dataset.label}: removed {swept} stale .part file(s) from an interrupted run")
    downloaded, failures = fetch_frames(missing, workers, dataset.label, log)
    for failure in failures[:10]:
        log(f"  {dataset.label}: failed {failure}")
    result = replace(base, downloaded=downloaded, failures=failures)
    if not downloaded and not needs_scan(dataset):
        return result
    try:
        scanned, ingested = scan_and_ingest(dataset, data_root, db)
    except Exception as exc:
        log(f"  {dataset.label}: scan/ingest FAILED ({exc})")
        return replace(result, error=str(exc) or type(exc).__name__)
    suffix = f", {ingested} ingested" if ingested is not None else ""
    log(f"  {dataset.label}: {scanned} frames scanned{suffix}")
    return replace(result, scanned=scanned, ingested=ingested)


# --- orchestration ----------------------------------------------------------

@contextmanager
def sync_lock(root: Path) -> Iterator[None]:
    """Hold an exclusive, non-blocking lock so overlapping cron runs bail out.

    The kernel drops a flock when its holder dies, so a killed run never
    leaves the next one locked out.
    """
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


def _describe(dataset: Dataset) -> str:
    n_missing = len(dataset.missing)
    state = "complete" if not n_missing else f"{n_missing} missing"
    return f"  {dataset.label}: {len(dataset.frames)} frames, {state}"


def sync_proposal(
    proposal_id: str,
    *,
    start: str,
    end: str,
    data_root: Path,
    db: str | None,
    user_name: str | None = None,
    workers: int = DEFAULT_WORKERS,
    max_nights: int = 0,
    dry_run: bool = False,
    log: Log = print,
) -> SyncReport:
    """Sync every dataset of *proposal_id* that overlaps ``[start, end)``.

    ``db=None`` stops after the obslog scan, leaving ingestion to ``build-db``.
    ``max_nights`` (0 = no cap) bounds how many incomplete nights one run
    downloads, oldest first; the rest are reported as deferred and picked up
    by the next run.
    """
    proposal_id = validate_proposal_id(proposal_id)
    frames = query_frames(proposal_id, start, end, user_name=user_name, log=log)
    datasets, engineering, unplaceable = group_datasets(frames, start, end)
    incomplete = [d for d in datasets if d.missing]
    log(
        f"{proposal_id} [{start} .. {end} UTC]: {len(datasets)} nights "
        f"({sum(len(d.frames) for d in datasets)} frames), {len(incomplete)} incomplete; "
        f"{engineering} engineering frames skipped, {len(unplaceable)} unplaceable"
    )
    for message in unplaceable[:10]:
        log(f"  unplaceable: {message}")
    for dataset in datasets:
        log(_describe(dataset))

    allowed = {d.label for d in (incomplete[:max_nights] if max_nights > 0 else incomplete)}
    results: list[DatasetResult] = []
    for dataset in datasets:
        if dataset.missing and dataset.label not in allowed:
            results.append(DatasetResult(
                dataset.instrument, dataset.obsdate, len(dataset.frames),
                len(dataset.missing), deferred=True,
            ))
            continue
        if dry_run:
            results.append(DatasetResult(
                dataset.instrument, dataset.obsdate, len(dataset.frames), len(dataset.missing),
            ))
            continue
        results.append(sync_dataset(dataset, data_root=data_root, db=db, workers=workers, log=log))
    deferred = [r for r in results if r.deferred]
    if deferred:
        log(f"  {len(deferred)} incomplete night(s) deferred to the next run (--max-nights {max_nights})")
    return SyncReport(
        proposal_id, start, end, len(frames), engineering, unplaceable, tuple(results)
    )
