"""Per-viewer data visibility (issues #144, #202, #228).

The access groups decide (:mod:`muscat_db.access_groups`): allow-only, so a
viewer sees exactly what some grant of some group they belong to covers.
Each request computes the viewer's *denied* set once --
:func:`denied_proposal_ids_for` -- and every data path that honors it drops
``summaries``/``frames`` rows outside it.

Viewer rules:

* an admin (``users.is_admin = 1``) is denied nothing;
* any other viewer is denied every proposal no grant of theirs covers;
* an anonymous viewer (no trusted ``X-Forwarded-User``) is in ``everyone``
  only, the same rule the static site applies at build time;
* a database without groups has no rule yet, so nothing is denied. Every
  writer below seeds the groups first (:func:`access_groups.ensure_seeded`),
  so the first rule an admin writes switches the model on.

The #144/#202 blocklist tables (``restricted_proposals``,
``user_proposal_access``) are no longer read or written here. An unseeded
database that still holds rules in them fails closed until an admin migrates
it with ``muscat-db access groups-seed``.

Proposal ids compare case-insensitively everywhere: the grant tables declare
``COLLATE NOCASE``, but ``summaries.proposal_id`` does not, so the denied set
is returned upper-cased and SQL filters use :func:`sql_not_denied`.

Only the ``muscat-db access`` CLI writes access rules; no HTTP route does.
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


def _is_scoped(conn: sqlite3.Connection, username: str) -> bool:
    """Whether *username* is excluded from ``everyone`` -- the group model's
    scoped viewer (#202). ``users.scoped`` mirrors it but no longer decides."""
    try:
        row = conn.execute(
            "SELECT 1 FROM group_exclusions WHERE groupname = ? AND username = ?",
            (access_groups.EVERYONE, username),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if _missing_table(exc):
            return False
        raise
    return row is not None


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
    """Proposal ids (upper-cased) this viewer may not see, from their grants
    (:func:`_group_denied`). The result feeds :func:`is_denied`,
    :func:`muscat_db.database.sql_not_denied` and :class:`NightVisibility`.
    """
    with get_conn(db_path) as conn:
        active = access_groups.is_active(conn)
        unmigrated = not active and access_groups.legacy_rules_unmigrated(conn)
        admin = bool(username) and _is_admin(conn, username)
        observed = _observed_ids(conn) if unmigrated and not admin else frozenset()
    if active:
        return _group_denied(db_path, username, normalize)
    if not unmigrated or admin:
        return frozenset()
    logger.error(
        "access: %s holds blocklist rules but no access groups; every non-admin is "
        "denied everything until `muscat-db access groups-seed` migrates it",
        db_path,
    )
    return ScopedDenied(observed | {""}, frozenset(), ())


def _group_denied(
    db_path: str,
    username: str | None,
    normalize: Callable[[str], str] | None,
) -> frozenset[str]:
    """The viewer's grants as a :class:`ScopedDenied`: plain proposal grants
    are its allowlist, every narrower grant (instrument, target, date) one of
    its rules. A proposal nobody granted, including one ingested after the
    seed, is therefore hidden. Admins are denied nothing."""
    grants = access_groups.load_access(db_path, username, normalize)
    if grants.is_admin:
        return frozenset()
    allowed = frozenset(g.proposal_id.upper() for g in grants.grants if g.is_proposal_only)
    rules = tuple(g for g in grants.grants if not g.is_proposal_only)
    with get_conn(db_path) as conn:
        denied = (_observed_ids(conn) | {""}) - allowed
    return ScopedDenied(denied, allowed, rules)


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

    def matched_objects(self, instrument: str, obsdate: str, target: str) -> list[str]:
        """Raw ``object`` spellings on this night that *target* names, matched
        like :meth:`hidden` (spaces and case ignored, then the normalizer).

        A run records its target as the job directory spells it, not as the
        header's ``object``; target grants are stated on the latter.
        """
        rows = self._night(instrument, obsdate)
        want = _compact(target)
        found = {obj for obj, _ in rows if _compact(obj) == want}
        if not found and self.normalize is not None:
            norm = self.normalize(target)
            found = {obj for obj, _ in rows if self.normalize(obj) == norm}
        return sorted(found)

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


def sql_object_visible(object_column: str, denied: frozenset[str]) -> tuple[str, list[str]]:
    """``(clause, params)`` keeping rows whose *object_column* is not hidden.

    The SQL form of :func:`object_hidden` (without the night narrowing), for
    filtering inside a query so a ``LIMIT`` counts only visible rows. An object
    with no observations stays visible, as there. *object_column* is a trusted,
    table-qualified identifier, never user input.
    """
    if not denied:
        return "1", []
    # Aliased so a scoped viewer's target grants address summaries, not the
    # outer table (which may share column names such as ``instrument``).
    clause, params = sql_not_denied(denied, "s.proposal_id")
    return (
        f"(NOT EXISTS (SELECT 1 FROM summaries s WHERE s.object = {object_column})"
        f" OR EXISTS (SELECT 1 FROM summaries s WHERE s.object = {object_column} AND {clause}))",
        params,
    )


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
    """Mark *username* as a scoped viewer (or clear it): excluded from
    ``everyone``, so only their own grants apply. Returns False when nothing
    changed. The user row is created if missing, the same way ``htpasswd
    promote`` does, and ``users.scoped`` is kept as a mirror."""
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        access_groups.ensure_seeded(conn)
        conn.execute(
            "INSERT OR IGNORE INTO users (username, display_name) VALUES (?, ?)",
            (username, username),
        )
        changed = access_groups.set_excluded(conn, access_groups.EVERYONE, username, scoped)
        conn.execute(
            "UPDATE users SET scoped = ? WHERE username = ?", (1 if scoped else 0, username)
        )
        conn.commit()
    return changed


def restrict(db_path: str, proposal_id: str, revoked_by: str = "") -> bool:
    """Hide *proposal_id* from ``everyone`` by revoking its ``everyone``
    grant. Grants to other groups and users are kept. Returns False if
    ``everyone`` did not have it."""
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        access_groups.ensure_seeded(conn)
        removed = access_groups.remove_proposal_grant(
            conn, access_groups.EVERYONE, proposal_id, revoked_by
        )
        conn.commit()
    return removed > 0


def unrestrict(db_path: str, proposal_id: str, granted_by: str = "") -> bool:
    """Share *proposal_id* with ``everyone``. Grants are kept so
    re-restricting restores them.

    This is also how a proposal ingested after the seed is shared: the groups
    are allow-only, so it was hidden without ever having been restricted.
    Returns False when ``everyone`` already had it."""
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        access_groups.ensure_seeded(conn)
        granted = access_groups.add_grant(
            conn,
            access_groups.EVERYONE,
            proposal_id=proposal_id,
            note="access unrestrict",
            granted_by=granted_by,
        )
        conn.commit()
    return granted


def _ensure_user_group(conn: sqlite3.Connection, username: str, by: str) -> str:
    group = access_groups.user_group(username)
    access_groups.ensure_group(conn, group, f"direct grants for {username}", builtin=True)
    access_groups.add_member(conn, group, username, by)
    return group


def grant(db_path: str, username: str, proposal_id: str, granted_by: str) -> bool:
    """Let *username* see *proposal_id*, as a grant to ``user:<name>``.
    Returns False if already granted."""
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        access_groups.ensure_seeded(conn)
        group = _ensure_user_group(conn, username, granted_by)
        added = access_groups.add_grant(
            conn, group, proposal_id=proposal_id, note="access grant", granted_by=granted_by
        )
        conn.commit()
    return added


def revoke(db_path: str, username: str, proposal_id: str, revoked_by: str = "") -> bool:
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        removed = access_groups.remove_proposal_grant(
            conn, access_groups.user_group(username), proposal_id, revoked_by
        )
        conn.commit()
    return removed > 0


def user_exists(db_path: str, username: str) -> bool:
    with get_conn(db_path) as conn:
        try:
            row = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
        except sqlite3.OperationalError as exc:
            if _missing_table(exc):
                return False
            raise
    return row is not None


def _everyone_proposals(conn: sqlite3.Connection) -> frozenset[str]:
    rows = conn.execute(
        "SELECT proposal_id FROM group_grants WHERE groupname = ? AND proposal_id IS NOT NULL "
        "AND instrument IS NULL AND target IS NULL AND date_from IS NULL AND date_to IS NULL",
        (access_groups.EVERYONE,),
    ).fetchall()
    return frozenset(r[0].upper() for r in rows)


def _user_proposal_grants(conn: sqlite3.Connection, username: str | None = None) -> list[tuple]:
    """``(username, proposal_id, granted_by, granted_at)`` of plain proposal
    grants to ``user:<name>`` groups."""
    prefix = access_groups.USER_PREFIX
    sql = (
        "SELECT substr(groupname, ?), proposal_id, granted_by, granted_at FROM group_grants "
        "WHERE groupname LIKE ? AND proposal_id IS NOT NULL AND instrument IS NULL "
        "AND target IS NULL AND date_from IS NULL AND date_to IS NULL"
    )
    params: list = [len(prefix) + 1, prefix + "%"]
    if username is not None:
        sql += " AND groupname = ?"
        params.append(access_groups.user_group(username))
    sql += " ORDER BY groupname, proposal_id COLLATE NOCASE"
    return conn.execute(sql, params).fetchall()


def list_restrictions(db_path: str) -> list[dict]:
    """Every observed proposal ``everyone`` may not see, with the users
    granted it directly, sorted by id. ``since`` is when ``everyone`` lost
    its grant, or empty for a proposal it never had (ingested after the
    seed)."""
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        if not access_groups.is_active(conn):
            return []
        shared = _everyone_proposals(conn)
        observed = sorted(
            {
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT proposal_id FROM summaries WHERE proposal_id <> ''"
                )
            },
            key=str.upper,
        )
        since = dict(
            conn.execute(
                "SELECT UPPER(proposal_id), MAX(revoked_at) FROM group_grants_revoked "
                "WHERE groupname = ? AND proposal_id IS NOT NULL GROUP BY UPPER(proposal_id)",
                (access_groups.EVERYONE,),
            ).fetchall()
        )
        by_pid: dict[str, list[str]] = {}
        for user, pid, _by, _at in _user_proposal_grants(conn):
            by_pid.setdefault(pid.upper(), []).append(user)
    out: list[dict] = []
    seen: set[str] = set()
    for pid in observed:
        key = pid.upper()
        if key in shared or key in seen:
            continue
        seen.add(key)
        out.append({"proposal_id": pid, "since": since.get(key, ""), "grantees": by_pid.get(key, [])})
    return out


def list_grants(db_path: str, username: str | None = None) -> list[dict]:
    """Direct proposal grants, optionally for one user. ``restricted`` flags a
    proposal ``everyone`` may not see."""
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        if not access_groups.is_active(conn):
            return []
        shared = _everyone_proposals(conn)
        rows = _user_proposal_grants(conn, username)
    return [
        {
            "username": u,
            "proposal_id": pid,
            "granted_by": by,
            "granted_at": at,
            "restricted": pid.upper() not in shared,
        }
        for u, pid, by, at in rows
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
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        access_groups.ensure_seeded(conn)
        group = _ensure_user_group(conn, username, granted_by)
        created = access_groups.add_grant(
            conn, group, target=target, note="target grant", granted_by=granted_by
        )
        conn.commit()
    return created


def revoke_target(db_path: str, username: str, target: str, revoked_by: str = "") -> bool:
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        removed = access_groups.remove_target_grants(
            conn, access_groups.user_group(username), target, revoked_by
        )
        conn.commit()
    return removed > 0


def create_group(db_path: str, name: str, description: str = "") -> bool:
    """Create an explicit group. Returns False if one by that name exists.
    The caller checks the name with :func:`access_groups.check_group_name`."""
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        created = access_groups.ensure_group(conn, name, description)
        conn.commit()
    return created


def group_exists(db_path: str, name: str) -> bool:
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        return access_groups.group_exists(conn, name)


def add_group_member(db_path: str, group: str, username: str, added_by: str) -> bool:
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        added = access_groups.add_member(conn, group, username, added_by)
        conn.commit()
    return added


def remove_group_member(db_path: str, group: str, username: str) -> bool:
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        removed = access_groups.remove_member(conn, group, username)
        conn.commit()
    return removed


def grant_group(
    db_path: str,
    group: str,
    selector: access_groups.Selector,
    note: str,
    granted_by: str,
) -> int | None:
    """Grant *selector* to *group*. A ``user:<name>`` group is created on the
    way, with that user as its member, as :func:`grant_target` does. Returns
    the new grant's id, or ``None`` when an identical grant exists."""
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        if group.lower().startswith(access_groups.USER_PREFIX):
            username = group[len(access_groups.USER_PREFIX):]
            access_groups.ensure_group(conn, group, f"direct grants for {username}", builtin=True)
            access_groups.add_member(conn, group, username, granted_by)
        created = access_groups.add_grant(
            conn,
            group,
            proposal_id=selector.proposal_id,
            instrument=selector.instrument,
            target=selector.target,
            date_from=selector.date_from,
            date_to=selector.date_to,
            note=note,
            granted_by=granted_by,
        )
        grant_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0] if created else None
        conn.commit()
    return grant_id


def revoke_group_grant(db_path: str, grant_id: int, revoked_by: str) -> dict | None:
    """Revoke one grant by id, keeping it in ``group_grants_revoked``."""
    with get_conn(db_path) as conn:
        _ensure_schema(conn)
        row = access_groups.revoke_grant(conn, grant_id, revoked_by)
        conn.commit()
    return row


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
        # group grants (#228) that match no observation: data gone dark
        "dead_group_grants": [
            {
                "id": g.id,
                "group": g.group,
                "label": access_groups.grant_label(vars(g)),
                "target": g.target,
            }
            for g in access_groups.dead_grants(db_path, username)
        ],
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
