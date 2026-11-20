"""Epoch + integer period predicts each planet independently (issue #453)."""
import io

import pytest
import yaml

from muscat_db import transit_fit as fit


@pytest.mark.parametrize('epoch,period,expected', [(2460000.21, 2, 2460000.21), (2459999.9, .2, 2460000.1)])
def test_epoch_predicts_prior_and_records_it(tmp_path, monkeypatch, epoch, period, expected):
    monkeypatch.setattr(fit, '_timer_version', lambda: 'test')
    csv = tmp_path / 'input.csv'
    csv.write_text('BJD_TDB,Flux,Flux_err\n2460000,1,0.001\n2460000.3,1,0.001\n')
    out = tmp_path / 'run'
    out.mkdir()
    fit._write_fit_inputs(out, 'muscat3', '260101', 'star', [csv], {
        'planets': 'b,c', 't0_b': str(epoch), 'period_b': str(period),
        'tc_pred_c': '2460000.25', 'tc_pred_unc_c': '0.1',
    })
    cfg = yaml.safe_load((out / 'fit.yaml').read_text())
    assert cfg['tc_pred'] == pytest.approx([expected, 2460000.25], rel=0, abs=1e-8)
    assert cfg['tc_pred_unc'] == [.04, .1]
    meta = yaml.safe_load((out / 'meta.yaml').read_text())
    priors = meta['transit_center_priors']
    assert priors['b']['lower_bjd'] == pytest.approx(expected - .02, rel=0, abs=1e-8)
    log = io.StringIO()
    fit._write_log_banner(log, ['timer-fit'], transit_center_priors=priors)
    assert f'{expected:.6f}' in log.getvalue()
    assert 'Uniform' in log.getvalue()


def _lightcurve(tmp_path):
    csv = tmp_path / 'input.csv'
    csv.write_text('BJD_TDB,Flux,Flux_err\n2460000,1,0.001\n2460000.3,1,0.001\n')
    return csv


@pytest.mark.parametrize('options,expected', [
    # Single-planet API callers send the unsuffixed keys.
    ({'t0': '2459990.15', 'period': '1'}, {'b': 2460000.15}),
    # Flat keys for the first planet next to suffixed ones for the second.
    ({'planets': 'b,c', 't0': '2459990.15', 'period': '1', 't0_c': '2459999.9', 'period_c': '0.2'},
     {'b': 2460000.15, 'c': 2460000.1}),
    # A Uniform period prior is predicted from the middle of its bounds.
    ({'t0': '2459990.15', 'period': '0.9', 'period_unc': '1.1', 'period_prior': 'uniform'}, {'b': 2460000.15}),
])
def test_flat_api_keys_predict_centers(tmp_path, options, expected):
    priors, err = fit.predict_transit_centers(options, [_lightcurve(tmp_path)])
    assert err is None
    assert {p: v['center_bjd'] for p, v in priors.items()} == pytest.approx(expected, rel=0, abs=1e-8)


@pytest.mark.parametrize('options,csv_text', [
    ({'t0': '2459990.15'}, 'BJD_TDB,Flux\n2460000,1\n'),
    ({'t0': '2459990.15', 'period': '1'}, 'time,Flux\n2460000,1\n'),
    ({'planets': 'b,c', 'tc_pred_b': '2460000.1', 't0_c': '2459990.15'}, 'BJD_TDB,Flux\n2460000,1\n'),
])
def test_launches_refuse_a_planet_without_a_computable_center(tmp_path, monkeypatch, options, csv_text):
    csv = tmp_path / 'TOI-1_muscat3_g_260101.csv'
    csv.write_text(csv_text)
    monkeypatch.setattr(fit, 'get_csv_lightcurves', lambda *a, **k: [csv])
    monkeypatch.setattr(fit, 'validate_no_duplicate_datasets', lambda *a, **k: None)
    monkeypatch.setattr(fit, 'fit_output_dir', lambda *a, **k: tmp_path / 'run')
    for launch in (fit.start_fit, fit.compute_logp):
        result = launch('muscat3', '260101', 'TOI-1', options)
        assert result['ok'] is False
        assert 'needs a transit-center prior' in result['error']
    assert not (tmp_path / 'run').exists()


def test_unreadable_meta_records_no_priors(tmp_path):
    (tmp_path / 'meta.yaml').write_text('transit_center_priors: [unclosed\n')
    assert fit.recorded_transit_center_priors(tmp_path) == {}


# --- Prior width from the propagated ephemeris uncertainty (issue #466) ---

# Epoch 300 periods before the light curve, with uncertainties on both terms.
_EPHEM = {'t0': '2459700.15', 't0_unc': '0.003', 'period': '1', 'period_unc': '0.0001'}
_SIGMA_300 = (0.003 ** 2 + (300 * 0.0001) ** 2) ** 0.5


def test_propagated_center_sigma():
    assert fit.propagated_center_sigma(0.003, 0.0001, 300) == pytest.approx(_SIGMA_300)
    # The epoch number enters squared, so its sign does not matter.
    assert fit.propagated_center_sigma(0.003, 0.0001, -300) == pytest.approx(_SIGMA_300)
    assert fit.propagated_center_sigma(None, 0.0001, 300) is None
    assert fit.propagated_center_sigma(0.003, 0, 300) is None


def test_default_width_is_three_sigma_either_side(tmp_path, monkeypatch):
    monkeypatch.setattr(fit, '_timer_version', lambda: 'test')
    out = tmp_path / 'run'
    out.mkdir()
    fit._write_fit_inputs(out, 'muscat3', '260101', 'star', [_lightcurve(tmp_path)], dict(_EPHEM))
    cfg = yaml.safe_load((out / 'fit.yaml').read_text())
    # timer takes the full width: 3 sigma either side is 6 sigma.
    assert cfg['tc_pred_unc'] == pytest.approx(6 * _SIGMA_300)
    prior = yaml.safe_load((out / 'meta.yaml').read_text())['transit_center_priors']['b']
    assert prior['mode'] == 'sigma'
    assert prior['n_sigma'] == 3
    assert prior['epoch_number'] == 300
    assert prior['sigma_days'] == pytest.approx(_SIGMA_300)
    assert prior['upper_bjd'] - prior['center_bjd'] == pytest.approx(3 * _SIGMA_300)
    assert prior['narrow'] is False


def test_sigma_mode_uses_the_chosen_number_of_sigma(tmp_path):
    priors, err = fit.predict_transit_centers(
        dict(_EPHEM, tc_pred_mode='sigma', tc_pred_nsigma='5'), [_lightcurve(tmp_path)])
    assert err is None
    assert priors['b']['full_width_days'] == pytest.approx(10 * _SIGMA_300)
    assert priors['b']['narrow'] is False


def test_explicit_full_width_without_a_mode_is_direct(tmp_path):
    """A caller that predates the mode keeps the width it sent."""
    priors, err = fit.predict_transit_centers(dict(_EPHEM, tc_pred_unc='0.5'), [_lightcurve(tmp_path)])
    assert err is None
    assert priors['b']['mode'] == 'direct'
    assert priors['b']['full_width_days'] == 0.5
    assert priors['b']['half_width_sigma'] == pytest.approx(0.25 / _SIGMA_300)


@pytest.mark.parametrize('options', [
    {'tc_pred_mode': 'sigma', 'tc_pred_nsigma': '2'},
    {'tc_pred_mode': 'direct', 'tc_pred_unc': '0.04'},
])
def test_narrow_width_warns_and_still_writes(tmp_path, monkeypatch, options):
    monkeypatch.setattr(fit, '_timer_version', lambda: 'test')
    out = tmp_path / 'run'
    out.mkdir()
    fit._write_fit_inputs(out, 'muscat3', '260101', 'star', [_lightcurve(tmp_path)], dict(_EPHEM, **options))
    assert (out / 'fit.yaml').is_file()
    priors = yaml.safe_load((out / 'meta.yaml').read_text())['transit_center_priors']
    assert priors['b']['narrow'] is True
    log = io.StringIO()
    fit._write_log_banner(log, ['timer-fit'], transit_center_priors=priors)
    assert 'WARNING' in log.getvalue()
    assert 'less than 3 sigma' in log.getvalue()


@pytest.mark.parametrize('options', [
    {'t0': '2459700.15', 'period': '1'},                                # no uncertainties
    {'t0': '2459700.15', 't0_unc': '0.003', 'period': '1'},              # no period uncertainty
    {'t0': '2459700.15', 't0_unc': '0.003', 'period': '0.9',             # Uniform period has bounds,
     'period_unc': '1.1', 'period_prior': 'uniform'},                    # not an uncertainty
])
def test_without_a_propagated_sigma_the_width_is_direct(tmp_path, options):
    priors, err = fit.predict_transit_centers(options, [_lightcurve(tmp_path)])
    assert err is None
    assert priors['b']['mode'] == 'direct'
    assert priors['b']['full_width_days'] == fit.DEFAULT_TC_UNC
    assert priors['b']['sigma_days'] is None
    assert priors['b']['narrow'] is False
    # Asking for sigma mode when it cannot be computed is an error, not a guess.
    priors, err = fit.predict_transit_centers(dict(options, tc_pred_mode='sigma'), [_lightcurve(tmp_path)])
    assert priors == {}
    assert 'uncertainty' in err


def test_sigma_applies_to_an_entered_center(tmp_path):
    """The epoch number comes from the entered center, not from the data."""
    priors, err = fit.predict_transit_centers(dict(_EPHEM, tc_pred='2459800.16'), [_lightcurve(tmp_path)])
    assert err is None
    assert priors['b']['epoch_number'] == 100
    assert priors['b']['sigma_days'] == pytest.approx((0.003 ** 2 + 0.01 ** 2) ** 0.5)


@pytest.mark.parametrize('options,message', [
    ({'tc_pred_mode': 'gaussian'}, 'tc_pred_mode'),
    ({'tc_pred_nsigma': '0'}, 'sigma'),
    ({'tc_pred_nsigma': 'three'}, 'sigma'),
])
def test_bad_width_options_are_refused(tmp_path, options, message):
    priors, err = fit.predict_transit_centers(dict(_EPHEM, **options), [_lightcurve(tmp_path)])
    assert priors == {}
    assert message in err


def test_old_meta_without_width_keys_still_prints(tmp_path):
    log = io.StringIO()
    fit._write_log_banner(log, ['timer-fit'], transit_center_priors={'b': {
        'center_bjd': 2460000.1, 'full_width_days': 0.04, 'lower_bjd': 2460000.08,
        'upper_bjd': 2460000.12, 'fixed': False}})
    assert 'full width 0.040000 d' in log.getvalue()
    assert 'WARNING' not in log.getvalue()


def test_sigma_does_not_depend_on_the_period_being_fixed(tmp_path):
    """The uncertainty is the ephemeris's, whether or not the fit samples the period."""
    for fixed in (['period'], []):
        priors, err = fit.predict_transit_centers(dict(_EPHEM, fixed=fixed), [_lightcurve(tmp_path)])
        assert err is None
        assert priors['b']['sigma_days'] == pytest.approx(_SIGMA_300)
        assert priors['b']['mode'] == 'sigma'


@pytest.mark.parametrize('value', [0, '0', 0.0, -2, '-2', False])
def test_explicit_zero_or_negative_sigma_count_is_refused(tmp_path, value):
    """A JSON number 0 is an explicit value, not a blank that defaults to 3."""
    priors, err = fit.predict_transit_centers(
        dict(_EPHEM, tc_pred_mode_b='sigma', tc_pred_nsigma_b=value), [_lightcurve(tmp_path)])
    assert priors == {}
    assert 'sigma' in err


@pytest.mark.parametrize('value', [None, '', '  '])
def test_missing_or_blank_sigma_count_defaults_to_three(tmp_path, value):
    priors, err = fit.predict_transit_centers(
        dict(_EPHEM, tc_pred_mode_b='sigma', tc_pred_nsigma_b=value), [_lightcurve(tmp_path)])
    assert err is None
    assert priors['b']['n_sigma'] == 3


@pytest.mark.parametrize('key', ['tc_pred_unc', 'tc_pred_unc_b'])
@pytest.mark.parametrize('value', [-1, '-1', 0, 'wide'])
def test_direct_width_is_checked_only_in_direct_mode(tmp_path, key, value):
    """A bad full width is ignored in sigma mode and refused in direct mode,
    whether it arrives suffixed (the page) or unsuffixed (API callers)."""
    csvs = [_lightcurve(tmp_path)]
    sigma = dict(_EPHEM, tc_pred_mode_b='sigma', **{key: value})
    assert fit.validate_fit_options(sigma) is None
    priors, err = fit.predict_transit_centers(sigma, csvs)
    assert err is None
    assert priors['b']['full_width_days'] == pytest.approx(6 * _SIGMA_300)

    direct = dict(_EPHEM, tc_pred_mode_b='direct', **{key: value})
    priors, err = fit.predict_transit_centers(direct, csvs)
    assert priors == {}
    assert 'full width' in err


def test_direct_width_without_a_mode_is_still_checked(tmp_path):
    priors, err = fit.predict_transit_centers(dict(_EPHEM, tc_pred_unc=-1), [_lightcurve(tmp_path)])
    assert priors == {}
    assert 'full width' in err
