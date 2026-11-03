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

import asyncio
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
        # Set on the first poll(): the stream has started reading the job.
        self.first_polled = threading.Event()

    def poll(self):
        self.first_polled.set()
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
    """Mutate the fake job's process/log from a real background thread once
    the stream has first read the job, plus a short delay, so the blocking test
    request always observes the running state first. Anchoring on the first
    poll (not on thread start) keeps this independent of how long a loaded CI
    runner takes to start the request."""
    def run():
        proc.first_polled.wait(timeout=10)
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


class _FakeRequest:
    """Stands in for the Starlette request: the client never disconnects."""

    async def is_disconnected(self):
        return False


class TestStreamEventLoop:
    """Issue #325 item 7: the generator runs its blocking status read off the
    event loop, and a failure inside it ends the stream instead of escaping."""

    def test_slow_status_read_does_not_delay_other_coroutines(self):
        def slow_status():
            time.sleep(0.3)
            return {"state": "done"}

        async def scenario():
            delays: list[float] = []

            async def timer():
                t0 = time.monotonic()
                await asyncio.sleep(0.01)
                delays.append(time.monotonic() - t0)

            timer_task = asyncio.create_task(timer())
            # Let the timer reach its sleep before the stream starts reading.
            await asyncio.sleep(0)
            events = [chunk async for chunk in web._sse_job_stream(_FakeRequest(), slow_status)]
            await timer_task
            return events, delays

        events, delays = asyncio.run(scenario())
        assert events and b'"state":"done"' in events[-1]
        # Blocked on the loop, the 10 ms timer would fire only after the full
        # 300 ms status read.
        assert delays[0] < 0.15, f"timer delayed {delays[0]:.3f}s by the status read"

    def test_status_read_failure_ends_the_stream_cleanly(self):
        calls: list[int] = []

        def flaky_status():
            calls.append(1)
            if len(calls) == 1:
                return {"state": "running", "log": "started"}
            raise RuntimeError("job store unavailable")

        async def scenario():
            return [chunk async for chunk in web._sse_job_stream(_FakeRequest(), flaky_status)]

        events = asyncio.run(scenario())
        assert len(events) == 1
        assert _parse_sse_events(events[0].decode())[0]["state"] == "running"
        assert calls == [1, 1]

    @pytest.mark.parametrize("route, module, params", [
        ("/api/photometry/log-stream", phot, {"inst": INST, "date": DATE, "target": TARGET}),
        ("/api/transit-fit/log-stream", fit, {"inst": INST, "date": DATE, "target": TARGET}),
    ])
    def test_routes_leave_reconciliation_to_the_background_loop(self, mock_db, monkeypatch, route, module, params):
        synced: list[int] = []
        monkeypatch.setattr(module, "sync_jobs", lambda: synced.append(1))
        monkeypatch.setattr(module, "job_status", lambda *a, **kw: {"state": "done", "log": ""})
        response = TestClient(app).get(route, params=params)
        assert response.status_code == 200
        assert _parse_sse_events(response.text)[-1]["state"] == "done"
        assert synced == [], "the stream must not reconcile jobs itself"

    def test_visibility_lookup_runs_as_the_requesting_viewer(self, mock_db, monkeypatch):
        """The visibility check now runs on a worker thread; it must still see
        the viewer the auth middleware set for this request."""
        monkeypatch.setenv("MUSCAT_PROXY_SECRET", "proxy-secret")
        seen: list[str | None] = []

        def fake_hidden(level, inst, date, target, run_id=""):
            seen.append(web._CURRENT_USER.get())
            return False

        monkeypatch.setattr(web, "_run_hidden", fake_hidden)
        monkeypatch.setattr(phot, "job_status", lambda *a, **kw: {"state": "done", "log": ""})
        response = TestClient(app, client=("127.0.0.1", 12345)).get(
            "/api/photometry/log-stream",
            params={"inst": INST, "date": DATE, "target": TARGET},
            headers={"X-Forwarded-User": "alice", "X-MuSCAT-Proxy-Secret": "proxy-secret"},
        )
        assert response.status_code == 200
        assert seen == ["alice"]
