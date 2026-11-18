"""Exercise the recommendation API with real models and isolated storage."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from muscat_db import exposure, exposure_model
from tests.test_precision import _model
from tests.test_exposure_model import _sample


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('MUSCAT_DB_PATH', str(tmp_path / 'models.db'))
    from muscat_db.web import app
    return TestClient(app)


def payload(**kwargs):
    return dict(instrument='muscat3', band='rp', mode='MUSCAT_FAST', site='ogg',
                mag=15, comp_mags=[15.5], sky_rate=20, read_noise_e=6, **kwargs)


def test_default_model_without_range_stays_in_focus(client):
    response = client.post('/api/exposure/recommend', json=payload())
    assert response.status_code == 200
    data = response.json()
    assert data['defocus_mm'] == 0
    assert data['max_defocus_mm'] == 0
    assert any('In-focus only' in note for note in data['notes'])
    assert data['inputs']['mode'] == 'MUSCAT_FAST'
    assert data['overhead_s'] == 6


def test_stored_calibration_enables_bounded_defocus(client):
    conn = exposure._conn()
    try:
        exposure_model.save_models(conn, 'muscat3', [replace(_model(), max_defocus_mm=4)])
    finally:
        conn.close()
    data = payload()
    data['mag'] = 10
    data['comp_mags'] = [10]
    response = client.post('/api/exposure/recommend', json=data)
    assert response.status_code == 200
    result = response.json()
    assert 0 < result['defocus_mm'] <= 4
    assert result['max_defocus_mm'] == 4
    assert result['peak_e'] <= result['model']['level'] * .5


@pytest.mark.parametrize('changes,status', [
    ({'instrument': 'muscat2'}, 422), ({'mode': 'invented'}, 400),
    ({'site': 'coj'}, 400), ({'sky_rate': -1}, 422),
    ({'airmass_min': 2, 'airmass_max': 1}, 400),
    ({'comp_mags': [-100]}, 400), ({'mode': 'MUSCAT_SLOW', 'max_cycle_s': 40}, 400),
])
def test_invalid_conditions_fail_without_recommending(client, changes, status):
    data = {**payload(), **changes}
    assert client.post('/api/exposure/recommend', json=data).status_code == status


def test_measured_sky_is_required(client):
    data = payload()
    del data['sky_rate']
    assert client.post('/api/exposure/recommend', json=data).status_code == 422


def test_calibration_records_measured_defocus_support_and_roundtrips(client):
    samples = [_sample() for _ in range(5)]
    samples += [_sample(defocus_mm=2, fwhm_arcsec=3) for _ in range(5)]
    model = exposure_model.fit_band(samples, .1)
    assert model.max_defocus_mm == 2
    conn = exposure._conn()
    try:
        exposure_model.save_models(conn, 'muscat3', [model])
        assert exposure_model.models_for(conn, 'muscat3')[('rp', '')].max_defocus_mm == 2
    finally:
        conn.close()
    old = model.as_dict()
    del old['max_defocus_mm']
    assert exposure_model.BandModel(**old).max_defocus_mm == 0


def test_readout_mode_controls_overhead_and_model(client):
    data = {**payload(), 'instrument': 'sinistro', 'mode': 'full_frame', 'site': 'lsc'}
    result = client.post('/api/exposure/recommend', json=data).json()
    assert result['ok']
    assert result['overhead_s'] == 28
    assert result['model']['mode'] == 'full_frame'
    assert result['model']['pixscale'] == .389


def test_missing_band_mode_model_is_not_borrowed(client):
    data = {**payload(), 'instrument': 'qhy600', 'band': 'gp', 'mode': 'full_frame'}
    response = client.post('/api/exposure/recommend', json=data)
    assert response.status_code == 400
    assert 'No calibrated model' in response.json()['error']
