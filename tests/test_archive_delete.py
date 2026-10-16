"""Forgetting finished LCO archive download jobs held in memory."""
from __future__ import annotations

import pytest

from muscat_db import lco


@pytest.fixture
def jobs(monkeypatch):
    monkeypatch.setattr(lco, "_ARCHIVE_DOWNLOAD_JOBS", {})
    monkeypatch.setattr(lco._ARCHIVE_DOWNLOAD_EXECUTOR, "submit", lambda *a, **k: None)
    return lco._ARCHIVE_DOWNLOAD_JOBS


def _start() -> str:
    frames = [{"basename": "elp1m008-fa16-20230902-0001-e91", "SITEID": "elp",
               "TELID": "1m0a", "INSTRUME": "fa16", "DATE_OBS": "2023-09-03T01:00:00",
               "OBJECT": "TIC 1", "url": "https://example.invalid/x.fits.fz"}]
    return lco.start_archive_download(frames)["job_id"]


def test_delete_archive_download_only_when_finished(jobs):
    job_id = _start()
    with pytest.raises(lco.LcoError) as exc:
        lco.delete_archive_download(job_id)
    assert exc.value.status == 409
    jobs[job_id]["state"] = "done"
    lco.delete_archive_download(job_id)
    assert job_id not in jobs
    lco.delete_archive_download(job_id)  # absent: no-op
