"""Per-viewer proposal access control (issue #144, PR4).

Restriction is opt-in per LCO proposal: a proposal gates nothing until an
admin adds it to ``restricted_proposals`` (``muscat-db access restrict``).
Each request then computes the viewer's *denied* set once --
:func:`denied_proposal_ids_for` -- and every data path that honors it drops
``summaries``/``frames`` rows whose ``proposal_id`` is in that set.

Viewer rules (decided for PR4):

* an admin (``users.is_admin = 1``) is denied nothing;
* an authenticated user is denied every restricted proposal they have no
  ``user_proposal_access`` grant for;
* an anonymous viewer (no trusted ``X-Forwarded-User``) is a zero-grants
  viewer: denied every restricted proposal, the same rule the static site
  (PR2) applies at build time.

The denied set is empty whenever nothing is restricted, which is the
overwhelmingly common case, and callers skip all extra filtering then.

Proposal ids compare case-insensitively everywhere: ``restricted_proposals``
and ``user_proposal_access`` declare ``COLLATE NOCASE``, but
``summaries.proposal_id`` does not, so the denied set is returned
upper-cased and SQL filters use :func:`sql_not_denied`.

Only the ``muscat-db access`` CLI writes the two tables; no HTTP route does.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable

from muscat_db.database import get_conn, sql_not_denied

logger = logging.getLogger(__name__)


def _missing_table(exc: sqlite3.OperationalError) -> bool:
    return "no such table" in str(exc)


def _restricted_ids(conn: sqlite3.Connection) -> frozenset[str]:
    try:
        rows = conn.execute("SELECT proposal_id FROM restricted_proposals").fetchall()
    except sqlite3.OperationalError as exc:
        # A database predating PR1's schema cannot have restricted anything.
        if _missing_table(exc):
            return frozenset()
        raise
    return frozenset(r[0].upper() for r in rows if r[0])


def _is_admin(conn: sqlite3.Connection, username: str) -> bool:
    try:
        row = conn.execute(
            "SELECT is_admin FROM users WHERE username = ?", (username,)
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if _missing_table(exc):
            return False
        raise
    return bool(row and row[0])


def _granted_ids(conn: sqlite3.Connection, username: str) -> frozenset[str]:
    try:
        rows = conn.execute(
            "SELECT proposal_id FROM user_proposal_access WHERE username = ?",
            (username,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if _missing_table(exc):
            return frozenset()
        raise
    return frozenset(r[0].upper() for r in rows if r[0])


def denied_proposal_ids_for(db_path: str, username: str | None) -> frozenset[str]:
    """Restricted proposal ids (upper-cased) this viewer may not see.

    Empty when nothing is restricted or the viewer is an admin. Any database
    error other than a missing table propagates: failing open here would
    silently expose restricted data.
    """
    with get_conn(db_path) as conn:
        restricted = _restricted_ids(conn)
        if not restricted:
            return frozenset()
        if username is None:
            return restricted
        if _is_admin(conn, username):
            return frozenset()
        return restricted - _granted_ids(conn, username)


def is_denied(proposal_id: str | None, denied: frozenset[str]) -> bool:
    """Whether one row's ``proposal_id`` falls in a denied set from
    :func:`denied_proposal_ids_for`."""
    return bool(denied) and (proposal_id or "").upper() in denied


def object_hidden(
    db_path: str,
    obj: str,
    denied: frozenset[str],
    *,
    obsdate: str = "",
    instrument: str = "",
) -> bool:
    """True when *obj* (optionally narrowed to one night) has observations but
    none the viewer may see.

    An object with no observations at all is *not* hidden: it behaves exactly
    as it did before PR4, so a restricted name and a never-observed name stay
    indistinguishable wherever the caller treats "hidden" like "absent".
    """
    if not denied:
        return False
    where = ["object = ?"]
    params: list[str] = [obj]
    if obsdate:
        where.append("obsdate = ?")
        params.append(obsdate)
    if instrument:
        where.append("instrument = ?")
        params.append(instrument)
    clause, deny_params = sql_not_denied(denied)
    sql = (
        "SELECT COUNT(*), SUM(CASE WHEN {visible} THEN 1 ELSE 0 END) "
        "FROM summaries WHERE {where}"
    ).format(visible=clause, where=" AND ".join(where))
    with get_conn(db_path) as conn:
        total, visible = conn.execute(sql, [*deny_params, *params]).fetchone()
    return bool(total) and not visible


# ── Admin writes (CLI only) ─────────────────────────────────────────────


def _ensure_schema(conn: sqlite3.Connection) -> None:
    from muscat_db.database import _apply_schema

    _apply_schema(conn)


def observed_spelling(db_path: str, proposal_id: str) -> str | None:
    """The exact ``proposal_id`` spelling already recorded in ``summaries``
    for a case-insensitive match, or ``None`` if no summary carries it yet."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT proposal_id FROM summaries WHERE proposal_id = ? COLLATE NOCASE LIMIT 1",
            (proposal_id,),
        ).fetchone()
    return row[0] if row else None


def restrict(db_path: str, proposal_id: str, description: str = "") -> bool:
    """Opt *proposal_id* into restriction. Returns False if it already was
    (the description is then updated in place)."""
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        existed = conn.execute(
            "SELECT 1 FROM restricted_proposals WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        if existed:
            conn.execute(
                "UPDATE restricted_proposals SET description = ? WHERE proposal_id = ?",
                (description, proposal_id),
            )
        else:
            conn.execute(
                "INSERT INTO restricted_proposals (proposal_id, description) VALUES (?, ?)",
                (proposal_id, description),
            )
        conn.commit()
    return not existed


def unrestrict(db_path: str, proposal_id: str) -> bool:
    """Lift a restriction. Grants are kept so re-restricting restores them."""
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        cur = conn.execute(
            "DELETE FROM restricted_proposals WHERE proposal_id = ?", (proposal_id,)
        )
        conn.commit()
    return cur.rowcount > 0


def grant(db_path: str, username: str, proposal_id: str, granted_by: str) -> bool:
    """Let *username* see *proposal_id*. Returns False if already granted."""
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        cur = conn.execute(
            "INSERT OR IGNORE INTO user_proposal_access (username, proposal_id, granted_by) "
            "VALUES (?, ?, ?)",
            (username, proposal_id, granted_by),
        )
        conn.commit()
    return cur.rowcount > 0


def revoke(db_path: str, username: str, proposal_id: str) -> bool:
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        cur = conn.execute(
            "DELETE FROM user_proposal_access WHERE username = ? AND proposal_id = ?",
            (username, proposal_id),
        )
        conn.commit()
    return cur.rowcount > 0


def user_exists(db_path: str, username: str) -> bool:
    with get_conn(db_path) as conn:
        try:
            row = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
        except sqlite3.OperationalError as exc:
            if _missing_table(exc):
                return False
            raise
    return row is not None


def list_restrictions(db_path: str) -> list[dict]:
    """Every restricted proposal with its grantees, sorted by id."""
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        restricted = conn.execute(
            "SELECT proposal_id, description, created_at FROM restricted_proposals "
            "ORDER BY proposal_id COLLATE NOCASE"
        ).fetchall()
        grants = conn.execute(
            "SELECT proposal_id, username FROM user_proposal_access ORDER BY username"
        ).fetchall()
    by_pid: dict[str, list[str]] = {}
    for pid, user in grants:
        by_pid.setdefault(pid.upper(), []).append(user)
    return [
        {
            "proposal_id": pid,
            "description": desc,
            "created_at": created,
            "grantees": by_pid.get(pid.upper(), []),
        }
        for pid, desc, created in restricted
    ]


def list_grants(db_path: str, username: str | None = None) -> list[dict]:
    """Grant rows, optionally for one user. Includes grants for proposals
    that are not (or no longer) restricted, flagged by ``restricted``."""
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        sql = (
            "SELECT a.username, a.proposal_id, a.granted_by, a.granted_at, "
            "       r.proposal_id IS NOT NULL "
            "FROM user_proposal_access a "
            "LEFT JOIN restricted_proposals r ON r.proposal_id = a.proposal_id"
        )
        params: tuple = ()
        if username is not None:
            sql += " WHERE a.username = ?"
            params = (username,)
        sql += " ORDER BY a.username, a.proposal_id COLLATE NOCASE"
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "username": u,
            "proposal_id": pid,
            "granted_by": by,
            "granted_at": at,
            "restricted": bool(r),
        }
        for u, pid, by, at, r in rows
    ]


def pending_backfill_dates(
    db_path: str, instruments: Iterable[str] | None = None
) -> dict[str, int]:
    """Per PROPID-capturing instrument, how many observed dates the PR3
    backfill has not yet processed (per its checkpoint). Rows on those dates
    carry ``proposal_id = ''`` and so stay visible to everyone even after
    their proposal is restricted. Instruments with nothing pending are
    omitted."""
    from muscat_db import database
    from muscat_db import propid_backfill as pb

    pending: dict[str, int] = {}
    for inst in instruments if instruments is not None else pb.PROPID_INSTRUMENTS:
        done = pb._load_checkpoint(pb._checkpoint_path(inst))
        dates = {d["obsdate"] for d in database.get_dates(db_path, inst)}
        n = len(dates - done)
        if n:
            pending[inst] = n
    return pending
