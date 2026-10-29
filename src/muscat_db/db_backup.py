"""Pre-rebuild snapshot and integrity gate for ``muscat.db`` (issue #182, finding 1).

``build_db`` replaces the derived catalog tables every night. Before publication
the live file is copied with SQLite's online backup API (safe while the web
server holds WAL connections: the copy is taken under a read transaction, so it
is a consistent snapshot rather than a byte copy of a file mid-write), and the
copy is checked with ``PRAGMA integrity_check``. Checking the copy instead of
the live file validates both at once -- the backup is page-for-page the source
at snapshot time -- without a second full scan of the live database.

``integrity_check`` output that is only ``Page N: never used`` can be leaked
page accounting (issue #237). The row-count gate below checks known application
tables against a clean baseline before accepting it; a marker and alert retain
the warning. Online rebuilds retain accepted leaked pages; VACUUM is deferred to a maintenance window. Other
integrity failures stop the build, and the failed copy is kept for forensics.

Only snapshots this module wrote (``<name>.nightly-<stamp>.sqlite``) are ever
pruned; hand-named backups sitting in the same directory are left alone.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_KEEP = 2
_NIGHTLY_TAG = ".nightly-"
# A snapshot whose integrity check found real damage. The name says what failed:
# the copy may be fine as a file and still hold a database that should not be
# rebuilt over.
_FAILED_SUFFIX = ".INTEGRITY-FAILED"
_SIDECARS = ("-wal", "-shm", "-journal")
# SQLite stops integrity_check after this many problems, so a report this long
# may hide something other than leaked pages behind them.
INTEGRITY_REPORT_LIMIT = 100
# Copy plus check normally take seconds to minutes; 3 h 11 min was seen on
# 2026-10-20 with a RAID consistency check running on the same disk.
SLOW_SNAPSHOT_S = 1800.0
_LEAK_LINE = re.compile(r"^(\*\*\* in database \w+ \*\*\*|Page \d+: never used)$")
_RUNBOOK = "notes/DEPLOYMENT.md, Database integrity failure"
# A leaked-pages snapshot is accepted, but "never used" can also be a subtree
# that lost its parent pointer, with its rows gone. The marker file beside the
# snapshot says it was not verified ``ok``, so prune keeps a clean one around.
_LEAK_MARKER = ".leaked-pages"
# Fixed token in the warning that deploy/nightly.sh greps for to raise an alert.
LEAK_ALERT_TOKEN = "INTEGRITY-LEAK"


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


def integrity_problems(db_path: str | os.PathLike) -> list[str]:
    """What ``PRAGMA integrity_check`` found in *db_path*, one problem per line;
    empty iff it returned exactly ``ok``."""
    conn = sqlite3.connect(f"file:{os.fspath(db_path)}?mode=ro", uri=True, timeout=30)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError as exc:
        # On a sufficiently corrupt file, some SQLite builds raise straight out
        # of PRAGMA integrity_check itself instead of returning a non-"ok" row
        # (observed to vary by linked libsqlite3 version) -- either way, the
        # database is not sound.
        return [f"integrity_check could not run: {exc}"]
    finally:
        conn.close()
    lines = [line for row in rows for line in str(row[0]).splitlines()]
    if lines == ["ok"]:
        return []
    return lines or ["integrity_check returned no result"]


def integrity_ok(db_path: str | os.PathLike) -> bool:
    """True iff ``PRAGMA integrity_check`` on *db_path* returns exactly ``ok``."""
    return not integrity_problems(db_path)


def leaked_pages_only(problems: list[str]) -> bool:
    """Whether an ``integrity_check`` report is nothing but ``Page N: never
    used`` lines (and the ``*** in database main ***`` header): pages no b-tree
    references. A report as long as SQLite's limit may be hiding other problems
    and does not count."""
    if not problems or len(problems) >= INTEGRITY_REPORT_LIMIT:
        return False
    return all(_LEAK_LINE.match(line) for line in problems) and any(
        line.startswith("Page ") for line in problems
    )


def app_table_counts(db_path: str | os.PathLike, tables) -> dict[str, int]:
    """Row count of each of *tables* present in *db_path* (absent ones skipped).
    Raises ``sqlite3.DatabaseError`` if a table cannot be read."""
    conn = sqlite3.connect(f"file:{os.fspath(db_path)}?mode=ro", uri=True, timeout=30)
    try:
        present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {
            t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            for t in tables if t in present
        }
    finally:
        conn.close()


def _marker(snap: Path) -> Path:
    return snap.with_name(snap.name + _LEAK_MARKER)


def _nightly_snapshots(dest_dir: Path, prefix: str) -> list[Path]:
    return sorted(
        p for p in dest_dir.glob(f"{prefix}*.sqlite")
        if p.is_file() and p.name.startswith(prefix)
    )


def has_leaked_pages(snapshot_path: str | os.PathLike) -> bool:
    """Whether a verified snapshot was accepted with leaked-page warnings."""
    return _marker(Path(snapshot_path)).exists()


def newest_clean_snapshot(db_path: str | os.PathLike, dest_dir: Path | None = None) -> Path | None:
    """The newest nightly snapshot whose integrity check returned exactly ``ok``
    (one without a leaked-pages marker), or None."""
    dest_dir = dest_dir if dest_dir is not None else backup_dir()
    prefix = Path(db_path).name + _NIGHTLY_TAG
    clean = [p for p in _nightly_snapshots(dest_dir, prefix) if not _marker(p).exists()]
    return clean[-1] if clean else None


def _row_drops(new: Path, baseline: Path, tables) -> list[str]:
    """``table before->after`` for each app table with fewer rows in *new* than
    in *baseline*; an unreadable table counts as a drop."""
    before = app_table_counts(baseline, tables)
    try:
        after = app_table_counts(new, tables)
    except sqlite3.DatabaseError as exc:
        return [f"app tables unreadable ({exc})"]
    return [f"{t} {n}->{after[t]}" for t, n in before.items() if after.get(t, 0) < n]


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
                # integrity_check, so it still becomes an .INTEGRITY-FAILED forensics
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
             now: datetime.datetime | None = None, app_tables=()) -> Path:
    """Copy *db_path* into *dest_dir* with the SQLite backup API and verify it.

    Returns the snapshot path. Raises :class:`IntegrityError` if the copy (and
    therefore the source) fails ``integrity_check`` with anything but leaked
    pages; the bad copy is kept, renamed with a ``.INTEGRITY-FAILED`` suffix,
    for forensics, and is never counted toward retention.

    A report of only unreferenced pages is accepted (issue #237) but is not
    proof that no rows are lost, so the snapshot gets a marker file, the
    warning carries :data:`LEAK_ALERT_TOKEN` for the nightly alert, and
    :func:`prune` keeps the newest clean snapshot. When *app_tables* is given,
    a leaked copy with fewer rows in any of them than the newest clean
    snapshot is refused like real damage.
    """
    src = Path(db_path)
    dest_dir = dest_dir if dest_dir is not None else backup_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / _snapshot_name(src, now or datetime.datetime.now())
    part = dest.with_name(dest.name + ".part")

    try:
        started = time.monotonic()
        _backup_to(src, part)
        copied = time.monotonic()
        problems = integrity_problems(part)
        checked = time.monotonic()
        leaked = bool(problems) and leaked_pages_only(problems)
        drops: list[str] = []
        if leaked and app_tables:
            baseline = newest_clean_snapshot(src, dest_dir)
            if baseline is not None:
                drops = _row_drops(part, baseline, app_tables)
        if leaked and not drops:
            _marker(dest).write_text("\n".join(problems[:5]) + "\n")
            logger.warning(
                "%s: %s -- %d unreferenced page(s) (%s). They are not proof that no row "
                "is lost; a clean snapshot is kept until a newer clean one exists (%s)",
                LEAK_ALERT_TOKEN, src,
                len(problems) - 1 if problems[0].startswith("***") else len(problems),
                problems[-1], _RUNBOOK,
            )
        elif problems:
            bad = dest.with_name(dest.name + _FAILED_SUFFIX)
            os.replace(part, bad)
            shown = "; ".join(problems[:3]) + (" ..." if len(problems) > 3 else "")
            if drops:
                shown = f"unreferenced pages and fewer rows than the last clean snapshot: {', '.join(drops)}"
            raise IntegrityError(
                f"{src} failed PRAGMA integrity_check ({shown}); snapshot kept at {bad}; "
                "refusing to rebuild over it -- restore from an earlier backup first "
                f"({_RUNBOOK})"
            )
        os.replace(part, dest)
    except BaseException:
        # Any failure from the copy through the final rename must not strand
        # ``.part``. On the corrupt path it was already renamed to .INTEGRITY-FAILED, so
        # this only clears whatever sidecars are still named after it.
        remove_with_sidecars(part)
        raise
    copy_s, check_s = copied - started, checked - copied
    logger.info("snapshot of %s written to %s (copy %.0f s, check %.0f s)", src, dest, copy_s, check_s)
    if copy_s + check_s > SLOW_SNAPSHOT_S:
        logger.warning(
            "slow snapshot of %s: copy %.0f s + check %.0f s; the disk is probably busy "
            "(a RAID consistency check or patrol read is the usual cause)",
            src, copy_s, check_s,
        )
    return dest


def prune(db_path: str | os.PathLike, dest_dir: Path | None = None,
          keep: int | None = None) -> list[Path]:
    """Delete all but the newest *keep* nightly snapshots of *db_path*.

    Only files matching ``<name>.nightly-*.sqlite`` are candidates, so manual
    backups and ``.INTEGRITY-FAILED`` forensics copies are never touched. The
    newest clean snapshot (no leaked-pages marker) is never pruned, however
    many leaked ones are newer. The stamp is
    fixed-width, so name order is chronological order. Orphaned sidecars of
    nightly snapshots are swept as well.
    """
    dest_dir = dest_dir if dest_dir is not None else backup_dir()
    keep = keep if keep is not None else backup_keep()
    prefix = Path(db_path).name + _NIGHTLY_TAG
    snaps = _nightly_snapshots(dest_dir, prefix)
    clean = [p for p in snaps if not _marker(p).exists()]
    protected = clean[-1] if clean else None
    removed: list[Path] = []
    for p in snaps[:-keep] if len(snaps) > keep else []:
        if p == protected:
            continue
        try:
            p.unlink()
        except OSError as exc:
            logger.warning("could not prune stale snapshot %s: %s", p, exc)
        else:
            removed.append(p)
            _marker(p).unlink(missing_ok=True)
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
