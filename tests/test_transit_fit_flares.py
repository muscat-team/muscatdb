"""Multi-flare options: one row per flare, validated before launch (issue #340).

The page sends each flare parameter as ``value,unc`` pairs joined by ``;``, one
pair per flare. ``validate_fit_options`` has to refuse a malformed or
inconsistent set of rows with a message naming the row, and the launch path has
to refuse a ``tpeak`` that lies outside the selected light curves before any
run directory is touched. ``_write_fit_inputs`` keeps writing the same
equal-length lists timer reads.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from muscat_db import transit_fit as fit

LC_T0 = 2461314.80
LC_T1 = 2461314.918
MID = (LC_T0 + LC_T1) / 2


def _lightcurve(tmp_path: pathlib.Path, name: str = "TOI-1000_muscat3_gp_261001.csv") -> pathlib.Path:
    rows = [f"{LC_T0 + i * 0.002:.5f},1.0,0.001" for i in range(60)]
    p = tmp_path / name
    p.write_text("BJD_TDB,Flux,Err\n" + "\n".join(rows) + "\n")
    return p


def _flare_options(**overrides) -> dict:
    options = {
        "planets": "b",
        "include_flare": "true",
        "flare_tpeak": f"{MID:.5f},0.01",
        "flare_tpeak_prior": "gaussian",
        "flare_fwhm": "0.02,0.01",
        "flare_fwhm_prior": "gaussian",
        "flare_ampl": "0.01,0.01",
        "flare_ampl_prior": "gaussian",
    }
    options.update(overrides)
    return options


# --- validate_fit_options ------------------------------------------------


def test_complete_flare_rows_validate():
    assert fit.validate_fit_options(_flare_options()) is None


def test_two_complete_flare_rows_validate():
    options = _flare_options(
        flare_tpeak=f"{MID:.5f},0.01; {MID + 0.02:.5f},0.01",
        flare_fwhm="0.02,0.01; 0.03,0.01",
        flare_ampl="0.01,0.01; 0.02,0.01",
    )
    assert fit.validate_fit_options(options) is None


def test_malformed_flare_row_is_refused_naming_the_row():
    options = _flare_options(flare_tpeak=f"{MID:.5f},0.01; abc,0.01")

    error = fit.validate_fit_options(options)

    assert error is not None
    assert "tpeak" in error and "row 2" in error


def test_incomplete_flare_row_is_refused_naming_the_row():
    options = _flare_options(flare_ampl="0.01,0.01; 0.02")

    error = fit.validate_fit_options(options)

    assert error is not None
    assert "ampl" in error and "row 2" in error


def test_blank_flare_tpeak_is_refused_when_flares_are_on():
    error = fit.validate_fit_options(_flare_options(flare_tpeak=""))

    assert error is not None
    assert "tpeak" in error


def test_flare_count_mismatch_is_refused():
    options = _flare_options(
        flare_tpeak=f"{MID:.5f},0.01; {MID + 0.02:.5f},0.01",
        flare_fwhm="0.02,0.01; 0.03,0.01; 0.04,0.01",
    )

    error = fit.validate_fit_options(options)

    assert error is not None
    assert "fwhm" in error and "3" in error and "2" in error


def test_single_fwhm_and_ampl_broadcast_across_flares_like_timer():
    options = _flare_options(flare_tpeak=f"{MID:.5f},0.01; {MID + 0.02:.5f},0.01")

    assert fit.validate_fit_options(options) is None


def test_nonpositive_gaussian_flare_uncertainty_is_refused():
    error = fit.validate_fit_options(_flare_options(flare_fwhm="0.02,0"))

    assert error is not None
    assert "fwhm" in error


def test_uniform_flare_bounds_must_be_ordered():
    options = _flare_options(flare_ampl="0.05,0.01", flare_ampl_prior="uniform")

    error = fit.validate_fit_options(options)

    assert error is not None
    assert "ampl" in error and "low < high" in error


def test_flare_rows_are_ignored_when_flares_are_off():
    options = _flare_options(include_flare="false", flare_tpeak="abc")

    assert fit.validate_fit_options(options) is None


def test_flare_tpeak_outside_the_lightcurve_is_refused():
    error = fit.validate_fit_options(_flare_options(flare_tpeak="2460000.5,0.01"), time_range=(LC_T0, LC_T1))

    assert error is not None
    assert "tpeak" in error and "row 1" in error
    assert f"{LC_T0:.5f}" in error and f"{LC_T1:.5f}" in error


def test_flare_tpeak_inside_the_lightcurve_is_accepted():
    assert fit.validate_fit_options(_flare_options(), time_range=(LC_T0, LC_T1)) is None


def test_uniform_flare_tpeak_bounds_must_overlap_the_lightcurve():
    options = _flare_options(flare_tpeak="2460000.0,2460000.5", flare_tpeak_prior="uniform")

    error = fit.validate_fit_options(options, time_range=(LC_T0, LC_T1))

    assert error is not None
    assert "tpeak" in error and "row 1" in error


def test_flare_tpeak_range_is_skipped_when_the_range_is_unknown():
    assert fit.validate_fit_options(_flare_options(flare_tpeak="2460000.5,0.01")) is None


# --- light-curve time range ----------------------------------------------


def test_lightcurve_time_range_reads_the_bjd_column(tmp_path):
    lo, hi = fit.lightcurve_time_range([_lightcurve(tmp_path)])

    assert lo == pytest.approx(LC_T0)
    assert hi == pytest.approx(LC_T1)


def test_lightcurve_time_range_is_unknown_without_a_bjd_column(tmp_path):
    p = tmp_path / "TOI-1000_muscat3_gp_261001.csv"
    p.write_text("time,flux\n1,2\n")

    assert fit.lightcurve_time_range([p]) is None
    assert fit.lightcurve_time_range([]) is None


def test_lightcurve_time_range_spans_every_selected_curve(tmp_path):
    first = _lightcurve(tmp_path)
    second = tmp_path / "TOI-1000_muscat3_rp_261001.csv"
    second.write_text("BJD_TDB,Flux,Err\n2461314.70,1.0,0.001\n2461314.75,1.0,0.001\n")

    lo, hi = fit.lightcurve_time_range([first, second])

    assert lo == pytest.approx(2461314.70)
    assert hi == pytest.approx(LC_T1)


# --- fit.yaml ----------------------------------------------------------------


def test_two_flare_rows_write_equal_length_lists(tmp_path):
    options = _flare_options(
        flare_tpeak=f"{MID:.5f},0.01; {MID + 0.02:.5f},0.02",
        flare_fwhm="0.02,0.01; 0.03,0.01",
        flare_ampl="0.01,0.01; 0.02,0.01",
    )
    assert fit.validate_fit_options(options) is None

    fit._write_fit_inputs(tmp_path, "muscat3", "261001", "TOI-1000", [], options)
    flare = yaml.safe_load((tmp_path / "fit.yaml").read_text())["flare"]

    assert flare["tpeak"] == pytest.approx([MID, MID + 0.02], abs=1e-5)
    assert flare["tpeak_unc"] == [0.01, 0.02]
    assert flare["fwhm"] == [0.02, 0.03]
    assert flare["ampl"] == [0.01, 0.02]
    assert len(flare["tpeak"]) == len(flare["fwhm"]) == len(flare["ampl"]) == 2


# --- launch path ---------------------------------------------------------


class _MustNotReachStore:
    def __getattr__(self, name):
        pytest.fail(f"a launch with an invalid flare must stop before the job store ({name})")


def test_start_fit_refuses_out_of_range_tpeak_before_touching_the_run(monkeypatch, tmp_path):
    csv_path = _lightcurve(tmp_path)
    rdir = tmp_path / "fit-run"
    monkeypatch.setattr(fit, "_FIT_JOBS", {})
    monkeypatch.setattr(fit, "fit_output_dir", lambda *_a: rdir)
    monkeypatch.setattr(fit, "get_job_store", lambda: _MustNotReachStore())
    monkeypatch.setattr(fit, "get_csv_lightcurves", lambda *a, **k: [csv_path])
    monkeypatch.setattr(fit, "validate_no_duplicate_datasets", lambda *a, **k: None)

    result = fit.start_fit(
        "muscat3", "261001", "TOI-1000",
        options=_flare_options(flare_tpeak="2460000.5,0.01", run_mode="new"), test_run=True,
    )

    assert result["ok"] is False
    assert "tpeak" in result["error"] and "row 1" in result["error"]
    assert not rdir.exists()


def test_compute_logp_refuses_out_of_range_tpeak(monkeypatch, tmp_path):
    csv_path = _lightcurve(tmp_path)
    monkeypatch.setattr(fit, "fit_output_dir", lambda *_a: tmp_path / "fit-run")
    monkeypatch.setattr(fit, "get_csv_lightcurves", lambda *a, **k: [csv_path])
    monkeypatch.setattr(fit, "validate_no_duplicate_datasets", lambda *a, **k: None)
    monkeypatch.setattr(fit, "_conda_env_python", lambda *_a: pytest.fail("logP must not run"))

    result = fit.compute_logp("muscat3", "261001", "TOI-1000", _flare_options(flare_tpeak="2460000.5,0.01"))

    assert result["ok"] is False
    assert "tpeak" in result["error"] and "row 1" in result["error"]
