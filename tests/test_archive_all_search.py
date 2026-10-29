"""The archive page defaults to complete object searches, not previews."""
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from muscat_db import lco, web
from tests.test_target_archive import frame


def test_default_object_search_reaches_nights_beyond_ten_thousand_frames(mock_db, monkeypatch):
    monkeypatch.setattr(web, '_resolve_archive_coords', lambda name: (1.0, 2.0, 'catalog'))
    monkeypatch.setattr(web, '_annotate_lco_archive_results', lambda inst, rows, **kw: (rows, 2))
    calls = []
    def page(url, **kw):
        calls.append(url)
        index = len(calls) - 1
        rows = [frame(index * 1000 + i) for i in range(1000)] if index < 10 else [frame(10001)]
        if index == 10:
            rows[0]['filename'] = 'lsc1m005-fa15-20250901-0001-e91.fits.fz'
        return {'results': rows, 'next': f'https://archive-api.lco.global/frames/?cursor={index+1}' if index < 10 else None}
    monkeypatch.setattr(lco, '_lco_api_request', page)
    data = TestClient(web.app).get('/api/lco/archive/frames?OBJECT=TOI-1000').json()
    assert len(data['results']) == 10001
    assert '20250901' in data['results'][-1]['filename']
    assert not data['truncated']
    assert parse_qs(urlsplit(calls[0]).query)['pagination_style'] == ['cursor']


def test_stream_filters_denied_proposals_and_keeps_later_telescope_matches(mock_db, monkeypatch):
    import json
    monkeypatch.setattr(web, '_resolve_archive_coords', lambda name: (1.0, 2.0, 'catalog'))
    monkeypatch.setattr(web, '_viewer_denied', lambda: frozenset({'HIDDEN'}))
    monkeypatch.setattr(lco, 'require_own_token_for_archive', lambda *a: None)
    rows = [frame(1), frame(2), frame(3)]
    rows[0]['TELID'] = '2m0a'
    rows[1]['proposal_id'] = 'HIDDEN'
    monkeypatch.setattr(lco, 'archive_cursor_pages', lambda *a, **kw: iter([[rows[0]], rows[1:]]))
    response = TestClient(web.app).get('/api/lco/archive/frames?OBJECT=TOI-1000&TELID=1m0&stream=true')
    events = [json.loads(line) for line in response.text.splitlines()]
    assert 'progress' in events[0]
    assert [r['filename'] for r in events[-1]['results']] == [rows[2]['filename']]
    assert events[-1]['count'] == 1


def test_numeric_preview_remains_explicit_and_limited(mock_db, monkeypatch):
    monkeypatch.setattr(web, '_resolve_archive_coords', lambda name: (1.0, 2.0, 'catalog'))
    def page(*a, **kw):
        return {'count': 10001, 'results': [frame(i) for i in range(1000)],
                'next': 'https://archive-api.lco.global/frames/?offset=1000'}
    monkeypatch.setattr(lco, '_lco_api_request', page)
    data = TestClient(web.app).get('/api/lco/archive/frames?OBJECT=TOI-1000&limit=5').json()
    assert len(data['results']) == 5 and data['truncated']


@pytest.mark.parametrize('stream', [False, True])
def test_blank_fuzzy_search_has_server_budget(mock_db, monkeypatch, stream):
    import json
    monkeypatch.setattr(lco, '_ARCHIVE_CURSOR_MAX_PAGES', 2, raising=False)
    monkeypatch.setattr(web, '_annotate_lco_archive_results', lambda inst, rows, **kw: (rows, 1))
    calls = []
    def page(url, **kw):
        calls.append(url)
        return {'results': [frame(len(calls))],
                'next': f'https://archive-api.lco.global/frames/?cursor={len(calls)}' if len(calls) < 5 else None}
    monkeypatch.setattr(lco, '_lco_api_request', page)
    response = TestClient(web.app).get('/api/lco/archive/frames', params={'fuzzy_name': 'true', 'stream': str(stream).lower()})
    data = json.loads(response.text.splitlines()[-1]) if stream else response.json()
    assert len(calls) == 2
    assert len(data['results']) == 2 and data['truncated']


def test_request_id_filters_locally_denied_rows(mock_db, monkeypatch):
    monkeypatch.setattr(web, '_viewer_denied', lambda: frozenset({'HIDDEN'}))
    monkeypatch.setattr(lco, 'require_own_token_for_archive', lambda *a: None)
    hidden = frame()
    hidden['proposal_id'] = 'HIDDEN'
    monkeypatch.setattr(lco, 'archive_search_all', lambda *a, **kw: {'results': [hidden], 'count': 1})
    monkeypatch.setattr(web, '_annotate_lco_archive_results', lambda inst, rows, **kw: (rows, 0))
    data = TestClient(web.app).get('/api/lco/archive/frames?request_id=101').json()
    assert data['results'] == [] and data['count'] == 0
