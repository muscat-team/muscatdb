"""On-host migration checks; all science outputs stay under pytest's temp dir.

Run with TMPDIR=~/temp uv run pytest -m slow tests/test_pinned_engines_slow.py
after provisioning deploy/engines.toml. No web job rows or production outputs
are changed. Photometry checks the product contract and compares two nights with
production 3.4.0; timer compares the unchanged engine with its baseline. These
are not convergence-quality MCMC runs.
"""
from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from muscat_db import photometry as phot, transit_fit

pytestmark = pytest.mark.slow
_ROOT = Path(__file__).resolve().parents[1]
_PINS = tomllib.loads((_ROOT / "deploy/engines.toml").read_text())["engines"]


def _pinned_interpreter(engine):
    pin = _PINS[engine]
    python = phot._conda_env_python(pin["environment"])
    if not python:
        pytest.skip(f"{engine} pinned environment is required")
    # A half-finished clone may still import the editable development checkout.
    conda = Path(python).parents[3] / "bin/conda"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "deploy/engines.py"), "verify", engine,
         "--conda", str(conda)], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return python


def _interpreters(engine):
    baseline = phot._conda_env_python(_PINS[engine]["source_environment"])
    if not baseline:
        pytest.skip(f"{engine} baseline environment is required")
    return baseline, _pinned_interpreter(engine)


def _run(command, directory, *, config=None):
    # Launch with the job environment and log header production uses, so a
    # frozen run gets PROSE_CONFIG_FILE/PROSE_CONFIG_SHA256 and the digest line.
    env = {**phot._job_env(), "TMPDIR": str(directory), "PYTHONNOUSERSITE": "1",
           "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2"}
    if config is not None:
        env["PROSE_CONFIG_FILE"] = str(config)
        env["PROSE_CONFIG_SHA256"] = hashlib.sha256(config.read_bytes()).hexdigest()
    # Each command is bounded, and its output remains available on failure.
    with (directory / "smoke.log").open("w") as log:
        if config is None:
            phot._write_log_header(log, command)
        else:
            log.write(f"$ {shlex.join(command)}\n\n")
            log.write(f"MuSCAT prose telescope snapshot sha256: {env['PROSE_CONFIG_SHA256']}\n")
            log.flush()
        result = subprocess.run(command, cwd=directory, env=env, stdout=log,
                                stderr=subprocess.STDOUT, timeout=1800)
    assert result.returncode == 0, (directory / "smoke.log").read_text()[-6000:]


@pytest.mark.parametrize("preview", [True, False], ids=["preview", "full"])
def test_photometry_pinned_install_produces_timer_inputs(tmp_path, monkeypatch, preview):
    python = _pinned_interpreter("prose")
    if not phot.raw_data_dir("sinistro", "260624").is_dir():
        pytest.skip("Sinistro/260624 raw data unavailable")
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
    monkeypatch.setenv("MUSCAT_OBSLOG_DIR", str(tmp_path / "obslog"))
    monkeypatch.delenv("MUSCAT_PROSE_PYTHON", raising=False)
    monkeypatch.setenv("MUSCAT_PROSE_CONDA_ENV", _PINS["prose"]["environment"])
    monkeypatch.setenv("MUSCAT_PROSE_FROZEN_CONFIG", "1")
    options = {
        "bands": ["zs"], "ref_band": "zs", "target_coord": "261.784554 -25.969731",
        "avoid_comparison_ids": "0,1,2", "avoid_nearby_star_mode": "custom",
        "avoid_nearby_star": "1.0", "min_star_separation": 4.0, "max_num_stars": 40,
        "aper_radii": "3,8,1", "annulus": "12,18", "plot_gaia_sources": False,
        "make_gif": False,
    }
    command = phot.build_command("sinistro", "260624", "TIC88297141", options,
                                 test_run=preview)
    assert command[0] == python
    command[command.index("--results_dir") + 1] = str(tmp_path)
    # Bound host resources for the smoke run; the GUI retains engine defaults.
    command += ["--workers", "2"]
    _run(command, tmp_path)
    assert "telescope snapshot sha256:" in (tmp_path / "smoke.log").read_text()
    assert list(tmp_path.glob("*_ref.png")), "reference diagnostic missing"
    if not preview:
        csvs = list(tmp_path.glob("*_zs_260624.csv"))
        assert csvs, "no light curve produced"
        assert list(tmp_path.glob("*.npz")), "photometry archive missing"
        for csv in csvs:
            curve = pd.read_csv(csv)
            assert len(curve) > phot.RUN_DEFAULTS["test_run_frames"]
            columns = ["BJD_TDB", "Flux", "Err"]
            assert np.isfinite(curve[columns].to_numpy()).all()
            assert (np.diff(curve["BJD_TDB"]) > 0).all()
            assert (curve["Err"] > 0).all()
            # 3.5.1 deliberately changes times, errors and retained frames.
            # Check that the timer handoff preserves those measurements exactly.
            staged = tmp_path / ("timer_" + csv.name)
            transit_fit._copy_lightcurve_for_timer(csv, staged)
            pd.testing.assert_frame_equal(curve[columns], pd.read_csv(staged)[columns],
                                          check_exact=True)


@pytest.mark.parametrize("preview", [True, False], ids=["optimize", "sample"])
def test_timer_pinned_install_matches_baseline(tmp_path, preview):
    interpreters = _interpreters("timer")
    source = Path(os.environ.get("MUSCAT_PIN_SMOKE_TIMER_RUN",
        str(Path.home() / "ql/timer/muscat2/260810/KIC/default")))
    if not (source / "fit.yaml").is_file():
        pytest.skip("timer KIC multi-band fixture unavailable")
    summaries = []
    for label, python in zip(("baseline", "pinned"), interpreters):
        directory = tmp_path / label
        directory.mkdir()
        config = yaml.safe_load((source / "fit.yaml").read_text())
        for data in config["data"].values():
            original = source / data["file"]
            if not original.is_file():
                pytest.skip(f"fixture light curve missing: {original.name}")
            shutil.copy2(original, directory / original.name)
            data["file"] = original.name
        config.update(tune=20, draws=20, chains=1, cores=1, random_seed=101,
                      plot_midtransit=False, plot_ingress_egress=False)
        (directory / "fit.yaml").write_text(yaml.safe_dump(config))
        shutil.copy2(source / "sys.yaml", directory / "sys.yaml")
        if preview:
            command = [python, "-u", str(transit_fit._OPTIMIZE_HELPER), str(directory)]
        else:
            command = [python, "-m", "timer.fit", "-v", str(directory)]
        _run(command, directory)
        assert list((directory / "out").glob("*.png")), "fit plots missing"
        if not preview:
            summary = pd.read_csv(directory / "out/summary.csv", index_col=0)
            assert not summary["mean"].isna().any()
            summaries.append(summary)
    if not preview:
        assert summaries[0].index.equals(summaries[1].index)
        # Same engine, dependencies, seed and input must reproduce the fit.
        np.testing.assert_allclose(summaries[0]["mean"], summaries[1]["mean"],
                                   rtol=0, atol=1e-8)


# Production before the 3.5.1 rollout, not the editable source_environment or
# the unreleased 3.5.0 candidate. Keep this baseline fixed for future pin checks.
_PROSE_PRODUCTION = "prose-prod-3daed56b"
_PROSE_PRODUCTION_CONFIG = _ROOT / "tests/data/prose_340_telescopes.yaml"


@pytest.mark.parametrize("instrument,date,target,coord", [
    ("muscat3", "230605", "TOI-3884", "181.57265454 12.50691217"),
    ("muscat2", "200227", "WASP104b", "160.602369 7.435077"),
])
def test_photometry_pinned_matches_production(tmp_path, monkeypatch,
                                             instrument, date, target, coord):
    """Compare complete g-band nights against production's exposure-start CSVs.

    3.5.1 changes uncertainties and may reject invalid target frames. Match on
    exposure start (midpoint minus EXPTIME/2), never by row number. Flux tolerance
    is an absolute 0.001 (1000 ppm), not a relative tolerance on Julian dates.
    """
    from astropy.io import fits

    baseline = phot._conda_env_python(_PROSE_PRODUCTION)
    if not baseline:
        pytest.skip("production prose 3.4.0 baseline environment is required")
    if not phot.raw_data_dir(instrument, date).is_dir():
        pytest.skip(f"{instrument}/{date} raw data unavailable")
    pinned = _pinned_interpreter("prose")
    calibration = Path.home() / f"ql/prose/muscat2/{date}_calibrated"
    if instrument == "muscat2" and not calibration.is_dir():
        pytest.skip("MuSCAT2/200227 calibrated fixture unavailable")
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
    monkeypatch.setenv("MUSCAT_OBSLOG_DIR", str(tmp_path / "obslog"))
    monkeypatch.delenv("MUSCAT_PROSE_PYTHON", raising=False)
    monkeypatch.setenv("MUSCAT_PROSE_CONDA_ENV", _PINS["prose"]["environment"])
    monkeypatch.setenv("MUSCAT_PROSE_FROZEN_CONFIG", "1")
    options = {
        "bands": ["gp"], "target_coord": coord, "make_gif": False,
        "plot_gaia_sources": False,
    }
    if instrument == "muscat2":
        # The recorded successful reduction selected IDs 2 and 3 on frame 417.
        # Production 3.4.0's automatic selector returns no stars on this night;
        # fix the same known comparison ensemble in both runs for a meaningful
        # numerical comparison (the automatic failure is documented separately).
        options.update(comparison_ids="2,3", refid=417)
    command = phot.build_command(instrument, date, target, options, test_run=False)
    curves = []
    exposures = []
    for label, python in (("baseline", baseline), ("pinned", pinned)):
        directory = tmp_path / label
        directory.mkdir()
        run = command.copy()
        run[0] = python
        run[run.index("--results_dir") + 1] = str(directory)
        if instrument == "muscat2":
            # A calibration cache can be repaired in-place by the engine. Use
            # private copies so the regression can never alter archived FITS.
            local_calibration = directory / "calibrated"
            local_calibration.mkdir()
            sources = sorted(calibration.glob("MCT20*_calibrated.fits"))
            assert sources, "g-band calibrated frames missing"
            for source in sources:
                shutil.copy2(source, local_calibration / source.name)
            if "--calib_dir" in run:
                run[run.index("--calib_dir") + 1] = str(local_calibration)
            else:
                run += ["--calib_dir", str(local_calibration)]
        if label == "pinned":
            run += ["--workers", "2", "--plots", "none"]
        # 3.4.0 has no --workers. Affinity bounds its pool's aggregate CPU use
        # without patching the installed engine. On-host checks target Linux.
        if hasattr(os, "sched_getaffinity"):
            cpus = ",".join(map(str, sorted(os.sched_getaffinity(0))[:2]))
            run = ["taskset", "-c", cpus, *run]
        config = (_PROSE_PRODUCTION_CONFIG if label == "baseline"
                  else _ROOT / "src/muscat_db/_prose_telescopes.yaml")
        _run(run, directory, config=config)
        # Finite fluxes can still describe the wrong star when localization
        # fails and prose falls back to the brightest detection.
        log = (directory / "smoke.log").read_text()
        assert "Target not found" not in log, "catalog target localization failed"
        assert "defaulting to source" not in log, "unexpected brightest-star fallback"
        # MuSCAT2's observing-night directory can precede the UTC product date.
        csvs = list(directory.glob(f"{target}_{instrument}_gp_??????.csv"))
        assert len(csvs) == 1, "expected one g-band light curve"
        csv = csvs[0]
        curves.append(pd.read_csv(csv))
        header = fits.Header.fromtextfile(csv.with_name(csv.stem + "_ref_header.txt"))
        exposures.append(float(header["EXPTIME"]))
    assert exposures[0] == exposures[1]
    exposure = exposures[0]
    if instrument == "muscat2":
        exposure = _muscat2_exposures(curves[0], calibration)
    _assert_prose_migration(curves[0], curves[1], exposure)


def _muscat2_exposures(baseline, calibration):
    """Associate retained frames with FITS exposure metadata, not the reference."""
    from astropy.io import fits

    metadata = []
    for source in calibration.glob("MCT20*_calibrated.fits"):
        header = fits.getheader(source)
        metadata.append({"GJD_UTC": float(header["MJD-STRT"]) + 2400000.5,
                         "exposure": float(header["EXPTIME"])})
    frames = pd.DataFrame(metadata).sort_values("GJD_UTC")
    matched = pd.merge_asof(baseline[["GJD_UTC"]], frames, on="GJD_UTC",
                            direction="nearest", tolerance=0.001 / 86400)
    assert np.isfinite(matched["exposure"]).all(), "FITS exposure metadata missing"
    assert (matched["exposure"] > 0).all()
    return matched["exposure"].to_numpy()


def _assert_prose_migration(baseline, pinned, exposure):
    assert list(baseline.columns) == list(pinned.columns)
    assert len(pinned) > phot.RUN_DEFAULTS["test_run_frames"]
    for curve in (baseline, pinned):
        assert np.isfinite(curve[["BJD_TDB", "GJD_UTC", "Flux", "Err"]].to_numpy()).all()
        assert (np.diff(curve["GJD_UTC"]) > 0).all()
        assert (np.diff(curve["BJD_TDB"]) > 0).all()
        assert (curve["Err"] > 0).all()
    # Each retained midpoint must correspond uniquely to an old exposure.
    old = baseline.copy()
    old["exposure"] = exposure
    old["midpoint"] = old["GJD_UTC"] + old["exposure"] / (2 * 86400)
    matched = pd.merge_asof(pinned.sort_values("GJD_UTC"), old.sort_values("midpoint"),
                            left_on="GJD_UTC", right_on="midpoint", tolerance=0.001 / 86400,
                            direction="nearest", suffixes=("_new", "_old"))
    assert matched["GJD_UTC_old"].notna().all(), "midpoints do not match exposure starts"
    assert matched["GJD_UTC_old"].is_unique, "multiple midpoints matched one exposure"
    # A 1% frame-loss budget allows invalid-target rejection, not silent losses.
    assert len(pinned) >= 0.99 * len(baseline)
    np.testing.assert_allclose(
        (matched["GJD_UTC_new"] - matched["GJD_UTC_old"]) * 86400,
        matched["exposure"] / 2, atol=0.001, rtol=0)
    # Barycentric correction changes over the exposure: allow 10 ms rather
    # than requiring the UTC and TDB offsets to be identical.
    np.testing.assert_allclose(
        (matched["BJD_TDB_new"] - matched["BJD_TDB_old"]) * 86400,
        matched["exposure"] / 2, atol=0.01, rtol=0)
    np.testing.assert_allclose(matched["Flux_new"], matched["Flux_old"],
                               atol=0.001, rtol=0)
