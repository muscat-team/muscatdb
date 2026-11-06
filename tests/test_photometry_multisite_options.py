"""Site and mode options are validated against the run's own instrument.

Both the form check (``validate_run_options``) and ``build_command``'s
allow-list used sinistro's sites and modes for every instrument, so a qhy600
or sbig run at ogg (the one LCO site sinistro has no unit at) was refused
with "site must be one of lsc, cpt, coj, tfn, elp", and qhy600's
``central30x30`` readout mode was refused the same way.
"""

from __future__ import annotations

import pytest

from muscat_db import photometry as phot

DATE = "261011"
TARGET = "TOI-6109"


def _options(**extra):
    return phot.normalize_run_options({"bands": ["gp"], **extra})


@pytest.mark.parametrize("inst", ["qhy600", "sbig"])
@pytest.mark.parametrize("site", ["ogg", "OGG", "lsc"])
def test_every_site_of_the_instrument_is_accepted(inst, site, monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
    assert phot.validate_run_options(_options(site=site), inst=inst) is None
    argv = phot.build_command(inst, DATE, TARGET, _options(site=site), test_run=False)
    assert argv[argv.index("--site") + 1] == site


def test_qhy600_readout_mode_is_accepted(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
    assert phot.validate_run_options(_options(mode="central30x30"), inst="qhy600") is None
    argv = phot.build_command("qhy600", DATE, TARGET, _options(mode="central30x30"), test_run=False)
    assert argv[argv.index("--mode") + 1] == "central30x30"


def test_sinistro_still_refuses_ogg(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
    err = phot.validate_run_options(_options(site="ogg"), inst="sinistro")
    assert err == f"site must be one of {', '.join(phot.MULTISITE_SITES['sinistro'])}"
    with pytest.raises(ValueError, match="site"):
        phot.build_command("sinistro", DATE, TARGET, _options(site="ogg"), test_run=False)


def test_sinistro_refuses_a_qhy600_only_mode():
    assert "mode must be one of" in phot.validate_run_options(_options(mode="central30x30"), inst="sinistro")


def test_error_lists_the_instruments_own_sites():
    err = phot.validate_run_options(_options(site="xyz"), inst="qhy600")
    assert err == f"site must be one of {', '.join(phot.MULTISITE_SITES['qhy600'])}"
    assert "ogg" in err
