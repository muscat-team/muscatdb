"""How old is the last ``build-db`` (issue #237).

A refused or failed nightly build leaves ``db_meta.last_build_at`` unchanged,
so its age is the one signal that stays true however the job died, including
when cron never ran it.
"""

from __future__ import annotations

import datetime
import os
import sqlite3

LAST_BUILD_KEY = "last_build_at"


def last_build_at(db_path: str | os.PathLike) -> datetime.datetime | None:
    """The ``last_build_at`` stamp of *db_path*, or ``None`` if the file, the
    table or the key is missing or unreadable. Never creates the file."""
    path = os.fspath(db_path)
    if not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        try:
            row = conn.execute(
                "SELECT value FROM db_meta WHERE key = ?", (LAST_BUILD_KEY,)
            ).fetchone()
        finally:
            conn.close()
        return datetime.datetime.fromisoformat(row[0]) if row else None
    except (sqlite3.Error, ValueError, TypeError):
        return None


def build_age(db_path: str | os.PathLike,
              now: datetime.datetime | None = None) -> datetime.timedelta | None:
    """Time since the last build, or ``None`` when it cannot be determined."""
    stamp = last_build_at(db_path)
    if stamp is None:
        return None
    return (now or datetime.datetime.now()) - stamp
