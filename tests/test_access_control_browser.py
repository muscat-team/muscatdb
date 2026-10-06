"""Per-viewer proposal access control on the raw obslog browser (issue #144 PR6).

Covers ``/{instrument}``, ``/{instrument}/{obsdate}``,
``/{instrument}/{obsdate}/ccd{N}`` and the two other endpoints that read the
``frames`` obslog directly (``/api/fov/observed-pointing`` and
``/api/lco/obslog-exposures``). The fixture DB has:

* sinistro 260101: ``OPENTGT`` under an open proposal;
* sinistro 260102: ``HIDDENTGT`` only, under the restricted proposal, so a
  denied viewer must not learn the night exists;
* sinistro 260103: a mixed night, ``OPENTGT`` (open) and ``HIDDENTGT``
  (restricted) on the same CCD.

As in PR4, the restriction row is stored lower-cased while frames carry the
upper-case header spelling.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from muscat_db import web
from muscat_db.coord import CoordRepr
from muscat_db.database import SCHEMA, _insert_summary_rows, _populate_targets, _summary_rows
from tests.test_access_control import (
    _OPEN,
    _PROXY_SECRET,
    _RESTRICTED,
    _headers,
    _set_admin,
)

pytestmark = pytest.mark.usefixtures("mock_target_coord_resolution")

_FRAMES = [
    ("sinistro", "260101", "SIN_2601010001", "OPENTGT", 1.0, _OPEN),
    ("sinistro", "260101", "SIN_2601010002", "OPENTGT", 1.1, _OPEN),
    ("sinistro", "260102", "SIN_2601020001", "HIDDENTGT", 2.0, _RESTRICTED),
    ("sinistro", "260102", "SIN_2601020002", "HIDDENTGT", 2.1, _RESTRICTED),
    ("sinistro", "260103", "SIN_2601030001", "OPENTGT", 3.0, _OPEN),
    ("sinistro", "260103", "SIN_2601030002", "HIDDENTGT", 3.1, _RESTRICTED),
    ("sinistro", "260103", "SIN_2601030003", "HIDDENTGT", 3.2, _RESTRICTED),
]


def _build_db(db_path: str, *, restrict: bool = True) -> None:
    conn = sqlite3.connect(db_path)
    conn.create_aggregate("coord_repr", 2, CoordRepr)
    conn.executescript(SCHEMA)
    conn.executemany(
        """INSERT INTO frames
           (instrument, obsdate, ccd, filename, object, jd_start, ut_start,
            exptime, read_mode, filter, ra, declination, airmass, focus, pa,
            proposal_id)
           VALUES (?, ?, 0, ?, ?, ?, '00:00:00', 10, 'fast', 'gp',
                   '10:00:00.0', '+10:00:00.0', 1, 0, NULL, ?)""",
        _FRAMES,
    )
    _insert_summary_rows(conn, _summary_rows(conn))
    _populate_targets(conn)
    if restrict:
        conn.execute(
            "INSERT INTO restricted_proposals (proposal_id) VALUES (?)",
            (_RESTRICTED.lower(),),
        )
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def _isolate_dirs(tmp_path, monkeypatch):
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
    return TestClient(web.app, client=("127.0.0.1", 12345))


def _get(client, path: str, user: str | None = "alice", **params):
    headers = _headers(user) if user else {}
    resp = client.get(path, headers=headers, params=params)
    assert resp.status_code == 200, resp.text
    return resp


# ── /{instrument} ───────────────────────────────────────────────────────


def test_instrument_page_drops_fully_restricted_night(client):
    html = _get(client, "/sinistro").text
    assert 'href="/sinistro/260101"' in html
    assert 'href="/sinistro/260103"' in html
    assert "260102" not in html


def test_instrument_page_hides_restricted_night_from_anonymous(client):
    assert "260102" not in _get(client, "/sinistro", user=None).text


def test_instrument_page_counts_only_visible_frames_on_mixed_night(db):
    dates = {d["obsdate"]: d for d in web._get_dates(db, "sinistro", denied=frozenset({_RESTRICTED}))}
    assert set(dates) == {"260101", "260103"}
    assert dates["260103"]["nframes"] == 1


def test_admin_and_granted_user_see_every_night(client, db):
    _set_admin(db, "root")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO user_proposal_access (username, proposal_id) VALUES ('bob', ?)",
            (_RESTRICTED,),
        )
    for user in ("root", "bob"):
        assert 'href="/sinistro/260102"' in _get(client, "/sinistro", user=user).text


def test_instrument_page_unchanged_when_nothing_restricted(tmp_path, monkeypatch):
    path = tmp_path / "open.db"
    _build_db(str(path), restrict=False)
    monkeypatch.setenv("MUSCAT_DB_PATH", str(path))
    monkeypatch.setenv("MUSCAT_PROXY_SECRET", _PROXY_SECRET)
    client = TestClient(web.app, client=("127.0.0.1", 12345))
    html = _get(client, "/sinistro", user=None).text
    assert 'href="/sinistro/260102"' in html


# ── /{instrument}/{obsdate} ─────────────────────────────────────────────


def test_restricted_night_looks_like_a_never_observed_night(client):
    hidden = _get(client, "/sinistro/260102").text
    never = _get(client, "/sinistro/991231").text
    assert "HIDDENTGT" not in hidden
    assert hidden.replace("260102", "991231") == never


def test_mixed_night_lists_only_visible_objects(client):
    html = _get(client, "/sinistro/260103").text
    assert "OPENTGT" in html
    assert "HIDDENTGT" not in html


def test_admin_sees_restricted_objects_on_night_page(client, db):
    _set_admin(db, "root")
    assert "HIDDENTGT" in _get(client, "/sinistro/260103", user="root").text


# ── /{instrument}/{obsdate}/ccd{N} ──────────────────────────────────────


def test_restricted_ccd_page_looks_like_a_never_observed_one(client):
    hidden = _get(client, "/sinistro/260102/ccd0").text
    never = _get(client, "/sinistro/991231/ccd0").text
    assert "SIN_2601020001" not in hidden
    assert hidden.replace("260102", "991231") == never


def test_mixed_ccd_page_lists_only_visible_frames(client):
    html = _get(client, "/sinistro/260103/ccd0").text
    assert "SIN_2601030001" in html
    assert "SIN_2601030002" not in html
    assert "HIDDENTGT" not in html


def test_admin_sees_restricted_frames(client, db):
    _set_admin(db, "root")
    html = _get(client, "/sinistro/260103/ccd0", user="root").text
    assert "SIN_2601030002" in html


# ── /api/fov/observed-pointing ──────────────────────────────────────────


def _pointing(client, user, obsdate, obj):
    return _get(
        client, "/api/fov/observed-pointing", user=user,
        inst="sinistro", obsdate=obsdate, obj=obj,
    ).json()


def test_observed_pointing_of_restricted_night_looks_unobserved(client):
    hidden = _pointing(client, "alice", "260102", "HIDDENTGT")
    never = _pointing(client, "alice", "991231", "HIDDENTGT")
    assert hidden["ok"] is False
    assert hidden["error"].replace("260102", "991231") == never["error"]


def test_observed_pointing_open_and_admin_still_work(client, db):
    assert _pointing(client, "alice", "260103", "OPENTGT")["ok"] is True
    _set_admin(db, "root")
    assert _pointing(client, "root", "260102", "HIDDENTGT")["ok"] is True


# ── /api/lco/obslog-exposures ───────────────────────────────────────────


def _exposures(client, user, target):
    return _get(client, "/api/lco/obslog-exposures", user=user, target=target).json()


def test_obslog_exposures_of_hidden_target_look_like_unknown_name(client):
    hidden = _exposures(client, "alice", "HIDDENTGT")
    assert hidden["objects"] == []
    assert hidden["exposures"] == []


def test_obslog_exposures_count_only_visible_frames(client, db):
    open_rows = _exposures(client, "alice", "OPENTGT")["exposures"]
    assert sum(r["nframes"] for r in open_rows) == 3
    _set_admin(db, "root")
    admin_rows = _exposures(client, "root", "HIDDENTGT")["exposures"]
    assert sum(r["nframes"] for r in admin_rows) == 4
