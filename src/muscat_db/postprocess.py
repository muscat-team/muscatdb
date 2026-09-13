"""Synchronous post-processing of photometry run band lightcurves.

Invokes prose2's ``prose.scripts.postprocess_lightcurves`` on the selected
run directory with the prose interpreter, parses its JSON report, and returns
a normalized result to the photometry page.

* ``test`` previews the frames that the polynomial sigma-clip would reject and
  returns the outlier plot as a base64 data URL, so nothing extra is persisted
  into the run directory.
* ``apply`` overwrites each band CSV in place (exactly one version is then seen
  by transit fit) and regenerates the run's summary ``*_lightcurves.png``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess

from muscat_db.photometry import (
    _job_env,
    _prose_prefix,
    _read_run_meta,
    _POSTPROCESS_MODULE,
    prose_tmpdir,
    run_output_dir,
)

logger = logging.getLogger("muscat_db.postprocess")

_POSTPROCESS_TIMEOUT_S = int(os.environ.get("MUSCAT_POSTPROCESS_TIMEOUT_S", 120))
_TERMINAL_STATES = {"done", "error", "cancelled", "none"}
_MAX_PNG_BYTES = 16 * 1024 * 1024


class PostprocessError(ValueError):
    """Raised for invalid post-process requests (missing run, bad params)."""


def _run_context(inst: str, date: str, target: str, run_id: str) -> dict:
    rdir = run_output_dir(inst, date, target, run_id)
    if not rdir.is_dir():
        raise PostprocessError(f"no such run directory: {rdir}")
    meta = _read_run_meta(rdir)
    run_type = str(meta.get("run_type") or "full").lower()
    confmode = "full" if "full" in run_type else ("single" if run_type else "")
    return {
        "results_dir": str(rdir),
        "target": target or str(meta.get("target") or ""),
        "inst": inst,
        "date": date,
        "site": str(meta.get("site") or "") or None,
        "confmode": confmode,
        "telescope": str(meta.get("telescope") or "") or None,
    }


def _command(
    context: dict,
    *,
    sigma: float,
    degree: int,
    iterations: int,
    apply: bool,
    preview_path: str | None = None,
) -> list[str]:
    args = [
        *_prose_prefix(_POSTPROCESS_MODULE, console_script=None),
        context["results_dir"],
        "--sigma",
        str(sigma),
        "--degree",
        str(degree),
        "--iterations",
        str(iterations),
    ]
    if preview_path:
        args += ["--preview", preview_path]
    if apply:
        args += [
            "--apply",
            "--target",
            context["target"],
            "--inst",
            context["inst"],
            "--date",
            context["date"],
        ]
        if context["site"]:
            args += ["--site", context["site"]]
        if context["confmode"]:
            args += ["--confmode", context["confmode"]]
        if context["telescope"]:
            args += ["--telescope", context["telescope"]]
    return args


def _run_sync(args: list[str], timeout: int = _POSTPROCESS_TIMEOUT_S) -> dict:
    env = _job_env()
    env["MPLBACKEND"] = "Agg"  # headless matplotlib: savefig only, never a window
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=timeout,
            text=True,
        )
    except subprocess.TimeoutExpired:
        logger.error("postprocess timed out after %ss: %s", timeout, args)
        return {"ok": False, "error": f"postprocess timed out after {timeout}s"}
    except OSError as exc:
        logger.error("could not launch prose postprocess (%s): %s", exc, args)
        return {"ok": False, "error": f"could not launch prose: {exc}"}
    out = (proc.stdout or "").strip()
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    stderr = (proc.stderr or "").strip().splitlines()
    detail = stderr[-1] if stderr else out or "no output"
    logger.error("postprocess failed (rc=%s): %s", proc.returncode, detail)
    return {"ok": False, "error": detail}


def _normalize(report: dict, *, preview_png: str | None) -> dict:
    files = report.get("files") or []
    return {
        "ok": bool(report.get("ok")),
        "applied": bool(report.get("applied")),
        "sigma": report.get("sigma"),
        "degree": report.get("degree"),
        "iterations": report.get("iterations"),
        "n_files": report.get("n_files", len(files)),
        "files": files,
        "summary_png": report.get("summary_png"),
        "preview_png": preview_png,
    }


def _read_preview(path: str) -> str | None:
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size > _MAX_PNG_BYTES:
        logger.warning("postprocess preview too large (%s bytes); dropping", size)
        return None
    try:
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return f"data:image/png;base64,{data}"


def _preview_path() -> str:
    tmpdir = prose_tmpdir()
    os.makedirs(tmpdir, exist_ok=True)
    return os.path.join(tmpdir, "muscat_postprocess_preview.png")


def validate_params(sigma, degree, iterations) -> str | None:
    """Return an error string for out-of-range post-process parameters."""
    try:
        sigma_f = float(sigma)
    except (TypeError, ValueError):
        return "sigma must be a number"
    try:
        deg_i = int(degree)
    except (TypeError, ValueError):
        return "poly degree must be an integer"
    try:
        iter_i = int(iterations)
    except (TypeError, ValueError):
        return "iterations must be an integer"
    if not (sigma_f > 0) or sigma_f > 100:
        return "sigma must be between 0 and 100"
    if not (0 <= deg_i <= 6):
        return "poly degree must be between 0 and 6"
    if not (1 <= iter_i <= 50):
        return "iterations must be between 1 and 50"
    return None


def postprocess(
    inst: str,
    date: str,
    target: str,
    run_id: str,
    sigma: float,
    degree: int,
    iterations: int,
    *,
    apply: bool,
    allow_active_job: bool = False,
) -> dict:
    """Run a post-process pass on a run's band lightcurves and return results.

    ``apply=False`` is a dry-run preview (report + base64 outlier plot);
    ``apply=True`` overwrites the band CSVs in place and regenerates the
    summary lightcurve figure. A run with a live (running/pending/finalizing)
    job is refused for ``apply`` unless ``allow_active_job`` is set (tests).
    """
    err = validate_params(sigma, degree, iterations)
    if err:
        return {"ok": False, "error": err}
    try:
        context = _run_context(inst, date, target, run_id)
    except PostprocessError as exc:
        return {"ok": False, "error": str(exc)}

    preview_png = None
    if apply:
        if not allow_active_job:
            try:
                from muscat_db.photometry import job_status

                state = str(job_status(inst, date, target, run_id=run_id).get("state") or "")
            except Exception:  # noqa: BLE001 - best-effort guard, never block apply
                state = ""
            if state not in _TERMINAL_STATES:
                return {
                    "ok": False,
                    "error": (
                        f"cannot post-process while the run's job is {state!r}; "
                        "wait for it to finish"
                    ),
                }
        report = _run_sync(
            _command(
                context,
                sigma=sigma,
                degree=degree,
                iterations=iterations,
                apply=True,
            )
        )
    else:
        preview_path = _preview_path()
        report = _run_sync(
            _command(
                context,
                sigma=sigma,
                degree=degree,
                iterations=iterations,
                apply=False,
                preview_path=preview_path,
            )
        )
        preview_png = _read_preview(preview_path)

    result = _normalize(report, preview_png=preview_png)
    if not result.get("ok") and report.get("error"):
        result["error"] = report.get("error")
    return result