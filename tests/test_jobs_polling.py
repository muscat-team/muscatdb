"""Completion polling must be independent for each viewer."""

import pytest
from fastapi.testclient import TestClient

from muscat_db import web


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("type_", ["photometry", "transit_fit", "ttv_fit"])
def test_every_viewer_receives_watched_completion(monkeypatch, type_, method):
    key = f"{type_}:muscat3/261020/T/r1"
    job = dict(key=key, type=type_, inst="muscat3", date="261020", target="T",
               state="running", elapsed=10, started_at=0)
    monkeypatch.setattr(web, "_jobs_with_lco_archive_rows", lambda: [job])
    monkeypatch.setattr(web.fit, "_discover_orphan_fits", lambda _: [])
    monkeypatch.setattr(web, "_visible_jobs", lambda rows: rows)
    monkeypatch.setattr(web, "_live_elapsed", lambda _: 10)
    client = TestClient(web.app)
    assert client.get('/api/jobs/status').json()['running']
    job['state'] = 'done'
    for _ in range(2):
        response = (client.get('/api/jobs/status', params={'watch': key}) if method == 'get'
                    else client.post('/api/jobs/status', json={'watch': [key]}))
        assert response.status_code == 200
        data = response.json()
        assert key in data['finished']
        assert data['finished'][key]['state'] == 'done'
    assert not client.get('/api/jobs/status', params={'watch': 'unrelated'}).json()['finished']


def test_watching_a_key_does_not_bypass_visibility(monkeypatch):
    monkeypatch.setattr(web, '_jobs_with_lco_archive_rows', lambda: [
        dict(key='private', type='photometry', state='done')
    ])
    monkeypatch.setattr(web.fit, '_discover_orphan_fits', lambda _: [])
    monkeypatch.setattr(web, '_visible_jobs', lambda _: [])
    data = TestClient(web.app).post('/api/jobs/status', json={'watch': ['private']}).json()
    assert data['finished'] == {}
