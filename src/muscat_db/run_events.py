"""Audit trail of who overwrote, deleted or cancelled a run (issue #307).

A run's ``_product.json`` keeps its author and, after someone else re-runs it,
``modified_by`` / ``modified_at`` (:func:`muscat_db.product_record.carry_authorship`).
A delete removes that record together with the run, so the record alone cannot
say who deleted what. This table can: one append-only row per change, kept in
``muscat.db`` and carried across ``build_db`` like the other app-owned tables.

Recording is best effort. A change that has already happened is never reported
as a failure because its audit row could not be written; the failure is logged.
"""

from __future__ import annotations

import logging
import sqlite3

from muscat_db.database import RUN_EVENTS_SCHEMA, get_conn
from muscat_db.product_record import now_iso

logger = logging.getLogger(__name__)

# ``overwrite_queued``: a confirmed overwrite that was queued rather than
# started. Nothing has been replaced yet, and a cancel of the queued job is
# its own row, so the trail never claims an overwrite that did not happen.
ACTIONS = ("overwrite", "overwrite_queued", "delete", "cancel")


def record(
    db_path: str,
    *,
    action: str,
    level: str,
    inst: str,
    date: str,
    target: str,
    run_id: str,
    actor: str | None,
    owner: str | None,
) -> bool:
    """Append one change to the trail. Returns False (and logs) on failure."""
    if action not in ACTIONS:
        raise ValueError(f"unknown run action {action!r}")
    row = (
        now_iso(), action, level, inst or "", date or "", target or "", run_id or "",
        (actor or "").strip(), (owner or "").strip(),
    )
    try:
        with get_conn(db_path) as conn:
            conn.executescript(RUN_EVENTS_SCHEMA)
            conn.execute(
                "INSERT INTO run_events (at, action, level, inst, date, target, run_id, actor, owner) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            conn.commit()
    except sqlite3.Error:
        logger.warning("could not record %s of %s run %s/%s/%s/%s", action, level, inst, date, target, run_id,
                       exc_info=True)
        return False
    return True


def for_run(db_path: str, level: str, inst: str, date: str, target: str, run_id: str) -> list[dict]:
    """The changes recorded for one run, oldest first. Empty when the table does
    not exist yet."""
    try:
        with get_conn(db_path, row_factory=sqlite3.Row, readonly=True) as conn:
            rows = conn.execute(
                "SELECT at, action, level, inst, date, target, run_id, actor, owner FROM run_events "
                "WHERE level = ? AND inst = ? AND date = ? AND target = ? AND run_id = ? ORDER BY id",
                (level, inst or "", date or "", target or "", run_id or ""),
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise
    return [dict(r) for r in rows]
