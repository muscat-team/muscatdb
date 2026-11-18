import pytest
import yaml
from muscat_db import transit_fit as fit


def test_trim_keeps_fractional_days_and_chunk_default(tmp_path):
    # Include a dataset to inspect its actual serialized preprocessing options.
    source = tmp_path / 'source'
    source.mkdir()
    csv = source / 'star_muscat3_gp_260101.csv'
    csv.write_text('BJD_TDB,Flux,Flux_err\n2460000.1,1,0.001\n2460000.2,1,0.001\n')
    fit._write_fit_inputs(tmp_path, 'muscat3', '260101', 'star', [csv], {'trim_beg': '0.005', 'trim_end': '0.01', 'chunk_offset': 'true', 'include_bump': 'true'})
    config = yaml.safe_load((tmp_path / 'fit.yaml').read_text())
    data = next(iter(config['data'].values()))
    assert data['trim_beg'] == .005 and data['trim_end'] == .01
    assert data['chunk_thresh'] == .02
    assert config['bump']['tcenter'] == pytest.approx(2460000.15, rel=0, abs=1e-8)
    assert config['bump']['ampl'] == 10


@pytest.mark.parametrize('options', [{'fit_basis': 'standard'}, {'chunk_offset': 'true', 'chunk_thresh': '0'}, {'include_bump': 'true', 'bump_tcenter': '0,0.1'}])
def test_unusable_options_rejected(options):
    assert fit.validate_fit_options(options, (2460000, 2460001)) is not None


def test_bump_range_checked_at_launch(tmp_path):
    csv = tmp_path / 'lc.csv'
    csv.write_text('BJD_TDB,Flux,Flux_err\n2460000.1,1,.001\n2460000.2,1,.001\n')
    assert 'outside' in fit.validate_flare_times({'include_bump': 'true', 'bump_tcenter': '0,0.1'}, [csv])


def test_blank_bump_center_without_time_range_rejected_at_launch(tmp_path):
    csv = tmp_path / 'lc.csv'
    csv.write_text('time,flux\n0.1,1\n')
    assert 'required' in fit.validate_flare_times({'include_bump': 'true'}, [csv])
    assert fit.validate_flare_times({'include_bump': 'true', 'bump_tcenter': '2460000.1,0.1'}, [csv]) is None
