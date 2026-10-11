"""Periodic backfill sweep across the whole raw archive (issue #196, part 2).

The nightly ``scan-yesterday`` looks at each date once, so a date that fails
or whose data arrives late is never revisited. The sweep closes that loop:

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


def _shrink_reason(inst_name: str, obsdate: str) -> str | None:
    """Why rescanning would lose rows, or None if it would not."""
    inst = INSTRUMENTS[inst_name]
    for ccd in range(inst.nccd):
        rows = _csv_rows(inst_name, obsdate, ccd)
        if not rows:
            continue
        raw = len(_find_fits_files(inst, obsdate, ccd))
        if raw < rows:
            return f"rescan would shrink ccd{ccd} from {rows} CSV rows to {raw} raw matches"
    return None


def _hold_reason(inst_name: str, obsdate: str, known: dict[tuple[str, str], str]) -> str | None:
    ref = known.get((inst_name, obsdate))
    if ref:
        return ref
    return _shrink_reason(inst_name, obsdate)


def _raw_signature(inst_name: str, obsdate: str) -> int | None:
    try:
        return os.stat(os.path.join(INSTRUMENTS[inst_name].data_dir, obsdate)).st_mtime_ns
    except OSError:
        return None


def _load_state() -> dict[str, int]:
    try:
        with open(os.path.join(OBSLOG_BASE, STATE_NAME)) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("ignoring unreadable sweep state: %s", exc)
        return {}


def _save_state(state: dict[str, int]) -> None:
    path = os.path.join(OBSLOG_BASE, STATE_NAME)
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
        why = _hold_reason(*key, known)
        if why:
            result.held.append((*key, why))
            continue
        try:
            ok = scan_date(*key, max_workers=max_workers)
        except Exception:
            logger.warning("sweep retry of %s %s failed again", *key, exc_info=True)
            result.failed.append(key)
            continue
        (result.retried if ok else result.failed).append(key)
