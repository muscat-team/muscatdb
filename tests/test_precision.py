"""Time-series noise model (muscat_db.precision), phase 1 of #344."""

import math

import pytest

from muscat_db import precision as subject
from muscat_db.exposure_model import BandModel, fwhm_arcsec


def _model(**overrides):
    values = dict(band="rp", mode="", zp_safe=25.2, zp_median=25.0, a_focus=1.0, a_defocus=1.1, defocus_c=0.75,
                  defocus_c_safe=0.5, seeing_good=1.25, seeing_median=1.6, pixscale=0.267, level=120000.0,
                  n_frames=100)
    values.update(overrides)
    return BandModel(**values)


def _budget(mag=10.0, exptime=30.0, **overrides):
    values = dict(airmass=1.2, extinction=0.09, diameter_m=2.0, altitude_m=3055.0, read_noise_e=6.0,
                  sky_rate=20.0)
    values.update(overrides)
    return subject.noise_budget(_model(), mag, exptime, **values)


# --- scintillation ---------------------------------------------------------------

def test_scintillation_matches_osborn_2015_equation_at_unit_inputs():
    # Osborn et al. 2015 eq. 7: sigma**2 = 10e-6 C_Y**2 D**(-4/3) t**-1 sec(z)**3 exp(-2h/H)
    sigma = subject.scintillation(diameter_m=1.0, exptime_s=1.0, airmass=1.0, altitude_m=0.0, c_y=1.0)

    assert sigma == pytest.approx(math.sqrt(10e-6))


def test_scintillation_without_correction_agrees_with_young_1967_at_zenith():
    # Young: 0.09 D[cm]**(-2/3) X**1.75 (2t)**-0.5 exp(-h/8000). Osborn rounds its
    # square (8.7e-6) to 10e-6, hence the 7% tolerance.
    young = 0.09 * 200.0 ** (-2 / 3) * (2 * 60.0) ** -0.5 * math.exp(-3055.0 / 8000.0)

    sigma = subject.scintillation(diameter_m=2.0, exptime_s=60.0, airmass=1.0, altitude_m=3055.0, c_y=1.0)

    assert sigma == pytest.approx(young, rel=0.08)


def test_scintillation_default_applies_the_median_osborn_correction():
    uncorrected = subject.scintillation(1.0, 10.0, 1.3, 2000.0, c_y=1.0)

    assert subject.scintillation(1.0, 10.0, 1.3, 2000.0) == pytest.approx(1.5 * uncorrected)


def test_scintillation_scales_with_diameter_exposure_airmass_and_altitude():
    base = subject.scintillation(1.0, 10.0, 1.0, 0.0)

    assert subject.scintillation(8.0, 10.0, 1.0, 0.0) == pytest.approx(base / 4.0)
    assert subject.scintillation(1.0, 40.0, 1.0, 0.0) == pytest.approx(base / 2.0)
    assert subject.scintillation(1.0, 10.0, 2.0, 0.0) == pytest.approx(base * 2.0 ** 1.5)
    assert subject.scintillation(1.0, 10.0, 1.0, 8000.0) == pytest.approx(base / math.e)


@pytest.mark.parametrize("kwargs", [
    dict(diameter_m=0.0), dict(exptime_s=0.0), dict(airmass=0.9), dict(exptime_s=float("nan")),
])
def test_scintillation_rejects_unphysical_inputs(kwargs):
    values = dict(diameter_m=1.0, exptime_s=10.0, airmass=1.0, altitude_m=0.0)
    values.update(kwargs)

    with pytest.raises(ValueError):
        subject.scintillation(**values)


# --- star rate and aperture ------------------------------------------------------

def test_star_rate_uses_the_median_zeropoint_and_extinction():
    model = _model()

    assert subject.star_rate(model, 25.0, 1.0, 0.09) == pytest.approx(1.0)
    assert subject.star_rate(model, 20.0, 1.0, 0.09) == pytest.approx(100.0)
    assert subject.star_rate(model, 25.0, 2.0, 0.09) == pytest.approx(10 ** (-0.4 * 0.09))


def test_aperture_area_follows_the_fwhm_and_grows_with_defocus():
    model = _model()
    radius_pix = subject.APERTURE_RADIUS_FWHM * fwhm_arcsec(model, 0.0, model.seeing_median) / model.pixscale

    focused = subject.aperture_npix(model, 0.0)

    assert focused == pytest.approx(math.pi * radius_pix ** 2)
    assert subject.aperture_npix(model, 3.0) > 2 * focused
    assert subject.aperture_npix(model, 0.0, seeing=3.2) == pytest.approx(4 * focused)


# --- noise budget ----------------------------------------------------------------

def test_budget_terms_match_hand_computed_values():
    model, mag, exptime = _model(), 12.0, 20.0
    electrons = subject.star_rate(model, mag, 1.2, 0.09) * exptime
    npix = subject.aperture_npix(model, 0.0)

    budget = subject.noise_budget(model, mag, exptime, airmass=1.2, extinction=0.09, diameter_m=2.0,
                                  altitude_m=3055.0, read_noise_e=6.0, sky_rate=20.0, dark_rate=0.5)

    assert budget.target_photon == pytest.approx(1 / math.sqrt(electrons))
    assert budget.sky == pytest.approx(math.sqrt(npix * 20.0 * exptime) / electrons)
    assert budget.read == pytest.approx(math.sqrt(npix) * 6.0 / electrons)
    assert budget.dark == pytest.approx(math.sqrt(npix * 0.5 * exptime) / electrons)
    assert budget.comparison == 0.0
    assert budget.scintillation == pytest.approx(subject.scintillation(2.0, exptime, 1.2, 3055.0))
    assert budget.total == pytest.approx(math.sqrt(sum(v ** 2 for v in budget.terms().values())))


def test_bright_star_is_scintillation_limited_and_faint_star_is_not():
    assert _budget(mag=8.0).dominant == "scintillation"
    assert _budget(mag=17.5, exptime=60.0).dominant in {"sky", "read", "target_photon"}
    assert _budget(mag=17.5, exptime=60.0).dominant != "scintillation"


def test_defocus_adds_sky_and_read_noise_but_not_photon_noise():
    focused, defocused = _budget(mag=14.0), _budget(mag=14.0, defocus_mm=4.0)

    assert defocused.sky > focused.sky
    assert defocused.read > focused.read
    assert defocused.target_photon == pytest.approx(focused.target_photon)
    assert defocused.total > focused.total


def test_equal_comparison_stars_add_noise_that_falls_as_root_n():
    alone = _budget(mag=12.0)
    one = _budget(mag=12.0, comp_mags=(12.0,))
    four = _budget(mag=12.0, comp_mags=(12.0,) * 4)
    own = math.sqrt(alone.target_photon ** 2 + alone.sky ** 2 + alone.read ** 2 + alone.dark ** 2)

    assert one.comparison == pytest.approx(own)
    assert four.comparison == pytest.approx(own / 2.0)
    # Scintillation is taken as uncorrelated between stars: sigma**2 * (1 + 1/N).
    assert one.scintillation == pytest.approx(alone.scintillation * math.sqrt(2.0))
    assert four.scintillation == pytest.approx(alone.scintillation * math.sqrt(1.25))


def test_a_bright_comparison_star_dominates_the_ensemble():
    faint_only = _budget(mag=12.0, comp_mags=(15.0,))
    with_bright = _budget(mag=12.0, comp_mags=(15.0, 10.0))

    assert with_bright.comparison < 0.2 * faint_only.comparison


@pytest.mark.parametrize("kwargs", [
    dict(exptime=0.0), dict(read_noise_e=-1.0), dict(sky_rate=-1.0), dict(dark_rate=-0.1),
    dict(mag=float("nan")), dict(comp_mags=(float("inf"),)),
])
def test_budget_rejects_unphysical_inputs(kwargs):
    with pytest.raises(ValueError):
        _budget(**kwargs)


# --- precision per unit time -----------------------------------------------------

def test_binned_precision_scales_by_frames_per_bin_including_overhead():
    budget = _budget(mag=10.0, exptime=24.0)

    binned = subject.binned(budget, exptime_s=24.0, overhead_s=6.0, bin_s=600.0)

    assert binned.total == pytest.approx(budget.total / math.sqrt(20.0))
    assert binned.dominant == budget.dominant
    assert binned.scintillation == pytest.approx(budget.scintillation / math.sqrt(20.0))


def test_overhead_costs_precision_at_fixed_exposure():
    budget = _budget(mag=10.0, exptime=10.0)

    fast = subject.binned(budget, exptime_s=10.0, overhead_s=6.0)
    slow = subject.binned(budget, exptime_s=10.0, overhead_s=46.0)

    assert slow.total == pytest.approx(fast.total * math.sqrt(56.0 / 16.0))


def test_binned_rejects_unphysical_inputs():
    budget = _budget()

    with pytest.raises(ValueError):
        subject.binned(budget, exptime_s=10.0, overhead_s=-1.0)
    with pytest.raises(ValueError):
        subject.binned(budget, exptime_s=10.0, overhead_s=6.0, bin_s=0.0)


def test_ppm_conversion():
    assert subject.ppm(1.5e-3) == pytest.approx(1500.0)


# --- readout overheads -----------------------------------------------------------

@pytest.mark.parametrize("instrument, mode, expected", [
    ("sinistro", "central_2k_2x2", 9.0), ("sinistro", "full_frame", 28.0),
    ("qhy600", "central30x30", 4.0), ("qhy600", "full_frame", 9.0),
    ("muscat3", "MUSCAT_FAST", 6.0), ("muscat4", "MUSCAT_SLOW", 46.0),
])
def test_readout_overhead_per_mode(instrument, mode, expected):
    assert subject.readout_overhead(instrument, mode) == expected


@pytest.mark.parametrize("instrument, mode", [("sbig", ""), ("sinistro", "nope"), ("muscat2", "MUSCAT_FAST")])
def test_readout_overhead_unknown_mode_is_an_error_not_a_guess(instrument, mode):
    with pytest.raises(ValueError, match=instrument):
        subject.readout_overhead(instrument, mode)
