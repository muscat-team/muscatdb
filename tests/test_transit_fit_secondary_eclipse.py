"""Unsupported secondary eclipse requests fail explicitly."""
from muscat_db import transit_fit as fit
import yaml


def test_secondary_eclipse_is_rejected():
    assert "not supported" in fit.validate_fit_options({"secondary_eclipse": "true"})


def test_ignored_keys_are_not_written(tmp_path):
    fit._write_fit_inputs(tmp_path, "muscat4", "250512", "star", [], {
        "secondary_eclipse": "true", "plot_midtransit": "true", "plot_ingress_egress": "true"})
    config = yaml.safe_load((tmp_path / "fit.yaml").read_text())
    assert not {"secondary_eclipse", "plot_midtransit", "plot_ingress_egress"} & config.keys()
