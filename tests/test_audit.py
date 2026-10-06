"""Raw-vs-db ingestion audit (issue #196, part 3).

The #157 gaps were found by hand, years late. ``audit`` compares, per
instrument/date/CCD, the raw files ``scan_date`` would pick up with the
``frames`` rows in ``muscat.db``. It only reads and reports: dates where a
rescan would destroy good rows (#197, #198) are listed, never acted on.
"""

from __future__ import annotations

import datetime
import json
import sqlite3

import pytest

from muscat_db import audit
from muscat_db.database import SCHEMA

TODAY = datetime.date(2026, 10, 6)


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    obslog = tmp_path / "obslog"
    obslog.mkdir()
    monkeypatch.setenv("MUSCAT_DATA_DIR", str(data))
    monkeypatch.setattr(audit, "OBSLOG_BASE", str(obslog))
    db = tmp_path / "muscat.db"
    with sqlite3.connect(db) as c:
        c.executescript(SCHEMA)
    return data, obslog, db


def _raw(data, subdir: str, obsdate: str, names: list[str]) -> None:
    d = data / subdir / obsdate
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_bytes(b"")


def _rows(db, inst: str, obsdate: str, ccd: int, n: int) -> None:
    with sqlite3.connect(db) as c:
        c.executemany(
            "INSERT INTO frames(instrument, obsdate, ccd, filename) VALUES (?, ?, ?, ?)",
            [(inst, obsdate, ccd, f"{inst}-{obsdate}-{ccd}-{i}") for i in range(n)],
        )


def _by_key(found):
    return {(m.instrument, m.obsdate): m for m in found}


# -- comparison ---------------------------------------------------------------


def test_matching_raw_and_db_reports_nothing(env):
    data, _, db = env
    _raw(data, "MuSCAT", "260101", ["MSCT0_2601010001.fits", "MSCT1_2601010001.fits"])
    _rows(db, "muscat", "260101", 0, 1)
    _rows(db, "muscat", "260101", 1, 1)

    assert audit.run_audit(str(db), today=TODAY, instruments=["muscat"]) == []


def test_ingestion_gap_is_reported_per_ccd(env):
    data, _, db = env
    # muscat has 3 CCDs; CCD2's frames never made it into the db.
    _raw(data, "MuSCAT", "260101", [f"MSCT{c}_26010100{i}.fits" for c in range(3) for i in range(3)])
    for ccd in (0, 1):
        _rows(db, "muscat", "260101", ccd, 3)

    (m,) = audit.run_audit(str(db), today=TODAY, instruments=["muscat"])

    assert (m.instrument, m.obsdate, m.raw, m.db) == ("muscat", "260101", (3, 3, 3), (3, 3, 0))
    assert m.kind == "missing"


def test_uses_scan_date_file_matching_not_a_bare_file_count(env):
    """A bare count produced false mismatches in #157: muscat3 matches only
    ``*e91.fits`` for its epoch names, so raw e00 frames, compressed copies and
    other cameras' files in the same directory must not count."""
    data, _, db = env
    _raw(data, "MuSCAT3", "260101", [
        "ogg2m001-ep02-20260101-0001-e91.fits",
        "ogg2m001-ep02-20260101-0001-e00.fits",
        "ogg2m001-ep02-20260101-0002-e91.fits.fz",
        "ogg2m001-kb09-20260101-0001-e91.fits",
    ])
    _rows(db, "muscat3", "260101", 0, 1)

    assert audit.run_audit(str(db), today=TODAY, instruments=["muscat3"]) == []


def test_more_rows_than_raw_files_is_reported_as_extra(env):
    """The #197 shape: a rescan would replace good rows with fewer or none."""
    data, _, db = env
    _raw(data, "MuSCAT", "260101", ["MSCT0_2601010001.fits"])
    _rows(db, "muscat", "260101", 0, 5)

    (m,) = audit.run_audit(str(db), today=TODAY, instruments=["muscat"])

    assert m.kind == "extra"


def test_db_date_with_no_raw_directory_is_reported_as_gone(env):
    """The #198 shape: rows survive for a date directory that no longer exists."""
    _, _, db = env
    _rows(db, "muscat", "220309", 0, 4)

    (m,) = audit.run_audit(str(db), today=TODAY, instruments=["muscat"])

    assert (m.kind, m.raw, m.db) == ("gone", (0, 0, 0), (4, 0, 0))


def test_recent_dates_are_skipped_until_delivery_settles(env):
    data, _, db = env
    _raw(data, "MuSCAT", "261005", ["MSCT0_2610050001.fits"])  # yesterday, not yet ingested
    _raw(data, "MuSCAT", "260101", ["MSCT0_2601010001.fits"])

    found = audit.run_audit(str(db), today=TODAY, instruments=["muscat"], min_age_days=3)

    assert set(_by_key(found)) == {("muscat", "260101")}


def test_non_date_directories_are_ignored(env):
    data, _, db = env
    _raw(data, "MuSCAT", "Hyades", ["MSCT0_x.fits"])
    _raw(data, "MuSCAT", "csv_old_220914", ["MSCT0_x.fits"])

    assert audit.run_audit(str(db), today=TODAY, instruments=["muscat"]) == []


def test_known_issue_dates_are_tagged(env):
    data, _, db = env
    _raw(data, "MuSCAT3", "210110", ["ogg2m001-ep02-20210110-0001-e91.fits"])
    _rows(db, "muscat3", "210110", 0, 7)

    (m,) = audit.run_audit(str(db), today=TODAY, instruments=["muscat3"])

    assert m.known == "#197"


def test_known_issues_cover_every_date_in_197_and_198():
    known = audit.known_issues()
    assert {d for (i, d), ref in known.items() if ref == "#197"} == {
        "210110", "210127", "210210", "231111", "210408", "260723"}
    assert {(i, d) for (i, d), ref in known.items() if ref == "#198"} == {
        ("muscat3", "220309"), ("muscat3", "251111"), ("muscat3", "250722"),
        ("muscat3", "260716"), ("sinistro", "260722")}


def test_reads_the_database_read_only(env):
    data, _, db = env
    before = db.read_bytes()
    _raw(data, "MuSCAT", "260101", ["MSCT0_2601010001.fits"])

    audit.run_audit(str(db), today=TODAY, instruments=["muscat"])

    assert db.read_bytes() == before
    assert not (db.parent / "muscat.db-wal").exists()


# -- alerting -----------------------------------------------------------------


def _m(inst="muscat", obsdate="260101", raw=(3, 0), db=(2, 0), kind="missing", known=None):
    return audit.Mismatch(inst, obsdate, raw, db, kind, known)


def test_alert_lists_only_mismatches_new_since_the_last_run(env, monkeypatch):
    _, obslog, _ = env
    sent: list[str] = []
    monkeypatch.setattr(audit, "post_slack", lambda text: sent.append(text) or True)

    audit.report([_m()], today=TODAY)
    assert len(sent) == 1 and "muscat 260101" in sent[0]

    audit.report([_m()], today=TODAY)  # unchanged: stays quiet
    assert len(sent) == 1

    audit.report([_m(), _m(obsdate="260202")], today=TODAY)
    assert len(sent) == 2 and "260202" in sent[1] and "260101" not in sent[1]

    audit.report([_m(raw=(5, 0)), _m(obsdate="260202")], today=TODAY)  # counts changed
    assert len(sent) == 3 and "260101" in sent[2]


def test_known_issues_never_alert_but_stay_in_the_report(env, monkeypatch, capsys):
    sent: list[str] = []
    monkeypatch.setattr(audit, "post_slack", lambda text: sent.append(text) or True)

    audit.report([_m(inst="muscat3", obsdate="210110", kind="extra", known="#197")], today=TODAY)

    assert sent == []
    assert "210110" in capsys.readouterr().out


def test_state_is_saved_even_when_slack_is_unreachable(env, monkeypatch):
    """Otherwise every later run would re-alert the same mismatches."""
    _, obslog, _ = env
    monkeypatch.setattr(audit, "post_slack", lambda text: False)

    audit.report([_m()], today=TODAY)

    state = json.loads((obslog / audit.STATE_NAME).read_text())
    assert "muscat 260101" in state["mismatches"]


def test_post_slack_without_a_webhook_file_warns_and_returns_false(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("SLACK_WEBHOOK_FILE", str(tmp_path / "missing"))

    assert audit.post_slack("hello") is False
    assert any("webhook" in r.getMessage() for r in caplog.records)


def test_open_scan_failures_are_part_of_the_report(env, monkeypatch, capsys):
    from muscat_db import scan_failures

    _, obslog, _ = env
    scan_failures.record(str(obslog), "muscat2", "250310", "OSError: boom")
    monkeypatch.setattr(audit, "post_slack", lambda text: True)

    audit.report([], today=TODAY)

    out = capsys.readouterr().out
    assert "muscat2 250310" in out and "OSError: boom" in out
