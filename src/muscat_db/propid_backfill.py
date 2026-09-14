"""Backfill PROPID for historical frames (issue #144 PR3).

PR1 (#169) added ``PROPID`` capture to ``instruments.py``'s ``keys`` list for
the five LCO-network instruments (muscat3, muscat4, sinistro, sbig, qhy600),
but only for frames scanned *after* that change shipped. Every frame ingested
before then carries ``proposal_id = ''``, which is publicly visible under the
opt-in restriction rule (#144, PR4+) -- restricting a proposal is a no-op on
its history until this backfill has run.

Design notes
------------
* **Per-date, incremental, never the full rebuild.** Each date is handled by
  re-running :func:`muscat_db.scanner.scan_date` (rewrites that date's obslog
  CSV, now including the ``PROPID`` column) followed by
  :func:`muscat_db.database.ingest_date` (re-ingests just that
  instrument/date's rows). Neither touches ``build_db``'s full
  drop-and-rebuild path, which has been implicated in two real
  ``muscat.db`` corruption incidents during unattended heavy writes (#157) --
  a per-date WAL-mode update touches a small, bounded slice of the database
  instead of dropping and rebuilding ``frames``/``summaries``/``targets``
  wholesale.
* **Checkpointed outside the database.** Progress is a small JSON file under
  ``$MUSCAT_TMPDIR`` (see :func:`_checkpoint_path`), not a ``db_meta`` row:
  ``db_meta`` is not one of ``build_db``'s ``_APP_OWNED_TABLES``, so any key
  stored there is silently wiped by the next nightly ``build-db`` cron run --
  and a backfill of the heaviest instrument (~940k muscat3 files) is expected
  to span many such nights.
* **A date is "done" once ingested, regardless of what PROPID turned out to
  be.** A frame with a genuinely blank ``PROPID`` header stays blank forever;
  that is correct data, not a sign the date needs retrying every run.
* **Tolerant of missing raw files.** Some historical dates' raw FITS may no
  longer be on disk; :func:`~muscat_db.scanner.scan_date` returning nothing
  is recorded and the date is still marked done (there is nothing more this
  tool can do for it), rather than retried forever.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from muscat_db.instruments import INSTRUMENTS

logger = logging.getLogger(__name__)

# Instruments whose keys list captures PROPID (issue #144 PR1) -- derived from
# the actual capture wiring rather than a second, independently-maintained
# list, so this can never drift from what scan_date really writes to disk.
PROPID_INSTRUMENTS: tuple[str, ...] = tuple(
    name for name, cfg in INSTRUMENTS.items() if "PROPID" in cfg.keys
)


@dataclass
class BackfillStats:
    """Summary of one instrument's backfill run."""

    instrument: str = ""
    dates_done: int = 0
    dates_skipped_no_raw_files: list[str] = field(default_factory=list)
    dates_failed: list[tuple[str, str]] = field(default_factory=list)
    frames_ingested: int = 0
    integrity_ok_before: bool = True
    integrity_ok_after: bool = True


def _tmpdir() -> Path:
    return Path(os.environ.get("MUSCAT_TMPDIR", str(Path.home() / "temp"))).expanduser()


def _checkpoint_path(instrument: str) -> Path:
    return _tmpdir() / f"propid_backfill_{instrument}.json"


def _load_checkpoint(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")).get("done", []))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("could not read backfill checkpoint %s, starting fresh: %s", path, e)
        return set()


def _save_checkpoint(path: Path, done: set[str]) -> None:
    """Atomic write (temp file + rename) so a kill mid-write never corrupts
    the checkpoint the next run relies on."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"done": sorted(done)}), encoding="utf-8")
    os.replace(tmp, path)


def _integrity_check_ok(db_path: str) -> bool:
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row) and row[0] == "ok"
    finally:
        conn.close()


def backfill_propid_for_instrument(
    instrument: str,
    *,
    db_path: str,
    sleep_s: float = 1.0,
    max_workers: int | None = None,
    max_dates: int | None = None,
    restart: bool = False,
    dry_run: bool = False,
    progress_log: Callable[[str], None] = print,
) -> BackfillStats:
    """Rescan and re-ingest one instrument's historical dates to backfill
    ``proposal_id``.

    Resumable by default: dates already recorded in this instrument's
    checkpoint file are skipped. ``restart=True`` discards that checkpoint
    and reprocesses every date from scratch. ``dry_run=True`` reports what
    would be processed without scanning, ingesting, or writing a checkpoint.
    """
    if instrument not in PROPID_INSTRUMENTS:
        raise ValueError(
            f"'{instrument}' has no PROPID capture; choose from: "
            f"{', '.join(PROPID_INSTRUMENTS)}"
        )

    from muscat_db import database
    from muscat_db import scanner

    stats = BackfillStats(instrument=instrument)

    stats.integrity_ok_before = _integrity_check_ok(db_path)
    if not stats.integrity_ok_before:
        raise RuntimeError(
            f"refusing to backfill {instrument}: {db_path} already fails "
            "PRAGMA integrity_check -- fix or restore the database first"
        )

    checkpoint = _checkpoint_path(instrument)
    done: set[str] = set() if restart else _load_checkpoint(checkpoint)

    dates = [d["obsdate"] for d in database.get_dates(db_path, instrument)]
    candidates = [d for d in dates if d not in done]
    if max_dates is not None:
        candidates = candidates[:max_dates]

    if dry_run:
        progress_log(
            f"[dry-run] {instrument}: {len(candidates)} date(s) would be "
            f"rescanned ({len(dates) - len(candidates)} already done)"
        )
        stats.dates_done = len(candidates)
        return stats

    for i, obsdate in enumerate(candidates):
        try:
            result = scanner.scan_date(instrument, obsdate, max_workers=max_workers)
            if not result:
                stats.dates_skipped_no_raw_files.append(obsdate)
                progress_log(f"  {instrument} {obsdate}: no raw files on disk, skipping")
            else:
                count = database.ingest_date(db_path, instrument, obsdate)
                stats.frames_ingested += count
                progress_log(f"  {instrument} {obsdate}: rescanned, {count} frames ingested")
            done.add(obsdate)
            stats.dates_done += 1
            _save_checkpoint(checkpoint, done)
        except Exception as e:
            logger.exception("backfill failed for %s %s", instrument, obsdate)
            stats.dates_failed.append((obsdate, str(e)))
            progress_log(f"  {instrument} {obsdate}: FAILED: {e}")

        if sleep_s and i < len(candidates) - 1:
            time.sleep(sleep_s)

    stats.integrity_ok_after = _integrity_check_ok(db_path)
    return stats
