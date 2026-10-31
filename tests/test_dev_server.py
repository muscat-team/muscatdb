"""Functional tests for deploy/dev-server.sh's production guard and uv lookup.

Runs the real script with stub `tmux`/`uv` binaries on PATH. The refusal cases
must die before anything is written or launched, so each one also asserts that
no `.env` was generated and tmux was never called. The positive control lets
the script get as far as the /healthz wait (cut short with a 1s timeout) so a
guard that refuses everything cannot pass.
"""

import os
import socket
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "deploy" / "dev-server.sh"


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _stub(path: Path, log: Path) -> None:
    path.write_text(f'#!/usr/bin/env bash\necho "$0 $@" >> "{log}"\n')
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _Host:
    """A fake host: dev checkout, production dirs, staging dir, one worktree."""

    def __init__(self, root: Path):
        self.home = root / "home"
        self.dev_checkout = self.home / "github" / "muscat-db"
        self.prod_root = self.home / "deploy" / "main"
        self.prod_obslog = root / "ut2" / "muscat" / "obslog"
        self.prod_output = self.home / "ql"
        self.staging = self.home / "deploy" / "test"
        self.worktree = self.home / "temp" / "wt-feature"
        self.bin = root / "bin"
        self.tmux_log = root / "tmux.log"
        self.uv_log = root / "uv.log"

        for d in (self.prod_root, self.prod_obslog, self.prod_output, self.staging, self.bin):
            d.mkdir(parents=True)
        (self.staging / "muscat_test.db").write_text("")
        (self.staging / "app").mkdir()
        (self.staging / "app" / ".env").write_text("MUSCAT_REQUIRE_AUTH=1\n")

        self.dev_checkout.mkdir(parents=True)
        _git("init", "-q", cwd=self.dev_checkout)
        self.worktree.mkdir(parents=True)
        _git("init", "-q", cwd=self.worktree)
        (self.worktree / ".gitignore").write_text(".env\n")
        (self.worktree / "src" / "muscat_db").mkdir(parents=True)
        (self.worktree / "src" / "muscat_db" / "__init__.py").write_text("")

        _stub(self.bin / "tmux", self.tmux_log)
        _stub(self.bin / "uv", self.uv_log)

    def run(self, **overrides):
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "DEV_CHECKOUT": str(self.dev_checkout),
            "PROD_ROOT": str(self.prod_root),
            "PROD_OBSLOG_DIR": str(self.prod_obslog),
            "PROD_OUTPUT_ROOT": str(self.prod_output),
            "STAGING_ROOT": str(self.staging),
            "EXT_TOOLS": str(self.home / "ext_tools"),
            "DEV_HEALTH_TIMEOUT_S": "1",
        }
        env.update({k: v for k, v in overrides.items() if v is not None})
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
        return subprocess.run(
            ["bash", str(SCRIPT), "start", "--port", str(_free_port()), str(self.worktree)],
            env=env, capture_output=True, text=True, timeout=60,
        )

    def assert_untouched(self):
        assert not (self.worktree / ".env").exists()
        assert not self.tmux_log.exists()


@pytest.fixture
def host(tmp_path):
    return _Host(tmp_path)


def test_staging_paths_pass_the_guard(host):
    """Positive control: a normal staging root gets past the guard, writes the
    resolved staging paths into .env and reaches the /healthz wait."""
    result = host.run()
    assert "no /healthz" in result.stderr, result.stderr
    env_text = (host.worktree / ".env").read_text()
    assert f"MUSCAT_DB_PATH={host.staging}/muscat_test.db" in env_text
    assert f"MUSCAT_OBSLOG_DIR={host.staging}/obslog" in env_text
    assert host.tmux_log.exists()


def test_obslog_symlinked_to_production_is_refused(host):
    """The reported incident: a staging obslog that resolves to production's."""
    (host.staging / "obslog").symlink_to(host.prod_obslog)
    result = host.run()
    assert result.returncode != 0
    assert "MUSCAT_OBSLOG_DIR" in result.stderr
    assert "inside production" in result.stderr
    host.assert_untouched()


def test_obslog_path_equal_to_production_is_refused(host):
    """STAGING_ROOT whose <root>/obslog *is* the production obslog."""
    staging = host.prod_obslog.parent  # /ut2/muscat, so <root>/obslog == prod
    (staging / "muscat_test.db").write_text("")
    (staging / "app").mkdir(exist_ok=True)
    (staging / "app" / ".env").write_text("")
    result = host.run(STAGING_ROOT=str(staging))
    assert result.returncode != 0
    assert "MUSCAT_OBSLOG_DIR" in result.stderr
    host.assert_untouched()


def test_db_under_dev_checkout_is_refused(host):
    staging = host.dev_checkout / "stage"
    (staging / "app").mkdir(parents=True)
    (staging / "app" / ".env").write_text("")
    (staging / "muscat_test.db").write_text("")
    result = host.run(STAGING_ROOT=str(staging))
    assert result.returncode != 0
    assert "MUSCAT_DB_PATH" in result.stderr
    host.assert_untouched()


def test_db_symlinked_to_production_db_is_refused(host):
    prod_db = host.dev_checkout / "muscat.db"
    prod_db.write_text("")
    (host.staging / "muscat_test.db").unlink()
    (host.staging / "muscat_test.db").symlink_to(prod_db)
    result = host.run()
    assert result.returncode != 0
    assert "MUSCAT_DB_PATH" in result.stderr
    host.assert_untouched()


def test_staging_root_inside_production_checkout_is_refused(host):
    staging = host.prod_root / "test"
    (staging / "app").mkdir(parents=True)
    (staging / "app" / ".env").write_text("")
    (staging / "muscat_test.db").write_text("")
    result = host.run(STAGING_ROOT=str(staging))
    assert result.returncode != 0
    assert "inside production" in result.stderr
    host.assert_untouched()


def test_sibling_with_shared_prefix_is_not_mistaken_for_production(host):
    """`/x/obslog2` is not inside `/x/obslog`: the check is path-component
    aware, not a string prefix."""
    staging = host.prod_obslog.parent / "obslog2"
    (staging / "app").mkdir(parents=True)
    (staging / "app" / ".env").write_text("")
    (staging / "muscat_test.db").write_text("")
    result = host.run(STAGING_ROOT=str(staging))
    assert "inside production" not in result.stderr, result.stderr


def test_uv_is_found_on_path_not_a_hardcoded_home(host):
    """With UV_BIN unset the stub `uv` on PATH is what the tmux launch uses."""
    host.run(UV_BIN=None)
    launched = host.tmux_log.read_text()
    assert f"{host.bin}/uv run muscat-db serve" in launched
    assert "/ut2/jerome" not in launched


def test_unusable_uv_bin_is_refused_before_any_write(host):
    result = host.run(UV_BIN=str(host.home / "nope" / "uv"))
    assert result.returncode != 0
    assert "uv not found" in result.stderr
    host.assert_untouched()
