"""Backfill product records for runs made before #208 (first PR of #228).

Runs launched before the record existed have none. For each job row this
rebuilds the record from what the job stored (author, settings, the light
curves a transit fit selected) and from the frames table for a light curve's
night. Where the inputs cannot be established exactly the record is still
written with ``inputs: null`` and a note, so the run is *known* to be
undetermined rather than merely absent, and stays admin-only once visibility
reads the record.

Run directories with no job row (everything made before the ``jobs`` table
existed) are then found by walking the output trees, because a run that no
record describes is hidden from every non-admin once visibility reads the
record. Their author is unknown, so they stay hidden from a viewer whose access
is denied for any input. A photometry run's inputs are the night's frames; a
transit fit's are the light curves its ``fit.yaml`` names, located by file name
under the same night's photometry outputs. A name found in several photometry
runs lists all of them, so the fit is visible only to a viewer who may see
every candidate; a name found nowhere leaves the fit undetermined.

Not recoverable, so left undetermined:

* TTV fits. The submitted CSV holds only planet, epoch, tc and tc_unc; nothing
  in it names the transit fit a time came from, and matching times back to
  fits by value could attach the wrong night and over-expose it.
* Transit fits whose job did not record ``selected_csvs`` (the page then fit
  every light curve present at the time, which may differ from what is on disk
  now).
* Light curves whose night/target has no frame rows to resolve against.
* Transit fits whose ``fit.yaml`` is unreadable or names a light curve that is
  no longer on disk.

Tool versions are left empty: the versions installed now are not the ones that
ran then, and recording them would be false provenance.

Idempotent: a run that already has a record is never touched.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
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
    from_disk: int = 0
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
    inputs, note = product_record.frames_inputs(
        db_path, row["instrument"], row["obsdate"], row["target"],
        product_record.target_normalizer(db_path),
    )
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

    seen: set[Path] = set()
    for row in rows:
        level = JOB_LEVELS.get(row["type"])
        if level is None:
            continue
        rdir = _run_dir(level, row)
        if rdir is None or not rdir.is_dir():
            stats.missing_dir += 1
            continue
        seen.add(rdir.resolve(strict=False))
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
        _store(stats, rdir, level, row.get("user_name"), inputs, settings, note,
               _iso(row.get("started_at")), dry_run)
    _backfill_disk_runs(db_path, stats, seen, dry_run)
    return stats


def _store(stats, rdir, level, author, inputs, settings, note, created_at, dry_run) -> None:
    """Build one backfilled record and write it, updating *stats*."""
    record = product_record.build_record(
        level,
        author=author,
        inputs=inputs,
        settings=settings,
        tool_versions={},
        backfilled=True,
        created_at=created_at,
        note=note,
    )
    if inputs is None:
        stats.undetermined += 1
        stats.note(note or "undetermined")
    else:
        stats.determined += 1
    if dry_run:
        return
    if product_record.write_record(rdir, record):
        stats.written += 1
    else:
        stats.errors += 1


def _subdirs(path: Path) -> list[Path]:
    """Real (non-symlink) child directories, sorted; empty if unreadable."""
    try:
        return sorted(p for p in path.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        return []


def _disk_photometry_runs(base: Path):
    """``(inst, date, target, run_id, rdir)`` for each named run under *base*."""
    for inst in _subdirs(base):
        for night in _subdirs(inst):
            for target in _subdirs(night / "_runs"):
                for run in _subdirs(target):
                    yield inst.name, night.name, target.name, run.name, run


def _disk_fit_runs(base: Path):
    """``(inst, date, target, run_id, rdir)`` for each transit-fit run on disk.

    A run is the target directory itself (legacy, ``run_id == ""``) or one of its
    children, and is recognised by the ``fit.yaml`` the page writes there. The
    ``out`` directory timer writes below a run also holds a ``fit.yaml`` copy
    and is not a run.
    """
    for inst in _subdirs(base):
        for night in _subdirs(inst):
            for target in _subdirs(night):
                if (target / "fit.yaml").is_file():
                    yield inst.name, night.name, target.name, "", target
                for run in _subdirs(target):
                    if run.name != "out" and (run / "fit.yaml").is_file():
                        yield inst.name, night.name, target.name, run.name, run


def _fit_yaml_files(rdir: Path) -> list[str] | None:
    """Light-curve file names a run's ``fit.yaml`` lists, or ``None`` if unreadable."""
    import yaml

    try:
        data = yaml.safe_load((rdir / "fit.yaml").read_text())
        entries = data["data"].values()
        names = [e["file"] for e in entries]
    except (OSError, yaml.YAMLError, KeyError, TypeError, AttributeError):
        return None
    if not names or not all(isinstance(n, str) and n and os.path.basename(n) == n for n in names):
        return None
    return names


def _disk_transit_record(inst: str, date: str, target: str, rdir: Path):
    """``(inputs, settings, note)`` for a transit fit found only on disk."""
    from muscat_db.photometry import output_base

    names = _fit_yaml_files(rdir)
    if names is None:
        return None, {}, "fit.yaml is unreadable or lists no light curves"
    night = output_base() / inst / date
    runs_root = night / "_runs" / target
    inputs, missing, ambiguous = [], [], False
    for name in names:
        found = [p for p in [night / name, *(r / name for r in _subdirs(runs_root))] if p.is_file()]
        if not found:
            missing.append(name)
        ambiguous = ambiguous or len(found) > 1
        inputs += [product_record.lightcurve_input(inst, date, target, p, runs_root) for p in found]
    if missing:
        return None, {}, f"light curve no longer on disk: {', '.join(missing)}"
    note = "light curve name found in several runs; all are listed" if ambiguous else None
    return inputs, {}, note


def _backfill_disk_runs(db_path: str, stats: BackfillStats, seen: set[Path], dry_run: bool) -> None:
    """Record every photometry and transit-fit run directory the job pass missed."""
    from muscat_db import photometry, transit_fit

    normalize = product_record.target_normalizer(db_path)
    passes = (
        ("photometry", _disk_photometry_runs(photometry.output_base())),
        ("transit_fit", _disk_fit_runs(transit_fit.fit_output_dir("_", "_", "_").parents[2])),
    )
    for level, runs in passes:
        for inst, date, target, run_id, rdir in runs:
            if rdir.resolve(strict=False) in seen:
                continue
            if product_record.read_record(rdir) is not None:
                stats.already += 1
                continue
            try:
                if level == "photometry":
                    inputs, note = product_record.frames_inputs(db_path, inst, date, target, normalize)
                    settings = {}
                else:
                    inputs, settings, note = _disk_transit_record(inst, date, target, rdir)
            except Exception:  # one bad directory must not stop the sweep
                logger.warning("disk backfill failed for %s", rdir, exc_info=True)
                stats.errors += 1
                continue
            stats.from_disk += 1
            _store(stats, rdir, level, None, inputs, settings, note, None, dry_run)
