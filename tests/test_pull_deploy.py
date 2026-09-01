"""Functional tests for deploy/pull-deploy.sh (issue #131).

Exercises the actual script against a local bare git repo standing in for
`origin`, with stub `uv`/`tmux` binaries on PATH -- not string assertions
against the script text, since the thing worth verifying is behavior: the
no-op path, the deploy-and-relaunch path, that the mid-script re-exec really
reads the post-reset script (not stale bytes), and that a failure reaches
the configured Slack webhook and still exits non-zero.
"""

import http.server
import os
import socket
import stat
import subprocess
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "deploy" / "pull-deploy.sh"


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _write_stub(path: Path, marker: Path, body: str = "") -> None:
    path.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{marker}"\n{body}')
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def deploy_env(tmp_path):
    """Bare 'origin' repo, a 'seed' clone to push new commits from, and a
    'checkout' clone with pull-deploy.sh installed under deploy/ -- mirroring
    the real layout where the script lives inside the checkout it deploys.
    """
    origin = tmp_path / "origin.git"
    _git("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)

    seed = tmp_path / "seed"
    _git("clone", str(origin), str(seed), cwd=tmp_path)
    _git("config", "user.email", "t@example.com", cwd=seed)
    _git("config", "user.name", "t", cwd=seed)
    (seed / "app.txt").write_text("v1\n")
    _git("add", "app.txt", cwd=seed)
    _git("commit", "-m", "v1", cwd=seed)
    _git("push", "origin", "main", cwd=seed)

    checkout = tmp_path / "checkout"
    _git("clone", str(origin), str(checkout), cwd=tmp_path)
    _git("config", "user.email", "t@example.com", cwd=checkout)
    _git("config", "user.name", "t", cwd=checkout)

    (checkout / "deploy").mkdir()
    script_dst = checkout / "deploy" / "pull-deploy.sh"
    script_dst.write_text(SCRIPT.read_text())
    script_dst.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv_marker = tmp_path / "uv-calls.log"
    tmux_marker = tmp_path / "tmux-calls.log"
    _write_stub(bin_dir / "uv", uv_marker)
    # send-keys always fails (no session exists yet in this test environment,
    # same as the real first deploy to a fresh checkout), forcing every call
    # through the new-session fallback, which succeeds.
    _write_stub(
        bin_dir / "tmux", tmux_marker,
        body='if [ "$1" = "send-keys" ]; then exit 1; fi\n',
    )

    return {
        "origin": origin,
        "seed": seed,
        "checkout": checkout,
        "bin_dir": bin_dir,
        "uv_marker": uv_marker,
        "tmux_marker": tmux_marker,
    }


def _run(env, branch="main", session="test-session", port="9999", env_extra=None):
    full_env = dict(os.environ)
    full_env["PATH"] = f"{env['bin_dir']}:{full_env['PATH']}"
    full_env["UV_BIN"] = str(env["bin_dir"] / "uv")
    if env_extra:
        full_env.update(env_extra)
    return subprocess.run(
        ["bash", str(env["checkout"] / "deploy" / "pull-deploy.sh"), branch, session, port],
        cwd=env["checkout"],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_noop_when_local_matches_remote(deploy_env):
    result = _run(deploy_env)

    assert result.returncode == 0
    assert not deploy_env["uv_marker"].exists()
    assert not deploy_env["tmux_marker"].exists()


def test_deploys_and_relaunches_on_new_remote_commit(deploy_env):
    (deploy_env["seed"] / "app.txt").write_text("v2\n")
    _git("add", "app.txt", cwd=deploy_env["seed"])
    _git("commit", "-m", "v2", cwd=deploy_env["seed"])
    _git("push", "origin", "main", cwd=deploy_env["seed"])

    result = _run(deploy_env, session="test-session", port="9999")

    assert result.returncode == 0, result.stderr
    assert (deploy_env["checkout"] / "app.txt").read_text() == "v2\n"
    assert deploy_env["uv_marker"].exists()
    assert "sync --dev" in deploy_env["uv_marker"].read_text()
    tmux_calls = deploy_env["tmux_marker"].read_text()
    assert "test-session" in tmux_calls
    assert "9999" in tmux_calls
    # Both the failed send-keys attempt and the new-session fallback ran.
    assert tmux_calls.count("test-session") >= 2


def test_reexec_reads_the_just_updated_script_not_stale_bytes(deploy_env):
    """The mid-script re-exec must run the post-reset file.

    If a stale (pre-reset) copy of the script kept running instead, this
    marker -- only present in the commit pushed below -- would never appear.
    """
    updated = SCRIPT.read_text().replace(
        "deployed $BRANCH at",
        "REEXEC_MARKER deployed $BRANCH at",
    )
    assert "REEXEC_MARKER" in updated
    (deploy_env["seed"] / "deploy").mkdir()
    updated_script = deploy_env["seed"] / "deploy" / "pull-deploy.sh"
    updated_script.write_text(updated)
    # git tracks the executable bit as of `git add` time -- without this, the
    # committed mode would be non-executable and `git reset --hard` would
    # strip +x from the checkout's copy, which is the exact real-world
    # failure mode this test exists to catch (the direct execve() in `exec
    # "$0"` needs +x; `bash script.sh` doesn't, so only the re-exec path
    # would break).
    updated_script.chmod(0o755)
    _git("add", "deploy/pull-deploy.sh", cwd=deploy_env["seed"])
    _git("commit", "-m", "update script", cwd=deploy_env["seed"])
    _git("push", "origin", "main", cwd=deploy_env["seed"])

    result = _run(deploy_env)

    assert result.returncode == 0, result.stderr
    assert "REEXEC_MARKER" in result.stdout


def test_failure_reports_to_slack_webhook_and_exits_nonzero(deploy_env, tmp_path):
    (deploy_env["seed"] / "app.txt").write_text("v2\n")
    _git("add", "app.txt", cwd=deploy_env["seed"])
    _git("commit", "-m", "v2", cwd=deploy_env["seed"])
    _git("push", "origin", "main", cwd=deploy_env["seed"])

    (deploy_env["bin_dir"] / "uv").write_text("#!/usr/bin/env bash\nexit 3\n")

    received = []
    received_event = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received.append(self.rfile.read(length))
            self.send_response(200)
            self.end_headers()
            received_event.set()

        def log_message(self, *_a):
            pass

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port_num = sock.getsockname()[1]
    sock.close()
    server = http.server.HTTPServer(("127.0.0.1", port_num), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        webhook_file = tmp_path / "slack-webhook-url"
        webhook_file.write_text(f"http://127.0.0.1:{port_num}/hook")

        result = _run(deploy_env, env_extra={"SLACK_WEBHOOK_FILE": str(webhook_file)})

        assert result.returncode == 3
        assert "FAILED" in result.stderr
        assert received_event.wait(timeout=5)
        assert b"pull-deploy failed" in received[0]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_missing_webhook_file_still_fails_loudly_not_silently(deploy_env, tmp_path):
    (deploy_env["seed"] / "app.txt").write_text("v2\n")
    _git("add", "app.txt", cwd=deploy_env["seed"])
    _git("commit", "-m", "v2", cwd=deploy_env["seed"])
    _git("push", "origin", "main", cwd=deploy_env["seed"])
    (deploy_env["bin_dir"] / "uv").write_text("#!/usr/bin/env bash\nexit 3\n")

    result = _run(
        deploy_env,
        env_extra={"SLACK_WEBHOOK_FILE": str(tmp_path / "does-not-exist")},
    )

    assert result.returncode == 3
    assert "FAILED" in result.stderr
