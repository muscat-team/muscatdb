"""Provenance record for every data product (issue #208, first PR of #228).

A light curve, a transit fit and a TTV fit are all products: each is made from
inputs with settings by an author. One record per product, stored next to its
outputs, captures that. This module only *writes and reads* the record. Nothing
here decides visibility: the access filters still use the night's proposals,
and a later PR computes visibility from ``inputs`` with one rule for every level.

Record layout (``_product.json`` in the run directory)::

    {
      "schema": 1,
      "level": "photometry" | "transit_fit" | "ttv_fit",
      "author": "<nginx user>" | "",
      "created_at": "<iso utc>",
      "backfilled": false,
      "inputs": [ {kind: "frames", ...}, {kind: "product", ...}, ... ] | null,
      "settings": {...},
      "tool_versions": {"muscat_db": ..., "prose": ..., "timer": ..., "harmonic": ...}
    }

``inputs`` is an exact manifest. ``null`` means *undetermined*: the record is
there but the inputs could not be established, so the product must be treated
as admin-only until someone can supply them. An empty list is a real answer
("no inputs") and is not used as a stand-in for unknown.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable

from muscat_db.database import get_conn

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
RECORD_NAME = "_product.json"
LEVELS = ("photometry", "transit_fit", "ttv_fit")

# Upper bound on manifest entries accepted from a client. A TTV fit combines
# points from many nights, but nothing legitimate approaches this.
MAX_INPUTS = 5000

_INPUT_KINDS = ("frames", "product", "manual")


def _compact(name: str) -> str:
    return (name or "").replace(" ", "").casefold()


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def build_record(
    level: str,
    *,
    author: str | None,
    inputs: list[dict] | None,
    settings: dict[str, Any] | None = None,
    tool_versions: dict[str, str | None] | None = None,
    backfilled: bool = False,
    created_at: str | None = None,
    note: str | None = None,
) -> dict:
    """Assemble a record. ``inputs=None`` marks the inputs undetermined, and
    *note* says why (kept only when set)."""
    if level not in LEVELS:
        raise ValueError(f"unknown product level {level!r}")
    record = {
        "schema": SCHEMA_VERSION,
        "level": level,
        "author": (author or "").strip(),
        "created_at": created_at or now_iso(),
        "backfilled": bool(backfilled),
        "inputs": None if inputs is None else list(inputs),
        "settings": dict(settings or {}),
        "tool_versions": dict(tool_versions or {}),
    }
    if note:
        record["note"] = note
    return record


def write_record(rdir: Path, record: dict) -> bool:
    """Write *record* into *rdir* atomically. Best effort: returns False and
    logs on failure, because losing provenance must not fail a reduction."""
    path = Path(rdir) / RECORD_NAME
    tmp = path.with_name(RECORD_NAME + ".tmp")
    try:
        tmp.write_text(json.dumps(record, sort_keys=True, indent=2, default=str))
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        logger.warning("could not write product record in %s", rdir, exc_info=True)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


def read_record(rdir: Path) -> dict | None:
    """Return the record in *rdir*, or None if absent or unreadable.

    A record this code does not understand (wrong schema, wrong shape) is
    treated as absent so a caller falls back to the no-manifest path rather
    than trusting it.
    """
    try:
        data = json.loads((Path(rdir) / RECORD_NAME).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
        return None
    if data.get("level") not in LEVELS:
        return None
    inputs = data.get("inputs")
    if inputs is not None and not isinstance(inputs, list):
        return None
    return data


def frame_input(
    db_path: str,
    instrument: str,
    obsdate: str,
    target: str,
    normalize: Callable[[str], str] | None = None,
) -> dict:
    """Frames input for one (instrument, night, target), from the frames table.

    ``resolved`` is False when no frame matched the name: the entry is still
    recorded, with no proposals, so the gap is visible instead of silent.
    """
    try:
        with get_conn(db_path) as conn:
            rows = conn.execute(
                "SELECT object, proposal_id, COUNT(*) FROM frames "
                "WHERE instrument = ? AND obsdate = ? GROUP BY object, proposal_id",
                (instrument, obsdate),
            ).fetchall()
    except sqlite3.Error:
        logger.warning("frame lookup failed for %s/%s/%s", instrument, obsdate, target, exc_info=True)
        rows = []
    want = _compact(target)
    matched = [(p, n) for obj, p, n in rows if _compact(obj or "") == want]
    if not matched and normalize is not None:
        norm = normalize(target)
        matched = [(p, n) for obj, p, n in rows if normalize(obj or "") == norm]
    return {
        "kind": "frames",
        "instrument": instrument,
        "obsdate": obsdate,
        "target": target,
        "proposal_ids": sorted({p for p, _ in matched if p}),
        "n_frames": sum(n for _, n in matched),
        "resolved": bool(matched),
    }


def lightcurve_input(instrument: str, obsdate: str, target: str, csv_path: Path, runs_root: Path | None) -> dict:
    """Product input for one photometry light-curve CSV used by a transit fit.

    The run id is the CSV's parent directory name when it sits under the
    target's ``_runs`` directory; a legacy CSV written straight into the
    night's directory has ``run_id == ""``.
    """
    run_id = ""
    if runs_root is not None:
        try:
            rel = Path(csv_path).resolve(strict=False).relative_to(runs_root.resolve(strict=False))
            run_id = rel.parts[0] if len(rel.parts) > 1 else ""
        except ValueError:
            run_id = ""
    return {
        "kind": "product",
        "level": "photometry",
        "instrument": instrument,
        "obsdate": obsdate,
        "target": target,
        "run_id": run_id,
        "file": Path(csv_path).name,
    }


def ttv_inputs_from_points(points: Any) -> tuple[list[dict], str | None]:
    """Validate the O-C points a client submits for a TTV fit.

    Returns ``(inputs, error)``. Each checked point becomes a ``product`` input
    naming the transit fit it came from, or a ``manual`` input when the point
    was typed in (it has no frames behind it). Duplicates collapse, since many
    epochs come from one fit.
    """
    if points is None:
        return [], None
    if not isinstance(points, list):
        return [], "inputs must be a list"
    if len(points) > MAX_INPUTS:
        return [], f"too many inputs (max {MAX_INPUTS})"
    out: list[dict] = []
    seen: set[tuple] = set()
    for i, p in enumerate(points):
        if not isinstance(p, dict):
            return [], f"inputs[{i}] must be an object"
        kind = p.get("kind")
        if kind not in ("product", "manual"):
            return [], f"inputs[{i}].kind must be one of product, manual"
        if kind == "manual":
            entry = {"kind": "manual", "note": str(p.get("note") or "")[:500]}
        else:
            fields = {}
            for key in ("instrument", "date", "target"):
                val = p.get(key)
                if not isinstance(val, str) or not val.strip():
                    return [], f"inputs[{i}].{key} is required"
                fields[key] = val.strip()
            run_id = p.get("run_id", "")
            if not isinstance(run_id, str):
                return [], f"inputs[{i}].run_id must be a string"
            entry = {
                "kind": "product",
                "level": "transit_fit",
                "instrument": fields["instrument"],
                "obsdate": fields["date"],
                "target": fields["target"],
                "run_id": run_id.strip(),
            }
        ident = tuple(sorted(entry.items()))
        if ident in seen:
            continue
        seen.add(ident)
        out.append(entry)
    return out, None
