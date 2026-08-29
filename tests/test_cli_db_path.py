"""The CLI ``--db`` option defaults from ``MUSCAT_DB_PATH``.

Commands like ``build-db`` used to hard-code ``muscat.db`` relative to cwd, so
they only found the real database when cwd happened to be a tree holding one.
With the dedicated deploy checkouts (issue #26) the configured DB lives
elsewhere, so the CLI must honour the same env the web server / ``db_path()``
read (the checkout's ``.env``), not a fragile cwd-relative default.
"""

from __future__ import annotations

from muscat_db import cli


def test_db_option_default_from_env(monkeypatch):
    monkeypatch.setenv("MUSCAT_DB_PATH", "/srv/muscat-db/muscat.db")
    assert cli._db_option().default == "/srv/muscat-db/muscat.db"


def test_db_option_defaults_to_muscat_db_when_unset(monkeypatch):
    monkeypatch.delenv("MUSCAT_DB_PATH", raising=False)
    assert cli._db_option().default == "muscat.db"
