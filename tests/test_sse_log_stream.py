"""Tests for the SSE log-streaming endpoints (architecture issue #51, step 4).

Each pipeline's ``/log-stream`` route wraps its existing ``job_status()``, so
these tests build a fake in-memory job the same way
``test_transit_fit_finalize.py`` does.

Starlette's ``TestClient`` runs the whole ASGI app to completion on a
background thread before handing back a response -- it does not support
reading a genuinely open-ended stream incrementally from the test thread (a
job stuck ``running`` forever would hang the request). So each test instead
starts a real background thread that flips the fake job to terminal shortly
after the request begins; the streamed request unblocks once our own
generator sees that and returns, and the full SSE transcript is asserted on
the completed response body.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from muscat_db import photometry as phot
from muscat_db import transit_fit as fit
from muscat_db import ttv_fit as ttv
from muscat_db import web
from muscat_db.web import app

INST = "muscat4"
DATE = "260101"
TARGET = "HIP67522"


class _FakeProc:
    def __init__(self, rc=None):
        self._rc = rc
        self.pid = os.getpid()

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        return self._rc


def _parse_sse_events(text: str) -> list[dict]:
    """Split a fully-buffered SSE response body into its ``data:`` payloads."""
    events = []
    for chunk in text.split("\n\n"):
        for line in chunk.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


def _flip_to_terminal(proc, log_path, marker: str, delay=0.05):
    """Mutate the fake job's process/log from a real background thread after
    a short delay, so the blocking test request has something to observe."""
    def run():
        time.sleep(delay)
        proc._rc = 0
        with open(log_path, "a") as f:
            f.write(marker + "\n")
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


@pytest.fixture(autouse=True)
def _fast_sse_interval(monkeypatch):
    # Keep these tests fast: the default 1s poll interval would make each
    # streamed transition take real wall-clock seconds.
    monkeypatch.setattr(web, "_SSE_POLL_INTERVAL_S", 0.01)


class TestPhotometryLogStream:
    def _clear(self):
        with phot._LOCK:
            for job in phot._JOBS.values():
                job.logf.close()
            phot._JOBS.clear()

    def _make_job(self, tmp_path, monkeypatch):
        self._clear()
        monkeypatch.setattr(phot, "_FINALIZE_GRACE_S", 0.05)
        monkeypatch.setattr(phot, "_FINALIZE_GRACE_TERMINAL_S", 0.05)
        log = tmp_path / "run_photometry.log"
        log.write_text("$ run_photometry\nINFO: started\n")
        proc = _FakeProc(rc=None)
        key = phot.job_key(INST, DATE, TARGET)
        job = phot.Job(
            key=key, inst=INST, date=DATE, target=TARGET,
            cmd=["x"], proc=proc, logf=open(log, "a"),
            log_path=log, run_type="full",
        )
        with phot._LOCK:
            phot._JOBS[key] = job
        return proc, log

    def test_streams_running_then_terminal_and_closes(self, mock_db, tmp_path, monkeypatch):
        proc, log = self._make_job(tmp_path, monkeypatch)
        t = _flip_to_terminal(proc, log, "photometry SUCCEEDED")
        try:
            response = TestClient(app).get(
                "/api/photometry/log-stream",
                params={"inst": INST, "date": DATE, "target": TARGET},
            )
            t.join(timeout=5)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["x-accel-buffering"] == "no"

            events = _parse_sse_events(response.text)
            assert events, "expected at least one SSE event"
            assert events[0]["state"] == "running"
            assert events[-1]["state"] == "done"
        finally:
            self._clear()


class TestTransitFitLogStream:
    def _clear(self):
        with fit._FIT_LOCK:
            for job in fit._FIT_JOBS.values():
                job.logf.close()
            fit._FIT_JOBS.clear()

    def _make_job(self, tmp_path, monkeypatch):
        self._clear()
        monkeypatch.setattr(fit, "_FINALIZE_GRACE_S", 0.05)
        monkeypatch.setattr(fit, "_FINALIZE_GRACE_TERMINAL_S", 0.05)
        log = tmp_path / "timer-fit.log"
        log.write_text("$ timer-fit\nINFO: started\n")
        proc = _FakeProc(rc=None)
        key = fit.fit_job_key(INST, DATE, TARGET)
        job = fit.TransitFitJob(
            key=key, inst=INST, date=DATE, target=TARGET,
            cmd=["x"], proc=proc, logf=open(log, "a"),
            log_path=log, run_type="full",
        )
        with fit._FIT_LOCK:
            fit._FIT_JOBS[key] = job
        return proc, log

    def test_streams_running_then_terminal(self, mock_db, tmp_path, monkeypatch):
        proc, log = self._make_job(tmp_path, monkeypatch)
        t = _flip_to_terminal(proc, log, "INFO: fit finished")
        try:
            response = TestClient(app).get(
                "/api/transit-fit/log-stream",
                params={"inst": INST, "date": DATE, "target": TARGET},
            )
            t.join(timeout=5)
            assert response.status_code == 200
            events = _parse_sse_events(response.text)
            assert events[0]["state"] == "running"
            assert events[-1]["state"] == "done"
        finally:
            self._clear()


class TestTtvFitLogStream:
    def _clear(self):
        with ttv._TTV_LOCK:
            for job in ttv._TTV_JOBS.values():
                job.logf.close()
            ttv._TTV_JOBS.clear()

    def _make_job(self, tmp_path, monkeypatch):
        self._clear()
        monkeypatch.setattr(ttv, "_FINALIZE_GRACE_S", 0.05)
        monkeypatch.setattr(ttv, "_FINALIZE_GRACE_TERMINAL_S", 0.05)
        log = tmp_path / "harmonic.log"
        log.write_text("$ harmonic\nINFO: started\n")
        proc = _FakeProc(rc=None)
        key = ttv.ttv_job_key(TARGET)
        job = ttv.TTVFitJob(
            key=key, inst="", date="", target=TARGET,
            cmd=["x"], proc=proc, logf=open(log, "a"),
            log_path=log, run_type="full",
        )
        with ttv._TTV_LOCK:
            ttv._TTV_JOBS[key] = job
        return proc, log

    def test_requires_target(self, mock_db):
        response = TestClient(app).get("/api/ttv-fit/log-stream")
        assert response.status_code == 400

    def test_streams_running_then_terminal(self, mock_db, tmp_path, monkeypatch):
        proc, log = self._make_job(tmp_path, monkeypatch)
        t = _flip_to_terminal(proc, log, "TTV fitting completed successfully")
        try:
            response = TestClient(app).get(
                "/api/ttv-fit/log-stream", params={"target": TARGET},
            )
            t.join(timeout=5)
            assert response.status_code == 200
            events = _parse_sse_events(response.text)
            assert events[0]["state"] == "running"
            assert events[-1]["state"] == "done"
        finally:
            self._clear()
