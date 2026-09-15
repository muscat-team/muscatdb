"""Tests for opt-in-restricted-proposal filtering in the static-site builder
(issue #144 PR2).

The static site has no per-viewer identity, so it must be treated as a
zero-grants viewer: every proposal an admin has restricted must never surface
anywhere in the published snapshot -- not in the targets/projects listing, not
in a job row, and not in the instrument/date/ccd drill-down -- while completely
unrestricted data must still publish exactly as before.
"""

from __future__ import annotations

import sqlite3

import pytest

from muscat_db.coord import CoordRepr
from muscat_db.database import (
    SCHEMA,
    _insert_summary_rows,
    _populate_targets,
    _summary_rows,
)
from muscat_db.static_site import build_site

pytestmark = pytest.mark.usefixtures("mock_target_coord_resolution")

# Distinctive, deliberately not a real-looking proposal ID: target.html (#169)
# hardcodes "KEY2026B-001" as its PROPID filter box's placeholder text, which
# every captured target page carries regardless of DB content or restriction.
# Reusing that exact string as a test fixture's restricted proposal id would
# make "not in html" assertions pass or fail on template UI copy instead of on
# actual filtering.
_RESTRICTED_PROPOSAL = "ZZZTEST-RESTRICTED-9001"
_OPEN_PROPOSAL = "ZZZTEST-OPEN-9002"


@pytest.fixture(autouse=True)
def _isolate_example_discovery_dirs(tmp_path, monkeypatch):
    """Point photometry/transit-fit example discovery at empty, nonexistent
    directories.

    ``_photometry_examples``/``_transit_fit_examples`` walk
    ``$MUSCAT_PROSE_DIR``/``$MUSCAT_TIMER_DIR`` on whatever host runs the
    test. Left unset, a dev workstation with real quicklook output there
    would have the static-site builder pick up real production targets ahead
    of anything in this module's tiny throwaway DB -- silently changing which
    code path (real examples vs. the DB fallback) a given test exercises
    depending on what happens to exist on disk.
    """
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path / "no-prose-output"))
    monkeypatch.setenv("MUSCAT_TIMER_DIR", str(tmp_path / "no-timer-output"))


def _insert_frames(conn, rows):
    conn.executemany(
        """INSERT INTO frames
           (instrument, obsdate, ccd, filename, object, jd_start, ut_start,
            exptime, read_mode, filter, ra, declination, airmass, focus, pa,
            proposal_id)
           VALUES (?, ?, ?, ?, ?, ?, '00:00:00', 10, 'fast', 'gp', '', '', 1, 0, 0, ?)""",
        rows,
    )


def _build_db(db_path: str) -> None:
    """A DB with three scenarios, chosen so a naive implementation fails each:

    * ``sinistro``: the *newest* night (260102) is fully restricted, an older
      night (260101) is open -- the drill-down must fall back to the older one
      rather than publish the embargoed night or nothing at all.
    * ``muscat3``: two open nights, newer (260202) after older (260201) --
      the drill-down must pick the genuinely newest one (regression check for
      the pre-existing ``dates[-1]`` bug this PR also fixes).
    * ``muscat4``: its only night is fully restricted -- the whole instrument
      must be skipped rather than publishing an empty drill-down.

    Target names are chosen so the restricted ones sort alphabetically first
    (``AAA...``), which is exactly the ordering the targets-page fallback in
    ``_enumerate`` reads from ``database.get_targets`` -- a slice-then-filter
    implementation would be starved of visible candidates by these names.
    """
    conn = sqlite3.connect(db_path)
    conn.create_aggregate("coord_repr", 2, CoordRepr)
    conn.executescript(SCHEMA)

    _insert_frames(conn, [
        ("sinistro", "260101", 0, "SIN0_2601010001", "OpenA", 1.0, _OPEN_PROPOSAL),
        ("sinistro", "260101", 0, "SIN0_2601010002", "OpenA", 2.0, _OPEN_PROPOSAL),
        ("sinistro", "260102", 0, "SIN0_2601020001", "AAARestricted", 3.0, _RESTRICTED_PROPOSAL),
        ("sinistro", "260102", 0, "SIN0_2601020002", "AAARestricted", 4.0, _RESTRICTED_PROPOSAL),
        ("muscat3", "260201", 0, "MSCT3_2602010001", "OpenB1", 5.0, _OPEN_PROPOSAL),
        ("muscat3", "260202", 0, "MSCT3_2602020001", "OpenB2", 6.0, _OPEN_PROPOSAL),
        ("muscat4", "260301", 0, "MSCT4_2603010001", "AAARestrictedOnly", 7.0, _RESTRICTED_PROPOSAL),
    ])

    rows = _summary_rows(conn)
    _insert_summary_rows(conn, rows)
    _populate_targets(conn)
    conn.execute(
        "INSERT INTO restricted_proposals (proposal_id, description) VALUES (?, ?)",
        (_RESTRICTED_PROPOSAL, "embargoed key project"),
    )
    conn.execute(
        "INSERT OR REPLACE INTO db_meta (key, value) VALUES ('last_build_at', '1700000000')"
    )
    conn.commit()
    conn.close()


@pytest.fixture
def restricted_db(tmp_path):
    db = tmp_path / "muscat.db"
    _build_db(str(db))
    return str(db)


def _read(path):
    return path.read_text(encoding="utf-8")


def _all_html(out):
    return "".join(_read(p) for p in out.rglob("index.html"))


def test_restricted_target_absent_from_targets_page(restricted_db, tmp_path):
    out = tmp_path / "site"
    build_site(out, db_path=restricted_db, n_examples=1, include_figures=False, log=lambda _m: None)

    targets_html = _read(out / "targets" / "index.html")
    assert "AAARestricted" not in targets_html
    assert "OpenA" in targets_html


def test_restricted_proposal_id_never_published(restricted_db, tmp_path):
    out = tmp_path / "site"
    build_site(out, db_path=restricted_db, n_examples=1, include_figures=False, log=lambda _m: None)

    assert _RESTRICTED_PROPOSAL not in _all_html(out)


def test_drilldown_falls_back_to_older_visible_night(restricted_db, tmp_path):
    """sinistro's newest night (260102) is fully restricted; the published
    drill-down must be the older, open night (260101) instead."""
    out = tmp_path / "site"
    build_site(out, db_path=restricted_db, n_examples=1, include_figures=False, log=lambda _m: None)

    assert (out / "sinistro" / "260101" / "index.html").is_file()
    assert not (out / "sinistro" / "260102").exists()
    date_html = _read(out / "sinistro" / "260101" / "index.html")
    assert "OpenA" in date_html
    assert "AAARestricted" not in date_html


def test_drilldown_picks_the_newest_open_night(restricted_db, tmp_path):
    """Regression for the pre-existing bug where ``_drilldown_urls`` indexed
    ``dates[-1]`` (oldest) despite its own docstring promising the newest."""
    out = tmp_path / "site"
    build_site(out, db_path=restricted_db, n_examples=1, include_figures=False, log=lambda _m: None)

    assert (out / "muscat3" / "260202" / "index.html").is_file()
    assert not (out / "muscat3" / "260201").exists()


def test_fully_restricted_instrument_is_skipped_entirely(restricted_db, tmp_path):
    out = tmp_path / "site"
    build_site(out, db_path=restricted_db, n_examples=1, include_figures=False, log=lambda _m: None)

    assert not (out / "muscat4").exists()


def test_target_example_page_skips_restricted_candidates(restricted_db, tmp_path):
    """The alphabetically-first candidates for the /target example slot are
    restricted; the build must keep scanning rather than publish nothing."""
    out = tmp_path / "site"
    build_site(out, db_path=restricted_db, n_examples=1, include_figures=False, log=lambda _m: None)

    target_pages = [p for p in out.rglob("index.html") if p.parent.parent.name == "target"]
    assert target_pages, "expected at least one /target example page"
    for page in target_pages:
        assert "restricted" not in page.parent.name.lower()
        html = _read(page)
        assert "AAARestricted" not in html


def test_unrestricted_build_is_unaffected(tmp_path):
    """No restricted_proposals rows at all -> identical to pre-PR2 behavior:
    nothing is filtered."""
    db = tmp_path / "muscat.db"
    conn = sqlite3.connect(str(db))
    conn.create_aggregate("coord_repr", 2, CoordRepr)
    conn.executescript(SCHEMA)
    _insert_frames(conn, [
        ("sinistro", "260101", 0, "SIN0_2601010001", "OpenOnly", 1.0, ""),
        ("sinistro", "260101", 0, "SIN0_2601010002", "OpenOnly", 2.0, ""),
    ])
    rows = _summary_rows(conn)
    _insert_summary_rows(conn, rows)
    _populate_targets(conn)
    conn.execute(
        "INSERT OR REPLACE INTO db_meta (key, value) VALUES ('last_build_at', '1700000000')"
    )
    conn.commit()
    conn.close()

    out = tmp_path / "site"
    build_site(out, db_path=str(db), n_examples=1, include_figures=False, log=lambda _m: None)

    assert "OpenOnly" in _read(out / "targets" / "index.html")
    assert (out / "sinistro" / "260101" / "index.html").is_file()


def test_jobs_page_omits_job_for_restricted_target(restricted_db, tmp_path, monkeypatch):
    monkeypatch.setenv("MUSCAT_DB_PATH", restricted_db)
    from muscat_db.job_store import get_job_store

    store = get_job_store()
    store.save(
        type_="photometry", inst="sinistro", date="260102", target="AAARestricted",
        state="done", returncode=0, elapsed=1, started_at=100.0,
        run_type="full", run_id="default", run_name="default", user_name="jerome",
    )
    store.save(
        type_="photometry", inst="sinistro", date="260101", target="OpenA",
        state="done", returncode=0, elapsed=1, started_at=100.0,
        run_type="full", run_id="default", run_name="default", user_name="jerome",
    )

    out = tmp_path / "site"
    build_site(out, db_path=restricted_db, n_examples=1, include_figures=False, log=lambda _m: None)

    jobs_html = _read(out / "jobs" / "index.html")
    assert "AAARestricted" not in jobs_html
    assert "OpenA" in jobs_html


def test_mixed_night_hides_only_the_restricted_row(tmp_path):
    """Same instrument/date/ccd carries both a restricted and an open object;
    the published ccd page must drop only the restricted row, not the whole
    night (row-level filtering, not night-level)."""
    db = tmp_path / "muscat.db"
    conn = sqlite3.connect(str(db))
    conn.create_aggregate("coord_repr", 2, CoordRepr)
    conn.executescript(SCHEMA)
    _insert_frames(conn, [
        ("sbig", "260401", 0, "SBIG0_2604010001", "OpenShared", 1.0, _OPEN_PROPOSAL),
        ("sbig", "260401", 0, "SBIG0_2604010002", "RestrictedShared", 2.0, _RESTRICTED_PROPOSAL),
    ])
    rows = _summary_rows(conn)
    _insert_summary_rows(conn, rows)
    _populate_targets(conn)
    conn.execute(
        "INSERT INTO restricted_proposals (proposal_id, description) VALUES (?, '')",
        (_RESTRICTED_PROPOSAL,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO db_meta (key, value) VALUES ('last_build_at', '1700000000')"
    )
    conn.commit()
    conn.close()

    out = tmp_path / "site"
    build_site(out, db_path=str(db), n_examples=1, include_figures=False, log=lambda _m: None)

    assert (out / "sbig" / "260401" / "index.html").is_file()
    date_html = _read(out / "sbig" / "260401" / "index.html")
    assert "OpenShared" in date_html
    assert "RestrictedShared" not in date_html

    ccd_page = out / "sbig" / "260401" / "ccd0" / "index.html"
    if ccd_page.is_file():
        ccd_html = _read(ccd_page)
        assert "RestrictedShared" not in ccd_html


def test_keep_notes_does_not_bypass_restriction(restricted_db, tmp_path):
    """scrub_notes=False (--keep-notes) is a note-privacy debug convenience,
    not an access-control bypass: restricted data must stay hidden."""
    out = tmp_path / "site"
    build_site(
        out, db_path=restricted_db, scrub_notes=False, n_examples=1,
        include_figures=False, log=lambda _m: None,
    )

    assert "AAARestricted" not in _all_html(out)
