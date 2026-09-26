"""Per-viewer proposal access control on the live app (issue #144 PR4).

Covers ``muscat_db.access``, the ``muscat-db access`` CLI, and enforcement on
``/targets``, ``/target`` and the ``/api/targets`` endpoints. The fixture DB
has three targets:

* ``MIXEDTGT``: one open sinistro night (260101, 2 frames) and one restricted
  night (260102, 3 frames), so a denied viewer must get a re-derived rollup
  rather than the precomputed one or nothing;
* ``HIDDENTGT``: only a restricted night, so a denied viewer must not learn it
  exists;
* ``OPENTGT``: a night with no proposal at all (muscat2-style ``''``).

The restriction row is stored lower-cased while the frames carry the
upper-case header spelling, so every path is also checked for
case-insensitive matching.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from muscat_db import access, web
from muscat_db.cli import app as cli_app
from muscat_db.coord import CoordRepr
from muscat_db.database import (
    SCHEMA,
    _insert_summary_rows,
    _populate_targets,
    _summary_rows,
    get_targets,
)

pytestmark = pytest.mark.usefixtures("mock_target_coord_resolution")

_RESTRICTED = "ZZZTEST-RESTRICTED-9001"
_OPEN = "ZZZTEST-OPEN-9002"
_PROXY_SECRET = "proxy-secret"


def _headers(user: str) -> dict:
    return {"X-Forwarded-User": user, "X-MuSCAT-Proxy-Secret": _PROXY_SECRET}


def _insert_frames(conn, rows):
    conn.executemany(
        """INSERT INTO frames
           (instrument, obsdate, ccd, filename, object, jd_start, ut_start,
            exptime, read_mode, filter, ra, declination, airmass, focus, pa,
            proposal_id)
           VALUES (?, ?, 0, ?, ?, ?, '00:00:00', 10, 'fast', 'gp', '', '', 1, 0, 0, ?)""",
        rows,
    )


def _build_db(db_path: str, *, restrict: bool = True) -> None:
    conn = sqlite3.connect(db_path)
    conn.create_aggregate("coord_repr", 2, CoordRepr)
    conn.executescript(SCHEMA)
    _insert_frames(conn, [
        ("sinistro", "260101", "SIN_2601010001", "MIXEDTGT", 1.0, _OPEN),
        ("sinistro", "260101", "SIN_2601010002", "MIXEDTGT", 2.0, _OPEN),
        ("sinistro", "260102", "SIN_2601020001", "MIXEDTGT", 3.0, _RESTRICTED),
        ("sinistro", "260102", "SIN_2601020002", "MIXEDTGT", 4.0, _RESTRICTED),
        ("sinistro", "260102", "SIN_2601020003", "MIXEDTGT", 5.0, _RESTRICTED),
        ("muscat3", "260201", "MSCT3_2602010001", "HIDDENTGT", 6.0, _RESTRICTED),
        ("muscat3", "260202", "MSCT3_2602020001", "OPENTGT", 7.0, ""),
    ])
    _insert_summary_rows(conn, _summary_rows(conn))
    _populate_targets(conn)
    if restrict:
        conn.execute(
            "INSERT INTO restricted_proposals (proposal_id) VALUES (?)",
            (_RESTRICTED.lower(),),
        )
    conn.execute(
        "INSERT OR REPLACE INTO db_meta (key, value) VALUES ('last_build_at', '1700000000')"
    )
    conn.commit()
    conn.close()


def _set_admin(db: str, user: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (username, display_name) VALUES (?, ?)", (user, user)
        )
        conn.execute("UPDATE users SET is_admin = 1 WHERE username = ?", (user,))


@pytest.fixture(autouse=True)
def _isolate_dirs(tmp_path, monkeypatch):
    """Keep photometry/fit status lookups and backfill checkpoints off the
    host's real directories."""
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path / "no-prose-output"))
    monkeypatch.setenv("MUSCAT_TIMER_DIR", str(tmp_path / "no-timer-output"))
    monkeypatch.setenv("MUSCAT_TMPDIR", str(tmp_path / "muscat-tmp"))


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "muscat.db"
    _build_db(str(path))
    monkeypatch.setenv("MUSCAT_DB_PATH", str(path))
    return str(path)


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("MUSCAT_PROXY_SECRET", _PROXY_SECRET)
    web._index_cache.clear()
    return TestClient(web.app, client=("127.0.0.1", 12345))


def _export_rows(client, user: str) -> dict[str, dict]:
    resp = client.get("/api/targets/export.csv", headers=_headers(user))
    assert resp.status_code == 200
    import csv
    import io

    return {r["object"]: r for r in csv.DictReader(io.StringIO(resp.text))}


def _n_observations(html: str) -> int:
    m = re.search(r"Photometric Observations \((\d+)\)", html)
    assert m, "target page lost its observation count heading"
    return int(m.group(1))


# ── denied_proposal_ids_for ─────────────────────────────────────────────


def test_denied_set_empty_when_nothing_restricted(tmp_path):
    path = str(tmp_path / "open.db")
    _build_db(path, restrict=False)
    assert access.denied_proposal_ids_for(path, None) == frozenset()
    assert access.denied_proposal_ids_for(path, "alice") == frozenset()


def test_anonymous_viewer_is_denied_every_restriction(db):
    assert access.denied_proposal_ids_for(db, None) == {_RESTRICTED}


def test_user_without_grant_is_denied(db):
    assert access.denied_proposal_ids_for(db, "alice") == {_RESTRICTED}


def test_grant_lifts_denial_case_insensitively(db):
    access.grant(db, "alice", _RESTRICTED.upper(), "admin")
    assert access.denied_proposal_ids_for(db, "alice") == frozenset()
    assert access.denied_proposal_ids_for(db, "bob") == {_RESTRICTED}


def test_admin_is_denied_nothing(db):
    _set_admin(db, "root")
    assert access.denied_proposal_ids_for(db, "root") == frozenset()


def test_database_without_access_tables_denies_nothing(tmp_path):
    path = str(tmp_path / "old.db")
    sqlite3.connect(path).close()
    assert access.denied_proposal_ids_for(path, None) == frozenset()


# ── empty-denied-set fast path ──────────────────────────────────────────


def test_unrestricted_viewer_keeps_precomputed_rows(db, monkeypatch):
    """No denied proposals -> the precomputed list comes back untouched and
    the live re-aggregation is never run."""
    base = get_targets(db)

    def _boom(*_a, **_k):
        raise AssertionError("live re-aggregation ran for an unrestricted viewer")

    monkeypatch.setattr("muscat_db.database._target_rows", _boom)
    rows = web._visible_target_rows(db, base, frozenset())
    assert rows is base


def test_targets_page_unchanged_when_nothing_restricted(tmp_path, monkeypatch):
    path = tmp_path / "open.db"
    _build_db(str(path), restrict=False)
    monkeypatch.setenv("MUSCAT_DB_PATH", str(path))
    web._index_cache.clear()
    client = TestClient(web.app, client=("127.0.0.1", 12345))
    html = client.get("/targets").text
    assert "HIDDENTGT" in html and "MIXEDTGT" in html and "OPENTGT" in html
    assert web._index_cache.get("index") is not None


# ── /targets and export ─────────────────────────────────────────────────


def test_restricted_target_absent_from_targets_page(client):
    html = client.get("/targets", headers=_headers("alice")).text
    assert "HIDDENTGT" not in html
    assert "MIXEDTGT" in html
    assert "OPENTGT" in html


def test_anonymous_targets_page_hides_restricted(client):
    html = client.get("/targets").text
    assert "HIDDENTGT" not in html


def test_mixed_target_rollup_is_rederived_for_denied_viewer(client, db):
    _set_admin(db, "root")
    denied = _export_rows(client, "alice")
    full = _export_rows(client, "root")

    assert "HIDDENTGT" not in denied
    assert full["MIXEDTGT"]["n_frames"] == "5"
    assert denied["MIXEDTGT"]["n_frames"] == "2"
    assert full["MIXEDTGT"]["dates"] == "260101, 260102"
    assert denied["MIXEDTGT"]["dates"] == "260101"
    assert denied["OPENTGT"] == full["OPENTGT"]


def test_granted_user_and_admin_see_everything(client, db):
    access.grant(db, "carol", _RESTRICTED, "admin")
    _set_admin(db, "root")
    for user in ("carol", "root"):
        html = client.get("/targets", headers=_headers(user)).text
        assert "HIDDENTGT" in html, user


def test_rendered_page_cache_is_not_shared_across_denied_sets(client, db):
    """An admin render must never be served back to a denied viewer."""
    _set_admin(db, "root")
    assert "HIDDENTGT" in client.get("/targets", headers=_headers("root")).text
    assert "HIDDENTGT" not in client.get("/targets", headers=_headers("alice")).text
    assert "HIDDENTGT" in client.get("/target?name=HIDDENTGT", headers=_headers("root")).text
    denied = client.get("/target?name=HIDDENTGT", headers=_headers("alice")).text
    assert _n_observations(denied) == 0


# ── /target ─────────────────────────────────────────────────────────────


def test_fully_restricted_target_looks_like_a_never_observed_name(client, db):
    hidden = client.get("/target?name=HIDDENTGT", headers=_headers("alice"))
    unknown = client.get("/target?name=NEVEROBSX", headers=_headers("alice"))
    assert hidden.status_code == unknown.status_code == 200
    assert _n_observations(hidden.text) == _n_observations(unknown.text) == 0
    # Byte-identical apart from the name itself.
    assert hidden.text.replace("HIDDENTGT", "X") == unknown.text.replace("NEVEROBSX", "X")


def test_mixed_target_page_lists_only_visible_nights(client, db):
    _set_admin(db, "root")
    denied = client.get("/target?name=MIXEDTGT", headers=_headers("alice")).text
    full = client.get("/target?name=MIXEDTGT", headers=_headers("root")).text
    assert _n_observations(full) == 2
    assert _n_observations(denied) == 1
    assert _RESTRICTED not in denied
    assert _RESTRICTED in full


# ── /api/targets writes and tags ───────────────────────────────────────


def _note(db: str, obj: str) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT obsdate, instrument, note FROM target_notes WHERE object = ?", (obj,)
        ).fetchall()


def test_note_write_on_hidden_target_is_404_and_writes_nothing(client, db):
    resp = client.put(
        "/api/targets/HIDDENTGT/note", json={"note": "x"}, headers=_headers("alice")
    )
    assert resp.status_code == 404
    assert _note(db, "HIDDENTGT") == []


def test_note_write_on_restricted_night_of_mixed_target_is_404(client, db):
    hidden = client.put(
        "/api/targets/MIXEDTGT/note",
        json={"note": "x", "obsdate": "260102", "instrument": "sinistro"},
        headers=_headers("alice"),
    )
    visible = client.put(
        "/api/targets/MIXEDTGT/note",
        json={"note": "ok", "obsdate": "260101", "instrument": "sinistro"},
        headers=_headers("alice"),
    )
    assert hidden.status_code == 404
    assert visible.status_code == 200
    assert _note(db, "MIXEDTGT") == [("260101", "sinistro", "ok")]


def test_writes_on_never_observed_name_keep_old_behavior(client, db):
    resp = client.put(
        "/api/targets/NEVEROBSX/note", json={"note": "x"}, headers=_headers("alice")
    )
    assert resp.status_code == 200


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("delete", "/api/targets/HIDDENTGT/note", None),
        ("put", "/api/targets/HIDDENTGT/identified", {"is_identified": 1}),
        ("put", "/api/targets/HIDDENTGT/norm-name", {"norm_name": "Other"}),
    ],
)
def test_other_writes_on_hidden_target_are_404(client, method, path, body):
    kwargs = {"headers": _headers("alice")}
    if body is not None:
        kwargs["json"] = body
    resp = client.request(method.upper(), path, **kwargs)
    assert resp.status_code == 404


def test_admin_can_write_restricted_target_note(client, db):
    _set_admin(db, "root")
    resp = client.put(
        "/api/targets/HIDDENTGT/note", json={"note": "admin"}, headers=_headers("root")
    )
    assert resp.status_code == 200


def test_norm_names_and_tags_treat_hidden_as_unknown(client, db):
    names = client.get("/api/targets/norm-names", headers=_headers("alice")).json()
    assert "HIDDENTGT" not in names["norm_names"]
    assert "MIXEDTGT" in names["norm_names"]

    client.post("/api/tags", json={"tag": "proj"}, headers=_headers("alice"))
    hidden = client.put(
        "/api/targets/HIDDENTGT/tags", json={"tag": "proj"}, headers=_headers("alice")
    )
    unknown = client.put(
        "/api/targets/NEVEROBSX/tags", json={"tag": "proj"}, headers=_headers("alice")
    )
    assert hidden.status_code == unknown.status_code == 404

    _set_admin(db, "root")
    assert client.put(
        "/api/targets/HIDDENTGT/tags", json={"tag": "proj"}, headers=_headers("root")
    ).status_code == 200
    assert client.get("/api/targets/HIDDENTGT/tags", headers=_headers("alice")).json()["tags"] == []
    assert client.get("/api/targets/HIDDENTGT/tags", headers=_headers("root")).json()["tags"] == ["proj"]

    # A hidden target's tag removal answers like an unknown name: a no-op.
    client.request(
        "DELETE", "/api/targets/HIDDENTGT/tags", json={"tag": "proj"}, headers=_headers("alice")
    )
    assert client.get("/api/targets/HIDDENTGT/tags", headers=_headers("root")).json()["tags"] == ["proj"]


def test_project_page_hides_restricted_members(client, db):
    _set_admin(db, "root")
    client.post("/api/tags", json={"tag": "proj"}, headers=_headers("root"))
    for obj in ("HIDDENTGT", "MIXEDTGT"):
        client.put(f"/api/targets/{obj}/tags", json={"tag": "proj"}, headers=_headers("root"))
    assert "HIDDENTGT" in client.get("/tag?name=proj", headers=_headers("root")).text
    denied = client.get("/tag?name=proj", headers=_headers("alice")).text
    assert "HIDDENTGT" not in denied
    assert "MIXEDTGT" in denied


# ── admin-only writes ───────────────────────────────────────────────────


def test_no_module_but_access_writes_the_access_tables():
    """Only the CLI (through muscat_db.access) may write restricted_proposals
    or user_proposal_access; no web route or other module does."""
    pattern = re.compile(
        r"(INSERT|UPDATE|DELETE|REPLACE)[^;\"']*\b(restricted_proposals|user_proposal_access)\b",
        re.IGNORECASE,
    )
    src = Path(access.__file__).parent
    offenders = [
        p.name
        for p in src.glob("*.py")
        if p.name != "access.py" and pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
    web_src = Path(web.__file__).read_text(encoding="utf-8")
    for writer in ("restrict", "unrestrict", "grant", "revoke"):
        assert f"access.{writer}(" not in web_src


# ── CLI ─────────────────────────────────────────────────────────────────


def test_cli_restrict_uses_observed_spelling_and_warns_about_backfill(tmp_path, monkeypatch):
    path = tmp_path / "cli.db"
    _build_db(str(path), restrict=False)
    monkeypatch.setenv("MUSCAT_DB_PATH", str(path))
    result = CliRunner().invoke(cli_app, ["access", "restrict", _OPEN.lower(), "-d", "embargo"])
    assert result.exit_code == 0, result.output
    assert "backfill has not processed every date" in result.output
    rows = access.list_restrictions(str(path))
    assert [(r["proposal_id"], r["description"]) for r in rows] == [(_OPEN, "embargo")]


def test_cli_restrict_is_quiet_about_backfill_once_complete(tmp_path, monkeypatch):
    import json

    path = tmp_path / "cli.db"
    _build_db(str(path), restrict=False)
    monkeypatch.setenv("MUSCAT_DB_PATH", str(path))
    ckpt = tmp_path / "muscat-tmp"
    ckpt.mkdir()
    for inst, dates in {"sinistro": ["260101", "260102"], "muscat3": ["260201", "260202"]}.items():
        (ckpt / f"propid_backfill_{inst}.json").write_text(json.dumps({"done": dates}))
    result = CliRunner().invoke(cli_app, ["access", "restrict", _OPEN])
    assert result.exit_code == 0, result.output
    assert "backfill" not in result.output


def test_cli_grant_revoke_list_roundtrip(db):
    runner = CliRunner()
    assert runner.invoke(cli_app, ["access", "grant", "alice", _RESTRICTED, "--by", "root"]).exit_code == 0
    listing = runner.invoke(cli_app, ["access", "list"])
    assert "alice" in listing.output
    mine = runner.invoke(cli_app, ["access", "list", "--user", "alice"])
    assert _RESTRICTED in mine.output
    assert access.denied_proposal_ids_for(db, "alice") == frozenset()

    assert runner.invoke(cli_app, ["access", "revoke", "alice", _RESTRICTED]).exit_code == 0
    assert access.denied_proposal_ids_for(db, "alice") == {_RESTRICTED}
    assert runner.invoke(cli_app, ["access", "revoke", "alice", _RESTRICTED]).exit_code == 1


def test_cli_unrestrict(db):
    runner = CliRunner()
    assert runner.invoke(cli_app, ["access", "unrestrict", _RESTRICTED]).exit_code == 0
    assert access.denied_proposal_ids_for(db, None) == frozenset()
    assert runner.invoke(cli_app, ["access", "unrestrict", _RESTRICTED]).exit_code == 1
