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
from collections.abc import Callable, Iterable

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


def _compact(name: str) -> str:
    """Job keys and product directories use the target with spaces removed."""
    return (name or "").replace(" ", "").casefold()


class NightVisibility:
    """Which objects on one (instrument, obsdate) a viewer may see (issue #144
    PR5), for resources keyed by night and target name: photometry and
    transit-fit runs, their files and logs, and jobs.

    Those keys are free text -- a job's target is whatever was submitted, and a
    product directory is the OBJECT with spaces removed -- so a name is matched
    to ``summaries.object`` with spaces and case ignored, then by *normalize*
    (the app's target-name normalizer) when nothing matches that way.

    A night with no denied summary is never hidden, so the common case costs
    one query per night and changes nothing. On a night with denied rows, a
    name is visible only if it matches an object with a visible summary: an
    unrecognized name there is hidden too (fail closed), since a run under it
    could still hold restricted frames. Rows are read once per night and
    cached on the instance, so build one per request.
    """

    def __init__(
        self,
        db_path: str,
        denied: frozenset[str],
        normalize: Callable[[str], str] | None = None,
    ) -> None:
        self.db_path = db_path
        self.denied = denied
        self.normalize = normalize
        self._rows: dict[tuple[str, str], list[tuple[str, bool]]] = {}

    def _night(self, instrument: str, obsdate: str) -> list[tuple[str, bool]]:
        """``(object, visible)`` per summary on one night."""
        key = (instrument, obsdate)
        if key not in self._rows:
            with get_conn(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT object, proposal_id FROM summaries "
                    "WHERE instrument = ? AND obsdate = ?",
                    (instrument, obsdate),
                ).fetchall()
            self._rows[key] = [
                (obj or "", not is_denied(pid, self.denied)) for obj, pid in rows
            ]
        return self._rows[key]

    def hidden(self, instrument: str, obsdate: str, target: str) -> bool:
        """True when the viewer may not see *target*'s data on this night."""
        if not self.denied:
            return False
        rows = self._night(instrument, obsdate)
        if all(visible for _, visible in rows):
            return False
        want = _compact(target)
        matched = [visible for obj, visible in rows if _compact(obj) == want]
        if not matched and self.normalize is not None:
            norm = self.normalize(target)
            matched = [visible for obj, visible in rows if self.normalize(obj) == norm]
        return not any(matched)

    def night_has_denied(self, instrument: str, obsdate: str) -> bool:
        """True when any object on this night is under a denied proposal.

        For whole-night actions that name no target (scan, ingest): such an
        action touches every object on the night, so it is refused when any
        one of them is restricted.
        """
        if not self.denied:
            return False
        return any(not visible for _, visible in self._night(instrument, obsdate))

    def hidden_objects(self, instrument: str, obsdate: str) -> set[str]:
        """Compact names of the objects on this night with nothing visible."""
        if not self.denied:
            return set()
        visible: dict[str, bool] = {}
        for obj, is_visible in self._night(instrument, obsdate):
            name = _compact(obj)
            visible[name] = visible.get(name, False) or is_visible
        return {name for name, v in visible.items() if name and not v}


def hidden_obsdates(db_path: str, instrument: str, denied: frozenset[str]) -> set[str]:
    """Obsdates of *instrument* with summaries but none the viewer may see.

    Pages that add dates from the photometry output tree (not just the
    obslog) subtract these, so a fully restricted night cannot come back
    through its products.
    """
    if not denied:
        return set()
    clause, deny_params = sql_not_denied(denied)
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT obsdate FROM summaries WHERE instrument = ? GROUP BY obsdate "
            f"HAVING SUM(CASE WHEN {clause} THEN 1 ELSE 0 END) = 0",
            [instrument, *deny_params],
        ).fetchall()
    return {r[0] for r in rows}


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
