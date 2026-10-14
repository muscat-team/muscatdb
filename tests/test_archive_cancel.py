"""Cancelling LCO archive download jobs (pending, running, finished)."""
from __future__ import annotations

import pytest

from muscat_db import lco


@pytest.fixture
def jobs(monkeypatch):
    monkeypatch.setattr(lco, "_ARCHIVE_DOWNLOAD_JOBS", {})
    monkeypatch.setattr(lco._ARCHIVE_DOWNLOAD_EXECUTOR, "submit", lambda *a, **k: None)
    return lco._ARCHIVE_DOWNLOAD_JOBS


def _start():
    frames = [{"basename": "elp1m008-fa16-20230902-0001-e91", "SITEID": "elp",
               "TELID": "1m0a", "INSTRUME": "fa16", "DATE_OBS": "2023-09-03T01:00:00",
               "OBJECT": "TIC 1", "url": "https://example.invalid/x.fits.fz"}]
    return lco.start_archive_download(frames)["job_id"]


def test_cancel_pending_job_is_immediate(jobs):
    job_id = _start()
    snap = lco.cancel_archive_download(job_id)
    assert snap["state"] == "cancelled"
    assert jobs[job_id]["finished_at"] is not None


def test_cancel_running_job_moves_to_cancelling(jobs):
    job_id = _start()
    jobs[job_id]["state"] = "running"
    snap = lco.cancel_archive_download(job_id)
    assert snap["state"] == "cancelling"
    assert jobs[job_id]["cancel_requested"] is True


def test_cancel_finished_job_conflicts(jobs):
    job_id = _start()
    jobs[job_id]["state"] = "done"
    with pytest.raises(lco.LcoError) as exc:
        lco.cancel_archive_download(job_id)
    assert exc.value.status == 409


def test_cancel_unknown_job_is_404(jobs):
    with pytest.raises(lco.LcoError) as exc:
        lco.cancel_archive_download("nope")
    assert exc.value.status == 404


def test_worker_skips_download_when_cancelled_before_start(jobs, monkeypatch):
    job_id = _start()
    jobs[job_id]["cancel_requested"] = True
    called = []
    monkeypatch.setattr(lco, "_download_frame_with_retry", lambda *a, **k: called.append(1))
    lco._run_archive_download_job(job_id)
    assert called == []
    assert jobs[job_id]["state"] == "cancelled"


def test_worker_settles_cancelling_job_as_cancelled(jobs, monkeypatch):
    job_id = _start()

    def fake_download(frame, overwrite=False):
        # The cancel arrives while the frame is in flight.
        lco.cancel_archive_download(job_id)
        return {"filename": "a.fits.fz", "status": "downloaded", "dest": "/nonexistent/a.fits.fz"}

    monkeypatch.setattr(lco, "_download_frame_with_retry", fake_download)
    funpacked = []
    monkeypatch.setattr(lco, "_funpack_file", lambda *a, **k: funpacked.append(1))
    lco._run_archive_download_job(job_id)
    assert jobs[job_id]["state"] == "cancelled"
    assert funpacked == []


def _big_frames(n):
    return [
        {"basename": f"ogg2m001-ep0{i % 4}-20231230-{i:04d}-e91", "SITEID": "ogg",
         "TELID": "2m0a", "INSTRUME": "ep03", "DATE_OBS": "2023-12-30T10:00:00",
         "OBJECT": "TIC 1", "url": "https://example.invalid/" + "x" * 600 + f"/{i}.fits.fz"}
        for i in range(n)
    ]


def test_client_posted_payload_is_capped(jobs, monkeypatch):
    monkeypatch.setattr(lco, "_ARCHIVE_DOWNLOAD_MAX_PAYLOAD_BYTES", 50_000)
    with pytest.raises(lco.LcoError) as exc:
        lco.start_archive_download(_big_frames(200))
    assert exc.value.status == 413


def test_server_side_search_result_is_not_payload_capped(jobs, monkeypatch):
    """The cap guards client-posted bodies; a frame list the server built
    itself (the ExoFOP download of a 3410-frame MuSCAT3 night) never crossed
    the wire, so it must not be rejected for its size."""
    monkeypatch.setattr(lco, "_ARCHIVE_DOWNLOAD_MAX_PAYLOAD_BYTES", 50_000)
    snap = lco.start_archive_download(_big_frames(200), server_side=True)
    assert snap["frames_total"] == 200
