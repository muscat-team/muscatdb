"""Exposure lookup remains usable during a CDS outage (no live requests)."""
import json
from unittest.mock import Mock

import pytest
from muscat_db import exposure, exofop, web, http_client


@pytest.fixture
def outage(monkeypatch):
    exofop._exofop_cache.clear()
    monkeypatch.setattr(exposure.SkyCoord, 'from_name', Mock(side_effect=RuntimeError('CDS down')))
    monkeypatch.setattr(exposure, '_query_vizier_catalog', Mock(side_effect=RuntimeError('CDS down')))
    get = Mock(return_value=Mock(json=lambda: {
        'basic_info': {'tic_id': '166527623'},
        'coordinates': {'ra': '207.526', 'dec': '-40.835896'},
    }))
    monkeypatch.setattr(exofop.catalog, '_sync_get', get)
    post = Mock(return_value=Mock(json=lambda: {'status': 'COMPLETE', 'data': [{
        'ID': '166527623', 'gmag': 10.1, 'rmag': 9.7, 'imag': 9.5, 'zmag': 9.4,
    }]}))
    monkeypatch.setattr(http_client, 'get_sync_client', lambda: Mock(post=post))
    yield get, post
    exofop._exofop_cache.clear()
    if hasattr(exposure, '_fetch_tic_cached'):
        exposure._fetch_tic_cached.cache_clear()
        exposure._fetch_gaia_dr3_cached.cache_clear()


def lookup():
    return json.loads(web.exposure_lookup_mags({'target': 'HIP 67522'}).body)


def test_cds_outage_uses_exofop_and_exact_tic_id(outage):
    get, post = outage
    result = lookup()
    assert result['ok'] is True
    assert result['ra'] == 207.526
    assert result['dec'] == -40.835896
    assert result['mags'] == {'gp': 10.1, 'rp': 9.7, 'ip': 9.5, 'zs': 9.4}
    assert result['is_approx'] is False
    assert 'TIC' in result['source']
    request = json.loads(post.call_args.kwargs['data']['request'])
    assert request['params']['filters'] == [{'paramName': 'ID', 'values': ['166527623']}]
    assert post.call_args.kwargs['timeout'] == 20
    assert lookup() == result
    assert get.call_count == post.call_count == 1


def test_vizier_outage_after_sesame_success(outage, monkeypatch):
    monkeypatch.setattr(exposure, 'resolve_target_coords', lambda _: (207.526, -40.835896))
    assert lookup()['ok'] is True


def test_non_tic_name_keeps_clear_failure(outage):
    outage[0].return_value.json = lambda: {'basic_info': {}}
    assert lookup()['ok'] is False
    outage[1].assert_not_called()


def test_mast_outage_is_graceful(outage):
    outage[1].side_effect = RuntimeError('MAST down')
    assert lookup()['ok'] is False


def test_mast_wrong_id_is_rejected(outage):
    outage[1].return_value.json = lambda: {'status': 'COMPLETE', 'data': [{'ID': '123', 'gmag': 10}]}
    assert lookup()['ok'] is False


def test_direct_catalog_success_needs_no_fallback(outage, monkeypatch):
    monkeypatch.setattr(exposure, 'resolve_target_coords', lambda _: (1, 2))
    monkeypatch.setattr(exposure, 'lookup_magnitudes', lambda *a, **k: ({'gp': 10}, 'PS1'))
    assert lookup()['mags'] == {'gp': 10}
    outage[0].assert_not_called()
    outage[1].assert_not_called()


def test_four_band_transform_during_cds_outage(outage):
    outage[1].side_effect = [
        Mock(json=lambda: {'status': 'COMPLETE', 'data': [{'ID': 166527623}]}),
        Mock(json=lambda: {'status': 'COMPLETE', 'data': [{
            'source_id': 6113920619134019456, 'ra': 207.526, 'dec': -40.835896,
            'phot_g_mean_mag': 10, 'phot_bp_mean_mag': 10.5, 'phot_rp_mean_mag': 9.5,
        }]}),
    ]
    result = lookup()
    assert result['ok'] is True
    # Published G-band polynomials evaluated independently at BP-RP=1.
    assert result['mags'] == pytest.approx({
        'gp': 10.565, 'rp': 9.96922, 'ip': 9.750794, 'zs': 9.640381,
    })
    assert result['is_approx'] is True
    assert 'Gaia DR3' in result['source']
    assert outage[1].call_count == 2


@pytest.mark.parametrize('color', [None, float('nan'), float('inf'), 0.5, 2.0, -1, 4])
def test_transform_refuses_outside_common_valid_range(color):
    assert exposure.gaia_to_griz_transform(10, color) is None


def test_transform_all_four_bands():
    assert exposure.gaia_to_griz_transform(10, 1) == pytest.approx({
        'gp': 10.565, 'rp': 9.96922, 'ip': 9.750794, 'zs': 9.640381,
    })


@pytest.mark.parametrize('value', [float('inf'), float('-inf'), float('nan')])
def test_transform_rejects_nonfinite_g(value):
    assert exposure.gaia_to_griz_transform(value, 1) is None


@pytest.mark.parametrize('rows', [[], [
    {'ra': 207.526, 'dec': -40.835896}, {'ra': 207.526, 'dec': -40.835896},
], [{'ra': 208, 'dec': -40}], [{'ra': 'bad', 'dec': 2}]])
def test_gaia_rejects_missing_ambiguous_and_distant_matches(outage, rows):
    outage[1].return_value.json = lambda: {'status': 'COMPLETE', 'data': rows}
    assert exposure._fetch_gaia_dr3_cached(207.526, -40.835896, 1) is None


@pytest.mark.parametrize('position', [None, {}, {'ra': 'nan', 'dec': 0}, {'ra': 400, 'dec': 0}])
def test_invalid_exofop_position_is_graceful(outage, position):
    outage[0].return_value.json = lambda: {'basic_info': {'tic_id': '166527623'}, 'coordinates': position}
    assert lookup()['ok'] is False
    outage[1].assert_not_called()


def test_exofop_cache_is_shared_with_time_series_and_expires(outage):
    data = exofop.fetch_target_by_name('HIP 67522', _now=0)
    data['basic_info']['tic_id'] = '123'
    assert exofop.fetch_by_name('HIP 67522', _now=1) == ('166527623', [])
    assert outage[0].call_count == 1
    exofop.fetch_target_by_name('HIP 67522', _now=3601)
    assert outage[0].call_count == 2


def test_partial_tic_photometry_is_preserved(outage):
    outage[1].return_value.json = lambda: {'status': 'COMPLETE', 'data': [{
        'ID': 166527623, 'gmag': 10.1, 'rmag': None, 'imag': float('nan'), 'zmag': float('inf'),
    }]}
    result = lookup()
    assert result['mags'] == {'gp': 10.1}
    assert result['is_approx'] is False
    assert outage[1].call_count == 1
