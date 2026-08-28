"""#77: a test run must get its small sampler size from fit.yaml, not timer's
``--test_run`` flag.

timer upstream ``master`` has no ``--test_run`` flag — only the checked-out
fork does — so relying on it to force chains=1/cores=2 (and a short trace)
blocks pointing timer at upstream. muscat-db already writes every value that
flag would force; ``_write_fit_inputs`` now writes them directly for a test
run instead of leaving it to the engine.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from muscat_db import transit_fit as fit

INST = "muscat4"
DATE = "250512"
TARGET = "TOI-1234"


def _fit_yaml(tmp_path: Path, run_type: str, options: dict) -> dict:
    fit._write_fit_inputs(tmp_path, INST, DATE, TARGET, [], options, run_type=run_type)
    return yaml.safe_load((tmp_path / "fit.yaml").read_text())


def test_test_run_writes_timers_test_run_sampler_size(tmp_path):
    data = _fit_yaml(tmp_path, "test", {"planets": "b"})
    assert data["tune"] == 20
    assert data["draws"] == 20
    assert data["chains"] == 1
    assert data["cores"] == 2


def test_test_run_sampler_size_ignores_user_supplied_options(tmp_path):
    """A test run is a quick sanity check; a bigger request must not defeat that."""
    data = _fit_yaml(
        tmp_path, "test",
        {"planets": "b", "tune": "5000", "draws": "5000", "chains": "8", "cores": "8"},
    )
    assert data["tune"] == 20
    assert data["draws"] == 20
    assert data["chains"] == 1
    assert data["cores"] == 2


def test_full_run_keeps_its_own_defaults(tmp_path):
    data = _fit_yaml(tmp_path, "full", {"planets": "b"})
    assert data["tune"] == 2000
    assert data["draws"] == 2000
    assert data["chains"] == 4
    assert data["cores"] == 4


def test_full_run_honors_user_supplied_sampler_options(tmp_path):
    data = _fit_yaml(
        tmp_path, "full",
        {"planets": "b", "tune": "500", "draws": "1000", "chains": "2", "cores": "2"},
    )
    assert data["tune"] == 500
    assert data["draws"] == 1000
    assert data["chains"] == 2
    assert data["cores"] == 2


def test_start_fit_never_passes_test_run_flag_to_timer(tmp_path, monkeypatch):
    """timer upstream master doesn't define --test_run; the flag must be gone
    from the launched command entirely, test run or not (#77)."""
    source_csv = tmp_path / "source.csv"
    source_csv.write_text("time,flux\n")
    run_dir = tmp_path / "run"

    captured_cmds = []

    class _Proc:
        pid = 1

        def poll(self):
            return None

    def _fake_popen(cmd, **_kwargs):
        captured_cmds.append(cmd)
        return _Proc()

    monkeypatch.setattr(fit, "fit_output_dir", lambda *_args: run_dir)
    monkeypatch.setattr(fit, "get_csv_lightcurves", lambda *_args: [source_csv])
    monkeypatch.setattr(fit, "_timer_prefix", lambda: ["timer-fit"])
    monkeypatch.setattr(fit.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(fit, "_FIT_JOBS", {})
    monkeypatch.setattr("muscat_db.database.save_job", lambda **_kwargs: None)

    try:
        result = fit.start_fit(
            "muscat3", "250101", "Target", {"planets": "b"}, test_run=True,
        )
        assert result["ok"] is True
        assert len(captured_cmds) == 1
        assert "--test_run" not in captured_cmds[0]

        fit_yaml = yaml.safe_load((run_dir / "fit.yaml").read_text())
        assert fit_yaml["tune"] == 20
        assert fit_yaml["draws"] == 20
        assert fit_yaml["chains"] == 1
        assert fit_yaml["cores"] == 2
    finally:
        for job in fit._FIT_JOBS.values():
            job.logf.close()
