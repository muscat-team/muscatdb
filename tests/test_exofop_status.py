"""ExoFOP status checks must distinguish unchecked rows from missing datasets."""

from unittest.mock import Mock

import pytest

from muscat_db import exofop


@pytest.mark.parametrize('telescope,site', [
    ('LCO-SSO-1m0 (1.0 m)', 'coj'),
    ('LCO-SSO-1.0m (1 m)', 'coj'),
    ('LCO-CPT-1m0 (1.0 m)', 'cpt'),
    ('LCO-LSC-1m0 (1.0 m)', 'lsc'),
    ('LCO-TLV-1m0 (1.0 m)', 'tlv'),
])
@pytest.mark.parametrize('matching', [None, {'object': 'TOI712', 'nframes': 12}])
def test_site_aliases_reach_database_check(monkeypatch, telescope, site, matching):
    monkeypatch.setattr(exofop, '_target_coords', lambda target: (30.0, -40.0))
    matcher = Mock(return_value=matching)
    monkeypatch.setattr(exofop.lco, 'local_lco_dataset_match', matcher)
    row = exofop.check_time_series_exists(
        {'tstel': telescope, 'tscam': 'Sinistro', 'tsdate': '2022-11-29'},
        target='TOI712', denied=frozenset({'private-proposal'}),
    )
    assert row['site'] == site
    assert row['exists_checked'] is True
    assert row['exists'] is bool(matching)
    matcher.assert_called_once_with(
        'sinistro', ['221128', '221129', '221130'], site, 30.0, -40.0,
        object_name='TOI712', denied=frozenset({'private-proposal'}),
    )


@pytest.mark.parametrize('telescope,camera,coords,reason', [
    ('LCO-unknown (1.0 m)', 'Sinistro', (30, -40), 'Observatory site could not be identified.'),
    ('LCO-CTIO (1.0 m)', 'unknown camera', (30, -40), 'Instrument could not be identified.'),
    ('LCO-CTIO (1.0 m)', 'Sinistro', None, 'Target coordinates could not be resolved.'),
    ('ASTEP-ANTARCTICA (0.4 m)', 'ANDOR iKon-L 936', (30, -40),
     'Instrument could not be identified.'),
])
def test_unchecked_rows_explain_why(monkeypatch, telescope, camera, coords, reason):
    monkeypatch.setattr(exofop, '_target_coords', lambda target: coords)
    matcher = Mock()
    monkeypatch.setattr(exofop.lco, 'local_lco_dataset_match', matcher)
    row = exofop.check_time_series_exists(
        {'tstel': telescope, 'tscam': camera, 'tsdate': '2022-11-29'}, target='TOI712',
    )
    assert row['exists_checked'] is False
    assert row['exists'] is False
    assert row['exists_unchecked_reason'] == reason
    matcher.assert_not_called()


def test_sso_alias_does_not_match_inside_other_words():
    assert exofop._site_from_tel('SOSS Observatory') == ''
    assert exofop.is_lco_telescope('SOSS Observatory') is False


@pytest.mark.parametrize('telescope', [
    'Myers - Siding Spring (0.4 m)',
    'iTelescope T17 Siding Spring (0.43 m)',
])
def test_shared_observatory_location_does_not_imply_lco(telescope):
    row = exofop.check_time_series_exists({'tstel': telescope, 'tscam': 'FLI-New-Beta'})
    assert row['lco'] is False
    assert row['exists_checked'] is False


@pytest.mark.parametrize('telescope', ['LCO-SSO (1 m)', 'LCOGT Siding Spring (1 m)'])
def test_lco_facility_names_are_recognized(telescope):
    assert exofop.is_lco_telescope(telescope) is True


# Distinct LCO labels observed in the 12-target live audit, 2026-10-30.
@pytest.mark.parametrize('telescope,camera,site,instrument', [
    ('LCO SAAO (1 m)', 'Sinistro', 'cpt', 'sinistro'),
    ('LCO-CTIO (1 m)', 'Sinistro', 'lsc', 'sinistro'),
    ('LCO-CTIO (1.0 m)', 'Sinistro', 'lsc', 'sinistro'),
    ('LCO-CTIO-1.0m', 'Sinistro, 1.0m', 'lsc', 'sinistro'),
    ('LCO-CTIO-1m0 (1.0 m)', 'SINISTRO', 'lsc', 'sinistro'),
    ('LCO-Hal-M3 (2.0 m)', 'MuSCAT3', 'ogg', 'muscat3'),
    ('LCO-McD (1 m)', 'Sinistro', 'elp', 'sinistro'),
    ('LCO-McD (1.0 m)', 'Sinistro', 'elp', 'sinistro'),
    ('LCO-McD-1m (1.0 m)', 'SINISTRO', 'elp', 'sinistro'),
    ('LCO-McD-1m0 (1.0 m)', 'SINISTRO', 'elp', 'sinistro'),
    ('LCO-SAAO (1 m)', 'SINISTRO', 'cpt', 'sinistro'),
    ('LCO-SAAO (1 m)', 'Sinistro', 'cpt', 'sinistro'),
    ('LCO-SAAO (1.0 m)', 'Sinistro', 'cpt', 'sinistro'),
    ('LCO-SAAO-1.0m', 'Sinistro, 1.0m', 'cpt', 'sinistro'),
    ('LCO-SAAO-1m0 (1.0 m)', 'SINISTRO', 'cpt', 'sinistro'),
    ('LCO-SSO (1 m)', 'Sinistro', 'coj', 'sinistro'),
    ('LCO-SSO (1.0 m)', 'Sinistro', 'coj', 'sinistro'),
    ('LCO-SSO-1.0m', 'Sinistro, 1.0m', 'coj', 'sinistro'),
    ('LCO-SSO-1.0m (1 m)', 'Sinistro', 'coj', 'sinistro'),
    ('LCO-SSO-1.0m (1.0 m)', 'Sinistro, 1.0m', 'coj', 'sinistro'),
    ('LCO-SSO-1m0', 'Sinistro, 1.0m', 'coj', 'sinistro'),
    ('LCO-SSO-1m0 (1.0 m)', 'SINISTRO', 'coj', 'sinistro'),
])
def test_audited_lco_labels_are_checkable(monkeypatch, telescope, camera, site, instrument):
    monkeypatch.setattr(exofop, '_target_coords', lambda target: (30, -40))
    matcher = Mock(return_value=None)
    monkeypatch.setattr(exofop.lco, 'local_lco_dataset_match', matcher)
    row = exofop.check_time_series_exists(
        {'tstel': telescope, 'tscam': camera, 'tsdate': '2022-11-29'}, target='TOI712',
    )
    assert (row['site'], row['instrument']) == (site, instrument)
    assert row['lco'] is True
    assert row['exists_checked'] is True
    matcher.assert_called_once()


def test_lco_report_without_site_stays_unchecked(monkeypatch):
    matcher = Mock()
    monkeypatch.setattr(exofop.lco, 'local_lco_dataset_match', matcher)
    row = exofop.check_time_series_exists(
        {'tstel': 'LCO (1 m)', 'tscam': 'Sinistro', 'tsdate': '2018-12-16'}, target='TOI270',
    )
    assert row['lco'] is True
    assert row['instrument'] == 'sinistro'
    assert row['exists_checked'] is False
    assert row['exists_unchecked_reason'] == 'Observatory site could not be identified.'
    matcher.assert_not_called()
