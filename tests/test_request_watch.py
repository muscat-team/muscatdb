"""The server must name the requests that hold it up.

On 2026-11-07 the stalled requests never reached the access log (it records a
request only when it finishes), and the host forbids attaching a debugger to
the server, so nothing showed which request was stuck.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
import time

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import Route

from muscat_db import request_watch


@pytest.fixture(autouse=True)
def fast_threshold(monkeypatch):
    monkeypatch.setattr(request_watch, "SLOW_REQUEST_S", 0.05)
    request_watch._IN_FLIGHT.clear()
    yield
    request_watch._IN_FLIGHT.clear()


def _app(release: asyncio.Event | None = None):
    async def slow(_request):
        await asyncio.sleep(0.1)
        return PlainTextResponse("ok")

    async def fast(_request):
        return PlainTextResponse("ok")

    async def held(_request):
        await release.wait()
        return PlainTextResponse("ok")

    async def stream(_request):
        async def body():
            yield b"data: 1\n\n"
            await asyncio.sleep(0.1)
            yield b"data: 2\n\n"
        return StreamingResponse(body(), media_type="text/event-stream")

    app = Starlette(routes=[Route("/slow", slow), Route("/fast", fast), Route("/held", held),
                            Route("/stream", stream), Route("/socket.io/", slow)])
    return request_watch.RequestWatch(app)


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def test_a_slow_request_is_logged_with_its_path_status_and_time(caplog):
    async def main():
        async with _client(_app()) as client:
            await client.get("/slow", params={"inst": "sinistro"})
            await client.get("/fast")

    with caplog.at_level(logging.WARNING, logger=request_watch.__name__):
        asyncio.run(main())
    slow = [r.getMessage() for r in caplog.records if "slow request" in r.getMessage()]
    assert len(slow) == 1
    assert "GET /slow?inst=sinistro -> 200 in" in slow[0]


def test_streams_and_socketio_long_polls_are_not_reported(caplog):
    async def main():
        async with _client(_app()) as client:
            await client.get("/stream")
            await client.get("/socket.io/")

    with caplog.at_level(logging.WARNING, logger=request_watch.__name__):
        asyncio.run(main())
    assert not [r for r in caplog.records if r.name == request_watch.__name__]


def test_a_request_that_never_answers_is_reported_while_stuck_and_only_once(caplog):
    async def main():
        release = asyncio.Event()
        async with _client(_app(release)) as client:
            pending = asyncio.create_task(client.get("/held"))
            await asyncio.sleep(0.1)
            first = request_watch.report_stuck()
            second = request_watch.report_stuck()
            release.set()
            await pending
        return first, second

    first, second = asyncio.run(main())
    assert any("still running" in line and "GET /held" in line for line in first)
    assert any("1 request(s) waiting" in line for line in first)
    assert second == []
    assert request_watch._IN_FLIGHT == {}


def test_sigusr1_dumps_every_threads_stack(tmp_path):
    path = tmp_path / "stacks.log"
    parked = threading.Event()

    def parked_worker():
        parked.wait(10)

    worker = threading.Thread(target=parked_worker, name="parked")
    worker.start()
    try:
        assert request_watch.install_stack_dump(str(path))
        os.kill(os.getpid(), signal.SIGUSR1)
        deadline = time.monotonic() + 5
        while "parked_worker" not in path.read_text() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "parked_worker" in path.read_text()
    finally:
        parked.set()
        worker.join()
        import faulthandler
        faulthandler.unregister(signal.SIGUSR1)
