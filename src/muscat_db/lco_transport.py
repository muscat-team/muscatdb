"""Keep-alive transport for archive frame downloads.

urllib opens a new TCP and TLS connection for every frame. Over the long round
trip to the archive's S3 bucket that setup, plus TCP slow start on each new
connection, cost about half of every frame's transfer time. A pooled client
reuses warm connections, so the same number of concurrent transfers moves
about twice the data. Request counts and the shared limiter are unchanged:
every hop, redirects included, is still validated and admitted one at a time.
"""

from __future__ import annotations

import email.message
import math
import os
import threading
import urllib.error
import urllib.parse

import httpx

from muscat_db import lco_rate

# Covers the local path's eight frame threads with room for a redirect hop.
_POOL_SIZE = 16
_KEEPALIVE_S = 30.0
_MAX_REDIRECTS = 10
_REDIRECTS = {301, 302, 303, 307, 308}

_LOCK = threading.Lock()
_CLIENT: httpx.Client | None = None
_CLIENT_PID: int | None = None


def _client() -> httpx.Client:
    """One process-wide pool. A forked child builds its own, never sharing sockets."""
    global _CLIENT, _CLIENT_PID
    with _LOCK:
        if _CLIENT is None or _CLIENT_PID != os.getpid():
            _CLIENT = httpx.Client(
                follow_redirects=False,
                limits=httpx.Limits(max_connections=_POOL_SIZE, max_keepalive_connections=_POOL_SIZE,
                                    keepalive_expiry=_KEEPALIVE_S),
            )
            _CLIENT_PID = os.getpid()
        return _CLIENT


class Response:
    """The subset of urllib's response API _download_to_file uses.

    ``read`` returns the next raw chunk (never content-decoded, as urllib
    does), so a declared Content-Length still matches the bytes written.
    """

    def __init__(self, response: httpx.Response, chunk: int):
        self._response = response
        self._chunks = response.iter_raw(chunk)
        self.headers = response.headers
        self.status = self.code = response.status_code

    def read(self, _size: int = -1) -> bytes:
        try:
            return next(self._chunks, b"")
        except httpx.TimeoutException:
            raise TimeoutError("Archive transfer timed out") from None
        except httpx.TransportError as exc:
            raise urllib.error.URLError(type(exc).__name__) from None

    def close(self) -> None:
        self._response.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
        return False


def _http_error(status: int, retry_after) -> urllib.error.HTTPError:
    # Callers classify by code and read Retry-After; never carry the signed URL
    # or the response body into an exception.
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("archive download", status, f"HTTP {status}", headers, None)


def open_url(url: str, *, timeout: float, validate, chunk: int) -> Response:
    """GET *url*, following validated redirects, with keep-alive connections."""
    for _ in range(_MAX_REDIRECTS + 1):
        validate(url)
        lco_rate.admit(url, authorized=False)
        client = _client()
        try:
            request = client.build_request("GET", url, timeout=httpx.Timeout(timeout))
            response = client.send(request, stream=True)
        except httpx.TimeoutException:
            raise TimeoutError("Archive transfer timed out") from None
        except httpx.TransportError as exc:
            raise urllib.error.URLError(type(exc).__name__) from None
        status = response.status_code
        if status in _REDIRECTS and response.headers.get("Location"):
            location = urllib.parse.urljoin(url, response.headers["Location"])
            response.close()
            url = location
            continue
        if status >= 400:
            retry_after = response.headers.get("Retry-After")
            response.close()
            seconds = lco_rate.throttled(url, status, retry_after)
            raise _http_error(status, math.ceil(seconds) if seconds is not None else retry_after)
        return Response(response, chunk)
    raise urllib.error.URLError("too many archive redirects")
