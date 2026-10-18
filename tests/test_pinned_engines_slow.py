"""On-host migration checks; all science outputs stay under pytest's temp dir.

Run with TMPDIR=~/temp uv run pytest -m slow tests/test_pinned_engines_slow.py
after provisioning deploy/engines.toml. No web job rows or production outputs
are changed. These are smoke comparisons, not convergence-quality MCMC runs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
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


def _interpreters(engine):
    pin = _PINS[engine]
    paths = [phot._conda_env_python(pin[key])
             for key in ("source_environment", "environment")]
    if not all(paths):
        pytest.skip(f"{engine} baseline and pinned environments are required")
    return paths


def _run(command, directory):
    env = {**os.environ, "TMPDIR": str(directory), "PYTHONNOUSERSITE": "1",
           "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2"}
    # Each command is bounded, and its output remains available on failure.
    with (directory / "smoke.log").open("w") as log:
        result = subprocess.run(command, cwd=directory, env=env, stdout=log,
                                stderr=subprocess.STDOUT, timeout=1800)
    assert result.returncode == 0, (directory / "smoke.log").read_text()[-6000:]


@pytest.mark.parametrize("preview", [True, False], ids=["preview", "full"])
def test_photometry_pinned_install_matches_baseline(tmp_path, monkeypatch, preview):
    interpreters = _interpreters("prose")
    if not phot.raw_data_dir("sinistro", "260624").is_dir():
        pytest.skip("Sinistro/260624 raw data unavailable")
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
    options = {
        "bands": ["zs"], "ref_band": "zs", "target_coord": "261.784554 -25.969731",
        "avoid_comparison_ids": "0,1,2", "avoid_nearby_star_mode": "custom",
        "avoid_nearby_star": "1.0", "min_star_separation": 4.0, "max_num_stars": 40,
        "aper_radii": "3,8,1", "annulus": "12,18", "plot_gaia_sources": False,
        "make_gif": False,
    }
    outputs = []
    for label, python in zip(("baseline", "pinned"), interpreters):
        directory = tmp_path / label
        directory.mkdir()
        command = phot.build_command("sinistro", "260624", "TIC88297141", options,
                                     test_run=preview)
        # Exercise the exact package entrypoint without relying on PATH or cwd.
        prefix_length = len(phot._prose_prefix())
        command = [python, "-m", "prose.scripts.run_photometry", *command[prefix_length:]]
        command[command.index("--results_dir") + 1] = str(directory)
        _run(command, directory)
        assert list(directory.glob("*_ref.png")), "reference diagnostic missing"
        outputs.append(directory)
    if not preview:
        before = list(outputs[0].glob("*_zs_260624.csv"))
        assert before, "no baseline light curve produced"
        for csv in before:
            left = pd.read_csv(csv)
            right = pd.read_csv(outputs[1] / csv.name)
            assert len(left) > phot.RUN_DEFAULTS["test_run_frames"]
            pd.testing.assert_frame_equal(left, right, check_exact=False, rtol=1e-8, atol=1e-10)
            for column in left:
                if column.casefold() in {"jd", "bjd", "bjd_tdb", "time"}:
                    np.testing.assert_array_equal(left[column], right[column])


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
