"""A Gaussian prior entered without an uncertainty gets a default width (#458)."""
import io

import yaml

from muscat_db import transit_fit as fit


def test_defaulted_widths_lists_sampled_mean_only_priors():
    options = {"planets": "b", "fixed": [], "period": "2.17", "t0": "2461314.86",
               "dur": "0.0875", "ror": "0.065,0.01", "b": "0.2"}
    # t0 is not listed: timer samples it from the Uniform transit-center prior,
    # whose width is tc_pred_unc, not the sys.yaml uncertainty.
    assert fit.defaulted_prior_widths(options) == [
        ("b", "period", 0.001), ("b", "dur", 0.01), ("b", "b", 0.1)]


def test_fixed_and_uniform_parameters_are_not_listed():
    options = {"planets": "b,c", "fixed": ["period", "t0"], "period": "2.17", "t0": "2461314.86",
               "dur_b": "0.0875", "dur_prior_b": "uniform", "ror_c": "0.05", "b_c": "0.1,0.1"}
    assert fit.defaulted_prior_widths(options) == [("c", "ror", 0.005)]


def test_default_fixed_list_applies_when_the_request_has_none():
    # Period and u_star are fixed unless the request says otherwise.
    assert ("b", "period", 0.001) not in fit.defaulted_prior_widths({"period": "2.17"})


def test_banner_names_the_defaulted_widths():
    log = io.StringIO()
    fit._write_log_banner(log, ["timer-fit"], {"planets": "b", "dur": "0.0875", "fixed": []})
    text = log.getvalue()
    assert "no uncertainty entered" in text
    assert "b: dur width 0.01 (default)" in text


def test_sys_yaml_uses_the_same_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(fit, "_timer_version", lambda: "test")
    csv = tmp_path / "input.csv"
    csv.write_text("BJD_TDB,Flux,Err\n2460000,1,0.001\n2460000.3,1,0.001\n")
    out = tmp_path / "run"
    out.mkdir()
    fit._write_fit_inputs(out, "muscat3", "260101", "star", [csv],
                          {"t0": "2460000.1", "period": "1", "dur": "0.08", "ror": "0.05", "b": "0.2"})
    planet = yaml.safe_load((out / "sys.yaml").read_text())["planets"]["b"]
    for param, width in fit.PRIOR_WIDTH_DEFAULTS.items():
        assert planet[param][1] == width
    assert "t0" not in fit.PRIOR_WIDTH_DEFAULTS


def test_second_planet_without_an_uncertainty_gets_the_advertised_default(tmp_path, monkeypatch):
    """The form also sends the first planet's values as unsuffixed keys. Those
    belong to the first planet only, so planet c's blank duration uncertainty
    must give the default the banner and the page announce (review on #490)."""
    monkeypatch.setattr(fit, "_timer_version", lambda: "test")
    csv = tmp_path / "input.csv"
    csv.write_text("BJD_TDB,Flux,Err\n2460000,1,0.001\n2460000.3,1,0.001\n")
    out = tmp_path / "run"
    out.mkdir()
    options = {"planets": "b,c", "fixed": [],
               "t0_b": "2460000.1", "period_b": "1", "dur_b": "0.09", "dur_unc_b": "0.03",
               "t0_c": "2460000.2", "period_c": "2", "dur_c": "0.1",
               # the first planet's values, repeated unsuffixed
               "t0": "2460000.1", "period": "1", "dur": "0.09", "dur_unc": "0.03"}
    fit._write_fit_inputs(out, "muscat3", "260101", "star", [csv], options)
    planets = yaml.safe_load((out / "sys.yaml").read_text())["planets"]
    assert planets["b"]["dur"] == [0.09, 0.03]
    assert planets["c"]["dur"] == [0.1, fit.PRIOR_WIDTH_DEFAULTS["dur"]]
    log = io.StringIO()
    fit._write_log_banner(log, ["timer-fit"], options)
    assert "c: dur width 0.01 (default)" in log.getvalue()
    assert "b: dur width" not in log.getvalue()


def test_unsuffixed_keys_still_serve_a_single_planet(tmp_path, monkeypatch):
    monkeypatch.setattr(fit, "_timer_version", lambda: "test")
    csv = tmp_path / "input.csv"
    csv.write_text("BJD_TDB,Flux,Err\n2460000,1,0.001\n2460000.3,1,0.001\n")
    out = tmp_path / "run"
    out.mkdir()
    fit._write_fit_inputs(out, "muscat3", "260101", "star", [csv],
                          {"t0": "2460000.1", "period": "1", "dur": "0.09", "dur_unc": "0.03"})
    assert yaml.safe_load((out / "sys.yaml").read_text())["planets"]["b"]["dur"] == [0.09, 0.03]
