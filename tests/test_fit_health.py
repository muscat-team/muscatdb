import json

from muscat_db import transit_fit, ttv_fit


def test_single_chain_divergence(tmp_path, monkeypatch):
    monkeypatch.setattr(transit_fit, "fit_output_dir", lambda *args: tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "summary.csv").write_text(",mean,sd,r_hat\nx,1,0.1,nan\n")
    (out / "timer-fit.log").write_text("ERROR - There was 1 divergence after tuning.\n")
    health = transit_fit.get_fit_outputs("muscat", "date", "target")["health"]
    assert health["metrics"]["Max R-hat"] is None
    assert health["metrics"]["Divergences"] == 1
    assert health["warnings"] == ["1 divergence after tuning"]


def test_harmonic_health_without_bic(tmp_path, monkeypatch):
    monkeypatch.setattr(ttv_fit, "ttv_output_dir", lambda *args: tmp_path)
    (tmp_path / "fit_stats.json").write_text(json.dumps({
        "converged": False, "tau_max": 146.6, "accept_frac": 0.325,
    }))
    outputs = ttv_fit.get_ttv_outputs("target")
    assert outputs["fit_stats"] is None
    assert outputs["health"]["warnings"] == ["Sampler reported non-convergence"]
    assert outputs["health"]["metrics"]["Acceptance fraction"] == 0.325


def test_optimize_only_rerun_discards_old_posterior_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr(transit_fit, "fit_output_dir", lambda *args: tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "summary.csv").write_text(",mean,sd,r_hat\nror[0],0.1,0.01,1.2\n")
    (out / "timer-fit.log").write_text("There were 2 divergences after tuning.\n")
    (tmp_path / "timer-fit.log").write_text("Optimize-only test run started (no MCMC sampling)\n")
    health = transit_fit.get_fit_outputs("muscat", "date", "target")["health"]
    assert list(health["metrics"].values()) == [None, None, None]
    assert health["warnings"] == []
