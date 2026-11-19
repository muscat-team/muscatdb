"""Optimizer contracts: saturation, cadence and the precision objective (#344)."""
import pytest

from muscat_db import precision
from tests.test_precision import _model


def recommend(mag=12, **overrides):
    kwargs = dict(model=_model(), mag=mag, comp_mags=[mag],
                  airmass_range=(1.0, 2.0), extinction=.1, diameter_m=2,
                  altitude_m=3000, read_noise_e=6, sky_rate=20, dark_rate=0,
                  overhead_s=6, max_cycle_s=120, bin_s=600, sat_frac=.5,
                  max_defocus_mm=4)
    kwargs.update(overrides)
    return precision.recommend(**kwargs)


def test_bright_star_defocuses_but_faint_star_does_not():
    bright = recommend(10)
    faint = recommend(17)
    assert bright['defocus_mm'] > 0
    assert faint['defocus_mm'] == 0
    assert bright['cycle_s'] <= 120
    assert faint['cycle_s'] <= 120
    assert bright['peak_e'] <= 60000
    assert faint['peak_e'] <= 60000


def test_comparison_saturation_limits_exposure():
    alone = recommend(15, comp_mags=[])
    with_bright_comp = recommend(15, comp_mags=[9])
    assert with_bright_comp['peak_e'] <= 60000
    assert (with_bright_comp['exptime_s'] < alone['exptime_s']
            or with_bright_comp['defocus_mm'] > alone['defocus_mm'])


def test_no_feasible_exposure_is_reported():
    with pytest.raises(ValueError, match='No safe'):
        recommend(-5, max_defocus_mm=0)


def test_cadence_and_calibrated_defocus_bound_are_respected():
    result = recommend(10, max_cycle_s=20, max_defocus_mm=1.25)
    assert result['cycle_s'] <= 20
    assert result['defocus_mm'] <= 1.25
    assert result['budget_ppm']['total'] > 0
    assert result['dominant'] in result['budget_ppm']


@pytest.mark.parametrize('overrides', [
    {'max_cycle_s': 6}, {'airmass_range': (2, 1)}, {'sat_frac': 1.1},
    {'sky_rate': -1}, {'comp_mags': [float('nan')]}, {'max_defocus_mm': -1},
    {'bin_s': float('nan')}, {'read_noise_e': float('inf')},
])
def test_invalid_inputs_are_rejected(overrides):
    with pytest.raises(ValueError):
        recommend(**overrides)


def test_known_peak_rate_sets_the_longest_safe_whole_second_exposure():
    # ZP=25 and mag=15 give 10,000 e-/s. A=1, FWHM=sqrt(10) pixels
    # gives 1,000 e-/s at the peak, plus 20 e-/s sky. At 60,000 e-,
    # 58 s is safe (59,160 e-) and 59 s is not (60,180 e-).
    result = recommend(15, model=_model(zp_safe=25, zp_median=24, pixscale=1,
                                       seeing_good=10**.5, seeing_median=10**.5),
                       comp_mags=[], max_defocus_mm=0)
    assert result['exptime_s'] == 58
    assert result['peak_e'] == pytest.approx(59160)
    assert result['cycle_s'] == 64


def test_reported_precision_uses_high_airmass():
    result = recommend(12)
    exposure = result['exptime_s']
    budget = precision.noise_budget(
        _model(), 12, exposure, airmass=2, extinction=.1, diameter_m=2,
        altitude_m=3000, read_noise_e=6, sky_rate=20, dark_rate=0,
        defocus_mm=result['defocus_mm'], comp_mags=[12],
    )
    expected = precision.binned(budget, exposure, 6, 600)
    assert result['budget_ppm']['total'] == pytest.approx(precision.ppm(expected.total))
