"""Refresh the Targets page for jobs a worker finished (#298).

A pipeline refreshes a target's persisted ``phot_status``/``fit_status`` the
moment one of its jobs reaches a terminal state
(``database.refresh_target_status``). A worker on a shared control plane may not
open the SQLite catalog, because it reaches ``muscat.db`` over NFS, so it skips
that refresh. The web process, which has the catalog on local disk, does it
instead: each reconciliation pass it compares the control plane's active jobs
with the previous pass, and refreshes the target of every worker job that has
since left the active set in a terminal state.

A job that is queued and finishes entirely between two passes (about 2 s) is
never seen active, and jobs that finish while the web process is down are not
seen either. The nightly ``build_db`` recomputes every target's status, so
those wait at most a day.
"""

from __future__ import annotations

import logging

from muscat_db import database

logger = logging.getLogger(__name__)

TERMINAL_STATES = frozenset({"done", "error", "cancelled"})


class FinishedJobWatcher:
    """Tracks the active job set across passes and reports finished worker jobs."""

    def __init__(self) -> None:
        self._active: set[str] | None = None
        self._unread: set[str] = set()

    def poll(self, store) -> set[str]:
        """Targets of worker jobs that were active last pass and are now terminal.

        The first pass only records the active set. A store error is logged and
        reported as nothing finished: a failed listing leaves the previous set
        in place, and a job whose row could not be read is read again next pass.
        """
        try:
            current = {row["key"] for row in store.active()}
        except Exception:
            logger.warning("could not list active jobs to refresh target status", exc_info=True)
            return set()
        previous, self._active = self._active, current
        if previous is None:
            return set()
        left, self._unread = (previous - current) | (self._unread - current), set()
        targets: set[str] = set()
        for key in left:
            try:
                row = store.get(key)
            except Exception:
                logger.warning("could not read finished job %s", key, exc_info=True)
                self._unread.add(key)
                continue
            # The web process refreshes its own jobs inline as they finish.
            if row and row.get("owner") == "worker" and row.get("state") in TERMINAL_STATES:
                targets.add(row["target"])
        return targets


def refresh_worker_finished_targets(watcher: FinishedJobWatcher, store) -> None:
    """Refresh the persisted status of every target *watcher* reports finished."""
    for target in sorted(watcher.poll(store)):
        database.refresh_target_status(target)
