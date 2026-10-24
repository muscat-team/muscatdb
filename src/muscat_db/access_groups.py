"""Allow-only, group-based data visibility (issue #228).

This module holds the group/grant model and its evaluator, the only source of
visibility. Once the ``everyone`` group exists (:func:`is_active`),
:func:`muscat_db.access.denied_proposal_ids_for` builds each viewer's denied
set from their grants. Every access writer seeds the groups first
(:func:`ensure_seeded`), so a database without them has no rule at all. The
#144/#202 blocklist tables are read only by :func:`seed_from_blocklist`, the
one-time migration of a database that predates the groups.

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
import re
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
    # Each object's keys are computed once, not once per target: there are
    # thousands of objects, and *normalize* may be far from free.
    keyed = [(o, _compact(o), normalize(o) if normalize else None) for o in objects if o]
    out: dict[str, frozenset[str]] = {}
    for target in set(targets):
        want = _compact(target)
        norm = normalize(target) if normalize else None
        out[target] = frozenset(
            o for o, compact, o_norm in keyed if compact == want or (norm is not None and o_norm == norm)
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


_GRANT_COLUMNS = (
    "id, groupname, proposal_id, instrument, target, date_from, date_to, "
    "note, granted_by, granted_at"
)


def _archive_grants(conn: sqlite3.Connection, where: str, params: tuple, revoked_by: str) -> int:
    """Move the grants selected by *where* to ``group_grants_revoked``, so a
    revoke keeps its history. *where* is a trusted literal. Returns the count."""
    conn.execute(
        f"INSERT INTO group_grants_revoked ({_GRANT_COLUMNS}, revoked_by) "
        f"SELECT {_GRANT_COLUMNS}, ? FROM group_grants WHERE {where}",
        (revoked_by, *params),
    )
    return conn.execute(f"DELETE FROM group_grants WHERE {where}", params).rowcount


def remove_target_grants(
    conn: sqlite3.Connection, group: str, target: str, revoked_by: str = ""
) -> int:
    """Revoke *group*'s grants on *target* (case-insensitive). Returns the count."""
    return _archive_grants(
        conn,
        "groupname = ? AND target IS NOT NULL AND lower(target) = lower(?)",
        (group, target),
        revoked_by,
    )


def revoke_grant(conn: sqlite3.Connection, grant_id: int, revoked_by: str = "") -> dict | None:
    """Revoke one grant by id. Returns the revoked row, or ``None`` if no live
    grant has that id."""
    row = conn.execute(
        f"SELECT {_GRANT_COLUMNS} FROM group_grants WHERE id = ?", (grant_id,)
    ).fetchone()
    if row is None:
        return None
    _archive_grants(conn, "id = ?", (grant_id,), revoked_by)
    return dict(zip([c.strip() for c in _GRANT_COLUMNS.split(",")], row))


# ── writes (CLI only) ───────────────────────────────────────────────────


def ensure_group(
    conn: sqlite3.Connection, name: str, description: str = "", builtin: bool = False
) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO groups (name, description, builtin) VALUES (?, ?, ?)",
        (name, description, 1 if builtin else 0),
    )
    return cur.rowcount > 0


def group_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM groups WHERE name = ?", (name,)).fetchone() is not None


def remove_member(conn: sqlite3.Connection, group: str, username: str) -> bool:
    cur = conn.execute(
        "DELETE FROM group_members WHERE groupname = ? AND username = ?", (group, username)
    )
    return cur.rowcount > 0


def add_member(conn: sqlite3.Connection, group: str, username: str, added_by: str = "") -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO group_members (groupname, username, added_by) VALUES (?, ?, ?)",
        (group, username, added_by),
    )
    return cur.rowcount > 0


def remove_proposal_grant(
    conn: sqlite3.Connection, group: str, proposal_id: str, revoked_by: str = ""
) -> int:
    """Revoke *group*'s plain ``proposal_id`` grant (no other field set),
    matched case-insensitively. Narrower grants on the proposal are kept.
    Returns the count."""
    return _archive_grants(
        conn,
        "groupname = ? AND proposal_id = ? COLLATE NOCASE "
        "AND instrument IS NULL AND target IS NULL AND date_from IS NULL AND date_to IS NULL",
        (group, proposal_id),
        revoked_by,
    )


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
    gives every viewer what they had under it. Idempotent: it only adds rows
    that are missing, and never removes or edits one an admin made by hand.

    * ``everyone`` gets a ``proposal_id`` grant for every observed proposal that
      is not restricted, an instrument grant for each no-PROPID instrument, and
      a blank-``proposal_id`` grant if an LCO instrument has frames without one
      (the blocklist never hid those);
    * each ``user_proposal_access`` row becomes a grant to ``user:<name>``,
      unless the revoke history shows that grant was revoked since;
    * a scoped user is excluded from ``everyone``, so only their own grants
      apply -- the #202 allowlist.

    The legacy tables are read here only, as the input of this one-time
    migration; nothing writes them any more. Proposals ingested after this run
    are not in ``everyone`` until an admin shares them (``access unrestrict``):
    that is the allow-only default.
    """
    with get_conn(db_path) as conn:
        _apply_schema(conn)
        report = _seed(conn)
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    return report


def ensure_seeded(conn: sqlite3.Connection) -> None:
    """Seed the groups on *conn* (uncommitted) unless they already decide.

    Every access writer calls this first, so a grant, restriction or scope
    made on a fresh database lands in the group model rather than in tables
    nothing reads. A fresh database has no legacy rows, so this only shares
    every observed proposal with ``everyone``, which is what the viewer saw
    before any rule existed."""
    if not is_active(conn):
        _seed(conn)


def legacy_rules_unmigrated(conn: sqlite3.Connection) -> bool:
    """Whether an unseeded database still holds #144/#202 blocklist rules.

    Such a database predates the group model and was never migrated with
    ``access groups-seed``. Its rules are no longer read, so visibility must
    fail closed rather than silently drop them."""
    probes = (
        "SELECT 1 FROM restricted_proposals LIMIT 1",
        "SELECT 1 FROM user_proposal_access LIMIT 1",
        "SELECT 1 FROM users WHERE scoped = 1 LIMIT 1",
    )
    for sql in probes:
        try:
            if conn.execute(sql).fetchone():
                return True
        except sqlite3.OperationalError as exc:
            if not (_missing_table(exc) or "no such column" in str(exc)):
                raise
    return False


def _legacy_rows(conn: sqlite3.Connection, sql: str) -> list[tuple]:
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.OperationalError as exc:
        if _missing_table(exc):
            return []
        raise


def _revoked_proposal_ids(conn: sqlite3.Connection, group: str) -> set[str]:
    """Proposals whose plain grant *group* lost, upper-cased, from the revoke
    history."""
    return {
        r[0].upper()
        for r in conn.execute(
            "SELECT proposal_id FROM group_grants_revoked WHERE groupname = ? "
            "AND proposal_id IS NOT NULL AND instrument IS NULL AND target IS NULL "
            "AND date_from IS NULL AND date_to IS NULL",
            (group,),
        )
        if r[0]
    }


def _seed(conn: sqlite3.Connection) -> SeedReport:
    report = SeedReport()
    # A proposal `everyone` lost is restricted too: the legacy table stopped
    # recording restrictions, so without this a reseed (`groups-seed --force`)
    # would share again every proposal restricted since.
    restricted = {
        r[0].upper()
        for r in _legacy_rows(conn, "SELECT proposal_id FROM restricted_proposals")
        if r[0]
    } | _revoked_proposal_ids(conn, EVERYONE)
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

    # Likewise a migrated user grant revoked since: `revoke` leaves its legacy
    # row, so a reseed would grant it again.
    revoked: dict[str, set[str]] = {}
    for username, pid, by in _legacy_rows(
        conn,
        "SELECT username, proposal_id, granted_by FROM user_proposal_access "
        "ORDER BY username, proposal_id",
    ):
        group = user_group(username)
        if group not in revoked:
            revoked[group] = _revoked_proposal_ids(conn, group)
        if (pid or "").upper() in revoked[group]:
            continue
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
    return report


# ── admin CLI support (rollout step 4) ──────────────────────────────────

# A group made by hand: no ``user:`` prefix (those are made implicitly per
# user) and short enough to read in a table.
_GROUP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_YYMMDD = re.compile(r"\d{6}")
RESERVED_GROUPS = (EVERYONE, ADMIN)


def check_group_name(name: str) -> str | None:
    """Why *name* cannot be created as a group, or ``None`` when it can."""
    if name.lower() in RESERVED_GROUPS:
        return f"'{name}' is a built-in group"
    if name.lower().startswith(USER_PREFIX):
        return f"'{USER_PREFIX}<name>' groups are made by granting to one user"
    if not _GROUP_NAME.fullmatch(name):
        return (
            "a group name is 1-64 letters, digits, '_', '.' or '-', "
            "starting with a letter or digit"
        )
    return None


@dataclass(frozen=True)
class Selector:
    """The fields of one grant, checked. ``None`` means "any"."""

    proposal_id: str | None = None
    instrument: str | None = None
    target: str | None = None
    date_from: str | None = None
    date_to: str | None = None


def check_selector(
    *,
    proposal_id: str | None,
    instrument: str | None,
    target: str | None,
    date: str | None,
    date_from: str | None,
    date_to: str | None,
    instruments: Iterable[str],
) -> Selector:
    """Validate a grant's fields, or raise :class:`ValueError` saying why."""
    proposal_id = (proposal_id or "").strip() or None
    target = (target or "").strip() or None
    instrument = (instrument or "").strip().lower() or None
    if date is not None and (date_from is not None or date_to is not None):
        raise ValueError("Give either --date or --from/--to, not both.")
    if date_to is not None and date_from is None:
        raise ValueError("--to needs --from.")
    if date is not None:
        date_from = date_to = date
    for value in (date_from, date_to):
        if value is not None and not _YYMMDD.fullmatch(value):
            raise ValueError(f"Dates are YYMMDD obsdates, got '{value}'.")
    if date_from is not None and date_to is None:
        date_to = date_from
    if date_from is not None and date_from > date_to:
        raise ValueError(f"--from {date_from} is after --to {date_to}.")
    known = sorted(instruments)
    if instrument is not None and instrument not in known:
        raise ValueError(f"unknown instrument '{instrument}' (known: {', '.join(known)}).")
    if not (proposal_id or instrument or target or date_from):
        raise ValueError("Give at least one of --proposal, --instrument, --target or --date/--from.")
    return Selector(proposal_id, instrument, target, date_from, date_to)


def grant_label(row: dict) -> str:
    """One grant as ``field=value`` pairs, the way the CLI prints it."""
    parts = []
    if row.get("proposal_id") is not None:
        parts.append(f"proposal={row['proposal_id'] or '(blank)'}")
    if row.get("instrument") is not None:
        parts.append(f"instrument={row['instrument']}")
    if row.get("target") is not None:
        parts.append(f"target={row['target']}")
    if row.get("date_from") is not None:
        same = row["date_from"] == row.get("date_to")
        parts.append(f"date={row['date_from']}" if same else f"dates={row['date_from']}-{row['date_to']}")
    return " ".join(parts)


def list_groups(db_path: str, name: str | None = None) -> list[dict]:
    """Groups with their members, exclusions and live grants, for ``access
    group list``. Hidden ``user:<name>`` groups are included: they hold the
    direct grants."""
    with get_conn(db_path) as conn:
        _apply_schema(conn)
        where, params = ("WHERE name = ?", (name,)) if name else ("", ())
        groups = conn.execute(
            f"SELECT name, description, builtin FROM groups {where} ORDER BY builtin DESC, name",
            params,
        ).fetchall()
        out = []
        for gname, desc, builtin in groups:
            members = [
                r[0]
                for r in conn.execute(
                    "SELECT username FROM group_members WHERE groupname = ? ORDER BY username",
                    (gname,),
                )
            ]
            excluded = [
                r[0]
                for r in conn.execute(
                    "SELECT username FROM group_exclusions WHERE groupname = ? ORDER BY username",
                    (gname,),
                )
            ]
            cols = [c.strip() for c in _GRANT_COLUMNS.split(",")]
            grants = [
                dict(zip(cols, r))
                for r in conn.execute(
                    f"SELECT {_GRANT_COLUMNS} FROM group_grants WHERE groupname = ? ORDER BY id",
                    (gname,),
                )
            ]
            out.append(
                {
                    "name": gname,
                    "description": desc,
                    "builtin": bool(builtin),
                    "members": members,
                    "excluded": excluded,
                    "grants": grants,
                }
            )
    return out


def matching_summaries(db_path: str, grant: Grant) -> int:
    """How many ``summaries`` rows *grant* matches. Zero means the grant shows
    its group nothing: a typo, a renamed target, or data not yet ingested."""
    clause, params = grant.sql()
    if clause == "0":
        return 0
    with get_conn(db_path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM summaries WHERE {clause}", params).fetchone()[0]


def dead_grants(db_path: str, user: str | None, normalize: Callable[[str], str] | None = None) -> list[Grant]:
    """The grants reaching *user* that match no observation."""
    return [g for g in load_access(db_path, user, normalize).grants if not matching_summaries(db_path, g)]


def load_grant(db_path: str, grant_id: int, normalize: Callable[[str], str] | None = None) -> Grant:
    """One live grant by id, its target resolved as :func:`load_access` does."""
    with get_conn(db_path) as conn:
        r = conn.execute(
            "SELECT id, groupname, proposal_id, instrument, target, date_from, date_to "
            "FROM group_grants WHERE id = ?",
            (grant_id,),
        ).fetchone()
        if r is None:
            raise KeyError(grant_id)
        objects = None
        if r[4] is not None:
            names = [o[0] for o in conn.execute("SELECT DISTINCT object FROM summaries")]
            objects = _resolve_objects([r[4]], names, normalize)[r[4]]
    return Grant(*r, objects=objects)
