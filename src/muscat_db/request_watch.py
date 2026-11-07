"""Name the requests that hold the web server up.

The access log records a request only once it finishes, so the requests that
stalled production on 2026-11-07 never appeared in it, and the host forbids
attaching a debugger to the running server (``kernel.yama.ptrace_scope=1``).
This module adds the missing evidence:

* :class:`RequestWatch`, an ASGI middleware, logs every request that took
  longer than ``MUSCAT_SLOW_REQUEST_S`` (default 2 s) to answer.
* :func:`watch_loop` logs, once, each request still waiting for its answer
  after that long, so a request that never finishes is named while it is
  stuck, not after.
* :func:`install_stack_dump` makes ``kill -USR1 <server pid>`` write every
  thread's Python stack to ``MUSCAT_STACK_DUMP_PATH`` (default: the server's
  stderr), showing what each worker thread is doing.

Streaming responses (server-sent events) and socket.io long-polls are
long-lived by design and are not reported.
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from typing import IO

logger = logging.getLogger(__name__)

SLOW_REQUEST_S = max(0.1, float(os.environ.get("MUSCAT_SLOW_REQUEST_S", "2")))
_WATCH_INTERVAL_S = 5.0
# socket.io's polling transport holds each GET open until it has a message.
_LONG_POLL_PREFIXES = ("/socket.io",)
_MAX_TARGET_CHARS = 200


@dataclass
class _InFlight:
    method: str
    target: str
    started: float
    reported: bool = False


# Request id -> request awaiting its response. Only the event-loop thread
# touches it (the middleware and watch_loop both run there), so no lock.
_IN_FLIGHT: dict[int, _InFlight] = {}
_next_id = 0


def _target(scope) -> str:
    path = scope.get("path", "")
    query = scope.get("query_string", b"").decode("latin-1")
    target = f"{path}?{query}" if query else path
    return target if len(target) <= _MAX_TARGET_CHARS else target[:_MAX_TARGET_CHARS] + "…"


def _is_event_stream(headers) -> bool:
    for name, value in headers or ():
        if name.lower() == b"content-type":
            return value.split(b";")[0].strip().lower() == b"text/event-stream"
    return False


class RequestWatch:
    """ASGI middleware that times each HTTP request to its last body byte."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "").startswith(_LONG_POLL_PREFIXES):
            await self.app(scope, receive, send)
            return
        global _next_id
        _next_id += 1
        request_id = _next_id
        entry = _InFlight(scope.get("method", ""), _target(scope), time.monotonic())
        _IN_FLIGHT[request_id] = entry
        status: int | None = None
        streaming = False

        async def send_watched(message):
            nonlocal status, streaming
            if message["type"] == "http.response.start":
                status = message.get("status")
                streaming = _is_event_stream(message.get("headers"))
                if streaming:
                    # The client is answered; the stream stays open by design.
                    _IN_FLIGHT.pop(request_id, None)
            await send(message)

        try:
            await self.app(scope, receive, send_watched)
        finally:
            _IN_FLIGHT.pop(request_id, None)
            elapsed = time.monotonic() - entry.started
            if not streaming and elapsed >= SLOW_REQUEST_S:
                logger.warning("slow request: %s %s -> %s in %.1f s",
                               entry.method, entry.target, status, elapsed)


def report_stuck(now: float | None = None) -> list[str]:
    """Log each request that has waited ``SLOW_REQUEST_S`` without an answer,
    once per request, plus how many are waiting in all. Returns the lines."""
    now = time.monotonic() if now is None else now
    waiting = [e for e in _IN_FLIGHT.values() if now - e.started >= SLOW_REQUEST_S]
    lines = []
    for entry in waiting:
        if not entry.reported:
            entry.reported = True
            lines.append(f"request still running after {now - entry.started:.1f} s: "
                         f"{entry.method} {entry.target}")
    if lines:
        oldest = max(now - e.started for e in waiting)
        lines.append(f"{len(waiting)} request(s) waiting over {SLOW_REQUEST_S:g} s, "
                     f"oldest {oldest:.1f} s; `kill -USR1 {os.getpid()}` dumps every thread's stack")
    for line in lines:
        logger.warning(line)
    return lines


async def watch_loop() -> None:
    """Report stuck requests every few seconds until cancelled."""
    while True:
        await asyncio.sleep(_WATCH_INTERVAL_S)
        try:
            report_stuck()
        except Exception:
            logger.exception("request watch failed")


_stack_dump_file: IO | None = None


def install_stack_dump(path: str | None = None) -> bool:
    """Dump every thread's stack on SIGUSR1. Returns False where the platform
    has no SIGUSR1. The file stays open for the life of the process, as
    :mod:`faulthandler` requires."""
    global _stack_dump_file
    if not hasattr(signal, "SIGUSR1"):
        return False
    path = path if path is not None else os.environ.get("MUSCAT_STACK_DUMP_PATH", "")
    target = sys.stderr
    if path:
        _stack_dump_file = open(path, "a", buffering=1)  # noqa: SIM115 -- must stay open
        target = _stack_dump_file
    faulthandler.register(signal.SIGUSR1, file=target, all_threads=True)
    return True


def configure_logging() -> None:
    """Timestamp this module's warnings: the app sets up no logging of its own,
    so they would otherwise reach the server log as bare text (via Python's
    last-resort handler, which a handler here replaces rather than doubles)."""
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
