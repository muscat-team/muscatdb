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
    ScopedDenied,
    _insert_summary_rows,
    _populate_targets,
    _summary_rows,
    get_targets,
    sql_not_denied,
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


# ── /obs instrument statistics ──────────────────────────────────────────


def _obs_stats(client, user: str) -> dict[str, tuple[int, int, int]]:
    """``{instrument: (n_dates, n_targets, n_frames)}`` from the /obs table."""
    html = client.get("/obs?min_frames=1", headers=_headers(user)).text
    rows = re.findall(
        r'<a href="/(\w+)">\1</a></strong></td>\s*'
        r'<td class="mono">(\d+)</td>\s*<td class="mono">(\d+)</td>\s*<td class="mono">(\d+)</td>',
        html,
    )
    return {name: (int(d), int(t), int(f)) for name, d, t, f in rows}


def _obs_has_data(client, user: str) -> dict[str, bool]:
    html = client.get("/obs", headers=_headers(user)).text
    cards = re.findall(r'<div class="name">(\w+)</div>\s*<div class="meta">([^<]*)', html)
    return {name: meta.startswith("Click to view") for name, meta in cards}


def test_obs_stats_count_only_what_the_viewer_may_see(client, db):
    _set_admin(db, "root")
    full = _obs_stats(client, "root")
    denied = _obs_stats(client, "alice")

    assert full["sinistro"] == (2, 1, 5)
    assert full["muscat3"] == (2, 2, 2)
    # The restricted sinistro night and HIDDENTGT's night drop out entirely.
    assert denied["sinistro"] == (1, 1, 2)
    assert denied["muscat3"] == (1, 1, 1)


def test_obs_instrument_with_only_hidden_data_looks_empty(client, db):
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO summaries (instrument, obsdate, ccd, object, nframes, proposal_id)
               VALUES ('muscat4', '260301', 0, 'HIDDENTGT', 4, ?)""",
            (_RESTRICTED,),
        )
    _set_admin(db, "root")

    assert _obs_has_data(client, "root")["muscat4"] is True
    assert _obs_stats(client, "root")["muscat4"] == (1, 1, 4)
    assert _obs_has_data(client, "alice")["muscat4"] is False
    assert _obs_stats(client, "alice")["muscat4"] == (0, 0, 0)


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
    ],
)
def test_other_writes_on_hidden_target_are_404(client, method, path, body):
    kwargs = {"headers": _headers("alice")}
    if body is not None:
        kwargs["json"] = body
    resp = client.request(method.upper(), path, **kwargs)
    assert resp.status_code == 404


@pytest.mark.parametrize("obj", ["OPENTGT", "HIDDENTGT", "NEVEROBSX"])
def test_norm_name_rename_is_admin_only(client, db, obj):
    """A rename moves the raw OBJECT into another target's group, so it is
    admin-only (#228). Non-admins get the same 403 for a visible, a hidden and
    a never-observed name: the status is no existence oracle."""
    resp = client.put(
        f"/api/targets/{obj}/norm-name", json={"norm_name": "Other"}, headers=_headers("alice")
    )
    assert resp.status_code == 403


def test_admin_can_rename_a_target(client, db):
    _set_admin(db, "root")
    resp = client.put(
        "/api/targets/HIDDENTGT/norm-name", json={"norm_name": "Other"}, headers=_headers("root")
    )
    assert resp.status_code == 200
    assert resp.json()["norm_name"] == "Other"


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


def _tag_projects(client, db) -> None:
    """alpha: MIXEDTGT (partly open); beta: HIDDENTGT only (restricted);
    gamma: OPENTGT (open to everyone)."""
    _set_admin(db, "root")
    for tag, obj in (("alpha", "MIXEDTGT"), ("beta", "HIDDENTGT"), ("gamma", "OPENTGT")):
        client.post("/api/tags", json={"tag": tag}, headers=_headers("root"))
        client.put(f"/api/targets/{obj}/tags", json={"tag": tag}, headers=_headers("root"))


def _project_cards(html: str) -> dict[str, str]:
    """Card name -> the card's meta text, from /projects."""
    cards = {}
    for block in html.split('<div class="card">')[1:]:
        name = re.search(r'<div class="name">([^<]+)</div>', block)
        meta = re.search(r'<div class="meta">([^<]+)</div>', block)
        if name:
            cards[name.group(1)] = meta.group(1) if meta else ""
    return cards


def test_projects_cards_follow_viewer_access(client, db):
    _tag_projects(client, db)

    # alice holds no grant to the restricted proposal: she can see the
    # partly-open MIXEDTGT and OPENTGT, never beta's only target.
    alice = _project_cards(client.get("/projects", headers=_headers("alice")).text)
    assert set(alice) == {"alpha", "gamma"}
    assert alice["alpha"] == "1 target"
    assert alice["gamma"] == "1 target"


def test_projects_card_counts_only_visible_targets(client, db):
    _tag_projects(client, db)
    client.put("/api/targets/HIDDENTGT/tags", json={"tag": "alpha"}, headers=_headers("root"))

    alice = _project_cards(client.get("/projects", headers=_headers("alice")).text)
    assert alice["alpha"] == "1 target"  # HIDDENTGT is not counted for alice


def test_projects_cards_for_granted_user_admin_and_anonymous(client, db):
    _tag_projects(client, db)
    access.grant(db, "bob", _RESTRICTED, "root")

    bob = _project_cards(client.get("/projects", headers=_headers("bob")).text)
    assert set(bob) == {"alpha", "beta", "gamma"}

    root = _project_cards(client.get("/projects", headers=_headers("root")).text)
    assert set(root) == {"alpha", "beta", "gamma"}

    anonymous = _project_cards(client.get("/projects").text)
    assert set(anonymous) == {"alpha", "gamma"}


def test_project_with_only_hidden_targets_is_not_listed(client, db):
    _tag_projects(client, db)
    html = client.get("/projects", headers=_headers("alice")).text
    assert "beta" not in _project_cards(html)
    assert "HIDDENTGT" not in html


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


def test_cli_restrict_uses_observed_spelling_and_warns_about_blank_propid(tmp_path, monkeypatch):
    path = tmp_path / "cli.db"
    _build_db(str(path), restrict=False)
    monkeypatch.setenv("MUSCAT_DB_PATH", str(path))
    result = CliRunner().invoke(cli_app, ["access", "restrict", _OPEN.lower(), "-d", "embargo"])
    assert result.exit_code == 0, result.output
    assert "no PROPID" in result.output
    assert "muscat3: 1" in result.output
    rows = access.list_restrictions(str(path))
    assert [(r["proposal_id"], r["description"]) for r in rows] == [(_OPEN, "embargo")]


def test_cli_restrict_warns_about_blank_propid_even_when_checkpoint_is_complete(
    tmp_path, monkeypatch
):
    """The warning counts the frames that would leak, not the dates the
    backfill checkpoint has not recorded (#144)."""
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
    assert "muscat3: 1" in result.output


def test_cli_restrict_is_quiet_when_no_frame_lacks_propid(tmp_path, monkeypatch):
    """No checkpoint at all (a night newer than the last backfill run), but
    every frame carries a PROPID: nothing to warn about."""
    path = tmp_path / "cli.db"
    _build_db(str(path), restrict=False)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE frames SET proposal_id = ? WHERE proposal_id = ''", (_OPEN,))
    monkeypatch.setenv("MUSCAT_DB_PATH", str(path))
    result = CliRunner().invoke(cli_app, ["access", "restrict", _OPEN])
    assert result.exit_code == 0, result.output
    assert "no PROPID" not in result.output


def test_blank_propid_frames_ignores_instruments_without_propid(tmp_path):
    """muscat/muscat2 never carry a PROPID, so a blank there is not a gap."""
    path = tmp_path / "blank.db"
    _build_db(str(path), restrict=False)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE frames SET instrument = 'muscat' WHERE proposal_id = ''")
    assert access.blank_propid_frames(str(path)) == {}


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


def test_explain_user_reports_visibility_per_proposal(db):
    info = access.explain_user(db, "alice")
    by_pid = {p["proposal_id"]: p for p in info["proposals"]}
    assert not info["admin"]
    assert by_pid[_RESTRICTED]["restricted"] and not by_pid[_RESTRICTED]["visible"]
    assert by_pid[_OPEN]["visible"] and not by_pid[_OPEN]["restricted"]
    assert by_pid[""]["visible"]
    assert by_pid[_RESTRICTED]["target_nights"] == 2
    assert by_pid[_RESTRICTED]["objects"] == 2


def test_explain_user_follows_grants_and_admin(db):
    access.grant(db, "alice", _RESTRICTED, "root")
    _set_admin(db, "root")
    alice = {p["proposal_id"]: p for p in access.explain_user(db, "alice")["proposals"]}
    assert alice[_RESTRICTED]["granted"] and alice[_RESTRICTED]["visible"]
    root = access.explain_user(db, "root")
    assert root["admin"]
    assert all(p["visible"] for p in root["proposals"])


def test_explain_user_flags_grants_that_match_nothing(db):
    access.grant(db, "alice", "NOSUCH-PROPOSAL-1", "root")
    assert access.explain_user(db, "alice")["unmatched_grants"] == ["NOSUCH-PROPOSAL-1"]


def test_explain_target_lists_nights_and_hides_by_proposal(db):
    rows = access.explain_target(db, "alice", "mixed tgt")
    assert [(r["obsdate"], r["visible"]) for r in rows] == [("260101", True), ("260102", False)]
    assert not any(r["visible"] for r in access.explain_target(db, "alice", "HIDDENTGT"))
    assert access.explain_target(db, "alice", "NEVEROBSX") == []


def test_explain_matches_what_the_web_serves(client, db):
    """The report and the page agree: a target explain calls hidden is a 404."""
    assert not any(r["visible"] for r in access.explain_target(db, "alice", "HIDDENTGT"))
    resp = client.put(
        "/api/targets/HIDDENTGT/note", json={"note": "x"}, headers=_headers("alice")
    )
    assert resp.status_code == 404


def test_cli_explain_summarises_and_explains_a_target(db):
    result = CliRunner().invoke(
        cli_app, ["access", "explain", "alice", "--target", "HIDDENTGT"]
    )
    assert result.exit_code == 0, result.output
    assert "2 of 4 target-nights visible, 2 hidden" in result.output
    assert "hidden (answers as an unknown target)" in result.output
    assert "has no users row" in result.output


def test_cli_explain_is_read_only(db):
    before = (access.list_restrictions(db), access.list_grants(db))
    CliRunner().invoke(cli_app, ["access", "explain", "alice"])
    assert (access.list_restrictions(db), access.list_grants(db)) == before


def test_cli_unrestrict(db):
    runner = CliRunner()
    assert runner.invoke(cli_app, ["access", "unrestrict", _RESTRICTED]).exit_code == 0
    assert access.denied_proposal_ids_for(db, None) == frozenset()
    assert runner.invoke(cli_app, ["access", "unrestrict", _RESTRICTED]).exit_code == 1


# ── scoped viewer (issue #202) ──────────────────────────────────────────


def _scope(db: str, user: str, *grants: str) -> None:
    access.set_scoped(db, user)
    for proposal in grants:
        access.grant(db, user, proposal, "admin")


def test_scoped_denied_set_is_everything_but_the_allowlist(db):
    _scope(db, "andrew", _OPEN)
    denied = access.denied_proposal_ids_for(db, "andrew")
    assert isinstance(denied, ScopedDenied)
    assert denied.allowed == {_OPEN}
    assert _RESTRICTED in denied
    assert "" in denied  # frames without a PROPID
    assert _OPEN not in denied


def test_scoped_viewer_sees_only_allowlisted_targets(client, db):
    _scope(db, "andrew", _OPEN)
    html = client.get("/targets", headers=_headers("andrew")).text
    assert "MIXEDTGT" in html
    assert "HIDDENTGT" not in html  # only under a different proposal
    assert "OPENTGT" not in html  # blank PROPID, e.g. muscat/muscat2


def test_scoped_viewer_target_rollup_counts_only_allowlisted_frames(client, db):
    _scope(db, "andrew", _OPEN)
    rows = _export_rows(client, "andrew")
    assert set(rows) == {"MIXEDTGT"}
    assert rows["MIXEDTGT"]["n_frames"] == "2"  # the 3 restricted frames are not counted


def test_scoped_viewer_with_no_grants_sees_nothing(client, db):
    access.set_scoped(db, "andrew")
    assert _export_rows(client, "andrew") == {}
    html = client.get("/targets", headers=_headers("andrew")).text
    assert not any(t in html for t in ("MIXEDTGT", "HIDDENTGT", "OPENTGT"))


def test_unscoping_restores_blocklist_behaviour(client, db):
    _scope(db, "andrew", _OPEN)
    assert set(_export_rows(client, "andrew")) == {"MIXEDTGT"}
    access.set_scoped(db, "andrew", False)
    web._index_cache.clear()
    assert set(_export_rows(client, "andrew")) == {"MIXEDTGT", "OPENTGT"}


def test_scoped_viewer_does_not_disturb_other_viewers(client, db):
    _scope(db, "andrew", _OPEN)
    assert set(_export_rows(client, "andrew")) == {"MIXEDTGT"}
    assert set(_export_rows(client, "alice")) == {"MIXEDTGT", "OPENTGT"}


def test_admin_is_never_scoped(db):
    _set_admin(db, "root")
    access.set_scoped(db, "root")
    assert access.is_scoped(db, "root") is False
    assert access.denied_proposal_ids_for(db, "root") == frozenset()


def test_scoped_sql_fails_closed_on_unseen_proposals():
    """A proposal ingested after the denied set was computed is not in it, so
    the filter must be an allowlist, not a blocklist."""
    scoped = ScopedDenied({"", "OLD-PROP"}, {"KEY-1"})
    clause, params = sql_not_denied(scoped)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (proposal_id TEXT)")
    conn.executemany(
        "INSERT INTO t VALUES (?)",
        [("KEY-1",), ("key-1",), ("OLD-PROP",), ("",), (None,), ("NEW-PROP",)],
    )
    kept = [r[0] for r in conn.execute(f"SELECT proposal_id FROM t WHERE {clause}", params)]
    assert kept == ["KEY-1", "key-1"]
    assert sql_not_denied(ScopedDenied({""}, set())) == ("0", [])


def test_is_denied_for_scoped_viewer_hides_unseen_and_blank():
    scoped = ScopedDenied({""}, {"KEY-1"})
    assert not access.is_denied("key-1", scoped)
    assert access.is_denied("", scoped)
    assert access.is_denied(None, scoped)
    assert access.is_denied("BRAND-NEW", scoped)


def test_catalog_rows_for_unseen_targets_lose_their_in_db_flag(monkeypatch):
    monkeypatch.setattr(web, "_viewer_denied", lambda: ScopedDenied({""}, {"KEY-1"}))
    monkeypatch.setattr(web, "_all_norm_names", lambda db, denied: {"visible"})
    monkeypatch.setattr(web, "_db_path", lambda: "unused.db")
    indb, tname = web._mask_unseen_catalog_rows(
        [1, 1, 0], ["visible", "hidden", ""], lambda i: f"fallback{i}"
    )
    assert indb == [1, 0, 0]
    assert tname == ["visible", "fallback1", ""]


def test_catalog_rows_untouched_for_unrestricted_viewer(monkeypatch):
    monkeypatch.setattr(web, "_viewer_denied", lambda: frozenset())
    indb, tname = web._mask_unseen_catalog_rows([1], ["anything"], lambda i: "x")
    assert (indb, tname) == ([1], ["anything"])


def test_cli_scope_roundtrip_and_admin_refusal(db, monkeypatch):
    monkeypatch.setenv("MUSCAT_DB_PATH", db)
    runner = CliRunner()
    first = runner.invoke(cli_app, ["access", "scope", "andrew"])
    assert first.exit_code == 0, first.output
    assert "is scoped" in first.output
    assert "no grants" in first.output
    assert access.is_scoped(db, "andrew")
    assert "already scoped" in runner.invoke(cli_app, ["access", "scope", "andrew"]).output
    off = runner.invoke(cli_app, ["access", "scope", "andrew", "--off"])
    assert off.exit_code == 0 and "no longer scoped" in off.output
    assert not access.is_scoped(db, "andrew")
    _set_admin(db, "root")
    refused = runner.invoke(cli_app, ["access", "scope", "root"])
    assert refused.exit_code == 1


def test_cli_grant_to_scoped_user_does_not_warn_about_unrestricted(db, monkeypatch):
    monkeypatch.setenv("MUSCAT_DB_PATH", db)
    access.set_scoped(db, "andrew")
    result = CliRunner().invoke(cli_app, ["access", "grant", "andrew", _OPEN])
    assert result.exit_code == 0, result.output
    assert "no effect" not in result.output


# ── target grants for a scoped viewer (issue #228 acceptance case) ──────


def _grant_target(db: str, user: str, target: str, **kwargs) -> None:
    from muscat_db import access_groups

    with sqlite3.connect(db) as conn:
        access_groups.ensure_group(conn, access_groups.user_group(user))
        access_groups.add_member(conn, access_groups.user_group(user), user)
        access_groups.add_grant(
            conn, access_groups.user_group(user), target=target, granted_by="admin", **kwargs
        )


def test_target_grant_shows_that_target_under_a_restricted_proposal(client, db):
    _scope(db, "andrew", _OPEN)
    _grant_target(db, "andrew", "HIDDENTGT")
    rows = _export_rows(client, "andrew")
    assert set(rows) == {"MIXEDTGT", "HIDDENTGT"}
    assert rows["HIDDENTGT"]["n_frames"] == "1"


def test_target_grant_leaves_the_rest_of_the_proposal_hidden(client, db):
    # MIXEDTGT's 260102 night is also under the restricted proposal.
    _scope(db, "andrew", _OPEN)
    _grant_target(db, "andrew", "HIDDENTGT")
    rows = _export_rows(client, "andrew")
    assert rows["MIXEDTGT"]["n_frames"] == "2"  # the 3 restricted frames stay hidden


def test_target_grant_alone_gives_only_that_target(client, db):
    access.set_scoped(db, "andrew")
    _grant_target(db, "andrew", "MIXEDTGT")
    rows = _export_rows(client, "andrew")
    assert set(rows) == {"MIXEDTGT"}
    assert rows["MIXEDTGT"]["n_frames"] == "5"  # every proposal's frames of the target
    html = client.get("/targets", headers=_headers("andrew")).text
    assert "HIDDENTGT" not in html and "OPENTGT" not in html


def test_target_grant_matches_spelling_with_case_and_spaces_ignored(db):
    access.set_scoped(db, "andrew")
    _grant_target(db, "andrew", "hidden tgt")
    denied = access.denied_proposal_ids_for(db, "andrew")
    assert not access.is_denied(
        _RESTRICTED, denied, instrument="muscat3", obsdate="260201", obj="HIDDENTGT"
    )


def test_target_grant_can_be_narrowed_by_date(db):
    access.set_scoped(db, "andrew")
    _grant_target(db, "andrew", "MIXEDTGT", date_from="260102", date_to="260102")
    denied = access.denied_proposal_ids_for(db, "andrew")
    kw = {"instrument": "sinistro", "obj": "MIXEDTGT"}
    assert not access.is_denied(_RESTRICTED, denied, obsdate="260102", **kw)
    assert access.is_denied(_OPEN, denied, obsdate="260101", **kw)


def test_is_denied_without_row_detail_fails_closed_for_target_grants(db):
    access.set_scoped(db, "andrew")
    _grant_target(db, "andrew", "HIDDENTGT")
    denied = access.denied_proposal_ids_for(db, "andrew")
    assert access.is_denied(_RESTRICTED, denied)  # no object given: not enough to allow


def test_target_grant_is_not_applied_to_other_viewers(client, db):
    _scope(db, "andrew", _OPEN)
    _grant_target(db, "andrew", "HIDDENTGT")
    _scope(db, "bob", _OPEN)
    assert set(_export_rows(client, "bob")) == {"MIXEDTGT"}


def test_rendered_page_cache_is_not_shared_between_different_target_grants(client, db):
    _scope(db, "andrew", _OPEN)
    _scope(db, "bob", _OPEN)
    _grant_target(db, "andrew", "HIDDENTGT")
    assert "HIDDENTGT" in client.get("/targets", headers=_headers("andrew")).text
    assert "HIDDENTGT" not in client.get("/targets", headers=_headers("bob")).text
    assert "HIDDENTGT" in client.get("/targets", headers=_headers("andrew")).text


def test_target_grant_night_is_visible_and_other_nights_are_not(db):
    _scope(db, "andrew", _OPEN)
    _grant_target(db, "andrew", "HIDDENTGT")
    nights = access.NightVisibility(db, access.denied_proposal_ids_for(db, "andrew"))
    assert not nights.hidden("muscat3", "260201", "HIDDENTGT")
    assert nights.hidden("sinistro", "260102", "MIXEDTGT")


def test_explain_target_reports_target_grant_rows_as_visible(db):
    _scope(db, "andrew", _OPEN)
    _grant_target(db, "andrew", "HIDDENTGT")
    assert all(r["visible"] for r in access.explain_target(db, "andrew", "HIDDENTGT"))
    mixed = {r["obsdate"]: r["visible"] for r in access.explain_target(db, "andrew", "MIXEDTGT")}
    assert mixed == {"260101": True, "260102": False}


def test_cli_grant_target_roundtrip(db, monkeypatch):
    monkeypatch.setenv("MUSCAT_DB_PATH", db)
    runner = CliRunner()
    access.set_scoped(db, "andrew")
    result = runner.invoke(cli_app, ["access", "grant-target", "andrew", "HIDDENTGT"])
    assert result.exit_code == 0, result.output
    denied = access.denied_proposal_ids_for(db, "andrew")
    assert not access.is_denied(
        _RESTRICTED, denied, instrument="muscat3", obsdate="260201", obj="HIDDENTGT"
    )
    again = runner.invoke(cli_app, ["access", "grant-target", "andrew", "HIDDENTGT"])
    assert again.exit_code == 0 and "already" in again.output
    revoked = runner.invoke(cli_app, ["access", "revoke-target", "andrew", "HIDDENTGT"])
    assert revoked.exit_code == 0, revoked.output
    assert access.is_denied(
        _RESTRICTED,
        access.denied_proposal_ids_for(db, "andrew"),
        instrument="muscat3", obsdate="260201", obj="HIDDENTGT",
    )
    assert runner.invoke(cli_app, ["access", "revoke-target", "andrew", "HIDDENTGT"]).exit_code == 1


def test_cli_grant_target_refuses_an_unscoped_user(db, monkeypatch):
    # A blocklist viewer cannot be given a target exception yet; saying nothing
    # would leave the grant silently inert.
    monkeypatch.setenv("MUSCAT_DB_PATH", db)
    result = CliRunner().invoke(cli_app, ["access", "grant-target", "alice", "HIDDENTGT"])
    assert result.exit_code == 1 and "scoped" in result.output


def test_cli_grant_target_warns_when_target_matches_nothing(db, monkeypatch):
    monkeypatch.setenv("MUSCAT_DB_PATH", db)
    access.set_scoped(db, "andrew")
    result = CliRunner().invoke(cli_app, ["access", "grant-target", "andrew", "NOSUCHTGT"])
    assert result.exit_code == 0 and "matches no" in result.output


# ── products built from a target-granted night (review of #262) ─────────


def _product_viewer(db: str, tmp_path, user: str, normalize=None):
    from muscat_db.product_visibility import ProductVisibility

    denied = access.denied_proposal_ids_for(db, user, normalize)
    return ProductVisibility(
        user,
        denied,
        access.NightVisibility(db, denied, normalize),
        run_dir=lambda level, inst, date, target, run_id: (
            tmp_path / level / inst / date / target / run_id
        ),
        job_owner=lambda *a: None,
    )


def _put_product(tmp_path, level, inst, date, target, run_id, inputs):
    from muscat_db import product_record as pr

    rdir = tmp_path / level / inst / date / target / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    assert pr.write_record(rdir, pr.build_record(level, author="someone", inputs=inputs))


def _frames_input(inst, date, target, *proposals):
    return {
        "kind": "frames", "instrument": inst, "obsdate": date, "target": target,
        "proposal_ids": sorted(proposals), "n_frames": 1, "blank_frames": 0, "resolved": True,
    }


@pytest.mark.parametrize("spelling", ["HIDDENTGT", "hidden tgt"])
def test_target_grant_shows_photometry_run_of_the_granted_target(db, tmp_path, spelling):
    _scope(db, "andrew", _OPEN)
    _grant_target(db, "andrew", "HIDDENTGT")
    _put_product(
        tmp_path, "photometry", "muscat3", "260201", spelling, "r1",
        [_frames_input("muscat3", "260201", spelling, _RESTRICTED)],
    )
    viewer = _product_viewer(db, tmp_path, "andrew")
    assert not viewer.hidden("photometry", "muscat3", "260201", spelling, "r1")


def test_target_grant_shows_transit_fit_built_on_the_granted_run(db, tmp_path):
    _scope(db, "andrew", _OPEN)
    _grant_target(db, "andrew", "HIDDENTGT")
    _put_product(
        tmp_path, "photometry", "muscat3", "260201", "HIDDENTGT", "r1",
        [_frames_input("muscat3", "260201", "HIDDENTGT", _RESTRICTED)],
    )
    _put_product(
        tmp_path, "transit_fit", "muscat3", "260201", "HIDDENTGT", "f1",
        [{"kind": "product", "level": "photometry", "instrument": "muscat3",
          "obsdate": "260201", "target": "HIDDENTGT", "run_id": "r1"}],
    )
    viewer = _product_viewer(db, tmp_path, "andrew")
    assert not viewer.hidden("transit_fit", "muscat3", "260201", "HIDDENTGT", "f1")


def test_target_grant_does_not_show_runs_of_other_targets_in_the_proposal(db, tmp_path):
    _scope(db, "andrew", _OPEN)
    _grant_target(db, "andrew", "HIDDENTGT")
    _put_product(
        tmp_path, "photometry", "sinistro", "260102", "MIXEDTGT", "r1",
        [_frames_input("sinistro", "260102", "MIXEDTGT", _RESTRICTED)],
    )
    viewer = _product_viewer(db, tmp_path, "andrew")
    assert viewer.hidden("photometry", "sinistro", "260102", "MIXEDTGT", "r1")


def test_target_grant_run_resolves_job_spelling_through_the_normalizer(db, tmp_path):
    # A run names its target as the job directory does ("TGT-HIDDEN"), the frames
    # carry the header's object; only the app normalizer links them.
    def normalize(name: str) -> str:
        return "HIDDENTGT" if name.replace("-", "").upper() in {"TGTHIDDEN", "HIDDENTGT"} else name

    _scope(db, "andrew", _OPEN)
    _grant_target(db, "andrew", "HIDDENTGT")
    _put_product(
        tmp_path, "photometry", "muscat3", "260201", "TGT-HIDDEN", "r1",
        [_frames_input("muscat3", "260201", "TGT-HIDDEN", _RESTRICTED)],
    )
    viewer = _product_viewer(db, tmp_path, "andrew", normalize)
    assert not viewer.hidden("photometry", "muscat3", "260201", "TGT-HIDDEN", "r1")


def test_groups_parity_covers_target_grants(db):
    from muscat_db import access_groups

    _scope(db, "andrew", _OPEN)
    access_groups.seed_from_blocklist(db)
    _grant_target(db, "andrew", "HIDDENTGT")
    assert access_groups.parity(db) == []

    # Without row detail the blocklist side would hide the granted night and
    # the check would report a mismatch for it, so this fails if detail is lost.
    nights = {(m["instrument"], m["obsdate"]) for m in access_groups.parity(db)}
    assert ("muscat3", "260201") not in nights


# --- LCO test observations ---------------------------------------------------

def _test_record(db: str, target: str, created_by: str = "") -> str:
    from muscat_db import test_observations

    record = test_observations.create_record({"kind": "muscat3", "target": target}, db, created_by=created_by)
    return record["id"]


def test_scoped_viewer_neither_lists_nor_fetches_a_test_on_a_hidden_target(client, db):
    hidden = _test_record(db, "HIDDENTGT")
    visible = _test_record(db, "OPENTGT")
    _set_admin(db, "root")

    listed = client.get("/api/lco/test-observations", headers=_headers("alice")).json()["records"]
    assert [r["id"] for r in listed] == [visible]
    by_target = client.get("/api/lco/test-observations?target=HIDDENTGT", headers=_headers("alice")).json()
    assert by_target["records"] == []
    response = client.get(f"/api/lco/test-observations/{hidden}", headers=_headers("alice"))
    assert response.status_code == 404
    # Same answer as an id that never existed.
    assert response.json() == client.get("/api/lco/test-observations/no-such-id", headers=_headers("alice")).json()
    assert client.post(f"/api/lco/test-observations/{hidden}/analyze", headers=_headers("alice")).status_code == 404
    assert client.post(f"/api/lco/test-observations/{hidden}/ipp", json={}, headers=_headers("alice")).status_code == 404

    admin = client.get("/api/lco/test-observations", headers=_headers("root")).json()["records"]
    assert {r["id"] for r in admin} == {hidden, visible}
    assert client.get(f"/api/lco/test-observations/{hidden}", headers=_headers("root")).status_code == 200


def test_only_the_creator_or_an_admin_may_reanalyze_a_test(client, db):
    record = _test_record(db, "OPENTGT", created_by="bob")
    _set_admin(db, "root")
    url = f"/api/lco/test-observations/{record}/analyze"

    assert client.post(url, headers=_headers("alice")).status_code == 403
    # The creator and an admin get past the ownership check; the draft then
    # cannot be re-analyzed, which is the state check's 400.
    for user in ("bob", "root"):
        response = client.post(url, headers=_headers(user))
        assert response.status_code == 400, user
        assert "cannot re-analyze" in response.json()["error"]
