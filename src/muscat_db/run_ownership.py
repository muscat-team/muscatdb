"""Who may delete or cancel a run (issue #272).

Proposal visibility (:mod:`muscat_db.product_visibility`) says who may *see* a
run. Changing one is narrower:

* an admin may;
* the run's owner may: the author in its ``_product.json``, else the
  ``user_name`` of its job row;
* a user who shares an explicit group with the owner may. ``everyone``,
  ``admin`` and the hidden ``user:<name>`` groups do not count, or every pair of
  users would share one (issue #228);
* a run with no owner (a backfilled or older run) may be changed by any viewer
  who is in some explicit group;
* with no identity at all (no authenticating proxy, single-user use) only a run
  with no owner may be changed, as for :func:`muscat_db.web._may_delete_job`.
"""

from __future__ import annotations

import logging
import sqlite3

from muscat_db import product_record
from muscat_db.access_groups import ADMIN, EVERYONE, USER_PREFIX
from muscat_db.database import get_conn
from muscat_db.product_visibility import default_job_owner, default_run_dir

logger = logging.getLogger(__name__)

# What the job store and the Jobs page call a job started without a user.
_ANONYMOUS = "anonymous"


def run_owner(level: str, inst: str, date: str, target: str, run_id: str = "") -> str:
    """The run's owner, or ``""`` when nothing names one.

    A legacy photometry run (no run id) shares the night's directory with every
    other target of the night, so its record cannot name an author: only the
    job row can.
    """
    if level != "photometry" or run_id:
        try:
            record = product_record.read_record(default_run_dir(level, inst, date, target, run_id))
        except ValueError:
            record = None
        if record is not None and record.get("level") == level:
            author = str(record.get("author") or "").strip()
            if author:
                return author
    return (default_job_owner(level, inst, date, target, run_id) or "").strip()


def _explicit_groups(db_path: str, user: str) -> frozenset[str]:
    """Names of the groups *user* was put in by hand (not ``everyone``, ``admin``
    or a ``user:<name>`` group). Missing group tables mean no groups."""
    try:
        with get_conn(db_path) as conn:
            rows = conn.execute(
                "SELECT m.groupname FROM group_members m "
                "LEFT JOIN groups g ON g.name = m.groupname "
                "WHERE lower(m.username) = ? AND COALESCE(g.builtin, 0) = 0",
                (user.casefold(),),
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return frozenset()
        raise
    names = {r[0].casefold() for r in rows}
    return frozenset(n for n in names if n not in (EVERYONE, ADMIN) and not n.startswith(USER_PREFIX))


def viewer_groups(db_path: str, viewer: str | None) -> frozenset[str]:
    """The explicit groups that count for :func:`may_modify_run`."""
    viewer = (viewer or "").strip()
    return _explicit_groups(db_path, viewer) if viewer else frozenset()


def _and_list(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def refusal_message(
    viewer: str | None, groups: frozenset[str], owner: str, action: str, offer_new_run: bool
) -> str:
    """Why :func:`may_modify_run` refused *viewer*, and what they can do.

    It names who the viewer was taken to be, the groups that counted and the
    run's owner, so a refusal on a server reached without the authenticating
    proxy (no identity at all) does not read like a missing group membership.
    *action* is the verb refused ("overwrite", "cancel"); *offer_new_run* adds
    the way round for a launch, which a new run name always has.
    """
    viewer = (viewer or "").strip()
    owner = (owner or "").strip()
    if owner.casefold() == _ANONYMOUS:
        owner = ""
    new_run = " To start a separate run instead, choose a new run name." if offer_new_run else ""
    contact = " Please contact an admin if this is a mistake."
    if not viewer:
        whose = f"This run belongs to {owner}." if owner else "This run has no recorded owner."
        return (
            f"You are not logged in, so you may only {action} runs that have no owner. {whose} "
            f"Open the site through the address that asks you to log in, not the server's port "
            f"directly, to {action} it.{new_run}"
        )
    if groups:
        names = sorted(groups)
        who = f"You are logged in as {viewer} under the {_and_list(names)} group{'s' if len(names) > 1 else ''}."
        if not owner:
            # Unreachable through may_modify_run (a group member may change an
            # unowned run) but kept total so a caller never gets a blank reason.
            return f"{who} You do not have permission to {action} this run.{new_run}{contact}"
        theirs = "that group" if len(names) == 1 else "any of those groups"
        return (
            f"{who} This run belongs to {owner}, who is not in {theirs}. "
            f"You do not have permission to {action} it.{new_run}{contact}"
        )
    who = f"You are logged in as {viewer}, who is not in any group."
    if not owner:
        return (
            f"{who} This run has no recorded owner, so only an admin or a member of a group "
            f"may {action} it. You do not have permission to {action} it; "
            f"ask an admin to add you to a group.{new_run}"
        )
    return (
        f"{who} This run belongs to {owner}, so only {owner}, an admin, or someone in a group "
        f"with {owner} may {action} it. You do not have permission to {action} it.{new_run}{contact}"
    )


def may_modify_run(db_path: str, viewer: str | None, is_admin: bool, owner: str) -> bool:
    """Whether *viewer* may delete or cancel a run owned by *owner* (``""`` if
    unowned). The caller has already checked that the viewer may see the run."""
    if is_admin:
        return True
    viewer = (viewer or "").strip()
    owner = (owner or "").strip()
    if owner.casefold() == _ANONYMOUS:
        owner = ""
    if not viewer:
        return not owner
    if owner and owner.casefold() == viewer.casefold():
        return True
    mine = _explicit_groups(db_path, viewer)
    if not mine:
        return False
    return not owner or bool(mine & _explicit_groups(db_path, owner))
