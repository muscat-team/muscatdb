"""Expose the pinned engine's precision controls without changing its policy."""
import numpy as np
import pytest

from muscat_db import photometry as phot

OPTIONS = dict(comp_ensemble="bright", comp_criterion="check", comp_min_flux_frac="0.4",
               clip_star_sigma="5", clip_star_window="13", save_flux_datacube=True)


def test_precision_options_reach_engine(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
    command = phot.build_command("muscat3", "261115", "k2-384", OPTIONS, test_run=False)
    for key in OPTIONS.keys() - {"save_flux_datacube"}:
        assert command[command.index("--" + key) + 1] == str(phot.normalize_run_options(OPTIONS)[key])
    assert "--save_flux_datacube" in command
    default = phot.build_command("muscat3", "261115", "k2-384", {}, test_run=False)
    assert not any("--" + key in default for key in OPTIONS)


@pytest.mark.parametrize("key,value", [
    ("comp_ensemble", "bad"), ("comp_criterion", "bad"),
    ("comp_min_flux_frac", "nan"), ("comp_min_flux_frac", "-1"),
    ("comp_min_flux_frac", "garbage"), ("clip_star_sigma", "inf"),
    ("clip_star_sigma", "0"), ("clip_star_sigma", "-1"),
    ("clip_star_window", "2"), ("clip_star_window", "3.5"),
])
def test_precision_options_reject_invalid_values(key, value):
    options = phot.normalize_run_options({key: value})
    assert phot.validate_run_options(options, "muscat3")
    with pytest.raises(ValueError):
        phot.build_command("muscat3", "261115", "k2-384", options)


def test_noise_budget_and_datacube_are_separate_products(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
    directory = phot.run_output_dir("sinistro", "261115", "target")
    directory.mkdir(parents=True)
    stem = "target_sinistro_coj_tel05_zs_261115"
    np.savez(directory / (stem + ".npz"), zs__noise__expected_ppt=2.04,
             zs__noise__achieved_ptp_ppt=2.0, zs__noise__ratio=2/2.04,
             zs__noise__photon_ppt=np.nan)
    np.savez(directory / (stem + "_flux_datacube.npz"), flux=np.zeros((2, 3, 4)))
    np.savez(directory / "target_sinistro_elp_tel06_zs_261115.npz", zs__noise__expected_ppt=99)
    out = phot.list_outputs("sinistro", "261115", "target", site="coj")
    assert out["npz"] == stem + ".npz"
    assert out["flux_datacube"] == stem + "_flux_datacube.npz"
    assert out["noise_budget"]["zs"]["expected_ppt"] == 2.04
    assert out["noise_budget"]["zs"]["photon_ppt"] is None
    (directory / (stem + ".npz")).write_bytes(b"incomplete archive")
    assert phot.list_outputs("sinistro", "261115", "target", site="coj")["noise_budget"] == {}
