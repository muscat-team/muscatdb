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


def test_bright_target_uses_measured_builtin_range(client):
    response = client.post('/api/exposure/recommend', json={**payload(), 'mag': 9, 'comp_mags': [9.5]})
    assert response.status_code == 200
    data = response.json()
    assert 0 < data['defocus_mm'] <= data['max_defocus_mm']
    assert any('all four bands' in note for note in data['notes'])
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


def test_defaults_use_matching_frame_medians_and_units(client, tmp_path, monkeypatch):
    from astropy.io import fits
    from muscat_db.cache import clear_all_caches
    from tests.test_exposure_model import _write_frame

    paths = {}
    conn = exposure._conn()
    for i, (sky, gain, exp, noise, dark, band, mode, site) in enumerate([
        (100, 2, 10, 4, .01, 'rp', 'MUSCAT_FAST', 'ogg'),
        (300, 1, 10, 6, .03, 'rp', 'MUSCAT_FAST', 'ogg'),
        (999, 1, 1, 99, 9, 'gp', 'MUSCAT_FAST', 'ogg'),
        (999, 1, 1, 99, 9, 'rp', 'MUSCAT_SLOW', 'ogg'),
        (999, 1, 1, 99, 9, 'rp', 'MUSCAT_FAST', 'coj'),
    ]):
        path = _write_frame(tmp_path / f'{i}.fits', band=band, mode=mode)
        with fits.open(path, mode='update') as hdul:
            hdul['SCI'].header.update(SITEID=site, L1MEDIAN=sky, GAIN=gain,
                                      EXPTIME=exp, RDNOISE=noise, DARKCURR=dark)
        filename = f'{site}-{i}'
        paths[filename] = path
        conn.execute('INSERT INTO frames (instrument, obsdate, ccd, filename, filter, read_mode) VALUES (?, ?, ?, ?, ?, ?)',
                     ('muscat3', '261119', 0, filename, band, mode))
    conn.commit()
    conn.close()
    monkeypatch.setattr(exposure, '_fits_exists', lambda inst, date, filename: paths[filename])
    clear_all_caches()
    response = client.get('/api/exposure/recommend-defaults', params={
        'instrument': 'muscat3', 'band': 'rp', 'mode': 'MUSCAT_FAST', 'site': 'ogg'})
    assert response.status_code == 200
    assert response.json()['values'] == {'sky_rate': 25, 'read_noise_e': 5, 'dark_rate': .02}
    assert response.json()['counts'] == dict.fromkeys(('sky_rate', 'read_noise_e', 'dark_rate'), 2)


def test_defaults_missing_measurements_are_not_zero(client, tmp_path, monkeypatch):
    from muscat_db.cache import clear_all_caches
    from tests.test_exposure_model import _write_frame
    path = _write_frame(tmp_path / 'no_noise.fits')
    conn = exposure._conn()
    conn.execute("INSERT INTO frames (instrument, obsdate, ccd, filename, filter, read_mode) "
                 "VALUES ('muscat3', '261119', 0, 'ogg-no-noise', 'rp', 'MUSCAT_FAST')")
    conn.commit()
    conn.close()
    from astropy.io import fits
    with fits.open(path, mode='update') as hdul:
        hdul['SCI'].header.update(SITEID='ogg', EXPTIME=0, GAIN=1, L1MEDIAN=100, RDNOISE=-1, DARKCURR=-1)
    monkeypatch.setattr(exposure, '_fits_exists', lambda *a: path)
    clear_all_caches()
    response = client.get('/api/exposure/recommend-defaults', params={
        'instrument': 'muscat3', 'band': 'rp', 'mode': 'MUSCAT_FAST', 'site': 'ogg'})
    assert response.status_code == 200
    assert response.json()['values'] == dict.fromkeys(('sky_rate', 'read_noise_e', 'dark_rate'))


def test_defaults_bound_header_reads_to_200_recent_frames(client, monkeypatch):
    from muscat_db.cache import clear_all_caches
    conn = exposure._conn()
    conn.executemany(
        "INSERT INTO frames (instrument, obsdate, ccd, filename, filter, read_mode) "
        "VALUES ('muscat3', '261119', 0, ?, 'rp', 'MUSCAT_FAST')",
        [(f'ogg-{i:04d}',) for i in range(205)])
    conn.commit()
    conn.close()
    visited = []

    def missing(inst, date, filename):
        visited.append(filename)
        return None

    monkeypatch.setattr(exposure, '_fits_exists', missing)
    clear_all_caches()
    params = dict(instrument='muscat3', band='rp', mode='MUSCAT_FAST', site='ogg')
    assert client.get('/api/exposure/recommend-defaults', params=params).status_code == 200
    assert len(visited) == 200
    assert visited[0] == 'ogg-0204'
    assert visited[-1] == 'ogg-0005'
    client.get('/api/exposure/recommend-defaults', params=params)
    assert len(visited) == 200  # subsequent requests reuse the existing cache


def test_one_extreme_frame_cannot_extend_calibrated_range():
    samples = [_sample() for _ in range(5)]
    samples += [_sample(defocus_mm=d, fwhm_arcsec=3) for d in [2, 2, 2, 2, 80]]
    assert exposure_model.fit_band(samples, .1).max_defocus_mm == 2


def test_legacy_stored_model_requires_recalibration(client):
    import json

    conn = exposure._conn()
    try:
        old = exposure_model.models_for(conn, 'muscat3')[('rp', '')].as_dict()
        old.pop('max_defocus_mm')
        conn.execute("INSERT INTO exposure_models (instrument,band,mode,params_json,n_frames) VALUES (?,?,?,?,?)",
                     ('muscat3', 'rp', '', json.dumps(old), old['n_frames']))
        conn.commit()
    finally:
        conn.close()
    response = client.post('/api/exposure/recommend', json={**payload(), 'mag': 9, 'comp_mags': [9.5]})
    assert response.status_code == 400
    assert 'no calibrated defocus range' in response.json()['error'].lower()
    faint = client.post('/api/exposure/recommend', json=payload()).json()
    assert faint['ok'] and faint['max_defocus_mm'] == 0


def test_measured_range_is_capped_at_hardware_limit(client):
    conn = exposure._conn()
    try:
        exposure_model.save_models(conn, 'sinistro', [replace(_model(), mode='full_frame', max_defocus_mm=7)])
    finally:
        conn.close()
    settings = {**payload(), 'instrument': 'sinistro', 'mode': 'full_frame', 'site': 'elp'}
    result = client.post('/api/exposure/recommend', json=settings).json()
    assert result['max_defocus_mm'] == 5
    assert not any('all four bands' in note for note in result['notes'])
