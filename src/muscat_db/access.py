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

from muscat_db import access_groups
from muscat_db.database import ScopedDenied, get_conn, sql_not_denied

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


def _is_scoped(conn: sqlite3.Connection, username: str) -> bool:
    try:
        row = conn.execute(
            "SELECT scoped FROM users WHERE username = ?", (username,)
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if _missing_table(exc) or "no such column" in str(exc):
            return False
        raise
    return bool(row and row[0])


def is_scoped(db_path: str, username: str | None) -> bool:
    """Whether *username* is a scoped viewer (issue #202). Admins never are."""
    if not username:
        return False
    with get_conn(db_path) as conn:
        return _is_scoped(conn, username) and not _is_admin(conn, username)


def _observed_ids(conn: sqlite3.Connection) -> frozenset[str]:
    rows = conn.execute("SELECT DISTINCT proposal_id FROM summaries").fetchall()
    return frozenset((r[0] or "").upper() for r in rows)


def denied_proposal_ids_for(
    db_path: str,
    username: str | None,
    normalize: Callable[[str], str] | None = None,
) -> frozenset[str]:
    """Proposal ids (upper-cased) this viewer may not see.

    Empty when nothing is restricted or the viewer is an admin. A scoped
    viewer (``users.scoped``, issue #202) is denied everything that is not on
    their allowlist, including frames with no PROPID, and gets a
    :class:`ScopedDenied` so SQL filters fail closed on proposals ingested
    later. Target grants (issue #228) ride on that set as ``rules``; *normalize*
    is the app's target-name normalizer, used to resolve them to the raw
    ``object`` spellings. Any database error other than a missing table
    propagates: failing open here would silently expose restricted data.
    """
    with get_conn(db_path) as conn:
        if username is not None and _is_scoped(conn, username):
            if _is_admin(conn, username):
                return frozenset()
            allowed = _granted_ids(conn, username)
            denied = (_observed_ids(conn) | _restricted_ids(conn) | {""}) - allowed
            rules = access_groups.scoped_target_rules(conn, username, normalize)
            return ScopedDenied(denied, allowed, rules)
        restricted = _restricted_ids(conn)
        if not restricted:
            return frozenset()
        if username is None:
            return restricted
        if _is_admin(conn, username):
            return frozenset()
        return restricted - _granted_ids(conn, username)


def is_denied(
    proposal_id: str | None,
    denied: frozenset[str],
    *,
    instrument: str | None = None,
    obsdate: str | None = None,
    obj: str | None = None,
) -> bool:
    """Whether one row's ``proposal_id`` falls in a denied set from
    :func:`denied_proposal_ids_for`.

    A scoped viewer's target grants (issue #228) also look at the row's
    *instrument*, *obsdate* and *obj*. A caller that omits them cannot match a
    grant on those fields, so it fails closed.
    """
    if not denied:
        return False
    pid = (proposal_id or "").upper()
    if isinstance(denied, ScopedDenied):
        if pid in denied.allowed:
            return False
        return not any(
            rule.matches(instrument or "", obsdate or "", obj, proposal_id)
            for rule in denied.rules
        )
    return pid in denied


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
                (
                    obj or "",
                    not is_denied(
                        pid, self.denied, instrument=instrument, obsdate=obsdate, obj=obj
                    ),
                )
                for obj, pid in rows
            ]
        return self._rows[key]

    def unknown_night(self, instrument: str, obsdate: str) -> bool:
        """True for a scoped viewer on a night with no summaries at all.

        Nothing on such a night can be tied to an allowlisted proposal, so a
        scoped viewer fails closed; other viewers keep the old behaviour.
        """
        return isinstance(self.denied, ScopedDenied) and not self._night(instrument, obsdate)

    def hidden(self, instrument: str, obsdate: str, target: str) -> bool:
        """True when the viewer may not see *target*'s data on this night."""
        if not self.denied:
            return False
        if self.unknown_night(instrument, obsdate):
            return True
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
        if self.unknown_night(instrument, obsdate):
            return True
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


def is_admin_user(db_path: str, username: str) -> bool:
    """Whether *username* has ``users.is_admin`` set."""
    with get_conn(db_path) as conn:
        return _is_admin(conn, username)


def set_scoped(db_path: str, username: str, scoped: bool = True) -> bool:
    """Mark *username* as a scoped viewer (or clear it). Returns False when
    the flag already had that value. The user row is created if missing, the
    same way ``htpasswd promote`` does."""
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            "INSERT OR IGNORE INTO users (username, display_name) VALUES (?, ?)",
            (username, username),
        )
        before = conn.execute(
            "SELECT scoped FROM users WHERE username = ?", (username,)
        ).fetchone()[0]
        if bool(before) == scoped:
            return False
        conn.execute(
            "UPDATE users SET scoped = ? WHERE username = ?", (1 if scoped else 0, username)
        )
        conn.commit()
    return True


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


def blank_propid_frames(
    db_path: str, instruments: Iterable[str] | None = None
) -> dict[str, int]:
    """Per PROPID-capturing instrument, how many frames carry no
    ``proposal_id``. Those rows stay visible to everyone even after their
    proposal is restricted, so this is the leak a new restriction would leave.
    Counting frames rather than the PR3 backfill's checkpoint means a night
    newer than the last backfill run, whose frames already carry a PROPID,
    does not warn. Instruments with nothing blank are omitted."""
    from muscat_db import propid_backfill as pb

    names = tuple(instruments if instruments is not None else pb.PROPID_INSTRUMENTS)
    if not names:
        return {}
    marks = ",".join("?" for _ in names)
    with get_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT instrument, COUNT(*) FROM frames "
            f"WHERE instrument IN ({marks}) AND TRIM(COALESCE(proposal_id, '')) = '' "
            f"GROUP BY instrument",
            names,
        ).fetchall()
    return {inst: n for inst, n in rows if n}


# ── Target grants for scoped viewers (issue #228, CLI only) ─────────────


def grant_target(db_path: str, username: str, target: str, granted_by: str) -> bool:
    """Let scoped viewer *username* see every observation of *target*, whatever
    its proposal. Returns False when the grant already exists."""
    group = access_groups.user_group(username)
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        access_groups.ensure_group(conn, group, f"direct grants for {username}")
        access_groups.add_member(conn, group, username, granted_by)
        created = access_groups.add_grant(
            conn, group, target=target, note="target grant", granted_by=granted_by
        )
        conn.commit()
    return created


def revoke_target(db_path: str, username: str, target: str) -> bool:
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        removed = access_groups.remove_target_grants(
            conn, access_groups.user_group(username), target
        )
        conn.commit()
    return removed > 0


def target_matches_observations(db_path: str, target: str) -> bool:
    """Whether *target* names any observed object (spaces and case ignored)."""
    want = _compact(target)
    with get_conn(db_path) as conn:
        return any(
            _compact(r[0] or "") == want
            for r in conn.execute("SELECT DISTINCT object FROM summaries")
        )


def list_target_grants(db_path: str, username: str) -> list[dict]:
    """*username*'s own target grants, oldest first."""
    with get_conn(db_path, row_factory=sqlite3.Row) as conn:
        try:
            rows = conn.execute(
                "SELECT target, granted_by, granted_at FROM group_grants "
                "WHERE groupname = ? AND target IS NOT NULL ORDER BY id",
                (access_groups.user_group(username),),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if _missing_table(exc):
                return []
            raise
    return [dict(r) for r in rows]


# ── Explain (read-only, CLI) ────────────────────────────────────────────


def explain_user(db_path: str, username: str) -> dict:
    """What *username* can and cannot see, per proposal, as the web computes it.

    Read-only. Built from the same :func:`denied_proposal_ids_for` the request
    path uses, so it cannot drift from what a viewer actually gets. An unknown
    username is explained as an ordinary authenticated viewer with no grants,
    which is what the web treats it as.
    """
    denied = denied_proposal_ids_for(db_path, username)
    restricted = {r["proposal_id"].upper() for r in list_restrictions(db_path)}
    granted = {g["proposal_id"].upper() for g in list_grants(db_path, username)}
    with get_conn(db_path) as conn:
        admin = _is_admin(conn, username)
        rows = conn.execute(
            "SELECT COALESCE(proposal_id, ''), "
            "COUNT(DISTINCT instrument || '|' || obsdate || '|' || COALESCE(object, '')), "
            "COUNT(DISTINCT object) FROM summaries GROUP BY 1 ORDER BY 1"
        ).fetchall()
    proposals = []
    observed: set[str] = set()
    for pid, target_nights, objects in rows:
        key = pid.upper()
        observed.add(key)
        proposals.append(
            {
                "proposal_id": pid,
                "restricted": key in restricted,
                "granted": key in granted,
                "visible": not is_denied(pid, denied),
                "target_nights": target_nights,
                "objects": objects,
            }
        )
    return {
        "username": username,
        "admin": admin,
        "proposals": proposals,
        # a grant on a proposal no summary carries gives the user nothing
        "target_grants": list_target_grants(db_path, username),
        "unmatched_grants": sorted(granted - observed),
        "unmatched_restrictions": sorted(restricted - observed),
    }


def explain_target(db_path: str, username: str, target: str) -> list[dict]:
    """Every summary row for *target* (spaces and case ignored, the same match
    :class:`NightVisibility` uses) with whether *username* may see it."""
    denied = denied_proposal_ids_for(db_path, username)
    want = _compact(target)
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT instrument, obsdate, object, COALESCE(proposal_id, '') "
            "FROM summaries ORDER BY instrument, obsdate"
        ).fetchall()
    return [
        {
            "instrument": inst,
            "obsdate": date,
            "object": obj,
            "proposal_id": pid,
            "visible": not is_denied(
                pid, denied, instrument=inst, obsdate=date, obj=obj
            ),
        }
        for inst, date, obj, pid in rows
        if _compact(obj or "") == want
    ]
