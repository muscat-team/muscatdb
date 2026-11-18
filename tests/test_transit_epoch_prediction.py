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
