"""Cloudflare Access identity mapping (issue #50).

nginx copies the email of a validated ``Cf-Access-Jwt-Assertion`` into
``X-Forwarded-User``. The app still trusts that header only from the loopback
proxy with the shared secret, and then maps an email to the stable username on
``users.email``. An email with no row is refused (403), never provisioned and
never treated as anonymous.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
import socketio
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from muscat_db import auth, chat, web
from muscat_db.cli import app as cli_app
from muscat_db.database import _apply_schema

SECRET = "proxy-secret"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "muscat.db")
    conn = sqlite3.connect(path)
    _apply_schema(conn)
    conn.execute(
        "INSERT INTO users (username, display_name, scoped, email) VALUES "
        "('andrew', 'andrew', 1, 'Andrew.Example@Univ.EDU')"
    )
    conn.execute("INSERT INTO users (username, display_name) VALUES ('jerome', 'jerome')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("MUSCAT_DB_PATH", path)
    monkeypatch.setenv("MUSCAT_PROXY_SECRET", SECRET)
    monkeypatch.delenv("MUSCAT_REQUIRE_AUTH", raising=False)
    return path


def _resolve(user, host="127.0.0.1", secret=SECRET):
    return auth.resolve_forwarded_identity(user, host, secret)


def test_email_maps_to_the_stable_username_case_insensitively(db):
    assert _resolve("andrew.example@univ.edu") == ("andrew", auth.OK)
    assert _resolve("ANDREW.EXAMPLE@UNIV.EDU") == ("andrew", auth.OK)


def test_unknown_email_is_not_anonymous_and_not_provisioned(db):
    assert _resolve("stranger@univ.edu") == (None, auth.UNKNOWN_EMAIL)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2


def test_plain_username_passes_through_unchanged(db):
    assert _resolve("jerome") == ("jerome", auth.OK)
    assert _resolve(None) == (None, auth.ANONYMOUS)


def test_email_from_an_untrusted_peer_or_without_the_secret_is_never_mapped(db):
    assert _resolve("andrew.example@univ.edu", host="10.0.0.5") == (None, auth.UNTRUSTED)
    assert _resolve("andrew.example@univ.edu", secret="wrong") == (None, auth.UNTRUSTED)


def test_unmappable_when_the_users_table_has_no_email_column(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    sqlite3.connect(path).executescript("CREATE TABLE users (username TEXT PRIMARY KEY);")
    monkeypatch.setenv("MUSCAT_DB_PATH", path)
    monkeypatch.setenv("MUSCAT_PROXY_SECRET", SECRET)
    assert _resolve("andrew.example@univ.edu") == (None, auth.UNKNOWN_EMAIL)


def test_unique_index_blocks_two_users_sharing_an_email(db):
    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO users (username, email) VALUES ('dup', 'ANDREW.example@univ.edu')"
        )


def _client():
    return TestClient(web.app, client=("127.0.0.1", 12345))


def _headers(user):
    return {"X-Forwarded-User": user, "X-MuSCAT-Proxy-Secret": SECRET}


def test_whoami_returns_the_username_for_a_mapped_email(db):
    r = _client().get("/whoami", headers=_headers("andrew.example@univ.edu"))
    assert r.status_code == 200
    assert r.json() == {"user": "andrew"}


def test_unknown_email_gets_403_but_healthz_stays_open(db):
    c = _client()
    r = c.get("/whoami", headers=_headers("stranger@univ.edu"))
    assert r.status_code == 403
    assert "no MuSCAT-DB account" in r.json()["error"]
    assert c.get("/healthz", headers=_headers("stranger@univ.edu")).status_code == 200


def test_unknown_email_is_403_even_when_auth_is_not_required(db, monkeypatch):
    monkeypatch.delenv("MUSCAT_REQUIRE_AUTH", raising=False)
    r = _client().get("/whoami", headers=_headers("stranger@univ.edu"))
    assert r.status_code == 403


def test_chat_refuses_an_unmapped_identity_and_maps_a_known_one(db, monkeypatch):
    def env(email):
        return {
            "HTTP_X_FORWARDED_USER": email,
            "HTTP_X_MUSCAT_PROXY_SECRET": SECRET,
            "asgi.scope": {"client": ("127.0.0.1", 5)},
        }

    assert chat._extract_identity(env("andrew.example@univ.edu")) == "andrew"
    with pytest.raises(socketio.exceptions.ConnectionRefusedError, match="account not provisioned"):
        asyncio.run(chat.connect("sid-x", env("stranger@univ.edu")))


# ── CLI ─────────────────────────────────────────────────────────────────


def _emails(db):
    with sqlite3.connect(db) as conn:
        return dict(conn.execute("SELECT username, email FROM users"))


def test_set_email_creates_the_row_and_normalizes(db):
    r = CliRunner().invoke(cli_app, ["htpasswd", "set-email", "newuser", " New.User@Univ.edu "])
    assert r.exit_code == 0, r.output
    assert _emails(db)["newuser"] == "new.user@univ.edu"
    assert auth.username_for_email("NEW.USER@univ.edu") == "newuser"


def test_set_email_refuses_a_taken_or_malformed_email_and_clear_works(db):
    run = lambda *a: CliRunner().invoke(cli_app, ["htpasswd", "set-email", *a])  # noqa: E731
    assert run("jerome", "andrew.example@univ.edu").exit_code == 1
    assert run("jerome", "not-an-email").exit_code == 1
    assert run("bad@name", "x@y.org").exit_code == 1
    assert run("andrew", "x@y.org", "--clear").exit_code == 0
    assert _emails(db)["andrew"] is None


def test_htpasswd_add_refuses_usernames_with_an_at_sign(db, tmp_path, monkeypatch):
    monkeypatch.setenv("MUSCAT_HTPASSWD_FILE", str(tmp_path / "htpasswd"))
    r = CliRunner().invoke(cli_app, ["htpasswd", "add", "bob@univ.edu", "--password", "pw"])
    assert r.exit_code == 1
    assert "@" in r.output
