"""Backfill product records for runs made before #208 (first PR of #228).

Runs launched before the record existed have none. For each job row this
rebuilds the record from what the job stored (author, settings, the light
curves a transit fit selected) and from the frames table for a light curve's
night. Where the inputs cannot be established exactly the record is still
written with ``inputs: null`` and a note, so the run is *known* to be
undetermined rather than merely absent, and stays admin-only once visibility
reads the record.

Not recoverable, so left undetermined:

* TTV fits. The submitted CSV holds only planet, epoch, tc and tc_unc; nothing
  in it names the transit fit a time came from, and matching times back to
  fits by value could attach the wrong night and over-expose it.
* Transit fits whose job did not record ``selected_csvs`` (the page then fit
  every light curve present at the time, which may differ from what is on disk
  now).
* Light curves whose night/target has no frame rows to resolve against.

Tool versions are left empty: the versions installed now are not the ones that
ran then, and recording them would be false provenance.

Idempotent: a run that already has a record is never touched.
"""

from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from muscat_db import product_record
from muscat_db.database import get_conn

logger = logging.getLogger(__name__)

JOB_LEVELS = {"photometry": "photometry", "transit_fit": "transit_fit", "ttv_fit": "ttv_fit"}


@dataclass
class BackfillStats:
    written: int = 0
    determined: int = 0
    undetermined: int = 0
    already: int = 0
    missing_dir: int = 0
    errors: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)

    def note(self, reason: str) -> None:
        self.by_reason[reason] = self.by_reason.get(reason, 0) + 1


def _iso(epoch: float | None) -> str | None:
    if not epoch:
        return None
    return datetime.datetime.fromtimestamp(float(epoch), datetime.timezone.utc).isoformat()


def _job_key_parts(key: str) -> tuple[str, str]:
    """``("photometry", "inst/date/target/run")`` from a persisted job key."""
    type_, _, rest = (key or "").partition(":")
    return type_, rest


def _run_dir(level: str, row: dict) -> Path | None:
    from muscat_db import photometry, transit_fit, ttv_fit

    inst, date, target = row["instrument"], row["obsdate"], row["target"]
    run_id = row.get("run_id") or ""
    try:
        if level == "photometry":
            return photometry.run_output_dir(inst, date, target, run_id or None)
        if level == "transit_fit":
            return transit_fit.fit_output_dir(inst, date, target, run_id or None)
        return ttv_fit.ttv_output_dir(target, row.get("run_name") or run_id)
    except ValueError:
        return None


def _params(row: dict) -> dict:
    try:
        data = json.loads(row.get("params") or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _photometry_record(db_path: str, row: dict, params: dict, created: str | None):
    inputs, note = product_record.frames_inputs(db_path, row["instrument"], row["obsdate"], row["target"])
    settings = {
        "options": params.get("options") or {},
        "test_run": bool(params.get("test_run", True)),
        "site": params.get("site", ""), "telescope": params.get("telescope", ""),
        "mode": params.get("mode", ""), "run_name": params.get("run_name", ""),
    }
    return inputs, settings, note


def _transit_record(row: dict, params: dict):
    from muscat_db import transit_fit
    from muscat_db.photometry import output_base

    settings = {"options": params.get("options") or {}, "test_run": bool(params.get("test_run", False))}
    selected = params.get("selected_csvs")
    if not isinstance(selected, list) or not selected:
        return None, settings, "job did not record which light curves were selected"
    inst, date, target = row["instrument"], row["obsdate"], row["target"]
    try:
        night = (output_base() / inst / date).resolve(strict=False)
        runs_root = night / "_runs" / transit_fit._target_dir_name(target)
    except ValueError:
        return None, settings, "invalid target"
    inputs = []
    for raw in selected:
        path = Path(str(raw)).resolve(strict=False)
        if path.parent != night and runs_root not in path.parents:
            return None, settings, f"selected light curve outside this night: {Path(str(raw)).name}"
        inputs.append(product_record.lightcurve_input(inst, date, target, path, runs_root))
    return inputs, settings, None


def backfill_products(db_path: str, *, dry_run: bool = False) -> BackfillStats:
    """Write a record for every job's run directory that lacks one."""
    stats = BackfillStats()
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "SELECT key, type, instrument, obsdate, target, run_id, run_name, "
            "user_name, params, started_at FROM jobs ORDER BY started_at"
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    for row in rows:
        level = JOB_LEVELS.get(row["type"])
        if level is None:
            continue
        rdir = _run_dir(level, row)
        if rdir is None or not rdir.is_dir():
            stats.missing_dir += 1
            continue
        if product_record.read_record(rdir) is not None:
            stats.already += 1
            continue
        params = _params(row)
        try:
            if level == "photometry":
                inputs, settings, note = _photometry_record(db_path, row, params, None)
            elif level == "transit_fit":
                inputs, settings, note = _transit_record(row, params)
            else:
                opts = params.get("options") or {}
                settings = {k: v for k, v in opts.items() if k not in ("csv_content", "ini_content", "inputs")}
                inputs, note = None, "TTV input CSV does not record which transit fits its times came from"
        except Exception:  # one bad row must not stop the sweep
            logger.warning("backfill failed for %s", row.get("key"), exc_info=True)
            stats.errors += 1
            continue
        record = product_record.build_record(
            level,
            author=row.get("user_name"),
            inputs=inputs,
            settings=settings,
            tool_versions={},
            backfilled=True,
            created_at=_iso(row.get("started_at")),
            note=note,
        )
        if inputs is None:
            stats.undetermined += 1
            stats.note(note or "undetermined")
        else:
            stats.determined += 1
        if dry_run:
            continue
        if product_record.write_record(rdir, record):
            stats.written += 1
        else:
            stats.errors += 1
    return stats
