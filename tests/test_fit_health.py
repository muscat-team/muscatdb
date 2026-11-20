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
    assert all(value is None for value in health["metrics"].values())
    assert health["warnings"] == []


def test_ephemeris_quality_boundaries():
    from muscat_db.transit_fit import midtime_review

    def review(snr=3, rhat=1.01, divergences=0, chains=None, bulk=400, tail=400):
        return midtime_review({"metrics": {"Detection SNR": snr, "Max R-hat": rhat,
                                           "Divergences": divergences, "Min bulk ESS": bulk,
                                           "Min tail ESS": tail}, "chains": chains})

    assert review() == review(snr=0.69) == review(snr=None) == ([], [])
    assert review(rhat=1.02)[0] == ["Max R-hat 1.02 above 1.01"]
    assert review(bulk=399)[0] == ["Min bulk ESS 399 below 400"]
    assert review(tail=399)[0] == ["Min tail ESS 399 below 400"]
    assert review(rhat=None, divergences=None, bulk=None, tail=None) == (
        [], ["Max R-hat", "Divergences", "Min bulk ESS", "Min tail ESS"])
    assert review(chains=1, divergences=1)[0] == [
        "1 divergence after tuning", "Single chain; convergence unverified"]


def test_clean_completed_sampling_and_ess_minima(tmp_path):
    outputs = {"summary": {"rows": [{"ess_bulk": "nan", "ess_tail": "399"},
                                    {"ess_bulk": "450", "ess_tail": "500"}]},
               "detection_snr": None}
    path = tmp_path / "timer-fit.log"
    path.write_text("Sampling 4 chains for 1000 tune and 1000 draw iterations took 20 seconds.\n")
    assert transit_fit._timer_health(tmp_path, outputs)["metrics"]["Divergences"] is None
    with path.open("a") as f:
        f.write("Timer-fit completed successfully in 90 seconds\n")
    health = transit_fit._timer_health(tmp_path, outputs)
    assert health["metrics"]["Divergences"] == 0
    assert health["metrics"]["Min bulk ESS"] == 450
    assert health["warnings"] == ["Min tail ESS 399 below 400"]
    with path.open("a") as f:
        f.write(" - INFO - Output directory: out\nTimer-fit completed successfully\n")
    assert transit_fit._timer_health(tmp_path, outputs)["metrics"]["Divergences"] is None


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
