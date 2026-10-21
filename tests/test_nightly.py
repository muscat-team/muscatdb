"""Nightly failure alerting (issue #237).

`deploy/nightly.sh` is exercised as a real script in a scratch tree with stub
`uv`/`curl`, because the behaviour that matters is that a refused build reaches
the webhook with the step name and exits non-zero, not what the script text
says. `build-status` and `build_status` are tested directly.
"""

import datetime
import os
import shutil
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from muscat_db import build_status
from muscat_db.cli import app

REPO_ROOT = Path(__file__).resolve().parents[1]


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def tree(tmp_path):
    """A scratch checkout with the real deploy scripts and stub tools."""
    root = tmp_path / "root"
    (root / "deploy").mkdir(parents=True)
    (root / "scripts").mkdir()
    for name in ("nightly.sh", "notify-slack.sh"):
        shutil.copy(REPO_ROOT / "deploy" / name, root / "deploy" / name)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    posts = tmp_path / "posts.log"
    _stub(root / "scripts" / "download_catalogs.sh", f'echo catalogs >> "{calls}"\n')
    # `uv run muscat-db <cmd> ...`: fail the command named in $FAIL_CMD.
    _stub(
        bin_dir / "uv",
        f'echo "$3" >> "{calls}"\n'
        '[ "$3" = "build-db" ] && [ -n "${LEAK:-}" ] && echo "WARNING INTEGRITY-LEAK: muscat.db"\n'
        f'[ "$3" = "backup-control-plane" ] && echo "dsn=${{MUSCAT_POSTGRES_DSN:-}}" >> "{calls}"\n'
        '[ "$3" = "${FAIL_CMD:-}" ] && { echo "boom from $3" ; exit "${FAIL_CODE:-3}"; }\n'
        'exit 0\n',
    )
    _stub(bin_dir / "curl", f'echo "$@" >> "{posts}"\n')
    webhook = tmp_path / "webhook"
    webhook.write_text("https://hooks.example.invalid/x\n")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "UV_BIN": str(bin_dir / "uv"),
        "SLACK_WEBHOOK_FILE": str(webhook),
    }
    return {"root": root, "env": env, "calls": calls, "posts": posts, "webhook": webhook}


def _run(tree, mode, **env):
    return subprocess.run(
        ["bash", str(tree["root"] / "deploy" / "nightly.sh"), mode],
        env={**tree["env"], **env}, capture_output=True, text=True,
    )


def _read(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def test_successful_run_executes_every_step_and_posts_nothing(tree):
    result = _run(tree, "run")

    assert result.returncode == 0
    assert _read(tree["calls"]).split() == ["catalogs", "scan-yesterday", "build-db"]
    assert _read(tree["posts"]) == ""


def test_refused_build_alerts_with_step_name_and_log_tail_and_exits_nonzero(tree):
    result = _run(tree, "run", FAIL_CMD="build-db", FAIL_CODE="7")

    assert result.returncode == 7
    posted = _read(tree["posts"])
    assert "hooks.example.invalid" in posted
    assert "build-db failed (exit 7)" in posted
    assert "boom from build-db" in posted


def test_leaked_pages_snapshot_alerts_even_though_the_build_succeeds(tree):
    result = _run(tree, "run", LEAK="1")

    assert result.returncode == 0
    posted = _read(tree["posts"])
    assert "rows may be lost" in posted
    assert "INTEGRITY-LEAK" in posted


def test_a_leak_logged_by_an_earlier_run_does_not_alert_again(tree):
    assert _run(tree, "run", LEAK="1").returncode == 0
    tree["posts"].unlink()

    result = _run(tree, "run")

    assert result.returncode == 0
    assert _read(tree["posts"]) == ""


def test_failed_scan_stops_the_chain_before_build_db(tree):
    result = _run(tree, "run", FAIL_CMD="scan-yesterday")

    assert result.returncode == 3
    assert "build-db" not in _read(tree["calls"]).split()
    assert "scan-yesterday failed" in _read(tree["posts"])


def test_missing_webhook_file_still_exits_nonzero_and_reports_on_stderr(tree):
    tree["webhook"].unlink()

    result = _run(tree, "run", FAIL_CMD="build-db")

    assert result.returncode == 3
    assert "build-db failed" in result.stderr
    assert _read(tree["posts"]) == ""


def test_message_with_quotes_is_posted_as_valid_json(tree):
    _stub(tree["root"] / "scripts" / "download_catalogs.sh", 'echo \'say "hi" \\ there\'; exit 1\n')

    result = _run(tree, "run")

    assert result.returncode == 1
    assert 'say \\"hi\\"' in _read(tree["posts"])


def _write_web_env(tree) -> None:
    (tree["root"] / ".web-env").write_text(
        "export MUSCAT_POSTGRES_DSN=postgresql://u@localhost:5432/muscatdb_prod\n"
    )


def test_control_plane_is_backed_up_last_with_the_web_env_dsn(tree):
    _write_web_env(tree)

    result = _run(tree, "run")

    assert result.returncode == 0
    assert _read(tree["calls"]).split() == [
        "catalogs", "scan-yesterday", "build-db", "backup-control-plane",
        "dsn=postgresql://u@localhost:5432/muscatdb_prod",
    ]
    assert _read(tree["posts"]) == ""


def test_no_web_env_means_no_control_plane_backup(tree):
    result = _run(tree, "run")

    assert result.returncode == 0
    assert "backup-control-plane" not in _read(tree["calls"])


def test_failed_control_plane_backup_alerts_after_the_build(tree):
    _write_web_env(tree)

    result = _run(tree, "run", FAIL_CMD="backup-control-plane", FAIL_CODE="1")

    assert result.returncode == 1
    assert "build-db" in _read(tree["calls"]).split()
    assert "backup-control-plane failed (exit 1)" in _read(tree["posts"])


def test_check_mode_alerts_when_build_status_reports_stale(tree):
    result = _run(tree, "check", FAIL_CMD="build-status", FAIL_CODE="1")

    assert result.returncode == 1
    assert "stale" in _read(tree["posts"])


def test_check_mode_is_quiet_when_fresh(tree):
    result = _run(tree, "check")

    assert result.returncode == 0
    assert _read(tree["posts"]) == ""


def _db_with_build(path: Path, stamp: datetime.datetime | None) -> str:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE db_meta (key TEXT PRIMARY KEY, value TEXT)")
    if stamp is not None:
        conn.execute(
            "INSERT INTO db_meta VALUES ('last_build_at', ?)", (stamp.isoformat(),)
        )
    conn.commit()
    conn.close()
    return str(path)


def test_build_age_reads_the_stamp(tmp_path):
    now = datetime.datetime(2026, 10, 14, 12, 0)
    db = _db_with_build(tmp_path / "m.db", now - datetime.timedelta(hours=5))

    assert build_status.build_age(db, now=now) == datetime.timedelta(hours=5)


@pytest.mark.parametrize("missing", ["file", "key", "table"])
def test_build_age_is_none_when_undeterminable(tmp_path, missing):
    path = tmp_path / "m.db"
    if missing == "key":
        _db_with_build(path, None)
    elif missing == "table":
        sqlite3.connect(path).close()

    assert build_status.build_age(path) is None
    # A read must never create a database as a side effect.
    assert path.exists() == (missing != "file")


def test_build_status_command_exit_codes(tmp_path):
    runner = CliRunner()
    fresh = _db_with_build(tmp_path / "fresh.db", datetime.datetime.now())
    stale = _db_with_build(
        tmp_path / "stale.db", datetime.datetime.now() - datetime.timedelta(days=3)
    )
    unknown = _db_with_build(tmp_path / "unknown.db", None)

    assert runner.invoke(app, ["build-status", "--db", fresh]).exit_code == 0
    stale_result = runner.invoke(app, ["build-status", "--db", stale])
    assert stale_result.exit_code == 1
    assert "last built" in stale_result.output
    assert runner.invoke(app, ["build-status", "--db", unknown]).exit_code == 1
    assert runner.invoke(
        app, ["build-status", "--db", stale, "--max-age-hours", "100"]
    ).exit_code == 0
