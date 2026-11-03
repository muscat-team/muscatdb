"""Target-page regressions: exact job identity, note metadata, and retries."""
from playwright.sync_api import expect
import pytest

URL = '/target?name=TOI-1000'


def report():
    return {'ok': True, 'toi': '1000', 'total': 1, 'time_series': [
        {'tsdate': '2026-10-03', 'tstel': 'LCO-CTIO', 'tscam': 'Sinistro',
         'instrument': 'sinistro', 'site': 'lsc', 'lco': True,
         'exists_checked': True, 'exists': False}]}


def test_only_the_launched_job_can_be_cancelled_even_after_reload(page):
    page.route('**/api/lco/archive/exofop?*', lambda r: r.fulfill(json=report()))
    jobs = [{'run_id': 'unrelated', 'inst': 'sinistro', 'date': '261003',
             'target': 'TOI-9999', 'state': 'running', 'started_at': 999}]
    page.route('**/api/lco/archive/downloads', lambda r: r.fulfill(json={'ok': True, 'jobs': jobs}))
    def download(route):
        jobs.append({'run_id': 'ours', 'inst': 'sinistro', 'date': '261003',
                     'target': 'TOI-1000', 'state': 'running', 'started_at': 1})
        route.fulfill(json={'ok': True, 'job_id': 'ours'})
    page.route('**/api/lco/archive/exofop-download', download)
    cancelled = []
    def cancel(route):
        cancelled.append(route.request.url)
        route.fulfill(json={'ok': True})
    page.route('**/api/lco/archive/download/*/cancel', cancel)
    page.goto(URL)
    with page.expect_response('**/api/lco/archive/downloads'):
        page.locator('#exofop-obslog-details > summary').click()
    expect(page.locator('.exofop-action')).not_to_contain_text('Cancel')
    page.locator('.exofop-action').get_by_role('button', name='Download').click()
    expect(page.locator('.exofop-action button')).to_have_attribute('data-job-id', 'ours')
    page.reload()
    page.locator('#exofop-obslog-details > summary').click()
    expect(page.locator('.exofop-action button')).to_have_attribute('data-job-id', 'ours')
    with page.expect_response('**/api/lco/archive/download/ours/cancel'):
        page.locator('.exofop-action').get_by_role('button', name='Cancel').click()
    assert len(cancelled) == 1 and cancelled[0].endswith('/ours/cancel')


def test_note_filter_tracks_save_edit_and_delete_without_reload(page):
    page.goto(URL)
    row = page.locator('#datasets-table tbody tr[data-date="261003"]')
    cell = row.locator('.note-cell')
    note_filter = page.locator('input[data-col="note"]')
    for text in ('newnote', 'editednote'):
        note_filter.fill('')
        cell.click()
        cell.locator('textarea').fill(text)
        with page.expect_response(lambda r: r.url.endswith('/note') and r.request.method == 'PUT'):
            cell.locator('button.save').click()
        expect(cell.locator('.note-text')).to_have_text(text)
        note_filter.fill(text)
        expect(row).to_be_visible()
    cell.click()
    with page.expect_response(lambda r: r.url.endswith('/note') and r.request.method == 'DELETE'):
        cell.locator('button.del').click()
    expect(row).to_be_hidden()
    note_filter.fill('')
    expect(row).to_have_attribute('data-note', '')


@pytest.mark.parametrize('http_status', [200, 503])
def test_exofop_error_retries_when_reopened(page, http_status):
    if http_status == 503:
        page.problems.allowed += ['HTTP 503', 'status of 503']
    calls = []
    def answer(route):
        calls.append(route.request.url)
        if len(calls) == 1:
            route.fulfill(status=http_status, json={'ok': False, 'error': 'Temporary lookup failure'})
        else:
            route.fulfill(json=report())
    page.route('**/api/lco/archive/exofop?*', answer)
    page.goto(URL)
    summary = page.locator('#exofop-obslog-details > summary')
    summary.click()
    expect(page.locator('#exofop-obslog-status')).to_contain_text('Temporary lookup failure')
    summary.click()
    expect(page.locator('#exofop-obslog-details')).not_to_have_attribute('open', '')
    summary.click()
    expect(page.locator('#exofop-obslog-table')).to_be_visible()
    assert len(calls) == 2


def test_old_poll_does_not_forget_a_newly_queued_job(page):
    page.route('**/api/lco/archive/exofop?*', lambda r: r.fulfill(json=report()))
    held = []
    def poll(route):
        if not held:
            held.append(route)
        else:
            route.fulfill(json={'ok': True, 'jobs': [{'run_id': 'ours', 'state': 'running'}]})
    page.route('**/api/lco/archive/downloads', poll)
    page.route('**/api/lco/archive/exofop-download', lambda r: r.fulfill(
        json={'ok': True, 'job_id': 'ours'}))
    page.goto(URL)
    with page.expect_request('**/api/lco/archive/downloads'):
        page.locator('#exofop-obslog-details > summary').click()
    page.locator('.exofop-action').get_by_role('button', name='Download').click()
    expect(page.locator('#exofop-obslog-msg')).to_have_text('Download queued.')
    held[0].fulfill(json={'ok': True, 'jobs': []})
    expect(page.locator('.exofop-action button')).to_have_attribute('data-job-id', 'ours')
