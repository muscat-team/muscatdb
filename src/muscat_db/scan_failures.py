"""Ledger of obslog scans that failed and have not since succeeded (issue #196).

A failed ``scan_date`` used to leave nothing behind but a DEBUG log line that
nothing printed, so a transient error silently dropped an instrument's whole
night -- the likeliest cause of muscat2's 2025 gap in #157. Each failure now
lands here, one JSON object per line, keyed by ``(instrument, obsdate)``:

    {"instrument": "muscat2", "obsdate": "250310", "reason": "OSError: ...",
     "first_failed": "...", "last_failed": "...", "attempts": 2}

and the entry is removed once a later scan of that date writes cleanly. The
file is the set of *open* failures, not a history; the scan logs keep that.

It lives at the obslog root (``$OBSLOG_BASE/.scan-failures.jsonl``), beside the
per-instrument directories, so it survives the nightly ``build-db`` (which
rebuilds ``muscat.db`` from those CSVs) and is visible to every host that
mounts the obslogs. Nothing that walks the obslog tree looks at the root
itself, and the leading dot keeps it out of casual listings.

Two writers can race: the nightly cron and the LCO monitor thread inside the
web server both call ``scan_date``. Every read-modify-write therefore holds an
exclusive ``flock`` on a separate lock file (locking the ledger itself would
not survive the atomic ``os.replace`` of it). A ledger that cannot be written
is logged and otherwise ignored: bookkeeping must never fail a scan.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import logging
import os
from collections.abc import Iterator

logger = logging.getLogger(__name__)

LEDGER_NAME = ".scan-failures.jsonl"
_LOCK_NAME = ".scan-failures.lock"
# Long tracebacks would bloat the ledger; the full one is in the scan log.
_MAX_REASON_CHARS = 500


def ledger_path(base: str) -> str:
    return os.path.join(base, LEDGER_NAME)


@contextlib.contextmanager
def _locked(base: str) -> Iterator[None]:
    with open(os.path.join(base, _LOCK_NAME), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def pending(base: str) -> list[dict]:
    """Open failures, oldest first. Unparseable lines are skipped with a warning."""
    path = ledger_path(base)
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    entries: list[dict] = []
    for lineno, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if not (isinstance(entry, dict) and entry.get("instrument") and entry.get("obsdate")):
                raise ValueError("missing instrument/obsdate")
        except ValueError as exc:
            logger.warning("skipping unreadable %s line %d (%r): %s", path, lineno, line[:80], exc)
            continue
        entries.append(entry)
    return entries


def _rewrite(base: str, entries: list[dict]) -> None:
    path = ledger_path(base)
    if not entries:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
        return
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
    os.replace(tmp, path)


def record(base: str, instrument: str, obsdate: str, reason: str) -> None:
    """Add or refresh the open failure for ``(instrument, obsdate)``."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    reason = reason[:_MAX_REASON_CHARS]
    try:
        with _locked(base):
            entries = pending(base)
            for entry in entries:
                if (entry["instrument"], entry["obsdate"]) == (instrument, obsdate):
                    entry.update(
                        reason=reason, last_failed=now,
                        attempts=int(entry.get("attempts", 1)) + 1,
                    )
                    break
            else:
                entries.append({
                    "instrument": instrument, "obsdate": obsdate, "reason": reason,
                    "first_failed": now, "last_failed": now, "attempts": 1,
                })
            _rewrite(base, entries)
    except OSError as exc:
        logger.warning(
            "could not record scan failure for %s %s in the ledger under %s: %s",
            instrument, obsdate, base, exc,
        )


def clear(base: str, instrument: str, obsdate: str) -> None:
    """Drop the open failure for ``(instrument, obsdate)``, if any."""
    if not os.path.exists(ledger_path(base)):
        return  # the common case: no lock, no I/O beyond one stat
    try:
        with _locked(base):
            entries = pending(base)
            kept = [e for e in entries if (e["instrument"], e["obsdate"]) != (instrument, obsdate)]
            if len(kept) != len(entries):
                _rewrite(base, kept)
    except OSError as exc:
        logger.warning(
            "could not clear scan failure for %s %s from the ledger under %s: %s",
            instrument, obsdate, base, exc,
        )
