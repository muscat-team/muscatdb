"""Migrate group visibility to explicit membership (issue #354).

Before: ``everyone`` implicitly holds every user not listed in
``group_exclusions`` (a "scoped viewer" is one excluded from it), and the
anonymous viewer of the public static site is in it too.

After: a user is in exactly the groups listed in ``group_members``.

* ``muscatjp`` (the team) gets every user not excluded from ``everyone`` and a
  copy of every ``everyone`` grant; its chat room is ``chat:public``;
* ``public`` (built in, no members) gets another copy, for the anonymous
  viewer; its existence is what switches the evaluator to explicit membership
  (:func:`muscat_db.access_groups.explicit_membership`);
* ``lcokey`` gets the members of ``unc`` plus every user with a direct
  ``KEY2026B-001`` grant, and that proposal; the direct and ``unc`` copies of
  the plain proposal grant are revoked (kept in ``group_grants_revoked``).
  ``unc`` keeps its other grants;
* ``everyone`` and its exclusions are removed, its grants revoked.

The migration runs in one transaction and compares, for every user and for
the anonymous viewer, the set of visible ``summaries`` rows before and after.
Any difference rolls it back, with or without ``apply``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field

from muscat_db import access_groups as ag
from muscat_db.database import _apply_schema, get_conn

KEY_GROUP = "lcokey"
KEY_PROPOSAL = "KEY2026B-001"
KEY_SOURCE_GROUP = "unc"
MIGRATED_BY = "migration #354"

_COPY_COLUMNS = (
    "proposal_id, instrument, target, date_from, date_to, note, granted_by, granted_at, "
    "target_key, target_key_version"
)


class MigrationRefused(RuntimeError):
    """The database is not in a state this migration can start from."""


@dataclass
class ExplicitReport:
    team_members: list[str] = field(default_factory=list)
    copied_grants: int = 0
    key_members: list[str] = field(default_factory=list)
    key_grants_revoked: list[str] = field(default_factory=list)  # groups that lost the plain grant
    removed_exclusions: list[str] = field(default_factory=list)
    viewers_checked: int = 0
    # viewer ("" = anonymous) -> (rows visible before, rows visible after)
    differences: dict[str, tuple[int, int]] = field(default_factory=dict)
    applied: bool = False


def _visible_rows(conn: sqlite3.Connection, user: str | None) -> frozenset[int]:
    clause, params = ag.load_access_conn(conn, user).sql_visible("s")
    return frozenset(
        r[0] for r in conn.execute(f"SELECT s.rowid FROM summaries s WHERE {clause}", params)
    )


def _effective_rules(conn: sqlite3.Connection, user: str | None) -> tuple:
    """What a viewer's grants say, independent of which group carries them:
    admin flag plus the distinct rules (with resolved target spellings). Equal
    rules give equal visibility on every table, frames included, not only the
    summaries rows :func:`_visible_rows` samples."""
    acc = ag.load_access_conn(conn, user)
    rules = {
        (g.proposal_id and g.proposal_id.upper(), g.instrument and g.instrument.lower(),
         g.target_key, g.date_from, g.date_to, g.objects)
        for g in acc.grants
    }
    return (acc.is_admin, frozenset(rules))


def _viewer_state(conn: sqlite3.Connection, user: str | None) -> tuple:
    return (_visible_rows(conn, user), _effective_rules(conn, user))


def _viewers(conn: sqlite3.Connection, extra_users: Iterable[str]) -> list[str | None]:
    """Every account the old model could have shown data to: users rows,
    extra (htpasswd) accounts, anyone named in a group or a ``user:`` group,
    and anonymous."""
    names = {r[0] for r in conn.execute("SELECT username FROM users")}
    names.update(r[0] for r in conn.execute("SELECT DISTINCT username FROM group_members"))
    names.update(
        r[0][len(ag.USER_PREFIX):]
        for r in conn.execute("SELECT name FROM groups WHERE name LIKE ?", (ag.USER_PREFIX + "%",))
    )
    names.update(u for u in extra_users if u)
    return [None, *sorted(n for n in names if n)]


def _copy_grants(conn: sqlite3.Connection, source: str, target: str) -> int:
    try:
        return conn.execute(
            f"INSERT INTO group_grants (groupname, {_COPY_COLUMNS}) "
            f"SELECT ?, {_COPY_COLUMNS} FROM group_grants WHERE groupname = ?",
            (target, source),
        ).rowcount
    except sqlite3.IntegrityError as exc:
        raise MigrationRefused(f"could not copy {source}'s grants to {target}: {exc}") from exc


def _has_plain_grant(conn: sqlite3.Connection, group: str, proposal_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM group_grants WHERE groupname = ? AND proposal_id = ? COLLATE NOCASE "
        "AND instrument IS NULL AND target IS NULL AND date_from IS NULL AND date_to IS NULL",
        (group, proposal_id),
    ).fetchone() is not None


def _move_everyone(conn: sqlite3.Connection, viewers: list[str | None], report: ExplicitReport) -> None:
    excluded = {
        r[0] for r in conn.execute(
            "SELECT username FROM group_exclusions WHERE groupname = ?", (ag.EVERYONE,)
        )
    }
    ag.ensure_group(conn, ag.TEAM_GROUP, "the MuSCAT team (was everyone, #354)")
    ag.ensure_group(conn, ag.PUBLIC, "the anonymous viewer: the public static site", builtin=True)
    for user in viewers:
        if user and user not in excluded:
            ag.add_member(conn, ag.TEAM_GROUP, user, MIGRATED_BY)
            report.team_members.append(user)
    report.copied_grants = _copy_grants(conn, ag.EVERYONE, ag.TEAM_GROUP)
    _copy_grants(conn, ag.EVERYONE, ag.PUBLIC)
    ag._archive_grants(conn, "groupname = ?", (ag.EVERYONE,), MIGRATED_BY)
    report.removed_exclusions = sorted(excluded)
    conn.execute("DELETE FROM group_exclusions WHERE groupname = ?", (ag.EVERYONE,))
    conn.execute("DELETE FROM group_members WHERE groupname = ?", (ag.EVERYONE,))
    conn.execute("DELETE FROM groups WHERE name = ?", (ag.EVERYONE,))


def _make_key_group(conn: sqlite3.Connection, report: ExplicitReport) -> None:
    holders = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT groupname FROM group_grants WHERE groupname LIKE ? "
            "AND proposal_id = ? COLLATE NOCASE AND instrument IS NULL AND target IS NULL "
            "AND date_from IS NULL AND date_to IS NULL ORDER BY groupname",
            (ag.USER_PREFIX + "%", KEY_PROPOSAL),
        )
    ]
    members = {h[len(ag.USER_PREFIX):] for h in holders}
    members.update(
        r[0] for r in conn.execute(
            "SELECT username FROM group_members WHERE groupname = ?", (KEY_SOURCE_GROUP,)
        )
    )
    if not members:
        return
    ag.ensure_group(conn, KEY_GROUP, f"{KEY_PROPOSAL} collaborators (#354)")
    for user in sorted(members):
        ag.add_member(conn, KEY_GROUP, user, MIGRATED_BY)
    report.key_members = sorted(members)
    ag.add_grant(conn, KEY_GROUP, proposal_id=KEY_PROPOSAL, note="#354", granted_by=MIGRATED_BY)
    if _has_plain_grant(conn, KEY_SOURCE_GROUP, KEY_PROPOSAL):
        holders.append(KEY_SOURCE_GROUP)
    for group in holders:
        if ag.remove_proposal_grant(conn, group, KEY_PROPOSAL, MIGRATED_BY):
            report.key_grants_revoked.append(group)


def migrate(conn: sqlite3.Connection, extra_users: Iterable[str] = ()) -> ExplicitReport:
    """Run the migration on *conn* without committing; the caller commits or
    rolls back. *extra_users* are accounts with no ``users`` row yet (htpasswd
    entries that never signed in), which would otherwise lose the team's data."""
    if ag.explicit_membership(conn):
        raise MigrationRefused("membership is already explicit (the 'public' group exists)")
    if not ag.is_active(conn):
        raise MigrationRefused("the group model is not active; run `access groups-seed` first")
    if ag.group_exists(conn, ag.PUBLIC):
        raise MigrationRefused(f"an ordinary group named '{ag.PUBLIC}' exists; rename it first")
    team_rows = conn.execute(
        "SELECT COUNT(*) FROM group_grants WHERE groupname = ?", (ag.TEAM_GROUP,)
    ).fetchone()[0]
    if team_rows:
        raise MigrationRefused(
            f"group '{ag.TEAM_GROUP}' already holds {team_rows} grant(s); the migration "
            "expects to create it from everyone"
        )
    report = ExplicitReport()
    viewers = _viewers(conn, extra_users)
    before = {v: _viewer_state(conn, v) for v in viewers}
    _move_everyone(conn, viewers, report)
    _make_key_group(conn, report)
    for viewer in viewers:
        after = _viewer_state(conn, viewer)
        if after != before[viewer]:
            report.differences[viewer or ""] = (len(before[viewer][0]), len(after[0]))
    report.viewers_checked = len(viewers)
    return report


def run(db_path: str, extra_users: Iterable[str] = (), apply: bool = False) -> ExplicitReport:
    """Dry run by default. Commits only when *apply* is set and no viewer's
    visible rows changed."""
    with get_conn(db_path) as conn:
        _apply_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            report = migrate(conn, extra_users)
        except BaseException:
            conn.rollback()
            raise
        if apply and not report.differences:
            conn.commit()
            report.applied = True
        else:
            conn.rollback()
    return report
