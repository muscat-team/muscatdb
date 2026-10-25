"""Functional tests for deploy/cloudflared/tunnel-check.sh (issue #50).

The tunnel is the only way external collaborators reach the app, so a down
tunnel locks them out without anything on ut2 failing. The check runs from
cron: it reads cloudflared's local readiness endpoint and, optionally, makes
sure the public hostname still redirects to Cloudflare Access. It alerts Slack
when the state changes (down, a different problem, recovered), never on every
run. Exercised against stub HTTP servers, not the script text.
"""

from __future__ import annotations

import contextlib
import http.server
import socket
import subprocess
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "cloudflared" / "tunnel-check.sh"


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@contextlib.contextmanager
def _server(handler_cls):
    port = _free_port()
    server = http.server.HTTPServer(("127.0.0.1", port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        thread.join(timeout=5)


class _Quiet(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass


@pytest.fixture
def slack():
    """A webhook stand-in that records every posted message."""
    received: list[bytes] = []

    class Handler(_Quiet):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            self.send_response(200)
            self.end_headers()

    with _server(Handler) as port:
        yield port, received


@pytest.fixture
def origin():
    """cloudflared's /ready and the public hostname, each answering whatever
    the test sets in ``state``."""
    state = {"ready": 200, "public": 302, "location": "https://team.cloudflareaccess.com/cdn-cgi/access/login"}

    class Handler(_Quiet):
        def do_GET(self):
            if self.path == "/ready":
                self.send_response(state["ready"])
                self.end_headers()
                return
            self.send_response(state["public"])
            if state["public"] in (301, 302, 303, 307):
                self.send_header("Location", state["location"])
            self.end_headers()

    with _server(Handler) as port:
        yield port, state


def _run(tmp_path, slack_port, metrics_url, public_url=None):
    webhook = tmp_path / "slack-webhook-url"
    webhook.write_text(f"http://127.0.0.1:{slack_port}/hook")
    env = {
        "PATH": "/usr/bin:/bin",
        "SLACK_WEBHOOK_FILE": str(webhook),
        "TUNNEL_METRICS_URL": metrics_url,
        "TUNNEL_CHECK_STATE": str(tmp_path / "tunnel-check.state"),
    }
    if public_url:
        env["TUNNEL_PUBLIC_URL"] = public_url
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)


def test_a_ready_tunnel_is_quiet(tmp_path, slack, origin):
    slack_port, received = slack
    port, _state = origin
    result = _run(tmp_path, slack_port, f"http://127.0.0.1:{port}/ready")
    assert result.returncode == 0, result.stderr
    assert received == []


def test_a_down_tunnel_alerts_once_then_reports_recovery(tmp_path, slack, origin):
    slack_port, received = slack
    port, state = origin
    url = f"http://127.0.0.1:{port}/ready"

    state["ready"] = 503
    first = _run(tmp_path, slack_port, url)
    second = _run(tmp_path, slack_port, url)
    assert first.returncode == 1 and second.returncode == 1
    assert len(received) == 1 and b"tunnel" in received[0].lower()

    state["ready"] = 200
    assert _run(tmp_path, slack_port, url).returncode == 0
    assert len(received) == 2 and b"recovered" in received[1].lower()


def test_cloudflared_not_listening_counts_as_down(tmp_path, slack):
    slack_port, received = slack
    result = _run(tmp_path, slack_port, f"http://127.0.0.1:{_free_port()}/ready")
    assert result.returncode == 1
    assert len(received) == 1


def test_the_public_hostname_must_redirect_to_access(tmp_path, slack, origin):
    slack_port, received = slack
    port, state = origin
    metrics = f"http://127.0.0.1:{port}/ready"
    public = f"http://127.0.0.1:{port}/"

    assert _run(tmp_path, slack_port, metrics, public).returncode == 0
    assert received == []

    state["public"] = 200  # served without the Access login in front
    result = _run(tmp_path, slack_port, metrics, public)
    assert result.returncode == 1
    assert len(received) == 1 and b"access" in received[0].lower()


def test_a_redirect_elsewhere_is_not_access(tmp_path, slack, origin):
    slack_port, received = slack
    port, state = origin
    state["location"] = "https://evil.example.org/cloudflareaccess.com/"
    result = _run(tmp_path, slack_port, f"http://127.0.0.1:{port}/ready", f"http://127.0.0.1:{port}/")
    assert result.returncode == 1
    assert len(received) == 1


def test_a_different_problem_alerts_again(tmp_path, slack, origin):
    slack_port, received = slack
    port, state = origin
    metrics = f"http://127.0.0.1:{port}/ready"
    public = f"http://127.0.0.1:{port}/"

    state["ready"] = 503
    _run(tmp_path, slack_port, metrics, public)
    state["ready"], state["public"] = 200, 200
    _run(tmp_path, slack_port, metrics, public)
    assert len(received) == 2
