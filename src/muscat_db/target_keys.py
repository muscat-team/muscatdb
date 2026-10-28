"""Stable target keys for access grants (issue #228).

A target grant names a target, but frames carry the header ``object``, spelled
however the observer typed it (``TOI-1234``, ``TOI 01234.01``, ``toi1234 b``).
Both sides are reduced to a *target key* by :func:`target_key`, and a grant
matches the objects whose key equals its own.

* The key is :func:`muscat_db.target_names._normalize_target_name` **without**
  the ``target_overrides`` table. An admin rename on the target page regroups
  objects for display only; it never changes who can see them.
* Keys are stored: per object in ``object_keys`` and per grant in
  ``group_grants.target_key``. :func:`refresh` writes both from the current
  rule, so they always agree with each other, and reports every grant whose
  key it changed. A rule change therefore shows up as a list of re-keyed
  grants in the refresh report instead of data silently going dark.
* :data:`TARGET_KEY_VERSION` names the rule. ``tests/test_target_keys.py`` pins
  the keys of representative names, so a normalizer change that alters a key
  fails there until the version is bumped and the change acknowledged.

:func:`refresh` runs wherever ``summaries`` is written (``build_db``,
``ingest_date``) and from ``muscat-db access target-keys``. An object or grant
with no stored key yet (rows written before this table existed) is keyed with
the current rule when read, so it is never hidden for lack of a refresh.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from muscat_db.target_names import _normalize_target_name

logger = logging.getLogger(__name__)

# Bump when target_key() changes the key of any name, and update the pinned
# keys in tests/test_target_keys.py.
TARGET_KEY_VERSION = 2


def target_key(name: str | None) -> str:
    """The access key of a target or object name: the host-level normalized
    name, ignoring manual overrides. ``''`` for a blank name."""
    return _normalize_target_name(name or "") if (name or "").strip() else ""


def _missing(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc)
    return "no such table" in msg or "no such column" in msg


def object_key_map(conn: sqlite3.Connection) -> dict[str, str]:
    """``{object: key}`` for every distinct non-blank ``summaries.object``.

    Uses the stored key where there is one and the current rule otherwise, so
    an object written without a refresh still matches its grants."""
    try:
        rows = conn.execute(
            "SELECT s.object, k.target_key FROM "
            "(SELECT DISTINCT object FROM summaries) s "
            "LEFT JOIN object_keys k ON k.object = s.object"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if not _missing(exc):
            raise
        rows = [(r[0], None) for r in conn.execute("SELECT DISTINCT object FROM summaries")]
    return {obj: (key if key is not None else target_key(obj)) for obj, key in rows if obj}


def objects_by_key(conn: sqlite3.Connection) -> dict[str, frozenset[str]]:
    """The inverse of :func:`object_key_map`: ``{key: {object, ...}}``."""
    out: dict[str, set[str]] = {}
    for obj, key in object_key_map(conn).items():
        if key:
            out.setdefault(key, set()).add(obj)
    return {k: frozenset(v) for k, v in out.items()}


@dataclass(frozen=True)
class OverrideGap:
    """An object the target page shows under ``target`` (a name override) whose
    own key differs, so a grant on ``target`` leaves its ``rows`` out."""

    object: str
    target: str
    object_key: str
    target_key: str
    rows: int


def override_gaps(conn: sqlite3.Connection, target: str | None = None) -> list[OverrideGap]:
    """Observed objects overridden to a target with a different key.

    Grants ignore overrides by design (a display rename never changes who sees
    what), so these rows fail closed: a grant on the name the target page shows
    does not cover them. Listed so an admin sees the gap before granting. With
    *target*, only the overrides shown under that target's key.
    """
    try:
        overrides = conn.execute(
            "SELECT object, norm_name FROM target_overrides "
            "WHERE norm_name IS NOT NULL AND trim(norm_name) != ''"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if not _missing(exc):
            raise
        return []
    keys = object_key_map(conn)
    rows = dict(conn.execute("SELECT object, COUNT(*) FROM summaries GROUP BY object"))
    wanted = target_key(target) if target is not None else None
    gaps = []
    for obj, shown in sorted(overrides):
        if not rows.get(obj):
            continue
        shown_key = target_key(shown)
        obj_key = keys.get(obj, target_key(obj))
        if obj_key == shown_key or (wanted is not None and shown_key != wanted):
            continue
        gaps.append(OverrideGap(obj, shown, obj_key, shown_key, rows[obj]))
    return gaps


def grant_key(stored: str | None, target: str | None) -> str:
    """A grant's key: the stored one, else computed from its target."""
    return stored if stored is not None else target_key(target)


@dataclass
class RefreshReport:
    objects_added: int = 0
    objects_removed: int = 0
    # (object, old key, new key) for objects whose stored key changed
    objects_rekeyed: list[tuple[str, str, str]] = field(default_factory=list)
    # (grant id, group, target, old key or None, new key) for live grants
    grants_rekeyed: list[tuple[int, str, str, str | None, str]] = field(default_factory=list)
    # (grant id, group, target) for live target grants matching no object
    dead_grants: list[tuple[int, str, str]] = field(default_factory=list)

    @property
    def changes(self) -> int:
        return (
            self.objects_added
            + self.objects_removed
            + len(self.objects_rekeyed)
            + len(self.grants_rekeyed)
        )


def refresh(conn: sqlite3.Connection) -> RefreshReport:
    """Bring ``object_keys`` and every live grant's ``target_key`` up to the
    current rule, on *conn* (uncommitted). Idempotent; cheap (one normalizer
    call per distinct object, a few thousand on production)."""
    report = RefreshReport()
    stored = {
        r[0]: (r[1], r[2])
        for r in conn.execute("SELECT object, target_key, version FROM object_keys")
    }
    current = {
        r[0]: target_key(r[0])
        for r in conn.execute("SELECT DISTINCT object FROM summaries")
        if r[0]
    }
    upserts = []
    for obj, key in current.items():
        old = stored.get(obj)
        if old is None:
            report.objects_added += 1
        elif old != (key, TARGET_KEY_VERSION):
            if old[0] != key:
                report.objects_rekeyed.append((obj, old[0], key))
        else:
            continue
        upserts.append((obj, key, TARGET_KEY_VERSION))
    conn.executemany(
        "INSERT OR REPLACE INTO object_keys (object, target_key, version) VALUES (?, ?, ?)",
        upserts,
    )
    gone = [(obj,) for obj in stored if obj not in current]
    conn.executemany("DELETE FROM object_keys WHERE object = ?", gone)
    report.objects_removed = len(gone)

    keys = set(current.values())
    for gid, group, target, old_key, old_version in conn.execute(
        "SELECT id, groupname, target, target_key, target_key_version FROM group_grants "
        "WHERE target IS NOT NULL ORDER BY id"
    ).fetchall():
        key = target_key(target)
        if (old_key, old_version) != (key, TARGET_KEY_VERSION):
            conn.execute(
                "UPDATE group_grants SET target_key = ?, target_key_version = ? WHERE id = ?",
                (key, TARGET_KEY_VERSION, gid),
            )
            if old_key != key:
                report.grants_rekeyed.append((gid, group, target, old_key, key))
        if key not in keys:
            report.dead_grants.append((gid, group, target))

    # A key that changed under a grant that already had one is a rule change
    # reaching live access: say so where the nightly log will show it.
    for gid, group, target, old, new in report.grants_rekeyed:
        if old is not None:
            logger.warning(
                "target_keys: grant #%s (%s, target=%s) re-keyed %s -> %s",
                gid, group, target, old, new,
            )
    return report
