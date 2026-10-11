"""Pre-rebuild snapshot and integrity gate for ``muscat.db`` (issue #182, finding 1).

``build_db`` replaces the live database wholesale every night. Before that swap
the live file is copied with SQLite's online backup API (safe while the web
server holds WAL connections: the copy is taken under a read transaction, so it
is a consistent snapshot rather than a byte copy of a file mid-write), and the
copy is checked with ``PRAGMA integrity_check``. Checking the copy instead of
the live file validates both at once -- the backup is page-for-page the source
at snapshot time -- without a second full scan of the live database.

Only snapshots this module wrote (``<name>.nightly-<stamp>.sqlite``) are ever
pruned; hand-named backups sitting in the same directory are left alone.
"""

from __future__ import annotations

import datetime
import logging
import os
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_KEEP = 2
_NIGHTLY_TAG = ".nightly-"
_CORRUPT_SUFFIX = ".CORRUPT"
_SIDECARS = ("-wal", "-shm", "-journal")


class IntegrityError(RuntimeError):
    """A database failed ``PRAGMA integrity_check``."""


def backup_dir() -> Path:
    """Where snapshots go: ``$MUSCAT_DB_BACKUP_DIR``, else ``$MUSCAT_TMPDIR``,
    else ``~/temp`` (the project's standing backup location)."""
    raw = (
        os.environ.get("MUSCAT_DB_BACKUP_DIR")
        or os.environ.get("MUSCAT_TMPDIR")
        or str(Path.home() / "temp")
    )
    return Path(raw).expanduser()


def backup_keep() -> int:
    """How many nightly snapshots to retain (``$MUSCAT_DB_BACKUP_KEEP``, min 1)."""
    raw = os.environ.get("MUSCAT_DB_BACKUP_KEEP", "")
    try:
        return max(1, int(raw)) if raw.strip() else DEFAULT_KEEP
    except ValueError:
        logger.warning("ignoring non-integer MUSCAT_DB_BACKUP_KEEP=%r", raw)
        return DEFAULT_KEEP


def integrity_ok(db_path: str | os.PathLike) -> bool:
    """True iff ``PRAGMA integrity_check`` on *db_path* returns exactly ``ok``."""
    conn = sqlite3.connect(f"file:{os.fspath(db_path)}?mode=ro", uri=True, timeout=30)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row) and row[0] == "ok"
    except sqlite3.DatabaseError:
        # On a sufficiently corrupt file, some SQLite builds raise straight out
        # of PRAGMA integrity_check itself instead of returning a non-"ok" row
        # (observed to vary by linked libsqlite3 version) -- either way, the
        # database is not sound.
        return False
    finally:
        conn.close()


def remove_with_sidecars(path: str | os.PathLike) -> None:
    """Remove a SQLite file and its ``-wal``/``-shm``/``-journal`` sidecars,
    ignoring any that are absent."""
    base = os.fspath(path)
    for suffix in ("", *_SIDECARS):
        try:
            os.remove(base + suffix)
        except OSError:
            pass


def _backup_to(src: Path, part: Path) -> None:
    """Copy *src* into *part* with the online backup API, as a single file."""
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    try:
        target = sqlite3.connect(str(part))
        try:
            source.backup(target)
            # The backup copies page 1, so the copy inherits the live file's
            # WAL mode, and every later open of it -- the read-only integrity
            # check first -- creates -wal/-shm that a read-only connection
            # cannot remove and the rename leaves behind (#194). A rollback
            # journal keeps the snapshot one self-contained file.
            try:
                target.execute("PRAGMA journal_mode=DELETE")
            except sqlite3.DatabaseError:
                # A malformed copy can refuse the switch. Leave the verdict to
                # integrity_check, so it still becomes a .CORRUPT forensics
                # copy; snapshot()'s cleanup removes the sidecars that check
                # then leaves behind.
                pass
        finally:
            target.close()
    finally:
        source.close()


def _snapshot_name(db_path: Path, now: datetime.datetime) -> str:
    return f"{db_path.name}{_NIGHTLY_TAG}{now.strftime('%Y%m%d-%H%M%S')}.sqlite"


def snapshot(db_path: str | os.PathLike, dest_dir: Path | None = None,
             now: datetime.datetime | None = None) -> Path:
    """Copy *db_path* into *dest_dir* with the SQLite backup API and verify it.

    Returns the snapshot path. Raises :class:`IntegrityError` if the copy (and
    therefore the source) fails ``integrity_check``; the bad copy is kept,
    renamed with a ``.CORRUPT`` suffix, for forensics, and is never counted
    toward retention.
    """
    src = Path(db_path)
    dest_dir = dest_dir if dest_dir is not None else backup_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / _snapshot_name(src, now or datetime.datetime.now())
    part = dest.with_name(dest.name + ".part")

    try:
        _backup_to(src, part)
        if not integrity_ok(part):
            bad = dest.with_name(dest.name + _CORRUPT_SUFFIX)
            os.replace(part, bad)
            raise IntegrityError(
                f"{src} failed PRAGMA integrity_check (snapshot kept at {bad}); "
                "refusing to rebuild over it -- restore from an earlier backup first"
            )
        os.replace(part, dest)
    except BaseException:
        # Any failure from the copy through the final rename must not strand
        # ``.part``. On the corrupt path it was already renamed to .CORRUPT, so
        # this only clears whatever sidecars are still named after it.
        remove_with_sidecars(part)
        raise
    logger.info("snapshot of %s written to %s", src, dest)
    return dest


def prune(db_path: str | os.PathLike, dest_dir: Path | None = None,
          keep: int | None = None) -> list[Path]:
    """Delete all but the newest *keep* nightly snapshots of *db_path*.

    Only files matching ``<name>.nightly-*.sqlite`` are candidates, so manual
    backups and ``.CORRUPT`` forensics copies are never touched. The stamp is
    fixed-width, so name order is chronological order. Orphaned sidecars of
    nightly snapshots are swept as well.
    """
    dest_dir = dest_dir if dest_dir is not None else backup_dir()
    keep = keep if keep is not None else backup_keep()
    prefix = Path(db_path).name + _NIGHTLY_TAG
    snaps = sorted(
        p for p in dest_dir.glob(f"{prefix}*.sqlite")
        if p.is_file() and p.name.startswith(prefix)
    )
    removed: list[Path] = []
    for p in snaps[:-keep] if len(snaps) > keep else []:
        try:
            p.unlink()
        except OSError as exc:
            logger.warning("could not prune stale snapshot %s: %s", p, exc)
        else:
            removed.append(p)
    _sweep_orphan_sidecars(dest_dir, prefix)
    return removed


def _sweep_orphan_sidecars(dest_dir: Path, prefix: str) -> None:
    """Remove nightly sidecars whose main file is gone: those of the snapshots
    just pruned, and the ``.part-wal``/``.part-shm`` pairs every snapshot left
    before #194. Sidecars of a file that still exists are left alone, since
    they may belong to an open connection (a snapshot in progress, or one
    someone is inspecting)."""
    for p in dest_dir.glob(f"{prefix}*"):
        suffix = next((x for x in _SIDECARS if p.name.endswith(x)), None)
        if suffix is None or p.with_name(p.name[:-len(suffix)]).exists():
            continue
        try:
            p.unlink()
        except OSError as exc:
            logger.warning("could not remove orphaned sidecar %s: %s", p, exc)
