"""Exercise the target inventory as a user: open, narrow, download, retry."""
import json

import pytest
from playwright.sync_api import expect

from muscat_db import target_archive, lco, exofop
from tests.test_target_archive import frame


@pytest.fixture
def archive(monkeypatch):
    target_archive._cache.clear()
    calls = []
    monkeypatch.setattr(lco, '_get_lco_api_token', lambda *a, **kw: 'browser-token')
    monkeypatch.setattr(exofop, 'build_time_series_report', lambda *a, **kw: {'ok': True, 'time_series': []})
    def search(filters, *a, **kw):
        calls.append(filters)
        yield [frame(1, 101), frame(2, 102, public_date='2999-01-01T00:00:00Z')]
    monkeypatch.setattr(lco, 'archive_cursor_pages', search)
    return calls


def test_lazy_load_grouping_date_persistence_and_download(page, archive):
    page.goto('/target?name=TOI-1000')
    page.wait_for_load_state('networkidle')
    assert archive == []
    page.locator('#target-lco-details > summary').click()
    expect(page.locator('#target-lco-table tbody tr')).to_have_count(2)
    assert len(archive) == 1
    expect(page.locator('#target-lco-table')).to_contain_text('30.123457')
    expect(page.locator('#target-lco-table')).to_contain_text('Proprietary')
    posted = []
    def download(route):
        posted.append(route.request.post_data_json)
        route.fulfill(json={'ok': True, 'run_id': 'test-download'})
    page.route('**/api/lco/archive/request-download', download)
    page.locator('#target-lco-table tbody tr').first.get_by_role('button', name='Download').click()
    expect(page.locator('#target-lco-status')).to_contain_text('Download queued')
    assert posted == [{'request_id': '102', 'reduction_level': '91'}]
    page.fill('#target-lco-start', '2026-10-01')
    page.fill('#target-lco-end', '2026-10-05')
    page.locator('#target-lco-form').get_by_role('button', name='Search').click()
    expect(page.locator('#target-lco-status')).to_have_text('2 archive datasets.')
    assert archive[-1]['end'] == '2026-10-06'
    page.reload()
    assert len(archive) == 2
    page.locator('#target-lco-details > summary').click()
    expect(page.locator('#target-lco-start')).to_have_value('2026-10-01')
    expect(page.locator('#target-lco-status')).to_have_text('2 archive datasets.')
    assert len(archive) == 2


def test_error_retry_and_safe_metadata_rendering(page, archive):
    replies = [dict(ok=False, error='Own token required'), dict(ok=True, rows=[dict(
        date='2026-10-03', instrument='sinistro', site='lsc', telescope='1m0a',
        filters=['<img src=x onerror=alert(1)>'], frame_count=1, exposures=['30'],
        proposal='<b>proposal</b>', request_id='101', proprietary=False,
        exists=True, local_frames=1, reported=None)])]
    def answer(route):
        route.fulfill(body=json.dumps(replies.pop(0)), content_type='application/json')
    page.route('**/api/lco/archive/target?**', answer)
    page.goto('/target?name=TOI-1000')
    page.locator('#target-lco-details > summary').click()
    expect(page.locator('#target-lco-status')).to_contain_text('Own token required')
    page.locator('#target-lco-form').get_by_role('button', name='Search').click()
    expect(page.locator('#target-lco-table')).to_be_visible()
    assert page.locator('#target-lco-table img, #target-lco-table b').count() == 0
    expect(page.locator('#target-lco-table')).to_contain_text('Unknown')
    page.locator('#target-lco-table').get_by_role('link', name='Photometry').click()
    page.wait_for_url('**/photometry?**')
    assert 'inst=sinistro' in page.url and 'date=261003' in page.url


def test_progress_arrives_before_last_archive_page(page, archive, monkeypatch):
    import threading
    release = threading.Event()
    def pages(*a, **kw):
        yield [frame(1, 101)]
        if not release.wait(15):
            raise lco.LcoError('Test did not release second page')
        yield [frame(2, 102)]
    monkeypatch.setattr(lco, 'archive_cursor_pages', pages)
    page.goto('/target?name=TOI-1000')
    try:
        page.locator('#target-lco-details > summary').click()
        expect(page.locator('#target-lco-status')).to_have_text('Searching all archive nights…')
        expect(page.locator('#target-lco-table')).to_be_hidden()
        expect(page.locator('#target-lco-form button')).to_be_disabled()
    finally:
        release.set()
    expect(page.locator('#target-lco-table tbody tr')).to_have_count(2)
    expect(page.locator('#target-lco-form button')).to_be_enabled()


def test_incomplete_stream_does_not_show_partial_results(page, archive):
    page.route('**/api/lco/archive/target?**', lambda route: route.fulfill(
        content_type='application/x-ndjson', body=json.dumps({'progress': 'Searching…'}) + '\n'))
    page.goto('/target?name=TOI-1000')
    page.locator('#target-lco-details > summary').click()
    expect(page.locator('#target-lco-status')).to_contain_text('ended before completion')
    expect(page.locator('#target-lco-table')).to_be_hidden()
    expect(page.locator('#target-lco-form button')).to_be_enabled()
