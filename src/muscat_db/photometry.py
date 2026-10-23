"""Helpers for the photometry page: locate prose pipeline outputs, serve
artifacts safely, and launch reductions as background jobs.

The prose pipeline (``../ext_tools/prose2``,
``python -m prose.scripts.run_photometry``) writes a flat directory of products
per instrument/date under ``$MUSCAT_PROSE_DIR/<inst>/<date>/`` with filenames

    {target}_{inst}_{band}_{date}_ref.png        # per-band reference image
    {target}_{inst}_{band}_{date}_apertures.png  # per-band aperture overlay
    {target}_{inst}_{band}_{date}_cutouts.png    # per-band star cutout montage
    {target}_{inst}_{band}_{date}_alignment.png  # per-band alignment diagnostic
    {target}_{inst}_{band}_{date}.gif            # per-band animation
    {target}_{inst}_{band}_{date}.csv            # per-band light curve
    {target}_{inst}_{date}_lightcurves.png       # multi-band summary
    {target}_{inst}_{date}_covariates.png        # multi-band summary
    {target}_{inst}_{date}_stacks.png            # multi-band summary
    {target}_{inst}_{date}.npz                   # data archive
    {iso-timestamp}.log                          # pipeline log

This module never trusts user input for filesystem access: see
``safe_artifact_path``.
"""

from __future__ import annotations

from collections.abc import Callable
import csv as _csv
import datetime
import hashlib
import json
import logging
import math
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from muscat_db import jobs, database
from muscat_db.job_store import current_instance_id, current_owner, get_job_store
from muscat_db.job_limits import JobLimitExceeded, claim_caps, claim_caps_for_row, enqueue_caps, limit_refusal, params_with_running_cap
from muscat_db.ref_time import is_valid_ref_time
from muscat_db.instruments import INSTRUMENTS
from muscat_db.cache import register_cache
from muscat_db.band_utils import DEFAULT_BANDS, NARROW_BANDS, _FILTER_BAND_ALIAS, bands_from_filters  # noqa: F401
from muscat_db.catalog import _resolve_archive_coords, resolve_lco_key_project_name

logger = logging.getLogger(__name__)

# --------------------------- configuration ---------------------------
# All paths are env-overridable so the page works in dev and on the server.
_HERE = Path(__file__).resolve().parent          # .../src/muscat_db
_REPO_ROOT = _HERE.parent.parent                 # .../muscat-db
_DEFAULT_PROSE_PROJECT = _REPO_ROOT.parent / "ext_tools" / "prose2"
_DEFAULT_OUTPUT_BASE = Path.home() / "ql" / "prose"
# Temp dir for spawned pipeline jobs. The root filesystem holding /tmp is small
# and prone to filling up (astropy's mmap probe and FITS I/O write ephemerals
# there), so default to a user-owned directory outside /tmp. Override
# MUSCAT_TMPDIR with another host-local path when needed.
_DEFAULT_TMPDIR = str(Path.home() / "temp")

# Default values for every optional run_photometry argument the form exposes.
# Kept here so the template, normalizer, and command builder share one source.
RUN_DEFAULTS: dict = {
    "run_name": "default",
    "bands": DEFAULT_BANDS,
    "ref_band": "",            # "" -> per-band self-reference (pipeline default)
    "refid": "",               # "" -> pipeline default (0 / middle frame, or the
                               # quality mode's persistent-catalog anchor)
    "ref_select": "position",  # position (legacy) | quality (local temporal persistence)
                               # | time (per-band frame closest in time to ref_time)
    "ref_time": "",            # ref_select=time: HH:MM[:SS] UT | ISO UTC | JD
    "ref_select_top_k": 5,     # local catalogs registered when ref_select=quality
    "aper_radii": "",          # "MIN,MAX,DR"; "" -> Gaia heuristic
    "annulus": "",             # "RIN,ROUT"; required with aper_radii
    "aper_unit": "pix",        # pix | fwhm (only applies with aper_radii)
    "make_gif": True,
    "plot_gaia_sources": True,
    "cmap": "gray",            # colormap for image-display plots (--cmap)
    "use_barycorrpy": False,
    "test_run_frames": 10,
    "min_star_separation": 10.0,
    "max_num_stars": 20,
    "n_stars_align": "",       # "" -> same as max_num_stars
    "cutout_size": 35,
    "display_stack_nframes": 15,  # own frames aligned+median-stacked into the
                                  # display reference for the ref/apertures/
                                  # cutouts/stacks plots of non-reference bands
                                  # under --ref_band (1 -> single frame)
    "ccd_trim": "",            # "Y,X"; "" -> no trim (pipeline default)
    "edge_margin": "",         # px from CCD edge to exclude comps; "" -> auto (half cutout), 0 -> off
    "bin_size_minutes": 10.0,
    "nan_imputation_method": "linear",
    "target_id": "",           # "" -> auto
    "comparison_ids": "",      # "" -> auto, or "1,2,3"
    "avoid_comparison_ids": "",  # "" -> none; "1,2,3" -> --avoid_cids (requires --ref_band)
    "avoid_nearby_star_mode": "auto",  # off | auto | custom
    "avoid_nearby_star": "",  # arcsec; used when avoid_nearby_star_mode == "custom"
    "target_coord": "",        # "" -> resolve via MAST; "RA Dec" -> bypass name resolution
    "gif_stride": 100,
    "overwrite": True,
    "sig_bkg": None,           # None -> sigma clipping disabled for bkg axis
    "sig_fwhm": None,          # None -> sigma clipping disabled for fwhm axis
    "sig_dx": None,            # None -> sigma clipping disabled for dx axis
    "sig_dy": None,            # None -> sigma clipping disabled for dy axis
    "sig_flux": None,          # None -> flux-residual sigma clipping disabled
                               # (a poly is fit to the raw target flux; see flux_poly_deg)
    "flux_poly_deg": 2,        # degree of the polynomial fitted to the raw target
                               # flux before residual sigma clipping (used with sig_flux)
    "min_star_area": 20,
    "wcs_method": "astrometry.net",
    "centroid_method": "auto",  # auto | quad | com (mirrors prose2's --centroid_method)
    "calib_dir": "",
    "site": "",                # multi-site instruments only: "" -> all sites; else one
                               # of MULTISITE_SITES[inst]
    "mode": "",                # mode-capable instruments only: "" -> all modes; else
                               # one of MULTISITE_MODES[inst]
    "telescope": "",           # multi-site instruments only: "" -> all telescopes; else
                               # a TELESCOP header value, e.g. "1m0-05"/"0m4-06"
                               # (open-ended: LCO's fleet changes over time, unlike the
                               # fixed site lists).
    "exclude_after_jd": "",    # "" -> none; comma-separated JD value(s) -> --exclude_after_jd
    "exclude_before_jd": "",   # "" -> none; comma-separated JD value(s) -> --exclude_before_jd
                               # Pairs positionally with exclude_after_jd when both are set
                               # (equal-length lists required; validated by run_photometry.py,
                               # not here -- see normalize_run_options).
}

# Defaults for the synchronous post-processing pass exposed on the photometry
# page (templates/photometry.html post-process card). Unlike RUN_DEFAULTS these
# never reach run_photometry; they are consumed only by /api/photometry/postprocess
# via muscat_db.postprocess. Iterations is fixed at the prose2 pipeline default.
POSTPROCESS_DEFAULTS: dict = {
    "post_sigma": 5.0,
    "post_poly_deg": 2,
    "post_exclude_before_jd": "",
    "post_exclude_after_jd": "",
}

# LCO instruments deployed across multiple sites/telescope units, needing
# --site/--telescope disambiguation (unlike the single-site muscat/muscat2/
# muscat3/muscat4). Mirrored from prose2's run_photometry.py.
# sinistro: lsc/cpt/coj/tfn/elp (no unit at ogg).
# sbig/qhy600 (0.4m network): all 6 LCO sites, including ogg.
MULTISITE_INSTRUMENTS = ("sinistro", "sbig", "qhy600")
MULTISITE_SITES: dict[str, tuple[str, ...]] = {
    "sinistro": ("lsc", "cpt", "coj", "tfn", "elp"),
    "sbig": ("lsc", "cpt", "coj", "tfn", "elp", "ogg"),
    "qhy600": ("lsc", "cpt", "coj", "tfn", "elp", "ogg"),
}
# Instruments with multiple readout/config modes needing --mode
# disambiguation. sbig is deliberately absent (single CONFMODE="default" on
# real archive headers). qhy600's "central30x30" is confirmed verbatim on a
# real archived header (coj0m416-sq36-20260804-0098-e91).
MULTISITE_MODES: dict[str, tuple[str, ...]] = {
    "sinistro": ("central_2k_2x2", "full_frame"),
    "qhy600": ("central30x30", "full_frame"),
}
# Backwards-compatible aliases (still imported by transit_fit.py and others).
SINISTRO_SITES = MULTISITE_SITES["sinistro"]
SINISTRO_MODES = MULTISITE_MODES["sinistro"]

# TELESCOP-value prefix per multi-site instrument, e.g. "1m0-05"/"0m4-06".
# Used to reconstruct the canonical TELESCOP value from a filename's compact
# "telNN" token (see _telescope_token_to_value).
_TELESCOPE_PREFIX: dict[str, str] = {
    "sinistro": "1m0",
    "sbig": "0m4",
    "qhy600": "0m4",
}

# --telescope is open-ended (no whitelist): format-validated only, against the
# TELESCOP header shape ("1m0-05") or a lowercase alnum/hyphen token generally.
TELESCOPE_RE = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")

# Colormaps offered for image-display plots (--cmap). run_photometry.py accepts
# any matplotlib name, so this curated set is the GUI/validation allowlist and
# must stay in sync with the <select> options in templates/photometry.html.
CMAP_CHOICES = (
    "gray", "gray_r",
    "coolwarm", "RdBu", "RdGy", "PiYG", "PRGn", "BrBG", "PuOr",
    "RdYlBu", "RdYlGn", "Spectral",
)

# Centroid refinement strategies exposed by prose2's --centroid_method: adaptive
# quadratic/COM selection ("auto"), quadratic only ("quad"), or center-of-mass
# only ("com"). Must stay in sync with the <select> options in
# templates/photometry.html.
CENTROID_METHODS = ("auto", "quad", "com")

# NaN imputation strategies exposed by prose2's --nan-imputation-method.
NAN_IMPUTATION_METHODS = (
    "none",
    "mean",
    "median",
    "linear",
    "spline",
    "forward_fill",
)


ALLOWED_EXTS = {".png", ".gif", ".csv", ".npz", ".log", ".txt"}
_RUN_LOG_NAME = "_webrun.log"
_RUNS_DIR_NAME = "_runs"
_RUN_META_NAME = "_webrun_meta.json"
# Written next to the run's own output, mirroring transit_fit's timer-fit.pid
# and ttv_fit's harmonic.pid: lets orphan reconciliation tell whether the
# detached subprocess itself is still alive after this process's own in-memory
# tracker is gone (a --reload restart, a crash), via jobs.pid_file_process_alive.
_PID_FILE_NAME = "photometry.pid"
_CONDA_ENV_DEFAULT = "prose"   # prose deps live in a conda env named "prose"
_MODULE = "prose.scripts.run_photometry"
_POSTPROCESS_MODULE = "prose.scripts.postprocess_lightcurves"

_DATE_RE = re.compile(r"^\d{6}$")
# A served filename is a single path segment of safe characters only.
_NAME_RE = re.compile(r"^[A-Za-z0-9._+:\-()]+$")

# Summary (multi-band) plot suffixes -> short key used by the template.
_SUMMARY_SUFFIX = {
    "_lightcurves.png": "lightcurves",
    "_raw_flux.png": "raw_flux",
    "_covariates.png": "covariates",
    "_systematics.png": "covariates",   # backward compat with old pipeline
    "_stacks.png": "stacks",
    "_nearby_stars.csv": "nearby_stars",
}
# Per-band product suffixes -> short key.
_BAND_SUFFIX = {
    "_ref.png": "ref",
    "_apertures.png": "apertures",
    "_cutouts.png": "cutouts",
    "_alignment.png": "alignment",
    ".gif": "gif",
    ".csv": "csv",
}


def output_base() -> Path:
    # ``.expanduser()`` for parity with the timer dir getter (transit_fit.py), so
    # a ``~``-prefixed MUSCAT_PROSE_DIR resolves instead of creating a literal '~'.
    return Path(os.environ.get("MUSCAT_PROSE_DIR", _DEFAULT_OUTPUT_BASE)).expanduser()


def prose_project_dir() -> Path:
    return Path(os.environ.get("MUSCAT_PROSE_PROJECT", str(_DEFAULT_PROSE_PROJECT)))


def prose_tmpdir() -> str:
    """Temp dir handed to spawned pipeline jobs (overridable via MUSCAT_TMPDIR)."""
    return os.environ.get("MUSCAT_TMPDIR", _DEFAULT_TMPDIR)


def _job_env() -> dict[str, str]:
    """Environment for spawned pipeline subprocesses.

    Routes all ephemeral files (TMPDIR/TMP/TEMP) to a raid-backed directory so
    jobs never trip over a full root ``/tmp``. The dir is created if missing;
    if that fails we fall back to the inherited environment rather than block
    the launch. Also applies jobs.core_pinning_env()'s optional thread-count
    caps (architecture issue #51 "Core Pinning"), a no-op unless
    MUSCAT_JOB_MAX_THREADS is configured.
    """
    env = {**os.environ, "PYTHONUNBUFFERED": "1", **jobs.core_pinning_env()}
    tmpdir = prose_tmpdir()
    try:
        Path(tmpdir).mkdir(parents=True, exist_ok=True)
        env["TMPDIR"] = tmpdir
        env["TMP"] = tmpdir
        env["TEMP"] = tmpdir
    except OSError as exc:
        logger.warning("could not prepare TMPDIR %s (%s); using inherited temp", tmpdir, exc)
    return env


def prose_python() -> str | None:
    """Explicit interpreter for prose, if configured (highest priority)."""
    return os.environ.get("MUSCAT_PROSE_PYTHON") or None


def prose_conda_env() -> str:
    """Name of the conda env that supplies prose's dependencies."""
    return os.environ.get("MUSCAT_PROSE_CONDA_ENV", _CONDA_ENV_DEFAULT)


def timer_conda_env() -> str:
    """Name of the conda env that supplies timer's dependencies."""
    return os.environ.get("MUSCAT_TIMER_CONDA_ENV", "timer")


def harmonic_conda_env() -> str:
    """Name of the conda env that supplies harmonic's dependencies."""
    return os.environ.get("MUSCAT_HARMONIC_CONDA_ENV", "harmonic")


def _conda_env_python(env: str) -> str | None:
    """Resolve ``envs/<env>/bin/python`` from the active conda install or the
    usual install locations. Returns the interpreter path, or ``None``."""
    bases: list[Path] = []
    exe = os.environ.get("CONDA_EXE")
    if exe:
        # .../miniconda3/bin/conda -> .../miniconda3
        bases.append(Path(exe).resolve().parent.parent)
    home = Path.home()
    bases += [
        home / "miniconda3", home / "anaconda3", home / "miniforge3",
        home / ".conda", Path("/opt/conda"),
    ]
    seen: set[Path] = set()
    for base in bases:
        if base in seen:
            continue
        seen.add(base)
        cand = base / "envs" / env / "bin" / "python"
        if cand.is_file():
            return str(cand)
    return None


_PROSE_VERSION: str | None = None
_PROSE_VERSION_FAILED_AT: float | None = None
_PROSE_VERSION_LOCK = threading.Lock()
_PROSE_DISTRIBUTIONS = ("prose2", "prose")
_PROSE_VERSION_TIMEOUT_S = 5
_PROSE_VERSION_RETRY_S = 300.0


def prose_version() -> str:
    """Installed prose version in the prose conda env.

    A success is cached for the life of the process. A failure is remembered
    only for ``_PROSE_VERSION_RETRY_S``: a missing env or one slow lookup must
    not label every later run ``unknown`` until the next restart, and must not
    make every launch in that window wait on the subprocess again.
    """
    global _PROSE_VERSION, _PROSE_VERSION_FAILED_AT
    if _PROSE_VERSION is not None:
        return _PROSE_VERSION
    now = time.monotonic()
    if _PROSE_VERSION_FAILED_AT is not None and now - _PROSE_VERSION_FAILED_AT < _PROSE_VERSION_RETRY_S:
        return "unknown"
    with _PROSE_VERSION_LOCK:
        if _PROSE_VERSION is not None:
            return _PROSE_VERSION
        found = None
        prose_py = _conda_env_python(prose_conda_env())
        if prose_py is not None:
            from muscat_db.engine_identity import version_probe
            code = version_probe(_PROSE_DISTRIBUTIONS)
            try:
                result = subprocess.run(
                    [prose_py, "-c", code], capture_output=True, text=True,
                    timeout=_PROSE_VERSION_TIMEOUT_S,
                )
                if result.returncode == 0 and result.stdout.strip():
                    found = result.stdout.strip()
            except Exception:  # a version lookup must never fail a launch
                logger.debug("prose version lookup failed", exc_info=True)
        if found is None:
            _PROSE_VERSION_FAILED_AT = now
            return "unknown"
        _PROSE_VERSION = found
    return _PROSE_VERSION


def _lookup_frames_inputs(inst: str, date: str, target: str) -> tuple[list[dict] | None, str | None]:
    """A run's frame inputs, read from the catalog (``product_record.frames_inputs``)."""
    from muscat_db import product_record

    db = database.db_path()
    return product_record.frames_inputs(db, inst, date, target, product_record.target_normalizer(db))


def frames_inputs_param(inst: str, date: str, target: str) -> dict | None:
    """The frame inputs a queued row carries to whichever worker launches it.

    Computed where the row is queued (the web process on ut2, which has the
    catalog on local disk), so the worker never opens the catalog (#298).
    ``None`` when the lookup fails; the worker then leaves the record for
    ``backfill-products`` instead of guessing.
    """
    try:
        inputs, note = _lookup_frames_inputs(inst, date, target)
    except Exception:  # a provenance lookup must never fail a launch
        logger.warning("could not look up frame inputs for %s/%s/%s", inst, date, target, exc_info=True)
        return None
    return {"inputs": inputs, "note": note}


def _frames_from_params(params: dict) -> dict | None:
    """The ``frames_inputs`` a queued row carries, or ``None`` if absent or malformed."""
    frames = params.get("frames_inputs")
    if not isinstance(frames, dict):
        return None
    inputs, note = frames.get("inputs"), frames.get("note")
    if inputs is not None and not (isinstance(inputs, list) and all(isinstance(i, dict) for i in inputs)):
        return None
    if note is not None and not isinstance(note, str):
        return None
    return {"inputs": inputs, "note": note}


def _write_run_product(
    rdir: Path,
    *,
    inst: str,
    date: str,
    target: str,
    opts: dict,
    test_run: bool,
    user_name: str | None,
    site: str,
    telescope: str,
    mode: str,
    run_name: str,
    frames: dict | None = None,
) -> None:
    """Record a photometry run's inputs, settings, versions and author (#208).

    *frames* is the ``{"inputs", "note"}`` pair a queued row carries (see
    :func:`frames_inputs_param`). Without it the inputs are looked up in the
    catalog, unless this process may not open it (a worker on a shared control
    plane, #298): the record is then left unwritten, because ``muscat-db
    backfill-products`` fills an absent record from the catalog but never
    rewrites one that says its inputs are undetermined.
    """
    from muscat_db import __version__
    from muscat_db import product_record

    if frames is not None:
        inputs, note = frames["inputs"], frames["note"]
    elif database.catalog_allowed():
        inputs, note = _lookup_frames_inputs(inst, date, target)
    else:
        logger.warning(
            "photometry run %s/%s/%s was queued without its frame inputs and this "
            "worker cannot read the catalog; leaving its product record for "
            "`muscat-db backfill-products`", inst, date, target,
        )
        return
    tool_versions = {"muscat_db": __version__, "prose": prose_version()}
    if os.environ.get("MUSCAT_PROSE_FROZEN_CONFIG") == "1":
        from muscat_db._prose_launcher import snapshot_bytes, SNAPSHOT_SHA256
        snapshot_bytes()
        tool_versions["prose_config_sha256"] = SNAPSHOT_SHA256
    record = product_record.build_record(
        "photometry",
        author=user_name,
        inputs=inputs,
        note=note,
        settings={
            "options": opts, "test_run": bool(test_run),
            "site": site, "telescope": telescope, "mode": mode, "run_name": run_name,
        },
        tool_versions=tool_versions,
    )
    product_record.write_record(rdir, record)


def _prose_prefix(module: str = _MODULE, console_script: str | None = "photometry") -> list[str]:
    """Resolve how to invoke a prose pipeline entrypoint, most robust first.

    Prefers the ``photometry`` console script installed into the prose conda
    env (``prose.scripts.run_photometry:main`` per prose2's ``pyproject.toml``)
    over ``-m`` module invocation, matching ``transit_fit._timer_prefix`` /
    ``ttv_fit``'s equivalent. This makes whatever prose2 is actually installed
    into the env authoritative (editable checkout today; a pinned
    ``pip install git+...@<sha>`` for #101) rather than a checkout pointed at
    by cwd, so the launch path no longer needs cwd for ``sys.path`` injection
    (see ``start_run``, which now runs with the job's own output dir as cwd).

    ``console_script`` may be ``None`` for entrypoints with no console script
    (e.g. ``prose.scripts.postprocess_lightcurves``), forcing module
    invocation.
    """
    explicit = prose_python()
    if os.environ.get("MUSCAT_PROSE_FROZEN_CONFIG") == "1":
        python = explicit or _conda_env_python(prose_conda_env())
        if python is None:
            raise RuntimeError("frozen prose configuration requires its pinned environment")
        return [python, "-I", str(_HERE / "_prose_launcher.py"),
                "--config", str(_HERE / "_prose_telescopes.yaml"), module]
    if explicit:
        return [explicit, "-m", module]
    env = prose_conda_env()
    conda_py = _conda_env_python(env)
    if conda_py:
        if console_script:
            script_path = Path(conda_py).parent / console_script
            if script_path.is_file():
                return [str(script_path)]
        return [conda_py, "-m", module]
    if shutil.which("conda"):
        return ["conda", "run", "-n", env, "--no-capture-output",
                "python", "-m", module]
    # Last resort: let uv resolve an interpreter from the project directory.
    return ["uv", "run", "--project", str(prose_project_dir()),
            "python", "-m", module]


def valid_date(date: str) -> bool:
    return bool(_DATE_RE.match(date or ""))


def results_dir(inst: str, date: str) -> Path:
    return output_base() / inst / date


# Run-id / path-segment helpers live in muscat_db.jobs so photometry and
# transit-fit share one implementation (audit C1). Re-exported here under their
# historical names for callers and tests.
_target_dir_name = jobs.target_dir_name
_run_dir_name = jobs.run_dir_name
slugify_run_name = jobs.slugify_run_name


def build_run_id(
    inst: str,
    site: str | None,
    mode: str | None,
    run_name: str | None,
    telescope: str | None = None,
) -> str:
    """Compose a photometry run id using the shared run-id convention.

    Single-site runs are identified by the run-name slug only, so
    site/telescope/mode are forced blank; multi-site runs (sinistro/sbig/
    qhy600) include site, telescope and only the non-default readout mode.
    See :func:`muscat_db.jobs.build_run_id` for the canonical join rules.
    """
    if inst not in MULTISITE_INSTRUMENTS:
        return jobs.build_run_id("", "", run_name)
    return jobs.build_run_id(site, mode, run_name, telescope=telescope)


def run_output_dir(inst: str, date: str, target: str, run_id: str | None = None) -> Path:
    """Output directory for a legacy or named photometry run."""
    base = results_dir(inst, date)
    if not run_id:
        return base
    return base / _RUNS_DIR_NAME / _target_dir_name(target) / _run_dir_name(run_id)


def _parse_run_dir_name(inst: str, name: str) -> tuple[str, str, str, str]:
    """Best-effort split of a run-id dir name into (site, telescope, mode, run_name)."""
    if inst not in MULTISITE_INSTRUMENTS:
        return "", "", "", name
    parts = name.split("-")
    site = telescope = mode = ""
    if parts and parts[0] in MULTISITE_SITES.get(inst, ()):
        site, parts = parts[0], parts[1:]
    if parts and parts[0].startswith("tel") and parts[0][3:].isdigit():
        telescope, parts = parts[0], parts[1:]
    inst_modes = MULTISITE_MODES.get(inst, ())
    if parts and parts[0] in inst_modes:
        mode, parts = parts[0], parts[1:]
    elif (site or telescope) and inst_modes:
        mode = inst_modes[0]  # default (non-full) mode for this instrument
    return site, telescope, mode, "-".join(parts)


def raw_data_dir(inst: str, date: str) -> Path:
    cfg = INSTRUMENTS.get(inst)
    if cfg is None:
        raise ValueError(f"unknown instrument: {inst}")
    return Path(cfg.data_dir) / date


def _stem(target: str, inst: str, date: str, band: str | None = None) -> str:
    """Mirror prose ``build_stem`` (spaces stripped from the target)."""
    t = target.replace(" ", "")
    return f"{t}_{inst}_{band}_{date}" if band else f"{t}_{inst}_{date}"


# --------------------------- output discovery ---------------------------


def output_dates(inst: str) -> list[str]:
    """6-digit date dirs that already have a prose output folder, newest first."""
    base = output_base() / inst
    if not base.is_dir():
        return []
    return sorted(
        (p.name for p in base.iterdir() if p.is_dir() and _DATE_RE.match(p.name)),
        reverse=True,
    )


def discovered_targets(inst: str, date: str) -> list[str]:
    """Target names inferred from product filenames already in the output dir.

    The pipeline embeds the date from the FITS header into each filename, which
    may differ from the directory name (obs-night vs UT date). We therefore
    match on the inst token only and accept any 6-digit date token.
    """
    rdir = results_dir(inst, date)
    if not rdir.is_dir():
        return []
    # Match: <target>_<inst>_[<band>_]<6digits>[._...]
    pat = re.compile(
        rf"^(?P<t>.+?)_{re.escape(inst)}_(?:[A-Za-z0-9_]+_)?\d{{6}}(?:[._]|$)"
    )
    found: set[str] = set()
    for p in rdir.iterdir():
        if not p.is_file() or p.suffix == ".log":
            continue
        m = pat.match(p.name)
        if m:
            found.add(m.group("t"))
    runs_dir = rdir / _RUNS_DIR_NAME
    if runs_dir.is_dir():
        for d in runs_dir.iterdir():
            if d.is_dir():
                found.add(d.name)
    return sorted(found)


def _telescope_token_to_value(digits: str | None, inst: str = "sinistro") -> str | None:
    """Reconstruct the canonical TELESCOP-style value from a filename token's
    captured digits, e.g. ``'05'`` -> ``'1m0-05'`` for sinistro or
    ``'06'`` -> ``'0m4-06'`` for sbig/qhy600 (the inverse of
    ``jobs.slugify_telescope``). ``None``/blank input -> ``None``."""
    if not digits:
        return None
    prefix = _TELESCOPE_PREFIX.get(inst, "1m0")
    return f"{prefix}-{digits}"


def empty_outputs() -> dict:
    """What :func:`list_outputs` returns for a target with no products."""
    return {
        "summary": {},
        "summary_items": [],
        "bands": {},
        "npz": None,
        "log": None,
        "has_any": False,
        "masters": [],
        "sites": [],
        "site": None,
        "telescopes": [],
        "telescope": None,
        "modes": [],
        "mode": None,
        "ref_header": None,
        "ref_selection": None,
    }


def list_outputs(
    inst: str,
    date: str,
    target: str,
    site: str | None = None,
    mode: str | None = None,
    run_id: str | None = None,
    telescope: str | None = None,
) -> dict:
    """Classify the existing products for one (inst, date, target).

    Returns a dict with ``summary`` (key->filename), ``bands``
    (band->{ref,apertures,alignment,gif,csv}), ``npz``, ``log`` (newest),
    ``has_any``, ``sites``/``modes``/``telescopes`` (distinct sinistro
    sites/readout modes/telescopes present, for the filter chips),
    ``site``/``mode``/``telescope`` (the ones actually shown), ``ref_header``
    (the reference-frame header sidecar, site/telescope/mode-scoped), and
    ``ref_selection`` (the ``--ref_select quality`` audit report, if present).
    Only filenames are returned; serve them via the file route.

    A single sinistro date+target can hold products from more than one LCO
    site, physical telescope (multiple 1m units can share a site), and readout
    mode (identical bands per combination). To avoid silently collapsing them
    via newest-wins, products are restricted to one (site, telescope, mode) at
    a time: ``site``/``telescope``/``mode`` select them when given and present,
    otherwise the newest reduction is shown by default. Telescope choices are
    scoped to the chosen site, and mode choices to the chosen (site,
    telescope), so switching a coarser filter never lands on an empty
    combination by default. Mode is read from the ``_full`` filename token
    prose appends for ``full_frame`` (``central_2k_2x2`` has no token). For
    non-sinistro instruments there is no site/telescope/mode dimension and
    ``sites``/``telescopes``/``modes`` stay empty.

    The date token embedded in filenames by the pipeline is taken from the FITS
    header and may differ from the directory name (obs-night vs UT date). We
    therefore build regexes that accept any 6-digit date token instead of
    requiring an exact match against the passed-in ``date``.
    """
    out = empty_outputs()
    try:
        rdir = run_output_dir(inst, date, target, run_id)
    except ValueError:
        return out
    if not rdir.is_dir():
        return out

    t = target.replace(" ", "")
    inst_esc = re.escape(inst)
    t_esc = re.escape(t)

    # Multi-site instrument filenames optionally carry a site token between
    # inst and the band/date when reduced with ``--site`` (prose
    # ``build_stem``):
    #   <target>_<inst>_<site>_<date6>            (summary)
    #   <target>_<inst>_<site>_<band>_<date6>     (per-band)
    # Constrain the token to the known site codes so it can't be confused with a
    # band name that itself contains underscores (e.g. g_narrow, Na_D). For all
    # other instruments there is no site token and this slot is omitted.
    inst_sites = MULTISITE_SITES.get(inst, ())
    site_opt = rf"(?:(?P<site>{'|'.join(inst_sites)})_)?" if inst_sites else ""
    # Multi-site instrument filenames optionally carry a telescope token right
    # after the site (prose ``build_stem``, ``_telescope_stem_token``): the
    # physical LCO unit id compacted from the TELESCOP header, e.g.
    # '1m0-05' -> 'tel05' / '0m4-06' -> 'tel06'. The 'tel' prefix keeps it
    # unambiguous against band names.
    telescope_opt = (
        r"(?:tel(?P<telescope>[0-9]+)_)?" if inst in MULTISITE_INSTRUMENTS else ""
    )
    # Prose appends ``_full`` after the date for the instrument's "full"
    # readout mode; the default/central mode has no token. Captured between
    # the date and the product suffix (e.g. ..._250710_full_lightcurves.png,
    # ..._250710_full.npz, ..._gp_250710_full.csv). Only meaningful for
    # mode-capable instruments (see MULTISITE_MODES).
    mode_opt = r"(?P<mode>_full)?" if inst in MULTISITE_MODES else ""

    # Summary stems exist in two generations:
    #   <target>_<inst>_[<site>_][<tel>_]<date6>[_full]                (legacy)
    #   <target>_<inst>_[<site>_][<tel>_]<bands>_<date6>[_full]        (band-set scoped)
    # Allow any 6-digit date so obs-night and UT-date both match.
    summary_re = re.compile(
        rf"^{t_esc}_{inst_esc}_{site_opt}{telescope_opt}(?P<file_date>\d{{6}}){mode_opt}(?P<rest>.*)$"
    )
    summary_bandset_re = re.compile(
        rf"^{t_esc}_{inst_esc}_{site_opt}{telescope_opt}(?P<bands>[A-Za-z0-9_]+?)_(?P<file_date>\d{{6}}){mode_opt}(?P<rest>.*)$"
    )
    # Per-band stem: <target>_<inst>_[<site>_][<tel>_]<band>_<date6>[_full]. The
    # band token may itself contain underscores (narrow-band/Johnson filters:
    # g_narrow, Na_D, z_s), so allow ``_`` in the band and match it lazily up to
    # the 6-digit date.
    band_re = re.compile(
        rf"^{t_esc}_{inst_esc}_{site_opt}{telescope_opt}(?P<band>[A-Za-z0-9_]+?)_(?P<file_date>\d{{6}}){mode_opt}(?P<rest>.*)$"
    )

    inst_modes = MULTISITE_MODES.get(inst, ())

    def _mode_of(m: re.Match) -> str:
        """Canonical readout mode for a matched product file."""
        if m.groupdict().get("mode"):
            return "full_frame"
        # inst_modes[1] is always the "full" mode, inst_modes[0] the default.
        return inst_modes[0] if inst_modes else "full_frame"

    def _telescope_of(m: re.Match) -> str | None:
        """Canonical TELESCOP-style value for a matched product file."""
        return _telescope_token_to_value(m.groupdict().get("telescope"), inst)

    # First pass (multi-site instruments only): discover which sites and
    # readout modes are present so multi-site/multi-mode dates expose one
    # chip per value and default to the most recently reduced combination
    # rather than a newest-wins mix. Mode chips are scoped to the chosen
    # site (each site may carry its own modes), so switching site never
    # lands on an empty (site, mode) pairing by default.
    effective_site: str | None = None
    effective_telescope: str | None = None
    effective_mode: str | None = None
    if inst in MULTISITE_INSTRUMENTS:
        # records: (site_or_None, telescope_or_None, canonical_mode, mtime)
        records: list[tuple[str | None, str | None, str, float]] = []
        for p in rdir.iterdir():
            if not p.is_file() or p.suffix == ".log":
                continue
            m = summary_re.match(p.name) or summary_bandset_re.match(p.name) or band_re.match(p.name)
            if not m:
                continue
            try:
                mt = p.stat().st_mtime
            except OSError:
                mt = 0.0
            records.append((m.groupdict().get("site"), _telescope_of(m), _mode_of(m), mt))

        site_mtime: dict[str, float] = {}
        for s, _tel, _m, mt in records:
            if s and mt > site_mtime.get(s, -1.0):
                site_mtime[s] = mt
        out["sites"] = sorted(site_mtime)
        if site and site in site_mtime:
            effective_site = site
        elif site_mtime:
            effective_site = max(site_mtime, key=site_mtime.get)  # newest wins
        out["site"] = effective_site

        # Telescopes available for the chosen site (or all records when no
        # site token).
        telescope_mtime: dict[str, float] = {}
        for s, tel, _m, mt in records:
            if effective_site is not None and s != effective_site:
                continue
            if tel and mt > telescope_mtime.get(tel, -1.0):
                telescope_mtime[tel] = mt
        out["telescopes"] = sorted(telescope_mtime)
        if telescope and telescope in telescope_mtime:
            effective_telescope = telescope
        elif telescope_mtime:
            effective_telescope = max(telescope_mtime, key=telescope_mtime.get)  # newest wins
        out["telescope"] = effective_telescope

        # Modes available for the chosen (site, telescope).
        mode_mtime: dict[str, float] = {}
        for s, tel, md, mt in records:
            if effective_site is not None and s != effective_site:
                continue
            if effective_telescope is not None and tel != effective_telescope:
                continue
            if mt > mode_mtime.get(md, -1.0):
                mode_mtime[md] = mt
        out["modes"] = sorted(mode_mtime)
        if mode and mode in mode_mtime:
            effective_mode = mode
        elif mode_mtime:
            effective_mode = max(mode_mtime, key=mode_mtime.get)  # newest wins
        out["mode"] = effective_mode

    logs: list[Path] = []

    for p in sorted(rdir.iterdir()):
        if not p.is_file():
            continue
        name = p.name
        if p.suffix == ".log":
            logs.append(p)
            continue

        try:
            st = p.stat()
            mtime = st.st_mtime
            version = str(st.st_mtime_ns)
            created_at = datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
        except Exception:
            mtime = 0.0
            version = "0"
            created_at = "Unknown"

        # Try summary suffixes first, including band-set-scoped summary stems.
        ms = summary_re.match(name) or summary_bandset_re.match(name)
        if ms:
            # When a site/telescope/mode is in force, only that combination is shown.
            if effective_site is not None and ms.group("site") != effective_site:
                continue
            if effective_telescope is not None and _telescope_of(ms) != effective_telescope:
                continue
            if effective_mode is not None and _mode_of(ms) != effective_mode:
                continue
            rest = ms.group("rest")
            if rest == ".npz":
                existing = out.get("npz")
                if existing is None or mtime > out.get("_npz_mtime", 0):
                    out["npz"] = name
                    out["_npz_mtime"] = mtime
                out["has_any"] = True
                continue
            if rest == "_ref_header.txt":
                if out["ref_header"] is None or mtime > out.get("_ref_header_mtime", 0):
                    out["ref_header"] = name
                    out["_ref_header_mtime"] = mtime
                out["has_any"] = True
                continue
            if rest == "_ref_selection.txt":
                if out["ref_selection"] is None or mtime > out.get("_ref_selection_mtime", 0):
                    out["ref_selection"] = name
                    out["_ref_selection_mtime"] = mtime
                out["has_any"] = True
                continue
            key = _SUMMARY_SUFFIX.get(rest)
            if key is not None:
                item = {
                    "key": key,
                    "file": name,
                    "created_at": created_at,
                    "version": version,
                    "_mtime": mtime,
                }
                existing = out["summary"].get(key)
                if existing is None or mtime > existing.get("_mtime", 0):
                    out["summary"][key] = item
                out["has_any"] = True
                continue
            # If the summary regex matched but rest is unrecognised, fall
            # through to the band regex (it is more specific).

        mb = band_re.match(name)
        if not mb:
            continue
        if effective_site is not None and mb.group("site") != effective_site:
            continue
        if effective_telescope is not None and _telescope_of(mb) != effective_telescope:
            continue
        if effective_mode is not None and _mode_of(mb) != effective_mode:
            continue
        rest = mb.group("rest")
        key = _BAND_SUFFIX.get(rest)
        if key is None:
            continue
        band = mb.group("band")
        existing = out["bands"].setdefault(band, {}).get(key)
        if existing is None or mtime > existing.get("_mtime", 0):
            out["bands"][band][key] = {
                "file": name,
                "created_at": created_at,
                "version": version,
                "_mtime": mtime,
            }
            out["has_any"] = True

    if inst in ("muscat", "muscat2"):
        for base_dir in (results_dir(inst, date), raw_data_dir(inst, date)):
            try:
                cal_dir = Path(str(base_dir) + "_calibrated")
                for p in sorted(cal_dir.glob("master_*.png")):
                    if p.is_file() and p.name not in out["masters"]:
                        out["masters"].append(p.name)
            except OSError:
                pass

    if logs:
        out["log"] = max(logs, key=lambda p: p.stat().st_mtime).name

    # Rebuild summary_items from summary dict to ensure only the newest
    # version of each summary product is displayed (avoid duplicates for
    # legacy vs band-set-scoped summary stems with the same key).
    out["summary_items"] = [
        item for key, item in out["summary"].items()
        if key != "nearby_stars"
    ]

    # Strip internal keys and order bands canonically (see band ordering below)
    out["summary_items"].sort(
        key=lambda item: (
            ["lightcurves", "raw_flux", "covariates", "stacks"].index(item["key"])
            if item["key"] in {"lightcurves", "raw_flux", "covariates", "stacks"}
            else 99,
            -item.get("_mtime", 0),
            item["file"],
        )
    )
    for d in out["summary"].values():
        d.pop("_mtime", None)
    for d in out["summary_items"]:
        d.pop("_mtime", None)
    for band_d in out["bands"].values():
        for d in band_d.values():
            d.pop("_mtime", None)
    out.pop("_npz_mtime", None)
    out.pop("_ref_header_mtime", None)
    out.pop("_ref_selection_mtime", None)
    # Order band tabs canonically: broadband (gp, rp, ip, zs) then narrowband
    # (g_narrow, Na_D, i_narrow, z_narrow); any unrecognized bands keep their
    # discovered (filename) order after the known ones.
    ordered = {
        b: out["bands"][b]
        for b in (*DEFAULT_BANDS, *NARROW_BANDS)
        if b in out["bands"]
    }
    for b, v in out["bands"].items():
        ordered.setdefault(b, v)
    out["bands"] = ordered
    return out


def csv_preview(path: Path, n: int = 8) -> tuple[list[str], list[list[str]]]:
    """Header row + first ``n`` data rows of a light-curve CSV."""
    try:
        with open(path, newline="") as f:
            reader = _csv.reader(f)
            headers = next(reader, [])
            rows = []
            for i, row in enumerate(reader):
                if i >= n:
                    break
                rows.append(row)
        return headers, rows
    except OSError:
        return [], []


def lightcurve_time_range(paths) -> tuple[float, float] | None:
    """Min/max finite ``BJD_TDB`` across band light-curve CSVs, or ``None``.

    This is the time axis prose2's post-process JD-range cut compares
    against -- not the raw header JD in ``frames.jd_start`` that the
    run-time ``--exclude_after_jd``/``--exclude_before_jd`` use. A
    zero-width span returns ``None`` (nothing to drag a slider across).
    """
    lo = hi = None
    for path in paths:
        try:
            with open(path, newline="") as f:
                reader = _csv.DictReader(f)
                if "BJD_TDB" not in (reader.fieldnames or []):
                    continue
                for row in reader:
                    try:
                        t = float(row["BJD_TDB"])
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(t):
                        continue
                    lo = t if lo is None else min(lo, t)
                    hi = t if hi is None else max(hi, t)
        except OSError:
            logger.debug("failed to read BJD_TDB span from %s", path, exc_info=True)
    if lo is None or hi is None or not hi > lo:
        return None
    return lo, hi


@dataclass
class RunDescriptor:
    run_id: str
    site: str = ""
    telescope: str = ""
    mode: str = ""
    run_name: str = ""
    mtime: float = 0.0
    is_legacy: bool = False
    run_type: str = "full"


def _read_run_meta(d: Path) -> dict:
    try:
        return json.loads((d / _RUN_META_NAME).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _dir_mtime(d: Path) -> float:
    try:
        return max((p.stat().st_mtime for p in d.iterdir() if p.is_file()), default=d.stat().st_mtime)
    except OSError:
        return 0.0


def _write_run_meta(d: Path, *, inst: str, date: str, target: str, run_id: str, site: str, mode: str, run_name: str, run_type: str, telescope: str = "") -> None:
    meta = {
        "inst": inst,
        "date": date,
        "target": target,
        "run_id": run_id,
        "site": site,
        "telescope": telescope,
        "mode": mode,
        "run_name": run_name,
        "run_type": run_type,
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    try:
        (d / _RUN_META_NAME).write_text(json.dumps(meta, sort_keys=True, indent=2))
    except OSError:
        pass


def list_photometry_runs(
    inst: str, date: str, target: str
) -> tuple[list[RunDescriptor], dict[str | None, dict]]:
    """Enumerate legacy and named photometry runs for a target, newest-first.

    Returns ``(runs, run_outputs)`` where ``run_outputs`` maps each run's
    ``run_id`` (``None`` for the legacy run) to its pre-computed
    ``list_outputs()`` result.  Callers that need the outputs for the selected
    run can pull them from the dict instead of calling ``list_outputs()`` again,
    avoiding a redundant directory scan.
    """
    runs: list[RunDescriptor] = []
    run_outputs: dict[str | None, dict] = {}

    legacy = list_outputs(inst, date, target)
    run_outputs[None] = legacy
    legacy_dir = run_output_dir(inst, date, target)
    if legacy.get("has_any"):
        runs.append(RunDescriptor(
            run_id="",
            site="",
            telescope="",
            mode="",
            run_name="legacy",
            mtime=_dir_mtime(legacy_dir),
            is_legacy=True,
            run_type="full",
        ))

    try:
        runs_root = results_dir(inst, date) / _RUNS_DIR_NAME / _target_dir_name(target)
    except ValueError:
        return runs, run_outputs
    if runs_root.is_dir():
        for d in sorted(runs_root.iterdir()):
            if not d.is_dir():
                continue
            out = list_outputs(inst, date, target, run_id=d.name)
            run_outputs[d.name] = out
            if not out.get("has_any"):
                continue
            meta = _read_run_meta(d)
            if meta.get("run_id") or meta.get("run_name"):
                site = str(meta.get("site") or "")
                telescope = str(meta.get("telescope") or "")
                mode = str(meta.get("mode") or "")
                run_name = str(meta.get("run_name") or "")
                run_type = str(meta.get("run_type") or "full")
            else:
                site, telescope, mode, run_name = _parse_run_dir_name(inst, d.name)
                run_type = "full"
            runs.append(RunDescriptor(
                run_id=d.name,
                site=site,
                telescope=telescope,
                mode=mode,
                run_name=run_name or d.name,
                mtime=_dir_mtime(d),
                is_legacy=False,
                run_type=run_type,
            ))
    runs.sort(key=lambda r: r.mtime, reverse=True)
    return runs, run_outputs


# --------------------------- safe file serving ---------------------------


def safe_artifact_path(inst: str, date: str, name: str) -> Path | None:
    """Resolve a served filename to a real file, or ``None`` if anything about
    the request is unsafe (bad instrument/date/name, traversal, wrong ext)."""
    if inst not in INSTRUMENTS or not valid_date(date):
        return None
    if ".." in name or "/" in name or not _NAME_RE.match(name):
        return None
    if Path(name).suffix.lower() not in ALLOWED_EXTS:
        return None

    # For muscat/muscat2 master images, they live in <results_dir>_calibrated or <raw_data_dir>_calibrated
    if inst in ("muscat", "muscat2") and name.startswith("master_") and name.endswith(".png"):
        for base_dir in (results_dir(inst, date), raw_data_dir(inst, date)):
            cal_dir = Path(str(base_dir) + "_calibrated").resolve()
            candidate = (cal_dir / name).resolve()
            try:
                candidate.relative_to(cal_dir)
                if candidate.is_file():
                    return candidate
            except ValueError:
                pass
        return None

    base = output_base().resolve()
    candidate = (base / inst / date / name).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def safe_run_artifact_path(inst: str, date: str, target: str, run_id: str, name: str) -> Path | None:
    """Resolve a named-run artifact path, or ``None`` when invalid/missing."""
    if inst not in INSTRUMENTS or not valid_date(date):
        return None
    if ".." in name or "/" in name or not _NAME_RE.match(name):
        return None
    if Path(name).suffix.lower() not in ALLOWED_EXTS:
        return None
    try:
        rdir = run_output_dir(inst, date, target, run_id).resolve()
    except ValueError:
        return None
    candidate = (rdir / name).resolve()
    try:
        candidate.relative_to(rdir)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


_phot_status_cache = register_cache(ttl=300.0)


def get_photometry_status(inst: str, date: str, target: str) -> str:
    """Determine the strongest photometry status across all of a target's runs:
    ``none``, ``test``, or ``full``.

    Reductions land either in the legacy ``{inst}/{date}/`` directory or in a
    per-run ``{inst}/{date}/_runs/{target}/{run_id}/`` subdirectory. The status
    is aggregated over *every* run (via :func:`list_photometry_runs`) so a modern
    run-scoped reduction is not missed — checking only the legacy dir made the
    Targets/target pages report ``none`` for run-scoped photometry.

    The combined mtime of the legacy dir and the target's ``_runs`` subtree is
    the cache key, so the result auto-invalidates whenever any run's products are
    created or removed, on top of the global ``clear_all_caches()`` fired after
    every ``build-db`` run.
    """
    return _get_status_mtime(inst, date, target, _photometry_status_mtime(inst, date, target))


def _photometry_status_mtime(inst: str, date: str, target: str) -> float:
    """Max mtime across the legacy results dir and the target's ``_runs`` subtree
    (one level deep), so the status cache busts when any run writes or removes
    products under ``_runs/{target}/{run_id}/``."""
    mtime = 0.0
    base = results_dir(inst, date)
    try:
        mtime = max(mtime, base.stat().st_mtime)
    except OSError:
        pass
    try:
        runs_root = base / _RUNS_DIR_NAME / _target_dir_name(target)
    except ValueError:
        return mtime
    try:
        mtime = max(mtime, runs_root.stat().st_mtime)
        for d in runs_root.iterdir():
            try:
                mtime = max(mtime, d.stat().st_mtime)
            except OSError:
                pass
    except OSError:
        pass
    return mtime


@_phot_status_cache
def _get_status_mtime(inst: str, date: str, target: str, mtime: float) -> str:
    """Inner cached worker keyed on (inst, date, target, mtime)."""
    return _aggregate_photometry_status(inst, date, target)


def _aggregate_photometry_status(inst: str, date: str, target: str) -> str:
    """Strongest status over all of a target's runs: ``full`` if any run has a
    full reduction, else ``test`` if any run has products, else ``none``."""
    runs, run_outputs = list_photometry_runs(inst, date, target)
    best = "none"
    for run in runs:
        key = None if run.is_legacy else run.run_id
        outputs = run_outputs.get(key)
        if not outputs or not outputs.get("has_any"):
            continue
        rdir = run_output_dir(inst, date, target, key)
        status = _calculate_photometry_status(target, rdir, outputs)
        if status == "full":
            return "full"
        if status == "test":
            best = "test"
    return best


def _calculate_photometry_status(target: str, rdir: Path, outputs: dict) -> str:
    """Classify one run's products as ``full``/``test``/``none`` from its
    ``list_outputs`` result (``outputs``) and its output directory (``rdir``)."""
    if not outputs.get("has_any"):
        return "none"

    # Fallback/Optimization: Check CSV files. If any CSV has more than 15 lines, it is a full run.
    csv_found = False
    max_rows = 0
    for band_data in outputs.get("bands", {}).values():
        csv_info = band_data.get("csv")
        if csv_info:
            csv_path = rdir / csv_info["file"]
            if csv_path.is_file():
                csv_found = True
                try:
                    with open(csv_path, errors="replace") as f:
                        row_count = sum(1 for _ in f)
                        if row_count > max_rows:
                            max_rows = row_count
                except Exception:
                    logger.debug("failed to count photometry CSV rows in %s", csv_path, exc_info=True)
    if csv_found and max_rows > 15:
        return "full"

    # Check log files to distinguish test vs full
    log_files = list(rdir.glob("*.log"))
    if (rdir / _RUN_LOG_NAME).is_file():
        log_files.append(rdir / _RUN_LOG_NAME)

    has_target_log = False
    has_full_log = False
    target_clean = target.replace(" ", "")

    for lf in log_files:
        try:
            content = lf.read_text(errors="replace")
            if target in content or target_clean in content:
                has_target_log = True
                # If the log doesn't indicate a test run, then it's a full run
                if "--test_run" not in content and "--test-run" not in content and "test-run:" not in content:
                    has_full_log = True
                    break
        except Exception:
            logger.debug("failed to read photometry log %s while determining status", lf, exc_info=True)

    if has_target_log:
        return "full" if has_full_log else "test"

    return "full"


# --------------------------- command building ---------------------------


def _to_int(v) -> int | None:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def _to_float(v) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "on", "yes")


_TRIPLE_RE = re.compile(r"^-?\d+(\.\d+)?,-?\d+(\.\d+)?,-?\d+(\.\d+)?$")
_PAIR_RE = re.compile(r"^-?\d+(\.\d+)?,-?\d+(\.\d+)?$")
_INTPAIR_RE = re.compile(r"^\d+,\d+$")
_BAND_RE = re.compile(r"^[A-Za-z0-9_]+$")
# Comma-separated options become one argv element per item in build_command, so
# an item that is not a plain number could be read by prose2's argparse as a flag
# of its own (--data_dir, --results_dir, ...) and override the ones set here.
# Ids and Julian dates are digits only; anything else is refused by
# validate_run_options and dropped by build_command.
_INT_ITEM_RE = re.compile(r"[0-9]+")
_JD_ITEM_RE = re.compile(r"[0-9]+(\.[0-9]+)?")


def _list_items(value) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _numeric_items(value, item_re: re.Pattern) -> list[str]:
    """The items of a comma-separated option that are plain numbers."""
    return [item for item in _list_items(value) if item_re.fullmatch(item)]


def _all_numeric(value, item_re: re.Pattern) -> bool:
    return len(_numeric_items(value, item_re)) == len(_list_items(value))


def normalize_run_options(raw: dict | None) -> dict:
    """Coerce a raw form/JSON dict into typed run options merged over defaults.

    Mirrors quicklook's ``_parse_params``: empty strings keep the pipeline
    default (omitted from the command), numbers are coerced, checkboxes become
    booleans. Unknown keys are ignored.
    """
    raw = raw or {}
    o = dict(RUN_DEFAULTS)

    bands = raw.get("bands")
    if isinstance(bands, str):
        bands = [b for b in re.split(r"[,\s]+", bands) if b]
    if "bands" in raw:  # present-but-empty must surface as an error, not default
        o["bands"] = [str(b).strip() for b in (bands or []) if str(b).strip()]

    for key in ("run_name", "ref_band", "ref_select", "ref_time", "aper_radii", "annulus", "aper_unit", "ccd_trim", "target_id", "comparison_ids", "avoid_comparison_ids", "avoid_nearby_star_mode", "avoid_nearby_star", "target_coord", "wcs_method", "centroid_method", "calib_dir", "site", "telescope", "mode", "cmap", "nan_imputation_method", "exclude_after_jd", "exclude_before_jd"):
        if raw.get(key) is not None:
            o[key] = str(raw[key]).strip()

    for key in ("refid", "n_stars_align"):  # optional ints; "" keeps default
        if key in raw:
            val = str(raw.get(key, "")).strip()
            o[key] = "" if val == "" else (_to_int(val) if _to_int(val) is not None else "")

    for key in ("test_run_frames", "max_num_stars", "cutout_size", "display_stack_nframes", "gif_stride", "min_star_area", "edge_margin", "ref_select_top_k", "flux_poly_deg"):
        if str(raw.get(key, "")).strip() != "":
            iv = _to_int(raw[key])
            if iv is not None:
                o[key] = iv

    for key in ("min_star_separation", "avoid_nearby_star", "bin_size_minutes", "sig_bkg", "sig_fwhm", "sig_dx", "sig_dy", "sig_flux"):
        if str(raw.get(key, "")).strip() != "":
            fv = _to_float(raw[key])
            if fv is not None:
                o[key] = fv

    for key in ("make_gif", "plot_gaia_sources", "use_barycorrpy"):
        if key in raw:
            o[key] = _to_bool(raw[key])
    # Overwrite requires special handling: explicitly False if present and falsy,
    # otherwise default to True. This ensures unchecking the box is honored.
    if "overwrite" in raw:
        o["overwrite"] = _to_bool(raw["overwrite"])
    # If not in raw dict at all, default to True (keep existing behavior)

    # Backward-compat: older UI stored a checkbox + optional value instead of a
    # 3-state mode. Translate that persisted shape when the new mode is absent.
    if "avoid_nearby_star_mode" not in raw and "avoid_nearby_stars" in raw:
        enabled = _to_bool(raw.get("avoid_nearby_stars"))
        if not enabled:
            o["avoid_nearby_star_mode"] = "off"
        else:
            o["avoid_nearby_star_mode"] = (
                "custom" if str(raw.get("avoid_nearby_star", "")).strip() != "" else "auto"
            )

    return o


def validate_run_options(o: dict, inst: str | None = None) -> str | None:
    """Return a user-facing error string for invalid options, else ``None``."""
    if not o.get("bands"):
        return "select at least one band"
    if any(not _BAND_RE.match(b) for b in o["bands"]):
        return "band names may only contain letters, digits and underscores"
    ref_band = (o.get("ref_band") or "").strip()
    if inst in MULTISITE_INSTRUMENTS and ref_band and len(o.get("bands") or []) > 1:
        return "reference band is disabled for multi-band reductions on multi-site instruments because simultaneous bands can be from different telescopes/pointings"
    if ref_band and ref_band not in set(o.get("bands") or []):
        return "reference band must be one of the selected bands"
    if (o.get("avoid_comparison_ids") or "").strip() and not (o.get("ref_band") or "").strip():
        return "avoid comparison IDs requires a reference band"
    if o.get("ref_select", "position") not in ("position", "quality", "time"):
        return "reference selection must be 'position', 'quality' or 'time'"
    if o.get("ref_select") == "time":
        if not is_valid_ref_time(str(o.get("ref_time") or "")):
            return (
                "reference selection 'time' requires a valid reference time "
                "(HH:MM[:SS] UT, YYYY-MM-DDTHH:MM:SS UTC, or a Julian date)"
            )
        if (o.get("ref_band") or "").strip():
            return "reference selection 'time' cannot be combined with a reference band"
    top_k = o.get("ref_select_top_k")
    if top_k not in (None, "") and int(top_k) < 1:
        return "quality candidates (top-K) must be >= 1"
    if o.get("avoid_nearby_star_mode") not in ("off", "auto", "custom"):
        return "avoid nearby stars mode must be one of off, auto, or custom"
    if o.get("avoid_nearby_star_mode") == "custom":
        nearby = o.get("avoid_nearby_star")
        if nearby not in (None, ""):
            try:
                nearby_f = float(nearby)
            except (TypeError, ValueError):
                return "nearby-star separation must be a number in arcsec"
            if nearby_f <= 0:
                return "nearby-star separation must be > 0 arcsec"
        else:
            return "nearby-star separation is required in custom mode"
    ar = (o.get("aper_radii") or "").replace(" ", "")
    an = (o.get("annulus") or "").replace(" ", "")
    if ar and not _TRIPLE_RE.match(ar):
        return "aperture radii must be MIN,MAX,DR (e.g. 10,20,2)"
    if an and not _PAIR_RE.match(an):
        return "annulus must be RIN,ROUT (e.g. 25,40)"
    if ar and not an:
        return "annulus (RIN,ROUT) is required when aperture radii is set"
    if an and not ar:
        return "aperture radii (MIN,MAX,DR) is required when annulus is set"
    if o.get("aper_unit") == "fwhm" and not ar:
        return "aperture unit 'fwhm' requires specifying aperture radii"
    max_r = 0.0
    if an and _PAIR_RE.match(an):
        try:
            _, rout = [float(x) for x in an.split(",")]
            max_r = max(max_r, rout)
        except ValueError:
            pass
    if ar and _TRIPLE_RE.match(ar):
        try:
            _, rmax, _ = [float(x) for x in ar.split(",")]
            max_r = max(max_r, rmax)
        except ValueError:
            pass
    if max_r > 0:
        min_cutout = int(math.ceil(2 * max_r + 5))
        curr_cutout = o.get("cutout_size")
        if curr_cutout is not None and curr_cutout < min_cutout:
            return f"cutout size ({curr_cutout}) is too small for annulus/aperture radii (minimum is {min_cutout} pix)"
    ct = (o.get("ccd_trim") or "").replace(" ", "")
    if ct and not _INTPAIR_RE.match(ct):
        return "CCD trim must be two integers Y,X (e.g. 10,10)"
    if o.get("aper_unit", "pix") not in ("pix", "fwhm"):
        return "aperture unit must be 'pix' or 'fwhm'"
    if o.get("wcs_method") not in ("twirl", "astrometry.net"):
        return "WCS method must be 'twirl' or 'astrometry.net'"
    if o.get("centroid_method", "auto") not in CENTROID_METHODS:
        return f"centroid method must be one of {', '.join(CENTROID_METHODS)}"
    poly_deg = o.get("flux_poly_deg")
    if poly_deg not in (None, ""):
        try:
            poly_deg = int(poly_deg)
        except (TypeError, ValueError):
            return "flux polynomial degree must be a non-negative integer"
        if poly_deg < 0:
            return "flux polynomial degree must be >= 0"
    # Colormap validation: default is "gray" (black=low, white=high).
    # Note: prose2's run_photometry has a bug where gray and gray_r render
    # identically; this is a prose2 issue and needs to be fixed there.
    if (o.get("cmap") or "gray") not in CMAP_CHOICES:
        return f"colormap must be one of {', '.join(CMAP_CHOICES)}"
    if (o.get("nan_imputation_method") or "linear") not in NAN_IMPUTATION_METHODS:
        return f"NaN imputation method must be one of {', '.join(NAN_IMPUTATION_METHODS)}"
    site = (o.get("site") or "").strip().lower()
    if site and site not in SINISTRO_SITES:
        return f"site must be one of {', '.join(SINISTRO_SITES)}"
    telescope = (o.get("telescope") or "").strip()
    if telescope and not TELESCOPE_RE.match(telescope):
        return "telescope id may only contain letters, digits and hyphens"
    mode = (o.get("mode") or "").strip()
    if mode and mode not in SINISTRO_MODES:
        return f"mode must be one of {', '.join(SINISTRO_MODES)}"
    for key, label in (("comparison_ids", "comparison IDs"), ("avoid_comparison_ids", "avoid comparison IDs")):
        if not _all_numeric(o.get(key), _INT_ITEM_RE):
            return f"{label} must be comma-separated integers"
    for key, label in (("exclude_after_jd", "exclude after JD"), ("exclude_before_jd", "exclude before JD")):
        if not _all_numeric(o.get(key), _JD_ITEM_RE):
            return f"{label} must be comma-separated Julian dates (digits and one decimal point)"
    target_id = str(o.get("target_id") or "").strip()
    if target_id and not _INT_ITEM_RE.fullmatch(target_id):
        return "target ID must be an integer"
    if str(o.get("calib_dir") or "").strip():
        return "the calibration directory cannot be set; it is always the night's own"
    return None


def build_command(
    inst: str,
    date: str,
    target: str,
    options: dict | None = None,
    *,
    test_run: bool = True,
    run_id: str | None = None,
) -> list[str]:
    """argv for a prose reduction, including any non-default options.

    ``--overwrite`` (on by default) lets a new target coexist with others
    already reduced in the same inst/date directory.
    """
    o = normalize_run_options(options)
    args = [
        *_prose_prefix(),
        "--target_name", target,
        "--data_dir", str(raw_data_dir(inst, date)),
        "--results_dir", str(run_output_dir(inst, date, target, run_id)),
        "--bands", *o["bands"],
    ]
    if o.get("ref_band"):
        args += ["--ref_band", o["ref_band"]]
    time_mode = o.get("ref_select") == "time"
    # --refid is a per-camera frame number; prose2 rejects it with --ref_select
    # time (the reference is the --ref_time instead), so a stale value in the
    # form is dropped rather than forwarded.
    if o.get("refid") not in (None, "") and not time_mode:
        args += ["--refid", str(o["refid"])]
    if time_mode:
        args += ["--ref_select", "time", "--ref_time", str(o["ref_time"]).strip()]
    elif o.get("ref_select") == "quality":
        args += ["--ref_select", "quality"]
        top_k = o.get("ref_select_top_k")
        if top_k not in (None, "") and int(top_k) != RUN_DEFAULTS["ref_select_top_k"]:
            args += ["--ref_select_top_k", str(top_k)]

    ar = (o.get("aper_radii") or "").replace(" ", "")
    an = (o.get("annulus") or "").replace(" ", "")
    if ar:
        args += ["--aper_radii", ar]
    if an:
        args += ["--annulus", an]
    if ar and o.get("aper_unit", "pix") != "pix":
        args += ["--aper_unit", o["aper_unit"]]

    target_coord = (o.get("target_coord") or "").strip()
    if not target_coord and resolve_lco_key_project_name(target) != target.strip():
        # An LCO key-project OBJECT name decorates a primary designation with
        # a parenthesized alias (e.g. "TIC245728942.01(TOI5012.01)"). Neither
        # MAST nor Simbad -- prose2's own name resolvers -- can resolve that
        # compound string, so every run for such a target aborted with an
        # uncaught ResolverError. prose2 has no way to know about this
        # LCO/muscat-db-specific convention, so resolve coordinates here
        # instead (the same offline-catalog-first path used elsewhere) and
        # pass them explicitly, bypassing name resolution rather than
        # widening prose2 to understand it. Falls through to --target_name
        # only, unchanged from before, when even that resolution fails.
        resolved = _resolve_archive_coords(target)
        if resolved is not None:
            ra_deg, dec_deg, _source = resolved
            target_coord = f"{ra_deg} {dec_deg}"

    if target_coord:
        parts = target_coord.split(None, 1)
        if len(parts) == 2:
            ra, dec = parts[0].strip(), parts[1].strip()
            if dec.startswith("-"):
                # Prepend a space to prevent Python's argparse from misinterpreting
                # a negative declination (e.g. -45:00:42) as a CLI flag.
                dec = f" {dec}"
            args += ["--target_coord", ra, dec]
    # Only plain numbers are forwarded, even though validate_run_options already
    # refuses anything else: queued and stored options reach this function
    # without it (the fleet worker, sync_jobs), and a stray flag here would
    # override --data_dir or --results_dir.
    target_id = str(o.get("target_id") or "").strip()
    if _INT_ITEM_RE.fullmatch(target_id):
        args += ["--tID", target_id]
    for flag, key, item_re in (
        ("--cID", "comparison_ids", _INT_ITEM_RE),
        ("--avoid_cids", "avoid_comparison_ids", _INT_ITEM_RE),
        ("--exclude_after_jd", "exclude_after_jd", _JD_ITEM_RE),
        ("--exclude_before_jd", "exclude_before_jd", _JD_ITEM_RE),
    ):
        items = _numeric_items(o.get(key), item_re)
        if items:
            args += [flag, *items]
    if o.get("avoid_nearby_star_mode") != "off":
        nearby = o.get("avoid_nearby_star")
        if o.get("avoid_nearby_star_mode") == "auto" or nearby in (None, ""):
            args.append("--avoid_nearby_star")
        else:
            args += ["--avoid_nearby_star", str(nearby)]

    # Numeric overrides: only emit when the user changed them from the default.
    for flag, key in (
        ("--min_star_separation", "min_star_separation"),
        ("--max_num_stars", "max_num_stars"),
        ("--cutout_size", "cutout_size"),
        ("--display_stack_nframes", "display_stack_nframes"),
        ("--bin_size_minutes", "bin_size_minutes"),
        ("--gif_stride", "gif_stride"),
        ("--sig_bkg", "sig_bkg"),
        ("--sig_fwhm", "sig_fwhm"),
        ("--sig_dx", "sig_dx"),
        ("--sig_dy", "sig_dy"),
        ("--sig_flux", "sig_flux"),
        ("--flux_poly_deg", "flux_poly_deg"),
        ("--min_star_area", "min_star_area"),
    ):
        val = o.get(key)
        if val in (None, ""):
            continue
        default = RUN_DEFAULTS.get(key)
        if default is None or float(val) != float(default):
            args += [flag, str(val)]
    if o.get("n_stars_align") not in (None, ""):
        args += ["--n_stars_align", str(o["n_stars_align"])]
    if (o.get("ccd_trim") or "").replace(" ", ""):
        args += ["--ccd_trim", o["ccd_trim"].replace(" ", "")]
    # Empty -> auto (half cutout); explicit int (incl. 0 to disable) is emitted.
    if str(o.get("edge_margin", "")).strip() != "":
        args += ["--edge_margin", str(o["edge_margin"])]

    if o.get("wcs_method", "astrometry.net") != "astrometry.net":
        args += ["--wcs_method", o["wcs_method"]]
    if o.get("centroid_method", "auto") != "auto":
        args += ["--centroid_method", o["centroid_method"]]
    # --site / --telescope are multi-site-only filters; --mode is further
    # restricted to mode-capable instruments (see MULTISITE_MODES). Ignored
    # for other instruments.
    if inst in MULTISITE_INSTRUMENTS:
        site = (o.get("site") or "").strip()
        if site:
            args += ["--site", site]
        telescope = (o.get("telescope") or "").strip()
        if telescope:
            args += ["--telescope", telescope]
    if inst in MULTISITE_MODES:
        mode = (o.get("mode") or "").strip()
        if mode:
            args += ["--mode", mode]
    if inst in ("muscat", "muscat2"):
        # Never taken from the options: prose2 writes calibration masters here.
        args += ["--calib_dir", str(results_dir(inst, date)) + "_calibrated"]
    if o.get("make_gif", False):
        args.append("--gif")
    if o.get("plot_gaia_sources", True):
        args.append("--plot_gaia_sources")
    cmap = (o.get("cmap") or "").strip()
    if cmap and cmap != RUN_DEFAULTS["cmap"]:
        args += ["--cmap", cmap]
    nan_method = (o.get("nan_imputation_method") or "").strip()
    if nan_method and nan_method != RUN_DEFAULTS["nan_imputation_method"]:
        args += ["--nan-imputation-method", nan_method]
    if o.get("use_barycorrpy"):
        args.append("--use_barycorrpy")

    args.append("--verbose")
    if test_run:
        args.append("--test_run")
        v = o.get("test_run_frames")
        if v not in (None, "") and v != RUN_DEFAULTS.get("test_run_frames"):
            args += ["--test_run_frames", str(v)]
    if o.get("overwrite", True):
        args.append("--overwrite")
    return args


def command_str(
    inst: str,
    date: str,
    target: str,
    options: dict | None = None,
    *,
    test_run: bool = False,
    run_id: str | None = None,
) -> str:
    if run_id is None:
        opts = normalize_run_options(options)
        run_id = build_run_id(
            inst, opts.get("site"), opts.get("mode"), opts.get("run_name"),
            telescope=opts.get("telescope"),
        )
    return shlex.join(build_command(inst, date, target, options, test_run=test_run, run_id=run_id))


# --------------------------- background job runner ---------------------------

# The in-memory job record and the finalizing state machine are shared with
# transit-fit via muscat_db.jobs (audit C1). ``Job`` keeps its historical name.
Job = jobs.PipelineJob

_JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()
# Concurrent full runs allowed across every process sharing this database. Set to
# 0 on a secondary instance (e.g. staging) so it never competes with production
# for the shared host: full runs are then refused outright, leaving test runs.
_MAX_FULL_JOBS = max(0, int(os.environ.get("MUSCAT_MAX_FULL_JOBS", "1")))
# Test runs are admitted without a durable slot, so they need a ceiling of their
# own or a caller varying the run key can spawn unbounded pipelines. Env-tunable
# for hosts with more cores than the 24-core production server.
_MAX_TEST_JOBS = max(1, int(os.environ.get("MUSCAT_MAX_TEST_JOBS", "4")))
_PROSE_PRODUCT_SUFFIXES = {".csv", ".npz", ".png", ".gif"}
_TARGET_PRODUCT_SUFFIXES = _PROSE_PRODUCT_SUFFIXES | {".txt"}

# Watchdog limits for hung reductions. A healthy run writes to its log
# continuously, so a long silence means it has stalled; the absolute cap is a
# backstop. Both are env-tunable. Observed legitimate full runs finish in <35 min.
_STALL_LIMIT_S = int(os.environ.get("MUSCAT_PHOT_STALL_LIMIT_S", 25 * 60))
_MAX_RUNTIME_S = int(os.environ.get("MUSCAT_PHOT_MAX_RUNTIME_S", 3 * 60 * 60))


def _count_running_full() -> int:
    """Number of currently-running full (non-test) photometry jobs."""
    return jobs.count_running_full(_JOBS)


def _count_running_test() -> int:
    """Number of currently-running test photometry jobs."""
    return jobs.count_running_test(_JOBS)


def job_key(inst: str, date: str, target: str, run_id: str = "") -> str:
    base = f"{inst}/{date}/{target.replace(' ', '')}"
    return f"{base}/{run_id}" if run_id else base


def _run_log_path(rdir: Path, inst: str, date: str, target: str, run_id: str = "") -> Path:
    """Return a deterministic, target-specific web-run log path."""
    digest = hashlib.sha256(job_key(inst, date, target, run_id).encode()).hexdigest()[:16]
    return rdir / f"_webrun_{digest}.log"


def log_path(inst: str, date: str, target: str, run_id: str = "") -> Path | None:
    try:
        rdir = run_output_dir(inst, date, target, run_id or None)
    except ValueError:
        return None
    p = _run_log_path(rdir, inst, date, target, run_id)
    return p if p.is_file() else None


def _target_product_paths(rdir: Path, inst: str, target: str) -> list[Path]:
    """Target-scoped prose products in a shared legacy date directory."""
    t = target.replace(" ", "")
    prefix = f"{t}_{inst}_"
    try:
        return [
            p for p in rdir.iterdir()
            if p.is_file()
            and p.name.startswith(prefix)
            and p.suffix.lower() in _TARGET_PRODUCT_SUFFIXES
        ]
    except OSError:
        return []


def _reduction_products_exist(
    inst: str,
    date: str,
    target: str,
    run_id: str,
) -> bool:
    """Check if prose products already exist for the given run."""
    try:
        rdir = run_output_dir(inst, date, target, run_id)
    except ValueError:
        return False
    if not rdir.is_dir():
        return False
    if run_id:
        try:
            return any(
                p.is_file() and p.suffix.lower() in _PROSE_PRODUCT_SUFFIXES
                for p in rdir.iterdir()
            )
        except OSError:
            return False
    return any(p.suffix.lower() in _PROSE_PRODUCT_SUFFIXES for p in _target_product_paths(rdir, inst, target))


def _delete_reduction_files(inst: str, date: str, target: str, run_id: str = "") -> tuple[bool, int, str | None]:
    """Delete on-disk products for one photometry run without touching job state."""
    try:
        rdir = run_output_dir(inst, date, target, run_id or None)
    except ValueError as exc:
        return False, 0, str(exc)
    if not rdir.is_dir():
        return True, 0, None

    removed = 0
    if run_id:
        try:
            for p in rdir.rglob("*"):
                if p.is_file():
                    removed += 1
            shutil.rmtree(rdir)
        except OSError as exc:
            return False, removed, f"failed to delete run: {exc}"
        return True, removed, None

    for p in _target_product_paths(rdir, inst, target):
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    web_log = _run_log_path(rdir, inst, date, target, run_id)
    if web_log.is_file():
        try:
            web_log.unlink()
            removed += 1
        except OSError:
            pass
    return True, removed, None


def start_run(
    inst: str,
    date: str,
    target: str,
    options: dict | None = None,
    test_run: bool = True,
    user_name: str | None = None,
    authorize_run: Callable[[str], None] | None = None,
) -> dict:
    """Launch a reduction in the background. Returns ``{ok, key}`` or
    ``{ok: False, error}``. A run already in flight for the same key is reused.

    *authorize_run* is called with the derived run id before anything is
    deleted or queued; it raises to refuse a launch over a run the caller may
    not replace.
    """
    if inst not in INSTRUMENTS:
        return {"ok": False, "error": f"unknown instrument {inst!r}"}
    if not valid_date(date):
        return {"ok": False, "error": "date must be 6-digit yymmdd"}
    if not (target or "").strip():
        return {"ok": False, "error": "target is required"}
    opts = normalize_run_options(options)
    err = validate_run_options(opts, inst=inst)
    if err:
        return {"ok": False, "error": err}
    rawdir = raw_data_dir(inst, date)
    if not rawdir.is_dir():
        return {"ok": False, "error": f"raw data not found: {rawdir}"}

    run_name = str(opts.get("run_name") or "").strip()
    site = (opts.get("site") or "").strip().lower() if inst in MULTISITE_INSTRUMENTS else ""
    telescope = (opts.get("telescope") or "").strip().lower() if inst in MULTISITE_INSTRUMENTS else ""
    mode = (opts.get("mode") or "").strip().lower() if inst in MULTISITE_MODES else ""
    run_id = build_run_id(inst, site, mode, run_name, telescope=telescope)
    if authorize_run is not None:
        authorize_run(run_id)
    key = job_key(inst, date, target, run_id)
    run_type = "test" if test_run else "full"

    with _LOCK:
        existing = _JOBS.get(key)
        overwrite = opts.get("overwrite", True)
        logger.info(f"start_run: {inst}/{date}/{target} run_id={run_id} overwrite={overwrite} existing={existing is not None}")

        # A live run is never restarted by a repeat launch, overwrite or not:
        # a double click or a second server process would otherwise kill a
        # 20+ minute reduction (or, from another process whose _JOBS cannot
        # see it, wipe the run directory under it). Restart means Cancel first.
        # The pid file covers jobs launched by a different server process.
        if existing is not None and existing.proc.poll() is None:
            live = True
        else:
            try:
                live = _detect_process_running(run_output_dir(inst, date, target, run_id))
            except ValueError:
                live = False
        if live:
            logger.info(f"start_run: {key} is already running; ignoring relaunch (overwrite={overwrite})")
            return {"ok": True, "key": key, "already_running": True, "run_id": run_id}

        if _reduction_products_exist(inst, date, target, run_id) and not overwrite:
            logger.info(f"start_run: refusing to overwrite existing reduction for {key} (overwrite=False)")
            return {
                "ok": False,
                "error": "reduction products already exist for this target; enable 'Overwrite existing products' to replace",
            }

        # Test runs take no durable slot, so cap them here instead: refuse rather
        # than queue, since a test run is a short interactive check and the
        # caller should retry once one finishes. Checked under _LOCK, after the
        # reuse-existing path above so re-running a live job is unaffected.
        if run_type != "full" and _count_running_test() >= _MAX_TEST_JOBS:
            logger.info(f"start_run: refusing test run for {key}; {_MAX_TEST_JOBS} already running")
            return {
                "ok": False,
                "error": f"{_MAX_TEST_JOBS} test runs are already in progress; wait for one to finish",
            }

        # A zero cap disables full runs entirely. Refuse instead of falling through
        # to the queue below: with no slot ever claimable the queued row would
        # never drain, so the job would sit pending forever with no explanation.
        if run_type == "full" and _MAX_FULL_JOBS == 0:
            logger.info(f"start_run: refusing full run for {key}; full runs disabled on this instance")
            return {
                "ok": False,
                "error": "full runs are disabled on this instance (MUSCAT_MAX_FULL_JOBS=0); run a test instead",
            }

        # The row carries its frame inputs, looked up here on the catalog's own
        # host, so a worker that launches or relaunches it never opens the
        # catalog (#298).
        frames = frames_inputs_param(inst, date, target)
        run_params = {"test_run": test_run, "options": opts, "run_id": run_id, "site": site, "telescope": telescope, "mode": mode, "run_name": run_name}
        if frames is not None:
            run_params["frames_inputs"] = frames
        run_params_json = json.dumps(run_params, separators=(",", ":"))

        # Claim a cross-process concurrency slot for full jobs (test jobs are
        # capped above instead). Not yet claimed here just means queue below.
        claimed_slot = False
        if run_type == "full":
            claimed_slot = get_job_store().claim_slot("photometry", key, _MAX_FULL_JOBS, **claim_caps(user_name))
        at_capacity = run_type == "full" and not claimed_slot

        # Queue full jobs when at capacity
        if at_capacity:
            try:
                get_job_store().enqueue(
                    type_="photometry",
                    inst=inst, date=date, target=target,
                    started_at=time.time(),
                    run_type=run_type,
                    params=params_with_running_cap(run_params_json, user_name),
                    run_id=run_id,
                    run_name=run_name,
                    user_name=user_name,
                    **enqueue_caps(user_name),
                )
            except JobLimitExceeded as exc:
                return limit_refusal(exc)
            except sqlite3.OperationalError as exc:
                return {"ok": False, "error": jobs.server_fault("The job could not be recorded because the job database is not writable", exc)}
            return {"ok": True, "key": key, "queued": True, "run_id": run_id}

        try:
            rdir = run_output_dir(inst, date, target, run_id)
        except ValueError as exc:
            if claimed_slot:
                get_job_store().release_slot("photometry", key)
            return {"ok": False, "error": str(exc)}
        if overwrite:
            ok, removed, delete_error = _delete_reduction_files(inst, date, target, run_id)
            if not ok:
                if claimed_slot:
                    get_job_store().release_slot("photometry", key)
                return {"ok": False, "error": jobs.server_fault("The previous reduction products could not be deleted before the re-run", delete_error)}
            if removed:
                logger.info(f"start_run: deleted {removed} previous product(s) for {key} before overwrite run")
        rdir.mkdir(parents=True, exist_ok=True)

        _write_run_meta(rdir, inst=inst, date=date, target=target, run_id=run_id, site=site, telescope=telescope, mode=mode, run_name=run_name, run_type=run_type)
        _write_run_product(rdir, inst=inst, date=date, target=target, opts=opts, test_run=test_run, user_name=user_name, site=site, telescope=telescope, mode=mode, run_name=run_name, frames=frames)
        cmd = build_command(inst, date, target, opts, test_run=test_run, run_id=run_id)
        log_path = _run_log_path(rdir, inst, date, target, run_id)
        logf = open(log_path, "w")
        logf.write(f"$ {shlex.join(cmd)}\n\n")
        logf.flush()
        try:
            env = _job_env()
            proc = subprocess.Popen(
                cmd,
                cwd=str(rdir),
                stdout=logf,
                stderr=subprocess.STDOUT,
                text=True,
                # Own session/process group so Cancel can kill the whole tree
                # (prose spawns multiprocessing workers via SequenceParallel).
                start_new_session=True,
                env=env,
            )
            try:
                with open(rdir / _PID_FILE_NAME, "w") as pidf:
                    pidf.write(str(proc.pid))
            except OSError:
                logger.debug("failed to write %s in %s", _PID_FILE_NAME, rdir, exc_info=True)
        except (FileNotFoundError, OSError) as exc:
            if claimed_slot:
                get_job_store().release_slot("photometry", key)
            logf.write(f"\nfailed to launch pipeline: {exc}\n")
            logf.close()
            return {"ok": False, "error": jobs.server_fault("The server could not start the photometry pipeline", exc)}
        _JOBS[key] = Job(
            key=key, inst=inst, date=date, target=target,
            cmd=cmd, proc=proc, logf=logf, log_path=log_path,
            run_type=run_type, run_id=run_id, site=site, telescope=telescope, mode=mode, run_name=run_name,
        )
        # Record new job in the database
        try:
                get_job_store().save(
                type_="photometry",
                inst=inst,
                date=date,
                target=target,
                state="running",
                returncode=None,
                elapsed=0,
                started_at=_JOBS[key].started_at,
                run_type=run_type,
                params=run_params_json,
                run_id=run_id,
                run_name=run_name,
                user_name=user_name,
                owner=current_owner(),
                instance_id=current_instance_id(),
            )
        except sqlite3.OperationalError as exc:
            # DB write failed (e.g. read-only database). Roll back the launched
            # process and job so we don't leak a running pipeline we can't track.
            if claimed_slot:
                get_job_store().release_slot("photometry", key)
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                logf.close()
            except OSError:
                pass
            _JOBS.pop(key, None)
            return {"ok": False, "error": jobs.server_fault("The job could not be recorded because the job database is not writable", exc)}
    return {"ok": True, "key": key, "run_id": run_id}


# Re-exported from the shared runner so transit-fit keeps importing it unchanged.
_tail = jobs.tail


# The pipeline is launched with start_new_session=True and prose spawns
# multiprocessing workers (SequenceParallel) that keep appending to the log
# *after* the tracked parent process returns. Declaring the job terminal at
# parent-exit freezes the photometry page's live log mid-output while the Jobs
# page (which reads the file directly) keeps advancing. Treat a finished parent
# as still "finalizing" until its log has been quiescent for this grace window,
# so the live view keeps streaming the trailing output. Env-tunable, in the
# style of _STALL_LIMIT_S / _MAX_RUNTIME_S.
_FINALIZE_GRACE_S = int(os.environ.get("MUSCAT_PHOT_FINALIZE_GRACE_S", 8))

# Once prose logs a terminal result line (SUCCEEDED / PARTIAL FAILURE / FAILED),
# its main work is done and the only remaining writes are worker teardown, so a
# much shorter quiescence window is enough to declare the job terminal. Before
# that line appears we keep the conservative default above so the live log never
# freezes mid-run. This lets a successful short run reload promptly instead of
# always waiting out the full window.
_FINALIZE_GRACE_TERMINAL_S = int(
    os.environ.get("MUSCAT_PHOT_FINALIZE_GRACE_TERMINAL_S", 2)
)

# Result lines emitted by prose's run_photometry once a run is decided. Any of
# these marks the end of the pipeline's own output (see run_photometry.py).
_TERMINAL_LOG_MARKERS = (
    "photometry SUCCEEDED",
    "photometry PARTIAL FAILURE",
    "photometry FAILED",
)
_PARTIAL_FAILURE_MARKER = "photometry PARTIAL FAILURE"
# prose logs this only after every band's outputs are written, so it is a
# reliable proxy for "the reduction finished successfully" even when the tracked
# parent was lost/killed (server --reload, watchdog) before muscat-db saw it exit.
_SUCCESS_MARKER = "photometry SUCCEEDED"


def _finalize_config() -> jobs.FinalizeConfig:
    """Build the finalizing config from the current module-level, env-tunable
    settings. Built per call (not cached) so tests/operators can monkeypatch
    ``_FINALIZE_GRACE_S`` etc. at runtime and have it take effect immediately."""
    return jobs.FinalizeConfig(
        grace_s=_FINALIZE_GRACE_S,
        grace_terminal_s=_FINALIZE_GRACE_TERMINAL_S,
        terminal_markers=_TERMINAL_LOG_MARKERS,
        partial_failure_marker=_PARTIAL_FAILURE_MARKER,
        success_marker=_SUCCESS_MARKER,
    )


# Thin wrappers binding the photometry finalize config; the shared logic lives
# in muscat_db.jobs (audit C1). Kept as module functions so existing call sites
# and tests that reference these names keep working.
def _log_has_partial_failure(path: Path | None) -> bool:
    return jobs.log_has_partial_failure(path, _finalize_config())


def _terminal_job_state(returncode: int, cancelled: bool, log_path_: Path | None) -> str:
    return jobs.terminal_job_state(returncode, cancelled, log_path_, _finalize_config())


def _log_has_terminal_marker(path: Path | None) -> bool:
    return jobs.log_has_terminal_marker(path, _finalize_config())


def _log_has_success(path: Path | None) -> bool:
    return jobs.log_has_success(path, _finalize_config())


def _finalize_grace_s(log_path_: Path | None) -> int:
    return jobs.finalize_grace_s(log_path_, _finalize_config())


def _log_quiescent(log_path_: Path, now: float) -> bool:
    return jobs.log_quiescent(log_path_, now, _finalize_config())


def _resolve_job_state(job: "Job", now: float | None = None) -> tuple[str, int | None, bool]:
    return jobs.resolve_job_state(job, _finalize_config(), now)


def _pending_status(inst: str, date: str, target: str, run_id: str = "") -> dict | None:
    """Return a queued-job status dict if a pending DB entry exists, else None.

    A full run launched while the single full-job slot is occupied is recorded
    in the DB as ``pending`` but not added to ``_JOBS``; surface that here so the
    photometry page can show a "queued" state instead of silently resetting.
    """
    db_key = f"photometry:{job_key(inst, date, target, run_id)}"
    try:
        for entry in get_job_store().all():
            if (
                entry["key"] == db_key
                and entry["type"] == "photometry"
                and entry["state"] == "pending"
            ):
                started = entry.get("started_at") or time.time()
                return {
                    "state": "pending",
                    "returncode": None,
                    "log": "",
                    "elapsed": round(time.time() - started),
                }
    except Exception:
        logger.debug("failed to read pending photometry status for %s/%s/%s/%s", inst, date, target, run_id, exc_info=True)
    return None


def _running_status(inst: str, date: str, target: str, run_id: str = "") -> dict | None:
    """Return a running-job status dict if a running DB entry exists, else None.

    A run held by another instance (a fleet worker) is ``running`` in the DB
    but absent from this process's ``_JOBS``. Report it as ``running`` with the
    log tail so the page keeps polling, rather than letting the on-disk log
    fallback in ``job_status`` present a still-growing log as a finished run.
    """
    db_key = f"photometry:{job_key(inst, date, target, run_id)}"
    try:
        for entry in get_job_store().all():
            if (
                entry["key"] == db_key
                and entry["type"] == "photometry"
                and entry["state"] == "running"
            ):
                lp = log_path(inst, date, target, run_id)
                started = entry.get("started_at") or time.time()
                return {
                    "state": "running",
                    "returncode": None,
                    "log": _tail(lp) if lp else "",
                    "elapsed": round(time.time() - started),
                }
    except Exception:
        logger.debug("failed to read running photometry status for %s/%s/%s/%s", inst, date, target, run_id, exc_info=True)
    return None


def _persisted_status(inst: str, date: str, target: str, run_id: str = "") -> dict | None:
    """Return a terminal-state status dict from the DB for a job no longer in
    ``_JOBS``.

    A run can leave ``_JOBS`` while still ending in error/cancelled/done: the
    watchdog kills hung runs and pops them, and a server restart loses the
    in-memory job entirely. Without this fallback ``job_status`` returns
    ``"none"``, and the page silently freezes the log instead of showing the
    failure. Surface the persisted outcome (plus its log tail and error
    description) so the final state is never lost.
    """
    db_key = f"photometry:{job_key(inst, date, target, run_id)}"
    try:
        for entry in get_job_store().all():  # newest-first; one row per key
            if entry["key"] != db_key or entry["type"] != "photometry":
                continue
            state = entry["state"]
            if state not in ("done", "error", "cancelled"):
                return None  # running/pending handled by the caller
            lp = log_path(inst, date, target, run_id)
            error_desc = entry.get("error_desc") or ""
            if state == "error" and not error_desc and lp:
                error_desc = _get_error_desc(lp, entry.get("returncode"))
            elif state == "cancelled" and not error_desc:
                error_desc = "Cancelled by user"
            return {
                "state": state,
                "returncode": entry.get("returncode"),
                "log": _tail(lp) if lp else "",
                "elapsed": round(entry.get("elapsed") or 0),
                "error_desc": error_desc,
            }
    except Exception:
        logger.debug("failed to read persisted photometry status for %s/%s/%s/%s", inst, date, target, run_id, exc_info=True)
    return None


def job_status(inst: str, date: str, target: str, run_id: str = "") -> dict:
    """Poll a job and return its state plus its target-specific log tail.

    A zero exit status is still an error when the pipeline logged
    ``photometry PARTIAL FAILURE``.
    """
    key = job_key(inst, date, target, run_id)
    with _LOCK:
        job = _JOBS.get(key)
        if job is None:
            pending = _pending_status(inst, date, target, run_id)
            if pending is not None:
                return pending
            running = _running_status(inst, date, target, run_id)
            if running is not None:
                return running
            persisted = _persisted_status(inst, date, target, run_id)
            if persisted is not None:
                return persisted
            # No job row (pruned, or run elsewhere) but the run's log file is
            # still on disk: surface it, as the Jobs page's Log button does.
            # The state stays "none" -- there is no record of the outcome.
            lp = log_path(inst, date, target, run_id)
            return {"state": "none", "log": _tail(lp) if lp else "", "returncode": None, "elapsed": 0}
        state, rc, is_terminal = _resolve_job_state(job)
        if is_terminal and job.state == "running":
            job.state = state
            job.returncode = rc
            job.elapsed = round(time.time() - job.started_at)
            try:
                job.logf.close()
            except OSError:
                pass
        job_log = job.log_path
        elapsed = job.elapsed if job.state not in ("running", "cancelling") and job.elapsed is not None else round(time.time() - job.started_at)
    error_desc = ""
    if state == "error" and job_log:
        error_desc = _get_error_desc(job_log, rc)
    elif state == "cancelled":
        error_desc = "Cancelled by user"
    return {
        "state": state,
        "returncode": rc,
        "log": _tail(job_log),
        "elapsed": round(elapsed),
        "error_desc": error_desc,
        "run_type": job.run_type if job else "full",
    }


# Process-group kill helpers live in the shared runner (audit C1).
_kill_after = jobs.kill_after
_terminate_pg = jobs.terminate_pg


def _watchdog_breach(job: "Job", now: float) -> str | None:
    """Return a kill reason if a running job looks hung, else None."""
    if now - job.started_at > _MAX_RUNTIME_S:
        return f"exceeded max runtime ({_MAX_RUNTIME_S // 3600}h)"
    try:
        mtime = job.log_path.stat().st_mtime
    except OSError:
        mtime = job.started_at
    stall = now - mtime
    if stall > _STALL_LIMIT_S:
        return f"no log output for {int(stall // 60)} min"
    return None


def delete_reduction(inst: str, date: str, target: str, run_id: str = "") -> dict:
    """Delete all reduction products for one (inst, date, target) from disk.

    Removes files matching the target's stem in the results directory, plus
    the web-run log. Also clears the persisted job record so the Jobs page
    no longer shows a stale entry. Never touches other targets.
    """
    try:
        rdir = run_output_dir(inst, date, target, run_id or None)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    with _LOCK:
        job = _JOBS.get(job_key(inst, date, target, run_id))
    if jobs.run_is_live(job.proc if job else None, rdir / _PID_FILE_NAME):
        return {"ok": False, "error": jobs.LIVE_RUN_DELETE_ERROR}
    ok, removed, error = _delete_reduction_files(inst, date, target, run_id)
    if not ok:
        return {"ok": False, "error": error or "failed to delete reduction"}
    get_job_store().delete(f"photometry:{job_key(inst, date, target, run_id)}")
    try:
        del _JOBS[job_key(inst, date, target, run_id)]
    except KeyError:
        pass
    return {"ok": True, "count": removed}


def cancel_run(inst: str, date: str, target: str, run_id: str = "") -> dict:
    """Cancel a running or pending reduction. Sends SIGTERM to the job's process group
    and escalates to SIGKILL after a short grace period."""
    key = job_key(inst, date, target, run_id)
    store = get_job_store()
    with _LOCK:
        job = _JOBS.get(key)
        if job is None:
            # May be a pending job (in DB but not yet launched)
            db_key = f"photometry:{key}"
            found = [j for j in store.all() if j["key"] == db_key]
            if found and found[0]["state"] == "pending":
                store.save(
                    type_="photometry", inst=inst, date=date, target=target,
                    state="cancelled", returncode=-1, elapsed=0,
                    started_at=found[0]["started_at"],
                    error_desc="Cancelled by user",
                    run_id=run_id,
                )
                return {"ok": True, "key": key}
            if (
                found and found[0]["state"] == "running"
                and jobs.held_by_other_instance(found[0], current_instance_id())
                and store.request_cancel(db_key)
            ):
                # A fleet worker launched it, so only that worker holds the
                # process; it acts on this request from its own sync_jobs pass.
                return {"ok": True, "key": key, "requested": True}
            return {"ok": False, "error": "no job to cancel"}
        if job.proc.poll() is not None:
            return {"ok": True, "already_finished": True}
        job.cancelled = True
        proc = job.proc

        # Immediately record cancellation in the database
        store.save(
            type_="photometry",
            inst=inst,
            date=date,
            target=target,
            state="cancelled",
            returncode=-1,
            elapsed=round(time.time() - job.started_at),
            started_at=job.started_at,
            error_desc="Cancelled by user",
            run_id=run_id,
        )

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            pass
    threading.Thread(target=_kill_after, args=(proc,), daemon=True).start()
    return {"ok": True, "key": key}


_ERROR_DESC_MAX = 200


def _clip_error(text: str) -> str:
    if len(text) <= _ERROR_DESC_MAX:
        return text
    return text[:_ERROR_DESC_MAX] + "… (see the log for the full message)"


def _get_error_desc(log_path: Path, returncode: int | None = None) -> str:
    """A failed job's ``error_desc``: the last error line of its log, led by
    how the process died when a signal killed it. A process killed for memory
    stops mid-stream, so its last logged error is usually an older, unrelated
    one; without the signal the description would blame that instead."""
    exit_text = jobs.describe_exit(returncode)
    if not log_path.is_file():
        return (
            f"{exit_text or 'The job failed'} and left no log, so the cause is unknown; "
            "please contact an admin if this keeps happening."
        )
    try:
        with open(log_path, errors="replace") as f:
            lines = [line.strip() for line in f if line.strip()]
    except OSError as exc:
        return f"{exit_text or 'The job failed'}, and its log could not be read ({exc})."
    if not lines:
        return (
            f"{exit_text or 'The job failed'} before writing any output; the engine "
            "probably could not start. Please contact an admin."
        )
    died = f"{exit_text}. " if jobs.exit_signal(returncode) is not None else ""
    for line in reversed(lines):
        if "Error" in line or "ERROR" in line or "Exception" in line or "failed" in line:
            # Remove ANSI escape codes
            clean = re.sub(r'\x1b\[[0-9;]*m', '', line)
            if " - ERROR: " in clean:
                clean = clean.split(" - ERROR: ")[1]
            elif " - WARNING: " in clean:
                clean = clean.split(" - WARNING: ")[1]
            return died + ("Last error in the log: " if died else "") + _clip_error(clean)
    last = _clip_error(re.sub(r'\x1b\[[0-9;]*m', '', lines[-1]))
    lead = f"{exit_text}. " if exit_text else "The job failed. "
    return f"{lead}There is no error line in the log; its last line was: {last}"


def _detect_process_running(rdir: Path) -> bool:
    return jobs.pid_file_process_alive(rdir / _PID_FILE_NAME)


def sync_jobs() -> None:
    store = get_job_store()
    jobs.apply_cancel_requests(
        store, "photometry", current_instance_id(),
        lambda r: cancel_run(r["inst"], r["date"], r["target"], r.get("run_id") or ""),
    )
    with _LOCK:
        # Watchdog: kill runs that have hung (no log output, or past the absolute
        # cap) and record them as errors. This frees the single full-job slot so the
        # queue-drain below can promote a pending job in the same pass.
        now = time.time()
        for key in list(_JOBS.keys()):
            job = _JOBS[key]
            if job.cancelled or job.state != "running" or job.proc.poll() is not None:
                continue
            reason = _watchdog_breach(job, now)
            if reason is None:
                continue
            _terminate_pg(job.proc)
            try:
                job.logf.close()
            except OSError:
                pass
            store.save(
                type_="photometry", inst=job.inst, date=job.date, target=job.target,
                state="error", returncode=-1,
                elapsed=round(now - job.started_at), started_at=job.started_at,
                error_desc=jobs.watchdog_desc(reason), run_type=job.run_type,
                run_id=job.run_id,
            )
            _JOBS.pop(key, None)

        db_jobs = store.all()
        running_keys = {j["key"] for j in db_jobs if j["state"] == "running" and j["type"] == "photometry"}
        db_by_key = {j["key"]: j for j in db_jobs}

        # Drop finished jobs a newer queued run has replaced (same key) so their
        # terminal state is not written over the pending row below.
        for key in list(_JOBS.keys()):
            stale = _JOBS[key]
            stale_key = f"photometry:{job_key(stale.inst, stale.date, stale.target, stale.run_id)}"
            if jobs.superseded_by_queued_row(db_by_key.get(stale_key), stale.started_at, stale.proc):
                try:
                    stale.logf.close()
                except OSError:
                    pass
                _JOBS.pop(key, None)

        for key, job in _JOBS.items():
            db_key = f"photometry:{job_key(job.inst, job.date, job.target, job.run_id)}"
            state, rc, is_terminal = _resolve_job_state(job)
            if is_terminal and job.state == "running":
                job.state = state
                job.returncode = rc
                job.elapsed = round(time.time() - job.started_at)
                try:
                    job.logf.close()
                except OSError:
                    pass

            # 'finalizing' is a live-view-only state; the DB tracks only
            # running/terminal, so persist a finalizing job as still running.
            # This keeps the Jobs page (which reads state from the DB) consistent
            # with the photometry page until the log truly goes quiescent.
            persist_state, persist_rc = jobs.persisted_state(state, rc)
            # Only persist when the row actually changed. A steadily-running job
            # whose DB row already says "running" needs no rewrite; elapsed is
            # computed live in the web layer, so we no longer write every 2s poll
            # just to bump it (each write also fired clear_all_caches, nullifying
            # the directory caches). Terminal transitions still write through.
            existing = db_by_key.get(db_key)
            # For terminal jobs: use stored elapsed (runtime), not time since start
            # For running jobs: calculate from current time
            if job.state not in ("running", "cancelling") and job.elapsed is not None:
                elapsed = job.elapsed  # Use calculated runtime when job hit terminal state
            elif existing is not None and existing.get("state") not in ("running", "cancelling"):
                elapsed = existing.get("elapsed") or 0  # Use existing DB value for completed jobs
            else:
                elapsed = round(time.time() - job.started_at)  # Calculate for running jobs
            unchanged = (
                existing is not None
                and existing.get("state") == persist_state
                and existing.get("returncode") == persist_rc
            )
            running_keys.discard(db_key)
            if unchanged:
                if persist_state == "running":
                    store.heartbeat(db_key, current_instance_id())
                continue

            error_desc = ""
            if persist_state == "done" and _log_has_partial_failure(job.log_path):
                persist_state = "error"
                job.state = "error"
                error_desc = _get_error_desc(job.log_path, persist_rc)
            elif persist_state == "error":
                error_desc = _get_error_desc(job.log_path, persist_rc)
            elif persist_state == "cancelled":
                error_desc = "Cancelled by user"

            store.save(
                type_="photometry",
                inst=job.inst,
                date=job.date,
                target=job.target,
                state=persist_state,
                returncode=persist_rc,
                elapsed=round(elapsed),
                started_at=job.started_at,
                error_desc=error_desc,
                run_id=job.run_id,
            )

            # A terminal transition may have produced new outputs; refresh the
            # target's persisted Phot/Fit status so the Targets page reflects it
            # on the next refresh instead of waiting for the daily build_db cron.
            if is_terminal and persist_state in ("done", "error", "cancelled"):
                database.refresh_target_status(job.target)
                # Notify chat once per job (dedup handled in jobs.fire_job_finished).
                jobs.fire_job_finished(
                    job_key=db_key, type_="photometry", target=job.target,
                    inst=job.inst, date=job.date, state=persist_state,
                    run_id=job.run_id,
                )

        for db_key in running_keys:
            _, rest = db_key.split(":", 1)
            parts = rest.split("/")
            if len(parts) < 3:
                continue
            inst, date, target = parts[:3]
            run_id = "/".join(parts[3:]) if len(parts) > 3 else ""
            started_at = time.time()
            elapsed = 0
            owner = ""
            instance_id = ""
            heartbeat_at = 0.0
            attempts = 0
            for j in db_jobs:
                if j["key"] == db_key:
                    started_at = j["started_at"]
                    elapsed = j["elapsed"]
                    owner = j.get("owner") or ""
                    instance_id = j.get("instance_id") or ""
                    heartbeat_at = j.get("heartbeat_at") or 0
                    attempts = int(j.get("attempts") or 0)
                    break
            if owner and owner != current_owner():
                # Another role's process (e.g. the web process, if this is the
                # standalone worker, or vice versa) launched this job and is
                # the only one whose own registry can tell whether it is truly
                # orphaned. Reconciling it here would be a false "Process
                # lost" verdict for a job that is not actually lost.
                continue
            if not jobs.is_orphan_reconcilable(instance_id, heartbeat_at, current_instance_id()):
                # A different *instance* of this same role (e.g. a sibling
                # `worker` process) holds this job and is still heartbeating --
                # only that instance's own sync_jobs() pass may judge it lost.
                # See job_store.py's _INSTANCE_ID docstring for the full
                # rationale; this closes the gap the owner check above cannot
                # (two same-role processes share one owner tag).
                continue
            # The tracked parent is gone (server --reload / restart lost _JOBS),
            # but prose's detached workers run independently and may well have
            # finished. Trust the log's success marker over the lost parent so a
            # completed reduction is not falsely reported as "exited with code -1".
            lp = log_path(inst, date, target, run_id)
            if _log_has_success(lp) and not _log_has_partial_failure(lp):
                store.save(
                    type_="photometry",
                    inst=inst,
                    date=date,
                    target=target,
                    state="done",
                    returncode=0,
                    elapsed=elapsed,
                    started_at=started_at,
                    error_desc="",
                    run_id=run_id,
                )
                database.refresh_target_status(target)
                continue
            try:
                rdir = run_output_dir(inst, date, target, run_id or None)
            except ValueError:
                rdir = None
            if rdir is not None and _detect_process_running(rdir):
                # The launching process is gone, but prose's own driver
                # process is still alive on the system and may yet finish --
                # leave state as "running" rather than relaunching a second
                # run into the same output directory.
                continue
            # No evidence of completion and the underlying process is gone
            # too -- reclaim-with-attempt-limit: retry by requeuing (the
            # pending-drain loop below relaunches it with its original,
            # preserved params) up to a limit, then give up for good.
            next_state, new_attempts = jobs.next_reconcile_attempt(attempts)
            if next_state == "pending":
                logger.warning(
                    "photometry job %s orphaned with no evidence of completion; "
                    "retrying (attempt %d)", db_key, new_attempts,
                )
                store.save(
                    type_="photometry",
                    inst=inst,
                    date=date,
                    target=target,
                    state="pending",
                    returncode=None,
                    elapsed=0,
                    started_at=time.time(),
                    error_desc="",
                    run_id=run_id,
                    attempts=new_attempts,
                )
            else:
                store.save(
                    type_="photometry",
                    inst=inst,
                    date=date,
                    target=target,
                    state="error",
                    returncode=-1,
                    elapsed=elapsed,
                    started_at=started_at,
                    error_desc=f"Process lost (server restart); gave up after {new_attempts} attempts",
                    run_id=run_id,
                    attempts=new_attempts,
                )
            database.refresh_target_status(target)

        # Release any concurrency slot whose claimant's persisted job row is
        # no longer 'running' (crashed/restarted without releasing cleanly).
        # Checked against the durable jobs table, so this is safe to run from
        # any process -- unlike the per-process _JOBS dict above, it does not
        # assume this process is the one that held the slot.
        store.reconcile_slots("photometry")

        # Launch pending full jobs if capacity allows
        if store.count_claimed("photometry") < _MAX_FULL_JOBS:
            pending = store.pending("photometry")
            for entry in pending:
                if store.count_claimed("photometry") >= _MAX_FULL_JOBS:
                    break
                run_id = entry.get("run_id") or ""
                # The DB key is prefixed ("photometry:..."), so compare against the
                # in-memory job key (unprefixed) to detect a job that is already
                # running and must not be relaunched from its stale pending row.
                candidate_key = job_key(entry["inst"], entry["date"], entry["target"], run_id)
                if candidate_key in _JOBS:
                    store.save(type_="photometry", inst=entry["inst"], date=entry["date"], target=entry["target"], state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc="Duplicate entry", run_id=run_id, run_name=entry.get("run_name", ""))
                    continue
                # Atomic: the slot and this row's pending -> running move are one
                # transaction, so two workers draining the same pending row can
                # never both launch it, even if a reconcile_slots pass elsewhere
                # frees the slot before the save(state="running") below.
                if not store.claim_slot("photometry", candidate_key, _MAX_FULL_JOBS, **claim_caps_for_row(entry), pending_key=entry["key"]):
                    continue
                try:
                    p = json.loads(entry.get("params") or "{}")
                except (json.JSONDecodeError, TypeError):
                    p = {}
                opts = p.get("options", {})
                test_run = p.get("test_run", True)
                inst, date, target = entry["inst"], entry["date"], entry["target"]
                run_id = p.get("run_id") or entry.get("run_id") or build_run_id(
                    inst, opts.get("site"), opts.get("mode"), opts.get("run_name"),
                    telescope=opts.get("telescope"),
                )
                site = p.get("site", opts.get("site", "")) if inst in MULTISITE_INSTRUMENTS else ""
                telescope = p.get("telescope", opts.get("telescope", "")) if inst in MULTISITE_INSTRUMENTS else ""
                mode = p.get("mode", opts.get("mode", "")) if inst in MULTISITE_MODES else ""
                run_name = p.get("run_name", opts.get("run_name", ""))
                key = job_key(inst, date, target, run_id)
                try:
                    rdir = run_output_dir(inst, date, target, run_id)
                except ValueError as exc:
                    store.release_slot("photometry", candidate_key)
                    store.save(type_="photometry", inst=inst, date=date, target=target, state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc=str(exc), run_id=run_id, run_name=run_name)
                    continue
                run_type = "test" if test_run else "full"
                try:
                    cmd = build_command(inst, date, target, opts, test_run=test_run, run_id=run_id)
                    rdir.mkdir(parents=True, exist_ok=True)
                    _write_run_meta(rdir, inst=inst, date=date, target=target, run_id=run_id, site=site, telescope=telescope, mode=mode, run_name=run_name, run_type=run_type)
                    _write_run_product(rdir, inst=inst, date=date, target=target, opts=opts, test_run=test_run, user_name=entry.get("user_name"), site=site, telescope=telescope, mode=mode, run_name=run_name, frames=_frames_from_params(p))
                except Exception as exc:
                    # The claim already moved the row to running: leave it
                    # terminal, not for orphan reconcile, which could trust a
                    # previous run's log in this reused directory.
                    logger.warning("failed to prepare queued photometry run %s", key, exc_info=True)
                    store.release_slot("photometry", candidate_key)
                    store.save(type_="photometry", inst=inst, date=date, target=target, state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc=f"Failed to prepare run: {exc}", run_id=run_id, run_name=run_name)
                    continue
                pending_log_path = _run_log_path(rdir, inst, date, target, run_id)
                try:
                    logf = open(pending_log_path, "w")
                    logf.write(f"$ {shlex.join(cmd)}\n\n")
                    logf.flush()
                    proc_env = _job_env()
                    proc = subprocess.Popen(cmd, cwd=str(rdir), stdout=logf, stderr=subprocess.STDOUT, text=True, start_new_session=True, env=proc_env)
                    try:
                        with open(rdir / _PID_FILE_NAME, "w") as pidf:
                            pidf.write(str(proc.pid))
                    except OSError:
                        logger.debug("failed to write %s for queued run %s", _PID_FILE_NAME, rdir, exc_info=True)
                except (FileNotFoundError, OSError) as exc:
                    try: logf.close()
                    except OSError: pass
                    store.release_slot("photometry", candidate_key)
                    store.save(type_="photometry", inst=inst, date=date, target=target, state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc=f"Failed to launch: {exc}", run_id=run_id, run_name=run_name)
                    continue
                _JOBS[key] = Job(key=key, inst=inst, date=date, target=target, cmd=cmd, proc=proc, logf=logf, log_path=pending_log_path, run_type=run_type, run_id=run_id, site=site, telescope=telescope, mode=mode, run_name=run_name)
                try:
                    store.save(type_="photometry", inst=inst, date=date, target=target, state="running", returncode=None, elapsed=0, started_at=_JOBS[key].started_at, run_type=run_type, params=entry.get("params", ""), run_id=run_id, run_name=run_name, owner=current_owner(), instance_id=current_instance_id(), attempts=int(entry.get("attempts") or 0))
                except sqlite3.OperationalError as exc:
                    try: proc.terminate()
                    except OSError: pass
                    try: logf.close()
                    except OSError: pass
                    _JOBS.pop(key, None)
                    store.release_slot("photometry", candidate_key)
                    store.save(type_="photometry", inst=inst, date=date, target=target, state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc=f"Database not writable: {exc}", run_id=run_id, run_name=run_name)
