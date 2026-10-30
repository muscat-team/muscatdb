"""The target inventory does not depend on ExoFOP-reported requests."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from muscat_db import target_archive as inventory, lco, exofop, web
from muscat_db.database import ScopedDenied

_REAL_CURSOR_PAGES = lco.archive_cursor_pages


def frame(seq=1, request=101, **extra):
    return dict(filename=f'lsc1m005-fa15-20261003-{seq:04d}-e91.fits.fz',
                SITEID='lsc', TELID='1m0a', INSTRUME='fa15', DAY_OBS='20261003',
                DATE_OBS='2026-10-04T02:00:00Z', OBJECT='TOI-1000', FILTER='ip',
                EXPTIME=30.12345678, proposal_id='OPEN', request_id=request, **extra)


@pytest.fixture
def setup(mock_db, monkeypatch):
    inventory._cache.clear()
    monkeypatch.setattr(inventory.catalog, '_target_crossmatch_coords', lambda *a: [(117.929, 12.536)])
    monkeypatch.setattr(lco, '_get_lco_api_token', lambda *a, **kw: 'token-one')
    monkeypatch.setattr(exofop, 'build_time_series_report', lambda *a, **kw: {
        'ok': True, 'time_series': [dict(tsdate='2026-10-04', instrument='sinistro', site='lsc', lco=True)]})
    calls = []
    rows = [frame(), frame(2, 102, public_date='2999-01-01T00:00:00Z')]
    def search(filters, user_name, **kw):
        calls.append((filters, user_name, kw))
        yield rows
    monkeypatch.setattr(lco, 'archive_cursor_pages', search)
    return mock_db, rows, calls


def insert(path, row, proposal='OPEN'):
    with sqlite3.connect(path) as conn:
        conn.execute('INSERT INTO frames (instrument, obsdate, ccd, filename, object, proposal_id) '
                     'VALUES (?, ?, ?, ?, ?, ?)',
                     ('sinistro', '261003', 0, row['filename'].removesuffix('.fz'), 'TOI-1000', proposal))


def test_two_requests_one_night_and_fresh_local_presence(setup):
    path, rows, calls = setup
    insert(path, rows[0])
    result = inventory.inventory('TOI-1000', 'alice')
    missing, present = result['rows']
    assert missing['request_id'] == '102' and missing['local_frames'] == 0
    assert missing['proprietary'] and not missing['exists']
    assert present['request_id'] == '101' and present['exists']
    assert present['frame_count'] == 1 and present['reported']
    assert calls[0][0] == {'covers': 'POINT(117.929 12.536)', 'reduction_level': '91',
                           'configuration_type': 'EXPOSE', 'limit': '1000'}
    insert(path, rows[1])
    assert all(r['exists'] for r in inventory.inventory('TOI-1000', 'alice')['rows'])
    assert len(calls) == 1


def test_scoped_viewer_hides_unknown_proposals_even_from_cache(setup):
    _, rows, calls = setup
    rows[1]['proposal_id'] = 'HIDDEN'
    assert len(inventory.inventory('TOI-1000', 'alice')['rows']) == 2
    denied = ScopedDenied({'HIDDEN'}, allowed={'OPEN'})
    result = inventory.inventory('TOI-1000', 'alice', denied=denied)
    assert [r['proposal'] for r in result['rows']] == ['OPEN']
    assert len(calls) == 1


def test_cache_isolated_by_user_token_dates_and_expiry(setup, monkeypatch):
    _, _, calls = setup
    inventory.inventory('TOI-1000', 'alice')
    inventory.inventory('TOI-1000', 'bob')
    monkeypatch.setattr(lco, '_get_lco_api_token', lambda *a, **kw: 'rotated')
    inventory.inventory('TOI-1000', 'alice')
    inventory.inventory('TOI-1000', 'alice', start='2026-10-01', end='2026-10-03')
    assert calls[-1][0]['end'] == '2026-10-04'
    assert len(calls) == 4
    monkeypatch.setattr(inventory.time, 'monotonic', lambda: 10**12)
    inventory.inventory('TOI-1000', 'alice')
    assert len(calls) == 5


@pytest.mark.parametrize('start,end', [('bad', ''), ('2026-02-30', ''), ('2026-10-04', '2026-10-03')])
def test_invalid_dates_do_not_query_archive(setup, start, end):
    with pytest.raises(lco.LcoError, match='valid dates'):
        inventory.inventory('TOI-1000', 'alice', start=start, end=end)
    assert setup[2] == []


def test_partial_group_deduplication_filters_and_missing_report(setup, monkeypatch):
    path, rows, _ = setup
    rows[:] = [frame(), frame(), frame(2), frame(3, 103)]
    rows[-1]['OBJECT'] = 'auto_focus'
    insert(path, rows[0])
    monkeypatch.setattr(exofop, 'build_time_series_report', lambda *a, **kw: {'ok': False})
    row, = inventory.inventory('TOI-1000', 'alice')['rows']
    assert row['frame_count'] == 2 and row['local_frames'] == 1 and not row['exists']
    assert row['reported'] is None


def test_later_page_failure_is_not_cached_or_reported_complete(setup, monkeypatch):
    def fail(*a, **kw):
        yield [frame()]
        raise lco.LcoError('Archive unavailable', status=502)
    monkeypatch.setattr(lco, 'archive_cursor_pages', fail)
    response = TestClient(web.app).get('/api/lco/archive/target?target=TOI-1000')
    assert response.status_code == 502
    assert response.json()['error'] == 'Archive unavailable'
    assert not inventory._cache
    streamed = TestClient(web.app).get('/api/lco/archive/target?target=TOI-1000&stream=true')
    import json
    events = [json.loads(line) for line in streamed.text.splitlines()]
    assert any('progress' in event for event in events)
    assert events[-1]['ok'] is False
    assert not any(event.get('ok') for event in events)
    assert not inventory._cache


def test_endpoint_requires_own_token_before_any_search(setup, monkeypatch):
    monkeypatch.setattr(web, '_viewer_denied', lambda: frozenset({'HIDDEN'}))
    def refuse(*a, **kw):
        raise lco.LcoError('Own token required', status=403)
    monkeypatch.setattr(lco, 'require_own_token_for_archive', refuse)
    response = TestClient(web.app).get('/api/lco/archive/target?target=TOI-1000')
    assert response.status_code == 403 and setup[2] == []


def test_header_fallback_uses_visible_median(setup, monkeypatch):
    path, rows, _ = setup
    insert(path, rows[0])
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE frames SET ra='10:00:00', declination='-20:00:00'")
    seen = []
    monkeypatch.setattr(inventory.catalog, '_target_crossmatch_coords',
                        lambda target, datasets: seen.append(datasets) or [(150.0, -20.0)])
    inventory.inventory('TOI-1000', 'alice')
    assert seen == [[{'ra': '10:00:00', 'dec': '-20:00:00'}]]


def test_real_pagination_groups_both_pages(mock_db, monkeypatch):
    inventory._cache.clear()
    monkeypatch.setattr(inventory.catalog, '_target_crossmatch_coords', lambda *a: [(1.0, 2.0)])
    monkeypatch.setattr(lco, '_get_lco_api_token', lambda *a, **kw: 'pagination-token')
    monkeypatch.setattr(exofop, 'build_time_series_report', lambda *a, **kw: {'ok': True})
    urls = []
    def page(url, **kw):
        urls.append(url)
        return {'count': 2, 'results': [frame(len(urls), 100 + len(urls))],
                'next': 'https://archive-api.lco.global/frames/?offset=1' if len(urls) == 1 else None}
    monkeypatch.setattr(lco, '_lco_api_request', page)
    result = inventory.inventory('TOI-1000', 'alice')
    assert len(urls) == 2 and len(result['rows']) == 2


def test_distinct_instruments_and_no_false_exofop_match(setup):
    _, rows, _ = setup
    rows[1].update(INSTRUME='sq36', TELID='0m4a', SITEID='coj')
    result = inventory.inventory('TOI-1000', 'alice')
    by_instrument = {r['instrument']: r for r in result['rows']}
    assert by_instrument['sinistro']['reported']
    assert not by_instrument['qhy600']['reported']


def test_photometry_uses_local_object_name_when_archive_target_is_an_alias(setup):
    path, rows, _ = setup
    insert(path, rows[0])
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE frames SET object='TIC-123'")
    result = inventory.inventory('TOI-1000', 'alice')
    local, = [r for r in result['rows'] if r['exists']]
    assert local['local_target'] == 'TIC-123'


def test_inventory_finds_a_new_night_after_ten_thousand_frames(mock_db, monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    inventory._cache.clear()
    monkeypatch.setattr(inventory.catalog, '_target_crossmatch_coords', lambda *a: [(1.0, 2.0)])
    monkeypatch.setattr(lco, '_get_lco_api_token', lambda *a, **kw: 'cursor-token')
    monkeypatch.setattr(exofop, 'build_time_series_report', lambda *a, **kw: {'ok': True})
    calls = []
    def page(url, **kw):
        calls.append(url)
        index = len(calls) - 1
        rows = [frame(index * 1000 + i) for i in range(1000)] if index < 10 else [frame(10001, 999)]
        if index == 10:
            rows[0]['filename'] = 'lsc1m005-fa15-20250901-0001-e91.fits.fz'
        return {'results': rows, 'next': f'https://archive-api.lco.global/frames/?cursor={index + 1}' if index < 10 else None}
    monkeypatch.setattr(lco, '_lco_api_request', page)
    result = inventory.inventory('TOI-1000', 'alice')
    assert sum(r['frame_count'] for r in result['rows']) == 10001
    assert {r['date'] for r in result['rows']} == {'2026-10-03', '2025-09-01'}
    assert len(calls) == 11
    query = parse_qs(urlsplit(calls[0]).query)
    assert query['pagination_style'] == ['cursor']
    assert query['configuration_type'] == ['EXPOSE']
    assert 'start' not in query and 'end' not in query


@pytest.mark.parametrize('next_url', [
    'https://archive-api.lco.global/frames/?cursor=loop',
    'https://example.com/frames/?cursor=secret',
    'https://archive-api.lco.global.evil.example/frames/',
    'http://archive-api.lco.global/frames/',
    'https://archive-api.lco.global/other/',
])
def test_cursor_rejects_cycles_and_untrusted_urls(monkeypatch, next_url):
    calls = []
    def page(url, **kw):
        calls.append(url)
        return {'results': [frame()], 'next': next_url}
    monkeypatch.setattr(lco, '_lco_api_request', page)
    with pytest.raises(lco.LcoError):
        list(lco.archive_cursor_pages({}, token='secret'))
    assert len(calls) == (2 if 'cursor=loop' in next_url else 1)


def test_documented_metadata_fields_are_retained(setup):
    _, rows, _ = setup
    rows[:] = [dict(filename='coj0m416-sq36-20260804-0098-e91.fits.fz',
                    site_id='coj', telescope_id='0m4a', instrument_id='sq36',
                    target_name='TOI-1000', exposure_time=30, primary_optical_element='rp',
                    observation_day='2026-08-04', request_id=123, proposal_id='OPEN')]
    row, = inventory.inventory('TOI-1000', 'alice')['rows']
    assert (row['instrument'], row['date'], row['site']) == ('qhy600', '2026-08-04', 'coj')
    assert row['filters'] == ['rp'] and row['exposures'] == ['30']


def test_abandoned_stream_releases_search_lock_without_caching(setup):
    events = inventory.inventory_events('TOI-1000', 'alice')
    assert 'progress' in next(events)
    assert 'progress' in next(events)
    events.close()
    assert not inventory._cache_lock.locked()
    assert not inventory._cache
    assert inventory.inventory('TOI-1000', 'alice')['ok']


def test_other_user_search_does_not_wait_for_open_stream(setup):
    first = inventory.inventory_events('TOI-1000', 'alice')
    second = inventory.inventory_events('TOI-1000', 'bob')
    try:
        next(first)
        next(first)
        next(second)
        assert next(second)['progress'] == 'Searching all archive nights…'
    finally:
        first.close()
        second.close()


def test_inventory_budget_is_incomplete_and_not_cached(setup, monkeypatch):
    monkeypatch.setattr(lco, '_ARCHIVE_CURSOR_MAX_PAGES', 2, raising=False)
    calls = []
    def page(url, **kw):
        calls.append(url)
        return {'results': [frame(len(calls))],
                'next': f'https://archive-api.lco.global/frames/?cursor={len(calls)}' if len(calls) < 5 else None}
    # Restore the real pager instead of the fixture's inventory stub.
    monkeypatch.setattr(lco, 'archive_cursor_pages', _REAL_CURSOR_PAGES)
    monkeypatch.setattr(lco, '_ARCHIVE_CURSOR_MAX_PAGES', 2, raising=False)
    monkeypatch.setattr(lco, '_get_lco_api_token', lambda *a, **kw: 'budget-token')
    monkeypatch.setattr(lco, '_lco_api_request', page)
    data = inventory.inventory('TOI-1000', 'alice')
    assert len(calls) == 2
    assert data['truncated']
    assert not inventory._cache


def test_matching_searches_share_fill_and_release_on_close(setup):
    first = inventory.inventory_events('TOI-1000', 'alice')
    second = inventory.inventory_events('TOI-1000', 'alice')
    try:
        next(first)
        next(first)
        next(second)
        assert 'Waiting' in next(second)['progress']
        list(first)
        assert list(second)[-1]['ok']
        assert len(setup[2]) == 1
    finally:
        first.close()
        second.close()
    assert not inventory._inflight


@pytest.mark.parametrize('rows,next_url,truncated', [
    ([], 'https://archive-api.lco.global/frames/?cursor=next', True),
    ([frame(), frame(2)], None, False),
    ([frame(), frame(2), frame(3)], None, True),
])
def test_cursor_budget_boundaries(monkeypatch, rows, next_url, truncated):
    monkeypatch.setattr(lco, '_ARCHIVE_CURSOR_MAX_PAGES', 1)
    monkeypatch.setattr(lco, '_ARCHIVE_CURSOR_MAX_FRAMES', 2)
    calls = []
    def page(url, **kw):
        calls.append(url)
        return {'results': rows, 'next': next_url}
    monkeypatch.setattr(lco, '_lco_api_request', page)
    result = lco.archive_search_all({}, max_frames=None)
    assert result['truncated'] is truncated
    assert len(result['results']) == min(2, len(rows))
    assert len(calls) == 1


def test_inventory_objects_preserve_header_names_and_missing_values(setup):
    _, rows, _ = setup
    rows[:] = [dict(frame(1), OBJECT='Header alias', target_name='Wrong alias'),
               dict(frame(2), OBJECT='Other header alias'),
               dict(frame(3), OBJECT='Header alias'),
               dict(frame(4, 102), OBJECT=None)]
    result = {r['request_id']: r for r in inventory.inventory('TOI-1000', 'alice')['rows']}
    assert result['101']['objects'] == ['Header alias', 'Other header alias']
    assert result['102']['objects'] == []
