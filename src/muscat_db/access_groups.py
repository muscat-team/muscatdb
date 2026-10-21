"""Allow-only, group-based data visibility (issue #228).

This module holds the group/grant model and its evaluator. Step 1 proved that
the model, seeded from the #144/#202 blocklist, gives every viewer what the
blocklist does (:func:`parity`). Step 2 made it authoritative: once the
``everyone`` group exists (:func:`is_active`),
:func:`muscat_db.access.denied_proposal_ids_for` builds each viewer's denied
set from their grants, so every call site of the blocklist helpers reads the
groups. Before that, the blocklist still decides.

Model (design: issue #228):

* a frame is visible to a user iff some grant of some group they belong to
  matches it; admins see everything; nothing else is implicit;
* the fields of one grant are ANDed, ``NULL`` means "any";
* ``everyone`` contains every user (and anonymous viewers) unless a row in
  ``group_exclusions`` removes them -- that is how a scoped viewer is expressed;
* a grant to one nginx user is stored as a grant to the hidden single-member
  group ``user:<name>``, so there is one mechanism and one evaluator.

Target names cannot be normalized in SQL (the rule lives in Python and honours
manual overrides), so a target grant is *resolved* to the raw ``object``
spellings it matches when an :class:`Access` is built. An object first seen
after that is hidden until the next build: it fails closed, like #202.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from muscat_db.database import _apply_schema, get_conn

logger = logging.getLogger(__name__)

EVERYONE = "everyone"
ADMIN = "admin"
USER_PREFIX = "user:"
SEED_NOTE = "migrated from blocklist"
SEED_BY = "migration"
# Instruments that carry no PROPID by design; seeded into ``everyone`` as
# instrument grants (issue #228, decision 4).
NO_PROPID_INSTRUMENTS = ("muscat", "muscat2")


def user_group(username: str) -> str:
    return f"{USER_PREFIX}{username}"


def _compact(name: str) -> str:
    return (name or "").replace(" ", "").casefold()


# ── grant + evaluator ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Grant:
    """One rule. ``None`` fields match anything. ``objects`` is the resolved
    set of raw ``object`` spellings for a target grant (``None`` = no target
    constraint)."""

    id: int | None = None
    group: str = ""
    proposal_id: str | None = None
    instrument: str | None = None
    target: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    objects: frozenset[str] | None = None

    @property
    def is_proposal_only(self) -> bool:
        """A plain ``proposal_id`` grant, with no other field narrowing it."""
        return self.proposal_id is not None and (
            self.instrument is None
            and self.target is None
            and self.date_from is None
            and self.date_to is None
        )

    def matches(
        self, instrument: str, obsdate: str, obj: str | None, proposal_id: str | None
    ) -> bool:
        if self.proposal_id is not None and (proposal_id or "").upper() != self.proposal_id.upper():
            return False
        if self.instrument is not None and (instrument or "").lower() != self.instrument.lower():
            return False
        if self.date_from is not None and not (obsdate or "") >= self.date_from:
            return False
        if self.date_to is not None and not (obsdate or "") <= self.date_to:
            return False
        if self.target is not None and (obj or "") not in (self.objects or frozenset()):
            return False
        return True

    def sql(self, alias: str = "") -> tuple[str, list]:
        """``(clause, params)`` equivalent of :meth:`matches`."""
        p = f"{alias}." if alias else ""
        parts: list[str] = []
        params: list = []
        if self.proposal_id is not None:
            parts.append(f"COALESCE({p}proposal_id, '') COLLATE NOCASE = ?")
            params.append(self.proposal_id)
        if self.instrument is not None:
            parts.append(f"{p}instrument COLLATE NOCASE = ?")
            params.append(self.instrument)
        if self.date_from is not None:
            parts.append(f"{p}obsdate >= ?")
            params.append(self.date_from)
        if self.date_to is not None:
            parts.append(f"{p}obsdate <= ?")
            params.append(self.date_to)
        if self.target is not None:
            objects = sorted(self.objects or ())
            if not objects:
                return "0", []
            parts.append(f"{p}object IN ({','.join('?' for _ in objects)})")
            params.extend(objects)
        return ("(" + " AND ".join(parts) + ")" if parts else "1"), params


@dataclass(frozen=True)
class Access:
    """What one viewer may see. Immutable; build one per request (or cache by
    :attr:`cache_key`)."""

    user: str | None
    is_admin: bool
    groups: frozenset[str]
    grants: tuple[Grant, ...] = field(default_factory=tuple)

    @property
    def cache_key(self) -> tuple:
        """Viewers with the same groups share caches. Admins collapse to one key
        whatever their groups; grants are a function of the group set."""
        return ("admin",) if self.is_admin else tuple(sorted(self.groups))

    def frame_visible(
        self, instrument: str, obsdate: str, target: str | None, proposal_id: str | None
    ) -> bool:
        if self.is_admin:
            return True
        return any(g.matches(instrument, obsdate, target, proposal_id) for g in self.grants)

    def sql_visible(self, alias: str = "") -> tuple[str, list]:
        """``(clause, params)`` keeping only visible rows of ``frames`` or
        ``summaries``. Admins get ``("1", [])``, a viewer with no grants
        ``("0", [])``, so callers can always splice the clause in."""
        if self.is_admin:
            return "1", []
        if not self.grants:
            return "0", []
        clauses: list[str] = []
        params: list = []
        for grant in self.grants:
            clause, p = grant.sql(alias)
            if clause == "0":
                continue
            if clause == "1":
                return "1", []
            clauses.append(clause)
            params.extend(p)
        if not clauses:
            return "0", []
        return "(" + " OR ".join(clauses) + ")", params


# ── loading ─────────────────────────────────────────────────────────────


def _missing_table(exc: sqlite3.OperationalError) -> bool:
    return "no such table" in str(exc)


def is_active(conn: sqlite3.Connection) -> bool:
    """Whether the group model decides visibility (rollout step 2).

    It does once ``access groups-seed`` has created the ``everyone`` group.
    Before that, a database has no grants at all, and reading it allow-only
    would hide everything from every non-admin, so the blocklist still decides.
    """
    try:
        return conn.execute("SELECT 1 FROM groups WHERE name = ?", (EVERYONE,)).fetchone() is not None
    except sqlite3.OperationalError as exc:
        if _missing_table(exc):
            return False
        raise


def _group_names_for(conn: sqlite3.Connection, user: str | None, is_admin: bool) -> frozenset[str]:
    groups: set[str] = set()
    excluded: set[str] = set()
    if user:
        groups.add(user_group(user))
        groups.update(
            r[0].lower()
            for r in conn.execute("SELECT groupname FROM group_members WHERE username = ?", (user,))
        )
        excluded.update(
            r[0].lower()
            for r in conn.execute(
                "SELECT groupname FROM group_exclusions WHERE username = ?", (user,)
            )
        )
    groups.add(EVERYONE)
    if is_admin:
        groups.add(ADMIN)
    return frozenset(groups - excluded)


def _resolve_objects(
    targets: Iterable[str],
    objects: Iterable[str],
    normalize: Callable[[str], str] | None,
) -> dict[str, frozenset[str]]:
    """Raw ``object`` spellings matching each target name: equal with spaces
    and case ignored, or equal under *normalize* when one is supplied."""
    objects = [o for o in objects if o]
    out: dict[str, frozenset[str]] = {}
    for target in set(targets):
        want = _compact(target)
        norm = normalize(target) if normalize else None
        out[target] = frozenset(
            o for o in objects if _compact(o) == want or (norm is not None and normalize(o) == norm)
        )
    return out


def load_access(
    db_path: str,
    user: str | None,
    normalize: Callable[[str], str] | None = None,
) -> Access:
    """The :class:`Access` for *user* (``None`` = anonymous, who is in
    ``everyone`` only). A database with no group tables yet grants nothing,
    which is the safe reading; any other error propagates, because failing
    open would expose data."""
    with get_conn(db_path) as conn:
        admin = False
        if user:
            row = conn.execute("SELECT is_admin FROM users WHERE username = ?", (user,)).fetchone()
            admin = bool(row and row[0])
        try:
            groups = _group_names_for(conn, user, admin)
            rows = conn.execute(
                "SELECT id, groupname, proposal_id, instrument, target, date_from, date_to "
                f"FROM group_grants WHERE groupname IN ({','.join('?' for _ in groups)}) "
                "ORDER BY id",
                sorted(groups),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if _missing_table(exc):
                return Access(user, admin, frozenset())
            raise
        targets = {r[4] for r in rows if r[4] is not None}
        resolved: dict[str, frozenset[str]] = {}
        if targets:
            objects = [r[0] for r in conn.execute("SELECT DISTINCT object FROM summaries")]
            resolved = _resolve_objects(targets, objects, normalize)
    grants = tuple(
        Grant(
            id=r[0],
            group=r[1],
            proposal_id=r[2],
            instrument=r[3],
            target=r[4],
            date_from=r[5],
            date_to=r[6],
            objects=resolved.get(r[4]) if r[4] is not None else None,
        )
        for r in rows
    )
    return Access(user, admin, groups, grants)


def scoped_target_rules(
    conn: sqlite3.Connection,
    user: str,
    normalize: Callable[[str], str] | None = None,
) -> tuple[Grant, ...]:
    """Target grants that widen a *scoped* viewer's proposal allowlist.

    Only grants that name a target, from the viewer's own groups other than
    ``everyone`` and ``admin``: a scoped viewer is outside ``everyone`` by
    definition, and plain proposal grants already live in their allowlist
    (#202). Missing group tables mean no grants.
    """
    try:
        groups = _group_names_for(conn, user, False) - {EVERYONE, ADMIN}
        if not groups:
            return ()
        rows = conn.execute(
            "SELECT id, groupname, proposal_id, instrument, target, date_from, date_to "
            f"FROM group_grants WHERE target IS NOT NULL AND groupname IN "
            f"({','.join('?' for _ in groups)}) ORDER BY id",
            sorted(groups),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if _missing_table(exc):
            return ()
        raise
    if not rows:
        return ()
    objects = [r[0] for r in conn.execute("SELECT DISTINCT object FROM summaries")]
    resolved = _resolve_objects({r[4] for r in rows}, objects, normalize)
    return tuple(
        Grant(
            id=r[0],
            group=r[1],
            proposal_id=r[2],
            instrument=r[3],
            target=r[4],
            date_from=r[5],
            date_to=r[6],
            objects=resolved[r[4]],
        )
        for r in rows
    )


def remove_target_grants(conn: sqlite3.Connection, group: str, target: str) -> int:
    """Delete *group*'s grants on *target* (case-insensitive). Returns the count."""
    cur = conn.execute(
        "DELETE FROM group_grants WHERE groupname = ? AND target IS NOT NULL "
        "AND lower(target) = lower(?)",
        (group, target),
    )
    return cur.rowcount


# ── writes (CLI only) ───────────────────────────────────────────────────


def ensure_group(
    conn: sqlite3.Connection, name: str, description: str = "", builtin: bool = False
) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO groups (name, description, builtin) VALUES (?, ?, ?)",
        (name, description, 1 if builtin else 0),
    )
    return cur.rowcount > 0


def add_member(conn: sqlite3.Connection, group: str, username: str, added_by: str = "") -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO group_members (groupname, username, added_by) VALUES (?, ?, ?)",
        (group, username, added_by),
    )
    return cur.rowcount > 0


def remove_proposal_grant(conn: sqlite3.Connection, group: str, proposal_id: str) -> int:
    """Delete *group*'s plain ``proposal_id`` grant (no other field set),
    matched case-insensitively. Narrower grants on the proposal are kept.
    Returns the count."""
    cur = conn.execute(
        "DELETE FROM group_grants WHERE groupname = ? AND proposal_id = ? COLLATE NOCASE "
        "AND instrument IS NULL AND target IS NULL AND date_from IS NULL AND date_to IS NULL",
        (group, proposal_id),
    )
    return cur.rowcount


def set_excluded(conn: sqlite3.Connection, group: str, username: str, excluded: bool) -> bool:
    """Add or remove *username*'s exclusion from *group*. Returns whether a
    row changed."""
    if excluded:
        cur = conn.execute(
            "INSERT OR IGNORE INTO group_exclusions (groupname, username) VALUES (?, ?)",
            (group, username),
        )
    else:
        cur = conn.execute(
            "DELETE FROM group_exclusions WHERE groupname = ? AND username = ?",
            (group, username),
        )
    return cur.rowcount > 0


def add_grant(
    conn: sqlite3.Connection,
    group: str,
    *,
    proposal_id: str | None = None,
    instrument: str | None = None,
    target: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    note: str = "",
    granted_by: str = "",
) -> bool:
    """Insert a grant unless an identical one exists. Returns whether it did."""
    if date_to is not None and date_from is None:
        raise ValueError("date_to needs date_from")
    if date_from is not None and date_to is None:
        date_to = date_from
    existing = conn.execute(
        "SELECT 1 FROM group_grants WHERE groupname = ? "
        "AND proposal_id IS ? AND instrument IS ? AND target IS ? "
        "AND date_from IS ? AND date_to IS ?",
        (group, proposal_id, instrument, target, date_from, date_to),
    ).fetchone()
    if existing:
        return False
    conn.execute(
        "INSERT INTO group_grants "
        "(groupname, proposal_id, instrument, target, date_from, date_to, note, granted_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (group, proposal_id, instrument, target, date_from, date_to, note, granted_by),
    )
    return True


@dataclass
class SeedReport:
    groups: int = 0
    everyone_grants: list[str] = field(default_factory=list)
    user_grants: list[tuple[str, str]] = field(default_factory=list)
    exclusions: list[str] = field(default_factory=list)
    blank_lco_instruments: list[str] = field(default_factory=list)

    @property
    def changes(self) -> int:
        return (
            self.groups + len(self.everyone_grants) + len(self.user_grants) + len(self.exclusions)
        )


def seed_from_blocklist(db_path: str, dry_run: bool = False) -> SeedReport:
    """Express the #144/#202 blocklist as groups and grants, so the new model
    gives every viewer what they have today. Idempotent: it only adds rows that
    are missing, and never removes or edits one an admin made by hand.

    * ``everyone`` gets a ``proposal_id`` grant for every observed proposal that
      is not restricted, an instrument grant for each no-PROPID instrument, and
      a blank-``proposal_id`` grant if an LCO instrument has frames without one
      (the blocklist never hid those);
    * each ``user_proposal_access`` row becomes a grant to ``user:<name>``;
    * a scoped user is excluded from ``everyone``, so only their own grants
      apply -- the #202 allowlist.

    Proposals ingested after this run are not in ``everyone`` until it is run
    again; that is the allow-only default, and :func:`parity` reports them.
    """
    report = SeedReport()
    with get_conn(db_path) as conn:
        _apply_schema(conn)
        restricted = {
            r[0].upper()
            for r in conn.execute("SELECT proposal_id FROM restricted_proposals")
            if r[0]
        }
        observed = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT proposal_id FROM summaries WHERE proposal_id <> '' "
                "ORDER BY proposal_id"
            )
        ]
        blank_lco = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT instrument FROM summaries WHERE proposal_id = '' ORDER BY 1"
            )
            if r[0].lower() not in NO_PROPID_INSTRUMENTS
        ]
        report.blank_lco_instruments = blank_lco

        for name, desc in (
            (EVERYONE, "every user, unless excluded"),
            (ADMIN, "mirrors users.is_admin"),
        ):
            if ensure_group(conn, name, desc, builtin=True):
                report.groups += 1

        seen_ids: set[str] = set()
        for pid in observed:
            if pid.upper() in restricted or pid.upper() in seen_ids:
                continue
            seen_ids.add(pid.upper())
            if add_grant(conn, EVERYONE, proposal_id=pid, note=SEED_NOTE, granted_by=SEED_BY):
                report.everyone_grants.append(f"proposal_id={pid}")
        for inst in NO_PROPID_INSTRUMENTS:
            if add_grant(conn, EVERYONE, instrument=inst, note=SEED_NOTE, granted_by=SEED_BY):
                report.everyone_grants.append(f"instrument={inst}")
        if blank_lco and add_grant(
            conn, EVERYONE, proposal_id="", note=SEED_NOTE, granted_by=SEED_BY
        ):
            report.everyone_grants.append("proposal_id='' (blank PROPID on LCO frames)")

        for username, pid, by in conn.execute(
            "SELECT username, proposal_id, granted_by FROM user_proposal_access "
            "ORDER BY username, proposal_id"
        ).fetchall():
            group = user_group(username)
            if ensure_group(conn, group, f"direct grants for {username}", builtin=True):
                report.groups += 1
            add_member(conn, group, username, SEED_BY)
            if add_grant(conn, group, proposal_id=pid, note=SEED_NOTE, granted_by=by or SEED_BY):
                report.user_grants.append((username, pid))

        for (username,) in conn.execute("SELECT username FROM users WHERE scoped = 1").fetchall():
            cur = conn.execute(
                "INSERT OR IGNORE INTO group_exclusions (groupname, username) VALUES (?, ?)",
                (EVERYONE, username),
            )
            if cur.rowcount:
                report.exclusions.append(username)

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    return report


# ── parity with the blocklist ───────────────────────────────────────────


def parity(db_path: str, normalize: Callable[[str], str] | None = None) -> list[dict]:
    """Every (viewer, night, object, proposal) where the group model disagrees
    with the blocklist. Viewers are every user known to ``users`` or
    ``user_proposal_access``, plus the anonymous viewer.

    The groups decide once seeded, and the blocklist CLI writes both, so the
    expected mismatch is a proposal ingested after the seed: visible under the
    blocklist, hidden under the groups until someone grants it."""
    from muscat_db import access

    with get_conn(db_path) as conn:
        keys = conn.execute(
            "SELECT DISTINCT instrument, obsdate, object, proposal_id FROM summaries"
        ).fetchall()
        users: set[str | None] = {None}
        users.update(r[0] for r in conn.execute("SELECT username FROM users"))
        try:
            users.update(r[0] for r in conn.execute("SELECT username FROM user_proposal_access"))
        except sqlite3.OperationalError as exc:
            if not _missing_table(exc):
                raise
    mismatches: list[dict] = []
    for user in sorted(users, key=lambda u: (u is not None, u or "")):
        denied = access.blocklist_denied_proposal_ids_for(db_path, user, normalize)
        grants = load_access(db_path, user, normalize)
        for inst, date, obj, pid in keys:
            old = not access.is_denied(pid, denied, instrument=inst, obsdate=date, obj=obj)
            new = grants.frame_visible(inst, date, obj, pid)
            if old != new:
                mismatches.append(
                    {
                        "user": user,
                        "instrument": inst,
                        "obsdate": date,
                        "object": obj,
                        "proposal_id": pid,
                        "blocklist": old,
                        "groups": new,
                    }
                )
    return mismatches
