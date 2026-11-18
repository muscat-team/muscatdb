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
