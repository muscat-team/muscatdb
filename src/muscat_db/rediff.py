"""Re-difference a finished run against chosen comparison stars and aperture.

A thin bridge to prose2's ``prose.scripts.rediff_lightcurves``, which reads
the run's ``*_flux_datacube.npz`` (written under ``--save_flux_datacube``).
All photometry lives in prose2; this module validates the request, runs the
script in the prose interpreter and manages the undo snapshot.

* ``info`` lists, per band, the selectable star IDs, the default comparisons
  and weights, and the aperture radii, plus this run's re-diff state.
* ``test`` compares a custom set with the default light curve (scatter
  metrics and an overlay plot). Nothing in the run is written.
* ``apply`` adopts the custom set: prose2 rewrites Flux/Err of the selected
  bands' CSVs and the summary figure. The first apply snapshots the pipeline's
  files into ``.rediff_backup``; ``restore_rediff`` puts them back.

Layering with the post-process clip: a re-diff replaces the base light curve
and needs the CSV rows to be the datacube's frames, so it is refused while a
clip is active. A clip then applies on top of whichever base is current, and
undoing the clip returns to that base.
"""

from __future__ import annotations

import fcntl
import json
import logging
import math
import re
import shutil
import tempfile
from pathlib import Path

from muscat_db import postprocess as pp
from muscat_db.photometry import _REDIFF_MODULE, _prose_prefix

logger = logging.getLogger("muscat_db.rediff")

_BACKUP_DIRNAME = ".rediff_backup"
# What was adopted; beside the snapshot (not in it), so restore never copies
# it into the run, and only trusted while the snapshot exists.
_APPLIED_NAME = ".rediff_applied.json"
_BAND_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
_MAX_COMPARISONS = 200
_MAX_STAR_ID = 100_000
_MAX_APERTURE = 1_000
_MAX_OFFSET = 1.0  # relative flux; light curves are normalised to ~1
_CLIP_ACTIVE = (
    "a post-process clip is active on this run; use Restore pre-clip first, "
    "then re-difference"
)


def parse_selections(raw) -> tuple[list[dict], str | None]:
    """Validate ``[{band, comparisons, aperture}, ...]`` from the request."""
    if not isinstance(raw, list) or not raw:
        return [], "select comparison stars for at least one band"
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            return [], "each selection needs a band, comparisons and an aperture"
        band = str(item.get("band") or "")
        if not _BAND_RE.match(band) or band in seen:
            return [], f"invalid or repeated band {band!r}"
        comps = item.get("comparisons")
        if not isinstance(comps, list) or not comps or len(comps) > _MAX_COMPARISONS:
            return [], f"band {band}: select 1 to {_MAX_COMPARISONS} comparison stars"
        try:
            ids = [int(c) for c in comps]
            aperture = int(item.get("aperture"))
        except (TypeError, ValueError):
            return [], f"band {band}: star IDs and the aperture must be integers"
        if any(not 0 <= i <= _MAX_STAR_ID for i in ids) or not 0 <= aperture <= _MAX_APERTURE:
            return [], f"band {band}: star ID or aperture out of range"
        if len(set(ids)) != len(ids):
            return [], f"band {band}: duplicate comparison star IDs"
        seen.add(band)
        out.append({"band": band, "comparisons": ids, "aperture": aperture})
    return out, None


def parse_offset(raw) -> tuple[float | None, str | None]:
    """The preview's vertical offset; ``None``/``""`` means automatic."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, "offset must be a number"
    if not (math.isfinite(value) and 0 <= value <= _MAX_OFFSET):
        return None, f"offset must be between 0 and {_MAX_OFFSET:g} (relative flux)"
    return value, None


def _select_args(selections: list[dict]) -> list[str]:
    args: list[str] = []
    for s in selections:
        ids = ",".join(str(i) for i in s["comparisons"])
        args += ["--select", f"{s['band']}={ids}@{s['aperture']}"]
    return args


def _apply_args(context: dict) -> list[str]:
    args = ["--apply", "--target", context["target"], "--inst", context["inst"],
            "--date", context["date"]]
    for key in ("site", "confmode", "telescope"):
        if context[key]:
            args += [f"--{key}", context[key]]
    return args


def _run(context: dict, extra: list[str]) -> dict:
    return pp._run_sync(
        [*_prose_prefix(_REDIFF_MODULE, console_script=None), context["results_dir"], *extra]
    )


def _context(inst: str, date: str, target: str, run_id: str) -> tuple[dict | None, str | None]:
    try:
        return pp._run_context(inst, date, target, run_id), None
    except pp.PostprocessError as exc:
        return None, str(exc)


def _read_applied(rdir: Path) -> dict:
    if not pp._backup_dir(rdir, _BACKUP_DIRNAME).is_dir():
        return {}
    try:
        data = json.loads((rdir / _APPLIED_NAME).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _state(rdir: Path) -> dict:
    return {
        "restorable": pp._backup_dir(rdir, _BACKUP_DIRNAME).is_dir(),
        "clip_active": pp._backup_dir(rdir).is_dir(),
        "applied_selection": _read_applied(rdir),
    }


def info(inst: str, date: str, target: str, run_id: str) -> dict:
    context, err = _context(inst, date, target, run_id)
    if err:
        return {"ok": False, "error": err}
    report = _run(context, ["--info"])
    if not report.get("ok"):
        return {"ok": False, "error": report.get("error") or "could not read the flux datacube"}
    return {"ok": True, "bands": report.get("bands") or {}, **_state(Path(context["results_dir"]))}


def _public_bands(report: dict) -> dict:
    """The script's per-band default/custom comparison, without internals."""
    bands = report.get("bands") or {}
    return {
        b: {k: v for k, v in (r or {}).items() if k in ("default", "custom")}
        for b, r in bands.items()
    }


def _equal_baselines(context: dict, bands: dict) -> str | None:
    """Compare weighting at the pipeline aperture, batching bands into one prose2 call."""
    selections = []
    for band, part in bands.items():
        default, custom = part["default"], part["custom"]
        if default["weighting"] == "equal":
            part["default_equal"] = default
        elif (default["aperture"] == custom["aperture"]
              and set(default["comparisons"]) == set(custom["comparisons"])):
            part["default_equal"] = custom
        else:
            selections.append({"band": band, **default})
    if selections:
        baseline = _run(context, _select_args(selections))
        for selection in selections:
            band = selection["band"]
            equal = (baseline.get("bands") or {}).get(band, {}).get("custom")
            if not baseline.get("ok") or not equal:
                return baseline.get("error") or f"missing equal-weight comparison for {band}"
            bands[band]["default_equal"] = equal
    return None


def _finish_publish(rdir: Path) -> None:
    """Finish an interrupted publication before another Apply or Restore."""
    backup = pp._backup_dir(rdir, _BACKUP_DIRNAME)
    pending = backup / "publishing"
    if not pending.is_dir():
        return
    journal = json.loads((pending / "publish.json").read_text())
    before, after = journal["before"], journal["after"]
    current = pp._fingerprint(rdir)
    for name in current.keys() | before.keys() | after.keys():
        if current.get(name) not in (before.get(name), after.get(name)):
            raise OSError(f"{name} changed outside the interrupted Apply; backup preserved")
    for source in pp._tracked_files(pending):
        if before.get(source.name) != after[source.name]:
            source.replace(rdir / source.name)
    # Keep the journal until both the manifest and adopted selection are durable
    # across a process exit. Replaying it after either write is idempotent.
    pp._write_manifest(rdir, _BACKUP_DIRNAME)
    state = pending / "selection.tmp"
    state.write_text(json.dumps(journal["selection"]))
    state.replace(rdir / _APPLIED_NAME)
    finished = pending.rename(backup / "published")
    shutil.rmtree(finished)


def _apply(context: dict, args: list[str]) -> dict:
    rdir = Path(context["results_dir"])
    _finish_publish(rdir)
    backup = pp._backup_dir(rdir, _BACKUP_DIRNAME)
    if backup.exists() and pp._read_manifest(rdir, _BACKUP_DIRNAME) != pp._fingerprint(rdir):
        (rdir / _APPLIED_NAME).unlink(missing_ok=True)
    pp._snapshot_unclipped(rdir, _BACKUP_DIRNAME)
    shutil.rmtree(backup / "published", ignore_errors=True)
    before = pp._fingerprint(rdir)
    with tempfile.TemporaryDirectory(dir=backup) as tmp:
        stage = Path(tmp)
        for source in pp._tracked_files(rdir):
            shutil.copy2(source, stage / source.name)
        for cube in rdir.glob("*_flux_datacube.npz"):
            (stage / cube.name).symlink_to(cube.resolve())
        report = _run({**context, "results_dir": str(stage)}, [*args, *_apply_args(context)])
        if not report.get("ok") or not report.get("applied"):
            return report
        selection = _read_applied(rdir)
        selection.update({b: part["custom"] for b, part in _public_bands(report).items()})
        journal = {"before": before, "after": pp._fingerprint(stage), "selection": selection}
        (stage / "publish.json").write_text(json.dumps(journal))
        stage.rename(backup / "publishing")
        _finish_publish(rdir)
    return report


def rediff(
    inst: str,
    date: str,
    target: str,
    run_id: str,
    selections,
    *,
    apply: bool,
    allow_active_job: bool = False,
    offset=None,
) -> dict:
    """Compare (``apply=False``) or adopt (``apply=True``) a custom set.

    ``offset`` sets the preview's gap between the curves (relative flux;
    ``None``/``""`` lets prose2 choose per band); apply ignores it.
    """
    sel, err = parse_selections(selections)
    if err:
        return {"ok": False, "error": err}
    offset_val, err = parse_offset(offset)
    if err:
        return {"ok": False, "error": err}
    context, err = _context(inst, date, target, run_id)
    if err:
        return {"ok": False, "error": err}
    rdir = Path(context["results_dir"])
    args = _select_args(sel)
    preview_png = None
    if apply:
        if pp._backup_dir(rdir).is_dir():
            return {"ok": False, "error": _CLIP_ACTIVE, **_state(rdir)}
        if not allow_active_job:
            err = pp._active_job_error(inst, date, target, run_id)
            if err:
                return {"ok": False, "error": err, **_state(rdir)}
        try:
            with (rdir / ".rediff.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                report = _apply(context, args)
        except OSError as exc:
            logger.exception("could not apply re-diff in %s", rdir)
            return {"ok": False, "error": str(exc), **_state(rdir)}
    else:
        preview_path = pp._preview_path()
        extra = [] if offset_val is None else ["--offset", repr(offset_val)]
        report = _run(context, [*args, "--preview", preview_path, *extra])
        preview_png = pp._read_preview(preview_path)
    if not report.get("ok"):
        return {"ok": False, "error": report.get("error") or "re-differencing failed",
                **_state(rdir)}
    bands = _public_bands(report)
    if not apply:
        error = _equal_baselines(context, bands)
        if error:
            return {"ok": False, "error": error, **_state(rdir)}
    return {
        "ok": True,
        "applied": bool(report.get("applied")),
        "bands": bands,
        "summary_png": report.get("summary_png"),
        "preview_png": preview_png,
        "plot_offset": report.get("plot_offset"),
        **_state(rdir),
    }


def restore_rediff(
    inst: str, date: str, target: str, run_id: str, *, allow_active_job: bool = False
) -> dict:
    """Put back the pipeline's own band CSVs and summary figure."""
    context, err = _context(inst, date, target, run_id)
    if err:
        return {"ok": False, "error": err}
    rdir = Path(context["results_dir"])
    if not pp._backup_dir(rdir, _BACKUP_DIRNAME).is_dir():
        return {"ok": False, "error": "no re-diff backup for this run; nothing to restore"}
    if pp._backup_dir(rdir).is_dir():
        return {"ok": False, "error": _CLIP_ACTIVE.replace("re-difference", "restore")}
    if not allow_active_job:
        err = pp._active_job_error(inst, date, target, run_id)
        if err:
            return {"ok": False, "error": err}
    try:
        with (rdir / ".rediff.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _finish_publish(rdir)
            result = pp._restore_backup(rdir, _BACKUP_DIRNAME, "re-diff")
            if result.get("ok"):
                (rdir / _APPLIED_NAME).unlink(missing_ok=True)
            return result
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
