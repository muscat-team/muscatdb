"""Backfill of missed and partial scans: the weekly sweep (issue #196, part 2)
and the nightly lookback (:func:`rescan_recent`, issue #500).

The plain nightly scan of yesterday looks at a date once, so on its own a date
that fails or whose data arrives late is never revisited. The sweep closes that
loop across the whole raw archive:

1. Every date ``scan-missing <inst> all`` would pick up, for every
   instrument. Since #163 that includes empty marker directories and CSVs a
   killed scan left behind. It is never forced, so a date whose CSVs are
   complete is not touched.
2. Every open entry in the scan-failure ledger. That covers what step 1
   cannot see: a CCD whose CSV could not be written leaves the date looking
   complete.

Before rescanning a date it checks three things, and holds the date instead
if any of them fails:

* the date has no known cause in ``audit.known_issues()`` (#197, #198);
* no CCD would lose rows: fewer raw matches than existing CSV rows means a
  rescan would overwrite good rows with fewer or none -- the #198 safeguard,
  applied to every date rather than only the listed ones;
* (step 1 only) the raw directory changed since the sweep last rescanned it.
  A date whose headers cannot be read stays "incomplete" however often it is
  rescanned (the production tree has dozens such), so without this every
  weekly run would redo them all for nothing. Adding, removing or renaming
  files changes the directory's mtime, which is exactly when a rescan can
  find something new. Recorded in ``$OBSLOG_BASE/.sweep-state.json``.

It shares the 24-core host with photometry: it does not start while any
photometry/fit job is active, caps its process pool well below the core
count, and a second sweep never runs alongside the first (an exclusive lock
at the obslog root). The cron entry adds ``nice``/``ionice`` on top.
"""

from __future__ import annotations

import csv
import fcntl
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, timedelta

from muscat_db import scan_failures
from muscat_db.audit import known_issues
from muscat_db.instruments import INSTRUMENTS, OBSLOG_BASE
from muscat_db.scanner import _find_fits_files, missing_dates, scan_date

logger = logging.getLogger(__name__)

# muscat2's full backfill ran at ~350 files/s with the whole host. A third of
# the 24 cores keeps a weekly sweep to hours while leaving photometry the rest.
DEFAULT_WORKERS = 8
LOCK_NAME = ".sweep.lock"
STATE_NAME = ".sweep-state.json"
# Sinistro reaches the host about two days late and one delivery spanned ~62 h
# (see scanner._DEFAULT_STALE_CSV_GRACE_S); an unchanged date costs one stat.
LOOKBACK_DAYS = 7
RECENT_STATE_NAME = ".recent-scan-state.json"


@dataclass
class SweepResult:
    scanned: dict[str, list[str]] = field(default_factory=dict)
    retried: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    held: list[tuple[str, str, str]] = field(default_factory=list)
    unchanged: int = 0
    skipped: str | None = None

    @property
    def changed(self) -> bool:
        """True when obslog CSVs were (re)written, i.e. a rebuild has news."""
        return bool(self.scanned or self.retried)


def _active_jobs() -> list[dict]:
    from muscat_db.job_store import get_job_store

    return get_job_store().active()


def run_sweep(max_workers: int = DEFAULT_WORKERS, progress=None) -> SweepResult:
    result = SweepResult()
    try:
        lock = open(os.path.join(OBSLOG_BASE, LOCK_NAME), "a")
    except OSError as exc:
        result.skipped = f"cannot open the sweep lock under {OBSLOG_BASE}: {exc}"
        return result
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result.skipped = "another sweep is already running"
            return result
        try:
            active = _active_jobs()
        except Exception as exc:
            # Unknown load is treated like heavy load: skip rather than compete.
            result.skipped = f"could not check for active jobs: {exc}"
            return result
        if active:
            keys = ", ".join(str(j.get("key", "?")) for j in active[:5])
            result.skipped = f"{len(active)} active job(s) ({keys}); not competing with them"
            return result
        known = known_issues()
        _sweep_missing(result, known, max_workers, progress)
        _retry_failures(result, known, max_workers)
    return result


# -- guards ----------------------------------------------------------------------


def _csv_rows(inst_name: str, obsdate: str, ccd: int) -> int:
    path = f"{OBSLOG_BASE}/{inst_name}/{obsdate}/obslog-{inst_name}-{obsdate}-ccd{ccd}.csv"
    try:
        with open(path, newline="") as f:
            return sum(1 for _ in csv.DictReader(f))
    except FileNotFoundError:
        return 0


def _shrink_reason(inst_name: str, obsdate: str, data_root: str | None = None) -> str | None:
    """Why rescanning would lose rows, or None if it would not.

    *data_root* is the root the rescan will look under; counting raw matches
    anywhere else would hold a date whose files live only in the LCO tree.
    """
    inst = INSTRUMENTS[inst_name]
    for ccd in range(inst.nccd):
        rows = _csv_rows(inst_name, obsdate, ccd)
        if not rows:
            continue
        raw = len(_find_fits_files(inst, obsdate, ccd, data_root=data_root))
        if raw < rows:
            return f"rescan would shrink ccd{ccd} from {rows} CSV rows to {raw} raw matches"
    return None


def _hold_reason(
    inst_name: str, obsdate: str, known: dict[tuple[str, str], str], data_root: str | None = None,
) -> str | None:
    ref = known.get((inst_name, obsdate))
    if ref:
        return ref
    return _shrink_reason(inst_name, obsdate, data_root)


def _raw_signature(inst_name: str, obsdate: str) -> int | None:
    try:
        return os.stat(os.path.join(INSTRUMENTS[inst_name].data_dir, obsdate)).st_mtime_ns
    except OSError:
        return None


def _load_state(name: str = STATE_NAME) -> dict[str, int]:
    try:
        with open(os.path.join(OBSLOG_BASE, name)) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("ignoring unreadable sweep state: %s", exc)
        return {}


def _save_state(state: dict[str, int], name: str = STATE_NAME) -> None:
    path = os.path.join(OBSLOG_BASE, name)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("could not save sweep state %s: %s", path, exc)


# -- the two steps -----------------------------------------------------------------


def _sweep_missing(result: SweepResult, known, max_workers: int, progress) -> None:
    state = _load_state()
    for name in INSTRUMENTS:
        candidates = missing_dates(name, "all")
        task = None
        if progress is not None and candidates:
            task = progress.add_task(f"[cyan]{name} sweep[/]", total=len(candidates), filename="")
        for obsdate in candidates:
            if progress is not None and task is not None:
                progress.update(task, advance=1, filename=obsdate)
            key = f"{name}/{obsdate}"
            signature = _raw_signature(name, obsdate)
            if signature is not None and state.get(key) == signature:
                result.unchanged += 1
                continue
            why = _hold_reason(name, obsdate, known)
            if why:
                result.held.append((name, obsdate, why))
                continue
            try:
                wrote = scan_date(name, obsdate, max_workers=max_workers)
            except Exception:
                # scan_date has recorded it in the ledger; step 2 retries it.
                logger.warning("sweep scan of %s %s failed", name, obsdate, exc_info=True)
                continue
            # The date is looked at and won't be again until its directory
            # changes, whether or not anything was written. Only a truthy
            # result means CSVs were (re)written, so it is scanned-but-empty
            # otherwise, not reported as success or counted as changed.
            if signature is not None:
                state[key] = signature
            if wrote:
                result.scanned.setdefault(name, []).append(obsdate)
        _save_state(state)  # per instrument, so a killed sweep keeps its progress


def _retry_failures(result: SweepResult, known, max_workers: int) -> None:
    for entry in scan_failures.pending(OBSLOG_BASE):
        key = (entry["instrument"], entry["obsdate"])
        if key[0] not in INSTRUMENTS:
            logger.warning("scan-failure ledger names unknown instrument %r; leaving it", key[0])
            continue
        if key[1] in result.scanned.get(key[0], []):
            continue  # step 1 rescanned it; still listed means it failed again
        # A failure recorded under the LCO download root is retried there:
        # on the default root the files are absent, so nothing is found and
        # the entry never closes.
        data_root = entry.get("data_root")
        why = _hold_reason(*key, known, data_root)
        if why:
            result.held.append((*key, why))
            continue
        try:
            ok = scan_date(*key, max_workers=max_workers, data_root=data_root)
        except Exception:
            logger.warning("sweep retry of %s %s failed again", *key, exc_info=True)
            result.failed.append(key)
            continue
        (result.retried if ok else result.failed).append(key)


# -- the nightly lookback ----------------------------------------------------------


def _arrival_signature(inst_name: str, obsdate: str) -> int | None:
    """The raw directory's ctime, which changes when a file is added to it.

    Not its mtime: the transfer preserves that from the telescope. The value is
    only compared with an earlier reading of itself, so no clock is involved.
    """
    try:
        return os.stat(os.path.join(INSTRUMENTS[inst_name].data_dir, obsdate)).st_ctime_ns
    except OSError:
        return None


def rescan_recent(max_workers: int | None = None, today: date | None = None) -> SweepResult:
    """Rescan the dates before yesterday whose raw files may have moved on (#500).

    Covers 2 to ``LOOKBACK_DAYS`` days ago. A date is scanned the first time it
    is seen here, because the scan on the following day may have run during its
    transfer, and again whenever its raw directory changes. The signature is
    read *before* the scan lists any file, so a frame that arrives while the
    scan runs differs from it next time. It is kept only after a scan that
    wrote every CSV, so failures are retried.
    """
    today = today or date.today()
    window = [
        (today - timedelta(days=n)).strftime("%y%m%d") for n in range(2, LOOKBACK_DAYS + 1)
    ]
    previous = _load_state(RECENT_STATE_NAME)
    # Dates that left the window are dropped, so the file does not grow.
    state = {key: sig for key, sig in previous.items() if key.rsplit("/", 1)[-1] in window}
    known = known_issues()
    result = SweepResult()
    for obsdate in window:
        for name in INSTRUMENTS:
            key = f"{name}/{obsdate}"
            signature = _arrival_signature(name, obsdate)
            if signature is None:
                continue
            if state.get(key) == signature:
                result.unchanged += 1
                continue
            try:
                why = _hold_reason(name, obsdate, known)
                if why:
                    result.held.append((name, obsdate, why))
                    continue
                wrote = scan_date(name, obsdate, max_workers=max_workers)
            except Exception:
                logger.warning("rescan of %s %s failed", name, obsdate, exc_info=True)
                result.failed.append((name, obsdate))
                continue
            if wrote.get("write_errors"):
                result.failed.append((name, obsdate))
            elif wrote:
                result.scanned.setdefault(name, []).append(obsdate)
                state = {**state, key: signature}
                _save_state(state, RECENT_STATE_NAME)  # a killed run keeps its progress
    return result
