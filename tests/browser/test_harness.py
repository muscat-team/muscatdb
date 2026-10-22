"""The harness itself: the server is isolated from the network and the host.

These guard the guarantees the other browser tests rely on. The async case
matters: uvloop resolves and connects in C, around the socket patches, so a
regression to it would let pages quietly query real archives.
"""

from __future__ import annotations

import socket

import pytest

from tests.browser.conftest import LAUNCHING_MODULES, LaunchBlocked, NetworkBlocked


def test_sync_connections_are_blocked(live_server):
    with pytest.raises((NetworkBlocked, socket.gaierror)):
        socket.create_connection(("example.org", 80), timeout=2)


def test_async_http_client_is_blocked(page, live_server):
    page.problems.allowed.append("status of 502")
    resp = page.request.get(f"{live_server.url}/api/targets/eso-archive?name=TOI1000")
    assert resp.status == 502
    assert "network disabled in browser tests" in resp.json()["error"]


def test_server_uses_the_fixture_tree(live_server):
    import os

    assert os.environ["MUSCAT_DB_PATH"] == str(live_server.db)
    assert str(live_server.root) in os.environ["MUSCAT_PROSE_DIR"]
    assert "LCO_API_TOKEN" not in os.environ


@pytest.mark.parametrize("module", LAUNCHING_MODULES)
def test_pipeline_modules_cannot_start_processes(live_server, module):
    import importlib

    mod = importlib.import_module(module)
    with pytest.raises(LaunchBlocked):
        mod.subprocess.Popen(["true"])
    with pytest.raises(LaunchBlocked):
        mod.subprocess.run(["true"])
    assert mod.subprocess.PIPE == -1  # everything else is the real module
