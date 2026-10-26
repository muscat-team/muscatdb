"""Confirm before a launch overwrites an existing run (issue #317).

A launch under a run name that is already taken used to replace that run with
no prompt: the "Overwrite existing products" option is on by default and the
run name is remembered across targets, so a forgotten run name silently
replaced another run, the viewer's own or someone else's.

The server now refuses such a launch with :class:`RunConflict` (HTTP 409) unless
the request carries the conflict's :func:`confirm_token`. The page shows the
conflict as a popup offering a free run name (:func:`suggest_run_name`) or the
overwrite. The token names the run, its owner and the version of its record,
so a confirmation does not cover the run after anyone has re-run it.

A run the viewer may not see is only reported as a taken name with a free one
to use instead (:func:`hidden_conflict_payload`): no owner, date or token, and
no overwrite. Who may overwrite a visible run is still
:func:`muscat_db.run_ownership.may_modify_run` and is checked next: a viewer
who may not change the run gets the 403, not a popup offering to overwrite it.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from muscat_db.jobs import _RUN_NAME_MAX, slugify_run_name

# How many numbered names to try before giving up on a suggestion.
_MAX_SUGGESTIONS = 99
_NUMBERED = re.compile(r"^(?P<stem>.+)_(?P<n>\d+)$")


class RunConflict(Exception):
    """A launch would replace an existing run and was not confirmed."""

    def __init__(self, payload: dict):
        super().__init__(payload.get("error", "run exists"))
        self.payload = payload


def _same_user(a: str | None, b: str | None) -> bool:
    a, b = (a or "").strip(), (b or "").strip()
    return bool(a) and a.casefold() == b.casefold()


def confirm_token(run_id: str, owner: str, version: str = "") -> str:
    """What a request echoes back to confirm overwriting *run_id* owned by
    *owner* as it stood at *version* (see :func:`record_version`)."""
    return f"{run_id}:{(owner or '').strip().casefold()}:{(version or '').strip()}"


def record_version(record: dict | None) -> str:
    """When the run's record was last written: ``modified_at`` after a re-run by
    someone else, else ``created_at``. A re-run by anyone changes it, which the
    owner alone does not, since a re-run by someone else keeps the author."""
    record = record or {}
    return str(record.get("modified_at") or record.get("created_at") or "")


def run_name_of(level: str, run_id: str) -> str:
    """The run-name part of a run id. A photometry or transit-fit id ends with
    the run-name slug after any site/telescope/mode prefix; a TTV run id is the
    name itself."""
    if level == "ttv_fit":
        return slugify_run_name(run_id)
    return (run_id or "").rsplit("-", 1)[-1]


def renamed_run_id(level: str, run_id: str, run_name: str) -> str:
    """*run_id* with its run-name part replaced by *run_name*."""
    if level == "ttv_fit":
        return run_name
    head, sep, _ = (run_id or "").rpartition("-")
    return f"{head}{sep}{slugify_run_name(run_name)}"


def _with_suffix(stem: str, suffix: str) -> str:
    return f"{stem[: _RUN_NAME_MAX - len(suffix)].rstrip('_')}{suffix}"


def _candidates(name: str, viewer: str, own: bool):
    if own or not viewer:
        match = _NUMBERED.match(name)
        stem, start = (match["stem"], int(match["n"]) + 1) if match else (name, 2)
    else:
        stem, start = _with_suffix(name, f"_{slugify_run_name(viewer)}"), 2
        yield stem
    for n in range(start, start + _MAX_SUGGESTIONS):
        yield _with_suffix(stem, f"_{n}")


def suggest_run_name(
    level: str, run_id: str, viewer: str | None, owner: str | None, is_taken: Callable[[str], bool]
) -> str | None:
    """A run name close to the taken one that no run uses yet, or None.

    Someone else's run suggests ``<name>_<viewer>``; the viewer's own suggests
    the next number (``default`` -> ``default_2``, ``default_2`` -> ``default_3``).
    *is_taken* answers for a candidate run id.
    """
    name = run_name_of(level, run_id)
    own = _same_user(viewer, owner)
    for candidate in _candidates(name, (viewer or "").strip(), own):
        if not is_taken(renamed_run_id(level, run_id, candidate)):
            return candidate
    return None


def conflict_payload(
    *,
    level: str,
    run_id: str,
    viewer: str | None,
    owner: str | None,
    created_at: str,
    suggested: str | None,
    version: str = "",
) -> dict:
    """The 409 body: what the page needs to ask, and the token to confirm."""
    owner = (owner or "").strip()
    own = _same_user(viewer, owner)
    name = run_name_of(level, run_id)
    whose = "your" if own else (f"{owner}'s" if owner else "an unowned")
    error = (
        f"A run named {name!r} already exists ({whose} run). Launching would overwrite it: "
        f"confirm the overwrite, or choose a new run name."
    )
    return {
        "ok": False,
        "conflict": "run_exists",
        "error": error,
        "level": level,
        "run_id": run_id,
        "run_name": name,
        "owner": owner,
        "own_run": own,
        "created_at": created_at or "",
        "suggested_run_name": suggested,
        "can_overwrite": True,
        "confirm_token": confirm_token(run_id, owner, version),
    }


def hidden_conflict_payload(*, level: str, run_id: str, suggested: str | None) -> dict:
    """The 409 body for a taken run name whose run the viewer may not see: the
    name is taken and here is a free one. Nothing about the run itself, and no
    token, so it cannot be overwritten from here."""
    name = run_name_of(level, run_id)
    return {
        "ok": False,
        "conflict": "run_exists",
        "error": f"The run name {name!r} is already taken: choose a new run name.",
        "level": level,
        "run_id": run_id,
        "run_name": name,
        "owner": "",
        "own_run": False,
        "created_at": "",
        "suggested_run_name": suggested,
        "can_overwrite": False,
    }
