"""A test run is optimize-only: it launches the MAP helper, never timer-fit's
MCMC, and the page treats the helper's result line as a finished run."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from muscat_db import _optimize_helper as helper
from muscat_db import transit_fit as fit

TEMPLATE = Path(fit.__file__).parent / "templates" / "transit_fit.html"
ENV_PY = "/envs/timer/bin/python"


class _Proc:
    pid = 1

    def poll(self):
        return None


def _launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, test_run: bool) -> list[str]:
    source_csv = tmp_path / "Target_muscat3_gp_250101.csv"
    source_csv.write_text("time,flux\n")
    captured: list[list[str]] = []

    def _fake_popen(cmd, **_kwargs):
        captured.append(cmd)
        return _Proc()

    monkeypatch.setattr(fit, "fit_output_dir", lambda *_args: tmp_path / "run")
    monkeypatch.setattr(fit, "get_csv_lightcurves", lambda *_args: [source_csv])
    monkeypatch.setattr(fit, "_conda_env_python", lambda _env: ENV_PY)
    monkeypatch.setattr(fit, "_timer_prefix", lambda: ["timer-fit"])
    monkeypatch.setattr(fit.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(fit, "_FIT_JOBS", {})
    monkeypatch.setattr(fit, "_MAX_FULL_JOBS", 1)
    monkeypatch.setattr(fit, "get_job_store", lambda: _Store())
    try:
        result = fit.start_fit("muscat3", "250101", "Target", {"planets": "b"}, test_run=test_run)
        assert result["ok"] is True, result
    finally:
        for job in fit._FIT_JOBS.values():
            job.logf.close()
    # the log banner also shells out (timer version lookup); the fit is last
    return captured[-1]


class _Store:
    def claim_slot(self, *_args):
        return True

    def save(self, **_kwargs):
        return None


def test_test_run_launches_optimize_helper_not_timer_fit(tmp_path, monkeypatch):
    cmd = _launch(tmp_path, monkeypatch, test_run=True)
    assert cmd[0] == ENV_PY
    assert Path(cmd[2]) == fit._OPTIMIZE_HELPER
    assert cmd[-1] == str(tmp_path / "run")
    assert "timer-fit" not in cmd


def test_full_run_still_launches_timer_fit(tmp_path, monkeypatch):
    cmd = _launch(tmp_path, monkeypatch, test_run=False)
    assert cmd == ["timer-fit", "-v", str(tmp_path / "run")]


def test_optimize_helper_script_ships_with_package():
    assert fit._OPTIMIZE_HELPER.is_file()


def test_helper_done_line_is_a_terminal_marker():
    # finalizing and orphan recovery both key off these markers; a test run
    # that never matches would sit in "finalizing" for the full grace window
    assert helper.DONE_MARKER in fit._TERMINAL_LOG_MARKERS


def test_run_type_detected_as_test_from_helper_banner(tmp_path):
    (tmp_path / "timer-fit.log").write_text(
        f"$ {ENV_PY} -u {fit._OPTIMIZE_HELPER} {tmp_path}\n"
    )
    assert fit._detect_run_type(tmp_path) == "test"


def test_format_map_params_lists_parameters_only():
    soln = {
        "ror": np.array([0.2497151234]),
        "ror_interval__": np.array([-1.1]),
        "i_mean": np.array(-0.16113),
        "i_weights": np.array([1.0, -2.5]),
        "i_lm": np.zeros(3),
        "i_light_curves": np.zeros((3, 1)),
        "i_light_curves_hr": np.zeros((500, 1)),
        "big": np.zeros(helper._MAX_PARAM_SIZE + 1),
        "gp": object(),
    }
    assert helper.format_map_params(soln) == [
        "i_mean = -0.16113",
        "i_weights[0] = 1",
        "i_weights[1] = -2.5",
        "ror = 0.249715",
    ]


def test_button_relabelled_as_optimize_test_run():
    html = TEMPLATE.read_text()
    assert "▶ Test run (optimize)" in html
    assert "Run Test Fit (steps=20)" not in html
