"""Exposure lookup remains usable during a CDS outage (no live requests)."""
import json
import threading
import time
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
    post.sdss_get = Mock(return_value=Mock(json=lambda: [{'Rows': []}]))
    monkeypatch.setattr(http_client, 'get_sync_client', lambda: Mock(post=post, get=post.sdss_get))
    yield get, post
    exofop._exofop_cache.clear()
    if hasattr(exposure, '_fetch_tic_cached'):
        exposure._fetch_tic_cached.cache_clear()
        exposure._fetch_gaia_dr3_cached.cache_clear()
        if hasattr(exposure, '_fetch_sdss_cached'):
            exposure._fetch_sdss_cached.cache_clear()


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
    assert 'empirical' in result['approx_reason']
    assert 'SDSS' in result['approx_reason']
    assert 'TIC' in result['approx_reason']
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


def test_sdss_measurements_precede_empirical_transform(outage):
    outage[1].return_value.json = lambda: {'status': 'COMPLETE', 'data': [{'ID': 166527623}]}
    row = {'ra': 207.526, 'dec': -40.835896}
    for band in 'griz':
        row['psfMag_' + band] = 12.3
        row['flags_' + band] = 0x10000000  # BINNED1, no bad flags
    outage[1].sdss_get.return_value.json = lambda: [{'Rows': [row]}]
    result = lookup()
    assert result['ok']
    assert result['mags'] == dict.fromkeys(['gp', 'rp', 'ip', 'zs'], 12.3)
    assert result['is_approx'] is False
    assert 'SDSS DR17' in result['source']
    assert outage[1].call_count == 1  # no Gaia request


def test_sdss_available_without_exofop(outage, monkeypatch):
    monkeypatch.setattr(exposure, 'resolve_target_coords', lambda _: (207.526, -40.835896))
    outage[0].side_effect = RuntimeError('ExoFOP down')
    row = {'ra': 207.526, 'dec': -40.835896, 'psfMag_g': 12.3, 'flags_g': 0x10000000}
    outage[1].sdss_get.return_value.json = lambda: [{'Rows': [row]}]
    assert lookup()['mags'] == {'gp': 12.3}


@pytest.mark.parametrize('bad', [0x40000, 0x4, 0x80, 0x800000000000, 0x10000000000])
def test_sdss_rejects_flagged_bands(outage, bad):
    row = {'ra': 207.526, 'dec': -40.835896, 'psfMag_g': 12, 'flags_g': 0x10000000 | bad}
    outage[1].sdss_get.return_value.json = lambda: [{'Rows': [row]}]
    mags, reason = exposure._fetch_sdss_cached(207.526, -40.835896, 1)
    assert mags is None
    assert 'no usable' in reason


@pytest.mark.parametrize('rows', [[], [{'ra': 1, 'dec': 2}, {'ra': 1, 'dec': 2}], [{'ra': 1, 'dec': 2}]])
def test_sdss_rejects_missing_ambiguous_or_distant_match(outage, rows):
    outage[1].sdss_get.return_value.json = lambda: [{'Rows': rows}]
    assert exposure._fetch_sdss_cached(207.526, -40.835896, 1)[0] is None


def test_sdss_outage_is_cached_and_explained(outage):
    outage[1].sdss_get.side_effect = RuntimeError('SDSS down')
    first = exposure._fetch_sdss_cached(207.526, -40.835896, 1)
    assert first[0] is None
    assert 'unavailable' in first[1]
    assert exposure._fetch_sdss_cached(207.526, -40.835896, 1) == first
    assert outage[1].sdss_get.call_count == 1


def test_sdss_query_is_small_and_bounded(outage):
    exposure._fetch_sdss_cached(207.526, -40.835896, 1)
    call = outage[1].sdss_get.call_args
    assert call.kwargs['timeout'] == 20
    assert 'TOP 2' in call.kwargs['params']['cmd']
    assert 'p.type=6 AND p.clean=1' in call.kwargs['params']['cmd']
    assert '0.05)' in call.kwargs['params']['cmd']  # arcminutes = 3 arcsec


# --- one wall-clock budget for the whole lookup (review of #348) -------------
# A hung stage must not hold the request past nginx's 60 s proxy timeout, and
# must not keep the stages after it (the fallbacks) from running.

@pytest.fixture
def tight_budget(monkeypatch):
    monkeypatch.setattr(exposure, '_LOOKUP_TOTAL_TIMEOUT_S', 1.0)
    monkeypatch.setattr(exposure, '_LOOKUP_STAGE_CAP_S', dict.fromkeys(exposure._LOOKUP_STAGE_CAP_S, 0.3))


@pytest.fixture
def stall(outage, monkeypatch):
    """Factory for a service call that blocks until released (or 3 s).

    ``stall()`` then fails; ``stall(value)`` then returns *value*, like a slow
    service that does answer. Each test gets its own stage executor, and
    ``stall.release()`` lets every stalled call go and waits until the
    executor's work has finished, including the cache write that follows the
    call. Depends on ``outage`` so this teardown runs before the hourly caches
    are cleared: no late result can leak into the next test.
    """
    from concurrent.futures import ThreadPoolExecutor

    release = threading.Event()
    executor = [ThreadPoolExecutor(max_workers=8)]
    monkeypatch.setattr(exposure, '_LOOKUP_STAGE_EXECUTOR', executor[0])

    def make(value=None):
        def _call(*args, **kwargs):
            release.wait(3)
            if value is None:
                raise RuntimeError('service hung')
            return value
        return _call

    def release_all():
        release.set()
        executor[0].shutdown(wait=True)
        executor[0] = ThreadPoolExecutor(max_workers=8)
        monkeypatch.setattr(exposure, '_LOOKUP_STAGE_EXECUTOR', executor[0])

    make.release = release_all
    yield make
    release_all()
    executor[0].shutdown(wait=True)


def timed_lookup():
    start = time.monotonic()
    result = lookup()
    return result, time.monotonic() - start


def test_hung_name_resolution_still_reaches_the_exofop_fallback(outage, tight_budget, stall, monkeypatch):
    monkeypatch.setattr(exposure.SkyCoord, 'from_name', Mock(side_effect=stall()))
    result, elapsed = timed_lookup()
    assert result['ok'] is True and 'TIC' in result['source']
    assert elapsed < 1.0


def test_every_stage_hung_still_answers_within_the_total_budget(outage, tight_budget, stall, monkeypatch):
    get, post = outage
    monkeypatch.setattr(exposure.SkyCoord, 'from_name', Mock(side_effect=stall()))
    get.side_effect = stall()
    post.side_effect = stall()
    post.sdss_get.side_effect = stall()
    result, elapsed = timed_lookup()
    assert result['ok'] is False
    assert elapsed < 1.5


def test_stages_left_without_budget_never_contact_their_service(outage, tight_budget, stall, monkeypatch):
    """Resolution and TIC hang through the whole budget: SDSS and Gaia, which
    would come next, are not sent at all rather than queued behind it."""
    monkeypatch.setattr(exposure, '_LOOKUP_TOTAL_TIMEOUT_S', 0.5)
    monkeypatch.setattr(exposure.SkyCoord, 'from_name', Mock(side_effect=stall()))
    post = outage[1]
    post.side_effect = stall()
    timed_lookup()
    assert post.call_count == 1  # TIC only, not the Gaia DR3 cone
    post.sdss_get.assert_not_called()


def test_a_late_answer_is_kept_for_the_next_lookup(outage, tight_budget, stall):
    """A stage that times out but whose service answers late keeps that answer
    in its cache: the next lookup uses it without asking MAST again."""
    post = outage[1]
    post.side_effect = stall(post.return_value)
    first, _ = timed_lookup()
    assert first['ok'] is False  # TIC (and the Gaia cone) timed out
    calls = post.call_count
    stall.release()  # MAST answers, late
    second, _ = timed_lookup()
    assert second['ok'] is True and 'TIC' in second['source']
    assert post.call_count == calls
