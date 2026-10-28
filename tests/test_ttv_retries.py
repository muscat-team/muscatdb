"""Recover from transient helper failures without waiting for cache expiry."""
import json
import subprocess
from unittest.mock import Mock

import pytest

from muscat_db import ttv_fit as ttv


@pytest.fixture
def saved_run(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSCAT_TTV_DIR", str(tmp_path))
    rdir = ttv.ttv_output_dir("audit", "default")
    rdir.mkdir(parents=True)
    for name in ("samples.csv.gz", "data.csv", "config.ini", "fit_config.json"):
        (rdir / name).write_text("x")
    ttv._ttv_model_cache.clear()
    yield rdir
    ttv._ttv_model_cache.clear()


@pytest.mark.parametrize("operation", ["model", "bic", "ranking"])
@pytest.mark.parametrize("failure", ["environment", "exit", "timeout", "json"])
def test_failed_evaluation_is_retried_but_success_is_cached(saved_run, monkeypatch, operation, failure):
    calls = {
        "model": lambda: ttv.get_ttv_model("audit", "default", "2026-10-28"),
        "bic": lambda: ttv.compute_delta_bic("audit", "default"),
        "ranking": lambda: ttv.get_ttv_ranking("audit", "default", "2026-10-01", "2026-10-28"),
    }
    values = {
        "model": {"points": {"b": [{"epoch": 0, "tc": 2461342}]}},
        "bic": dict(delta_bic=10, evidence="strong", chi2_lin=20, chi2_harm=5,
                    k_lin=2, k_harm=4, n_data=10),
        "ranking": {"rows": [], "sigmas": {"b": 0.001}, "rank_by": "total"},
    }
    env = Mock(return_value="/fake/python")
    monkeypatch.setattr(ttv, "_conda_env_python", env)
    success = subprocess.CompletedProcess([], 0, json.dumps(values[operation]), "")
    failed = {
        "environment": success,
        "exit": subprocess.CompletedProcess([], 1, "", "temporary failure"),
        "timeout": subprocess.TimeoutExpired("helper", 300),
        "json": subprocess.CompletedProcess([], 0, "invalid json", ""),
    }[failure]
    run = Mock(side_effect=[failed, success])
    if failure == "environment":
        env.side_effect = [None, "/fake/python"]
        run.side_effect = [success]
    monkeypatch.setattr(ttv.subprocess, "run", run)
    call = calls[operation]
    assert call()["ok"] is False
    recovered = call()
    assert recovered["ok"] is True
    assert call() == recovered
    assert env.call_count == 2
    assert run.call_count == (1 if failure == "environment" else 2)
