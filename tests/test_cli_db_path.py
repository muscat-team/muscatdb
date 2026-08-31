"""The CLI ``--db`` option defaults from ``MUSCAT_DB_PATH``.

Commands like ``build-db`` used to hard-code ``muscat.db`` relative to cwd, so
they only found the real database when cwd happened to be a tree holding one.
With the dedicated deploy checkouts (issue #26) the configured DB lives
elsewhere, so the CLI must honour the same env the web server / ``db_path()``
read (the checkout's ``.env``), not a fragile cwd-relative default.
"""

from __future__ import annotations

import os
import subprocess
import sys

from muscat_db import cli


def test_db_option_default_from_env(monkeypatch):
    monkeypatch.setenv("MUSCAT_DB_PATH", "/srv/muscat-db/muscat.db")
    assert cli._db_option().default == "/srv/muscat-db/muscat.db"


def test_db_option_defaults_to_muscat_db_when_unset(monkeypatch):
    monkeypatch.delenv("MUSCAT_DB_PATH", raising=False)
    assert cli._db_option().default == "muscat.db"


def _build_db_help_output(tmp_path, muscat_db_path: str | None) -> str:
    """Run ``build-db --help`` in a fresh subprocess and return its output.

    ``db: str = _db_option()`` is a Typer default *argument*, which Python
    binds once when ``muscat_db.cli`` is first imported. Calling
    ``cli._db_option()`` directly in-process (as the tests above do)
    re-evaluates it fresh against the current env and would keep passing even
    if the fix were never wired into a command's signature. Only a clean
    process -- with no ``.env`` reachable from ``cwd`` -- exercises the
    default that ``build-db --help`` actually reports.
    """
    env = dict(os.environ)
    if muscat_db_path is None:
        env.pop("MUSCAT_DB_PATH", None)
    else:
        env["MUSCAT_DB_PATH"] = muscat_db_path
    result = subprocess.run(
        [sys.executable, "-m", "muscat_db.cli", "build-db", "--help"],
        capture_output=True, text=True, env=env, cwd=tmp_path,
    )
    return result.stdout + result.stderr


def _default_line(out: str) -> str:
    """Collapse Rich's wrapped help output onto one line for substring checks."""
    return " ".join(out.split())


def test_build_db_help_default_follows_muscat_db_path(tmp_path):
    # The static help text ("...or muscat.db)") always contains "muscat.db",
    # so assert on the rendered [default: ...] line, not a bare substring --
    # otherwise this passes no matter what the actual bound default is.
    out = _default_line(_build_db_help_output(tmp_path, "/srv/configured/muscat.db"))
    assert "[default: /srv/configured/muscat.db]" in out


def test_build_db_help_default_falls_back_to_muscat_db(tmp_path):
    out = _default_line(_build_db_help_output(tmp_path, None))
    assert "[default: muscat.db]" in out
