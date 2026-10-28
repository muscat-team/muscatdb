"""On-host migration checks; all science outputs stay under pytest's temp dir.

Run with TMPDIR=~/temp uv run pytest -m slow tests/test_pinned_engines_slow.py
after provisioning deploy/engines.toml. No web job rows or production outputs
are changed. Photometry checks the product contract; timer also compares the
unchanged engine with its baseline. These are not convergence-quality MCMC runs.
"""
from __future__ import annotations

import os
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


def _run(command, directory):
    # Launch with the job environment and log header production uses, so a
    # frozen run gets PROSE_CONFIG_FILE/PROSE_CONFIG_SHA256 and the digest line.
    env = {**phot._job_env(), "TMPDIR": str(directory), "PYTHONNOUSERSITE": "1",
           "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2"}
    # Each command is bounded, and its output remains available on failure.
    with (directory / "smoke.log").open("w") as log:
        phot._write_log_header(log, command)
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
