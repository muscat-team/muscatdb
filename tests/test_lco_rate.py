"""Offline HTTP and deterministic clocks: shared admission on every wire hop."""

from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from unittest.mock import Mock
import urllib.error
import urllib.request

import pytest

from muscat_db import lco, lco_download, lco_rate


class Clock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(lco_rate.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(lco_rate.time, "sleep", clock.sleep)
    return clock


@pytest.mark.parametrize("value,expected", [(None, 60), ("", 60), ("oops", 60),
    ("-5", 60), ("inf", 60), ("0", 1), ("120", 120), ("86400", 86400),
    ("Thu, 01 Jan 1970 00:02:00 GMT", 20), ("Thu, 01 Jan 1970 00:00:00 GMT", 1)])
def test_retry_after(value, expected):
    assert lco_rate.retry_after_seconds(value, 100) == expected


@pytest.mark.parametrize("value", ["86401", "172800", "99999999999", "Fri, 01 Jan 2100 00:00:00 GMT"])
def test_retry_after_cannot_park_the_fleet_beyond_one_day(value):
    assert lco_rate.retry_after_seconds(value, 100) == lco_rate.MAX_RETRY_AFTER_S


def test_strictest_policy_takes_lowest_budget_and_longest_interval():
    lax = dict(lco_rate._POLICY, api_daily=50000, api_interval=0.5)
    strict = dict(lco_rate._POLICY, api_daily=2000, api_interval=2.0)
    for stored, local in ((lax, strict), (strict, lax)):
        merged = lco_rate._strictest(stored, local)
        assert merged["api_daily"] == 2000
        assert merged["api_interval"] == 2.0


@pytest.mark.parametrize("stored", [{"api_daily": "oops"}, {"api_daily": -1}, "not a mapping"])
def test_malformed_stored_policy_is_rejected(stored):
    with pytest.raises(ValueError):
        lco_rate._strictest(stored, lco_rate._POLICY)


def test_backoff_is_jittered_and_bounded(monkeypatch):
    seen = []
    monkeypatch.setattr(lco_rate.random, "uniform", lambda a, b: seen.append((a, b)) or b)
    assert [lco_rate.backoff(a) for a in [0, 1, 2, 10000]] == [2, 4, 8, 30]
    assert seen == [(1, 2), (2, 4), (4, 8), (15, 30)]


def test_waiting_worker_rechecks_lease_but_holds_no_connection(monkeypatch, clock):
    coord = Mock()
    coord.reserve.side_effect = [86400, 0]
    monkeypatch.setattr(lco_rate, "coordinator", lambda: coord)
    checks = [0]
    def check():
        checks[0] += 1
    lco_rate.acquire("archive", check=check)
    assert clock.now == 86400
    assert checks[0] > 86400
    assert coord.reserve.call_count == 2


@pytest.mark.parametrize("failure", ["cancelled", "lease lost", "database unavailable"])
def test_wait_aborts_before_another_request(monkeypatch, clock, failure):
    coord = Mock()
    coord.reserve.return_value = 86400
    monkeypatch.setattr(lco_rate, "coordinator", lambda: coord)
    def check():
        if clock.now >= 0.25:
            raise lco_download.LeaseLost(failure)
    with pytest.raises(lco_download.LeaseLost, match=failure):
        lco_rate.acquire("storage", check=check)
    assert coord.reserve.call_count == 1
    assert clock.now == 0.25


def test_web_daily_budget_returns_promptly(monkeypatch, clock):
    coord = Mock()
    coord.reserve.return_value = 3600
    monkeypatch.setattr(lco_rate, "coordinator", lambda: coord)
    with pytest.raises(lco.LcoError) as error:
        lco_rate.acquire("archive")
    assert error.value.status == 429
    assert error.value.retry_after == 3600
    assert clock.now == 0


def test_no_fallback_to_profile_database(monkeypatch):
    monkeypatch.delenv("MUSCAT_LCO_RATE_DSN", raising=False)
    monkeypatch.setenv("MUSCAT_POSTGRES_DSN", "postgresql://user:secret@production/queue")
    with pytest.raises(lco.LcoError) as error:
        lco_rate.acquire("archive")
    assert error.value.status == 503
    assert "secret" not in str(error.value)


def test_unavailable_coordinator_does_not_leak_dsn(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    def fail(*args, **kwargs):
        raise OSError("postgresql://user:password@host/secret-db")
    monkeypatch.setattr(psycopg, "connect", fail)
    with pytest.raises(lco.LcoError) as error:
        lco_rate.Coordinator("private-dsn").reserve("archive")
    assert error.value.status == 503
    assert "password" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.fixture
def http(monkeypatch):
    requests = []
    reply = {"status": 200, "headers": {}, "body": b'{"results": []}'}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/storage")
            else:
                self.send_response(reply["status"])
                for name, value in reply["headers"].items():
                    self.send_header(name, value)
            self.end_headers()
            self.wfile.write(reply["body"])

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    # Only these loopback test URLs are classified; no external service used.
    monkeypatch.setattr(lco_rate, "_scope", lambda url: "storage" if "/storage" in url else "archive")
    coord = Mock()
    coord.reserve.return_value = 0
    coord.cooldown.return_value = 123
    monkeypatch.setattr(lco_rate, "coordinator", lambda: coord)
    yield base, reply, requests, coord
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_redirect_counts_each_hop_without_storing_urls_or_tokens(http):
    base, reply, requests, coord = http
    opener = urllib.request.build_opener(lco_rate.ArchiveRateHandler())
    req = urllib.request.Request(base + "/redirect", headers={"Authorization": "Token private-token"})
    with opener.open(req) as response:
        assert response.status == 200
    assert requests == ["/redirect", "/storage"]
    assert [c.args for c in coord.reserve.call_args_list] == [("archive",), ("storage",)]
    assert "private-token" not in str(coord.mock_calls)
    assert base not in str(coord.mock_calls)


@pytest.mark.parametrize("path,status", [("/api?signature=secret", 429), ("/storage?signature=secret", 429),
                                        ("/storage", 503)])
def test_throttling_publishes_shared_cooldown_before_error(http, path, status):
    base, reply, requests, coord = http
    reply.update(status=status, headers={"Retry-After": "123"}, body=b"private response")
    opener = urllib.request.build_opener(lco_rate.ArchiveRateHandler())
    with pytest.raises(urllib.error.HTTPError) as error:
        opener.open(base + path)
    assert error.value.headers["Retry-After"] == "123"
    error.value.close()
    coord.cooldown.assert_called_once_with("storage" if "storage" in path else "archive", "123")
    assert "secret" not in str(coord.mock_calls)
    coord.reserve.return_value = 123
    with pytest.raises(lco.LcoError, match="cooldown"):
        opener.open(base + path)
    assert len(requests) == 1


def test_archive_api_error_and_web_retry_after_are_safe(http):
    from muscat_db.web import _lco_error_response

    base, reply, requests, coord = http
    reply.update(status=429, body=b"private signed URL and token")
    with pytest.raises(lco.LcoError) as error:
        lco._lco_api_request(base + "/api", token="private-token")
    assert error.value.status == 429
    response = _lco_error_response(error.value)
    assert response.headers["Retry-After"] == "123"
    assert json.loads(response.body)["retry_after"] == 123
    assert b"private" not in response.body
    coord.cooldown.assert_called_once_with("archive", None)


def test_rate_failure_sends_no_http_request(http, monkeypatch):
    base, reply, requests, coord = http
    coord.reserve.side_effect = lco.LcoError("coordinator unavailable", status=503)
    with pytest.raises(lco.LcoError):
        lco._lco_api_request(base + "/api", token="private-token")
    assert requests == []


def test_download_transport_has_limiter_and_lease_context(http, monkeypatch, tmp_path):
    base, reply, requests, coord = http
    monkeypatch.setattr(lco, "_validate_download_url", lambda url: url)
    checks = Mock()
    def reserve(*args, **kwargs):
        assert lco_rate._CHECK.get() is checks
        return 0
    coord.reserve.side_effect = reserve
    dest = tmp_path / "frame.fits"
    lco._download_to_file(base + "/storage", dest, check=checks, deadline_s=1)
    assert dest.read_bytes() == reply["body"]
    assert requests == ["/storage"]
    coord.reserve.assert_called_once_with("storage", anonymous=True)
    assert lco_rate._CHECK.get() is None


def test_scopes_separate_api_and_storage():
    assert lco_rate._scope("https://archive-api.lco.global/frames/") == "archive"
    assert lco_rate._scope("https://archive-lco-global.s3.us-west-2.amazonaws.com/x?signature=secret") == "storage"
    assert lco_rate._scope("https://observe.lco.global/api/requestgroups/") is None


def test_http_date_wait_uses_reference_clock():
    header = format_datetime(datetime.fromtimestamp(90000, timezone.utc), usegmt=True)
    assert lco_rate.retry_after_seconds(header, 80000) == 10000


def test_policy_value_added_by_a_newer_release_is_adopted():
    assert lco_rate._strictest({"api_daily": 4000}, {"api_daily": 4000, "storage_interval": 0.25}) == {
        "api_daily": 4000, "storage_interval": 0.25}
