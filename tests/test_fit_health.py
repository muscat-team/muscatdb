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


def test_ephemeris_quality_boundaries():
    from muscat_db.transit_fit import midtime_review

    def review(snr=3, rhat=1.009, divergences=0, chains=None):
        return midtime_review({"metrics": {"Detection SNR": snr, "Max R-hat": rhat,
                                           "Divergences": divergences}, "chains": chains})

    assert review() == ([], [])
    assert review(snr=2.999)[0] == ["Detection SNR 2.999 below 3"]
    assert review(rhat=1.01)[0] == ["Max R-hat 1.01 at or above 1.01"]
    assert review(snr=None)[0] == ["Detection SNR unavailable"]
    assert review(rhat=None, divergences=None) == ([], ["Max R-hat", "Divergences"])
    reasons, _ = review(snr=0.69, rhat=None, divergences=1, chains=1)
    assert reasons == ["1 divergence after tuning", "Single chain; convergence unverified",
                       "Detection SNR 0.69 below 3"]


def test_chain_count_from_latest_sampling_run(tmp_path):
    outputs = {"summary": None, "detection_snr": None}
    log = tmp_path / "timer-fit.log"
    log.write_text("INFO - Sampling 1 chain for 20 tune and 20 draw iterations\n")
    assert transit_fit._timer_health(tmp_path, outputs)["chains"] == 1
    with log.open("a") as f:
        f.write(" - INFO - Output directory: out\n")
    assert transit_fit._timer_health(tmp_path, outputs)["chains"] is None
    with log.open("a") as f:
        f.write("INFO - Sampling 4 chains for 1000 tune and 1000 draw iterations\n")
    assert transit_fit._timer_health(tmp_path, outputs)["chains"] == 4
