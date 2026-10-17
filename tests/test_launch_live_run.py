"""A fit launch must not start on top of a run another server process holds.

The in-memory job dict only sees jobs launched by this process. Past it, a
launch rewrites the inputs, truncates the live log and overwrites the pid file,
leaving the first run untracked. The pid file covers the other-process case.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from muscat_db import transit_fit, ttv_fit


class _MustNotReachStore:
    def __getattr__(self, name):
        pytest.fail(f"a launch over a live run must stop before the job store ({name})")


def test_start_fit_rejects_run_held_by_another_process(monkeypatch, tmp_path):
    rdir = tmp_path / "fit-run"
    rdir.mkdir()
    (rdir / "timer-fit.pid").write_text(str(os.getpid()))
    (rdir / "timer-fit.log").write_text("live output\n")
    monkeypatch.setattr(transit_fit, "_FIT_JOBS", {})
    monkeypatch.setattr(transit_fit, "fit_output_dir", lambda *_a: rdir)
    monkeypatch.setattr(transit_fit, "get_job_store", lambda: _MustNotReachStore())
    monkeypatch.setattr(transit_fit, "_fit_reduction_exists", lambda *a, **k: False)
    monkeypatch.setattr(transit_fit, "validate_fit_options", lambda options: None)
    monkeypatch.setattr(
        transit_fit, "get_csv_lightcurves",
        lambda *a, **k: [pathlib.Path("TOI-123_muscat3_g.csv")],
    )
    monkeypatch.setattr(transit_fit, "validate_no_duplicate_datasets", lambda *a, **k: None)

    result = transit_fit.start_fit(
        "muscat3", "260720", "TOI-123", options={"run_mode": "new"}, test_run=False,
    )

    assert result["ok"] is True
    assert result["already_running"] is True
    assert (rdir / "timer-fit.log").read_text() == "live output\n"
    assert (rdir / "timer-fit.pid").read_text() == str(os.getpid())


def test_start_ttv_fit_rejects_run_held_by_another_process(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_TTV_DIR", str(tmp_path))
    rdir = ttv_fit.ttv_output_dir("TOI-123", "r1")
    rdir.mkdir(parents=True)
    (rdir / "harmonic.pid").write_text(str(os.getpid()))
    (rdir / "harmonic.log").write_text("live output\n")
    monkeypatch.setattr(ttv_fit, "_TTV_JOBS", {})
    monkeypatch.setattr(ttv_fit, "get_job_store", lambda: _MustNotReachStore())
    monkeypatch.setattr(ttv_fit, "validate_ttv_options", lambda options: None)

    result = ttv_fit.start_ttv_fit("TOI-123", options={"run_name": "r1"})

    assert result["ok"] is True
    assert result["already_running"] is True
    assert (rdir / "harmonic.log").read_text() == "live output\n"
    assert (rdir / "harmonic.pid").read_text() == str(os.getpid())
