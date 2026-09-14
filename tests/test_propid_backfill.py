"""Tests for the PROPID backfill tool (issue #144 PR3).

Exercises the real scan_date -> ingest_date pipeline against tiny mock FITS
files (never the production muscat.db), so the backfill is proven against
the actual code path an operator running ``muscat-db backfill-propid``
would hit, not a synthetic shortcut.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile

import pytest
from astropy.io import fits

from muscat_db.coord import CoordRepr
from muscat_db.database import (
    SCHEMA,
    _insert_summary_rows,
    _replace_target_rows,
    _summary_rows,
)
from muscat_db.instruments import INSTRUMENTS
from muscat_db.propid_backfill import (
    PROPID_INSTRUMENTS,
    backfill_propid_for_instrument,
)

# Modules holding a module-level reference to OBSLOG_BASE / INSTRUMENTS that
# scan_date/ingest_date read at call time -- mirrors test_main.py's own list,
# scoped down to what this module's functions actually touch.
_OBSLOG_MODULES = ["muscat_db.instruments", "muscat_db.scanner", "muscat_db.database"]


@pytest.fixture
def tmp_obslog(monkeypatch):
    td = tempfile.mkdtemp()
    for m in _OBSLOG_MODULES:
        monkeypatch.setattr(f"{m}.OBSLOG_BASE", td)
    yield td
    shutil.rmtree(td, ignore_errors=True)


@pytest.fixture
def tmp_data(monkeypatch):
    from dataclasses import replace
    td = tempfile.mkdtemp()
    patched = {name: replace(cfg, data_subdir=f"{td}/{name}") for name, cfg in INSTRUMENTS.items()}
    for m in _OBSLOG_MODULES:
        monkeypatch.setattr(f"{m}.INSTRUMENTS", patched)
    yield td
    shutil.rmtree(td, ignore_errors=True)


@pytest.fixture
def tmp_home_temp(monkeypatch, tmp_path):
    """Isolate the backfill checkpoint dir from the real $HOME/temp."""
    d = tmp_path / "temp"
    d.mkdir()
    monkeypatch.setenv("MUSCAT_TMPDIR", str(d))
    return d


def _make_fits(path: str, header: dict) -> str:
    hdu = fits.PrimaryHDU()
    for k, v in header.items():
        hdu.header[k] = v
    hdu.writeto(path, overwrite=True)
    return path


def _write_lco_frame(tmp_data: str, inst_name: str, obsdate: str, frame_num: int,
                      object_name: str, propid: str) -> str:
    """Write one single-CCD LCO-instrument FITS file (sinistro/sbig/qhy600
    all share this filename shape: ``*e91.fits``, no epoch/prefix tokens)."""
    ddir = f"{tmp_data}/{inst_name}/{obsdate}"
    os.makedirs(ddir, exist_ok=True)
    path = f"{ddir}/{inst_name}-{obsdate}-{frame_num:04d}-e91.fits"
    _make_fits(path, {
        "OBJECT": object_name,
        "EXPTIME": 10.0,
        "FILTER": "gp",
        "RA": "12:00:00",
        "DEC": "+00:00:00",
        "MJD-OBS": 60000.0 + frame_num / 1440,
        "UTSTART": f"{frame_num:02d}:00:00",
        "CONFMODE": "high",
        "FOCPOSN": 0.0,
        "AIRMASS": 1.2,
        "PROPID": propid,
    })
    return path


def _seed_pre_propid_frame(db_path: str, inst_name: str, obsdate: str, object_name: str) -> None:
    """Insert a frame/summary/target row the way build_db would have, before
    PR1 ever captured PROPID -- i.e. proposal_id stays at its schema default
    of ''. Mirrors test_static_site.py's tiny-DB pattern."""
    conn = sqlite3.connect(db_path)
    conn.create_aggregate("coord_repr", 2, CoordRepr)
    conn.executescript(SCHEMA)
    conn.execute(
        """INSERT INTO frames
           (instrument, obsdate, ccd, filename, object, jd_start, ut_start,
            exptime, read_mode, filter, ra, declination, airmass, focus, pa)
           VALUES (?, ?, 0, ?, ?, 1.0, '00:00:00', 10, 'high', 'gp', '', '', 1.2, 0, 0)""",
        (inst_name, obsdate, f"{inst_name}-{obsdate}-0001-e91", object_name),
    )
    rows = _summary_rows(conn, instrument=inst_name, obsdate=obsdate)
    _insert_summary_rows(conn, rows)
    # Seed incrementally (per-object), matching ingest_date -- not the full
    # _populate_targets rebuild, which re-inserts every aggregated object and
    # collides when a later date adds a second object to the same DB.
    _replace_target_rows(conn, {object_name})
    conn.commit()
    conn.close()


def _proposal_ids(db_path: str, inst_name: str, obsdate: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [
            r[0] for r in conn.execute(
                "SELECT proposal_id FROM frames WHERE instrument = ? AND obsdate = ?",
                (inst_name, obsdate),
            ).fetchall()
        ]
    finally:
        conn.close()


def test_propid_instruments_matches_the_five_lco_instruments():
    assert set(PROPID_INSTRUMENTS) == {"muscat3", "muscat4", "sinistro", "sbig", "qhy600"}


def test_rejects_an_instrument_with_no_propid_capture(tmp_path):
    with pytest.raises(ValueError, match="muscat"):
        backfill_propid_for_instrument("muscat", db_path=str(tmp_path / "muscat.db"))


def test_backfill_updates_proposal_id_from_rescanned_fits(
    tmp_obslog, tmp_data, tmp_home_temp, tmp_path,
):
    db_path = str(tmp_path / "muscat.db")
    _seed_pre_propid_frame(db_path, "sinistro", "260101", "OpenA")
    assert _proposal_ids(db_path, "sinistro", "260101") == [""]

    _write_lco_frame(tmp_data, "sinistro", "260101", 1, "OpenA", "KEY2026B-001")

    stats = backfill_propid_for_instrument("sinistro", db_path=db_path, sleep_s=0)

    assert stats.dates_done == 1
    assert not stats.dates_failed
    assert not stats.dates_skipped_no_raw_files
    assert stats.integrity_ok_before is True
    assert stats.integrity_ok_after is True
    assert _proposal_ids(db_path, "sinistro", "260101") == ["KEY2026B-001"]


def test_backfill_skips_already_done_dates_on_a_second_run(
    tmp_obslog, tmp_data, tmp_home_temp, tmp_path,
):
    db_path = str(tmp_path / "muscat.db")
    _seed_pre_propid_frame(db_path, "sinistro", "260101", "OpenA")
    _write_lco_frame(tmp_data, "sinistro", "260101", 1, "OpenA", "FIRST-RUN-001")

    backfill_propid_for_instrument("sinistro", db_path=db_path, sleep_s=0)
    assert _proposal_ids(db_path, "sinistro", "260101") == ["FIRST-RUN-001"]

    # Raw file on disk now claims a different PROPID; an un-restarted second
    # run must not touch this already-checkpointed date.
    _write_lco_frame(tmp_data, "sinistro", "260101", 1, "OpenA", "SHOULD-NOT-APPEAR")

    stats = backfill_propid_for_instrument("sinistro", db_path=db_path, sleep_s=0)

    assert stats.dates_done == 0
    assert _proposal_ids(db_path, "sinistro", "260101") == ["FIRST-RUN-001"]


def test_restart_flag_reprocesses_a_checkpointed_date(
    tmp_obslog, tmp_data, tmp_home_temp, tmp_path,
):
    db_path = str(tmp_path / "muscat.db")
    _seed_pre_propid_frame(db_path, "sinistro", "260101", "OpenA")
    _write_lco_frame(tmp_data, "sinistro", "260101", 1, "OpenA", "FIRST-RUN-001")
    backfill_propid_for_instrument("sinistro", db_path=db_path, sleep_s=0)

    _write_lco_frame(tmp_data, "sinistro", "260101", 1, "OpenA", "AFTER-RESTART")

    stats = backfill_propid_for_instrument(
        "sinistro", db_path=db_path, sleep_s=0, restart=True,
    )

    assert stats.dates_done == 1
    assert _proposal_ids(db_path, "sinistro", "260101") == ["AFTER-RESTART"]


def test_date_with_no_raw_files_is_skipped_and_marked_done(
    tmp_obslog, tmp_data, tmp_home_temp, tmp_path, monkeypatch,
):
    db_path = str(tmp_path / "muscat.db")
    _seed_pre_propid_frame(db_path, "sinistro", "260101", "OpenA")
    # Deliberately no matching FITS file written under tmp_data.

    stats = backfill_propid_for_instrument("sinistro", db_path=db_path, sleep_s=0)

    assert stats.dates_skipped_no_raw_files == ["260101"]
    assert stats.dates_done == 1
    assert _proposal_ids(db_path, "sinistro", "260101") == [""]

    # Marked done -> a second run must not re-invoke scan_date for it.
    calls = []
    import muscat_db.scanner as scanner_mod
    monkeypatch.setattr(
        scanner_mod, "scan_date",
        lambda *a, **kw: calls.append(a) or {},
    )
    stats2 = backfill_propid_for_instrument("sinistro", db_path=db_path, sleep_s=0)
    assert stats2.dates_done == 0
    assert calls == []


def test_max_dates_limits_one_run_and_resumes_next(
    tmp_obslog, tmp_data, tmp_home_temp, tmp_path,
):
    db_path = str(tmp_path / "muscat.db")
    _seed_pre_propid_frame(db_path, "sinistro", "260101", "OpenA")
    _seed_pre_propid_frame(db_path, "sinistro", "260102", "OpenB")
    _write_lco_frame(tmp_data, "sinistro", "260101", 1, "OpenA", "PID-A")
    _write_lco_frame(tmp_data, "sinistro", "260102", 1, "OpenB", "PID-B")

    first = backfill_propid_for_instrument(
        "sinistro", db_path=db_path, sleep_s=0, max_dates=1,
    )
    assert first.dates_done == 1

    second = backfill_propid_for_instrument(
        "sinistro", db_path=db_path, sleep_s=0, max_dates=1,
    )
    assert second.dates_done == 1

    assert _proposal_ids(db_path, "sinistro", "260101") == ["PID-A"]
    assert _proposal_ids(db_path, "sinistro", "260102") == ["PID-B"]


def test_dry_run_reports_without_writing_anything(
    tmp_obslog, tmp_data, tmp_home_temp, tmp_path,
):
    db_path = str(tmp_path / "muscat.db")
    _seed_pre_propid_frame(db_path, "sinistro", "260101", "OpenA")
    _write_lco_frame(tmp_data, "sinistro", "260101", 1, "OpenA", "SHOULD-NOT-APPLY")

    stats = backfill_propid_for_instrument(
        "sinistro", db_path=db_path, sleep_s=0, dry_run=True,
    )

    assert stats.dates_done == 1  # "would process" count, not "processed"
    assert _proposal_ids(db_path, "sinistro", "260101") == [""]
    from muscat_db.propid_backfill import _checkpoint_path
    assert not _checkpoint_path("sinistro").exists()


def test_refuses_to_run_against_an_already_corrupt_database(
    tmp_obslog, tmp_data, tmp_home_temp, tmp_path,
):
    db_path = str(tmp_path / "muscat.db")
    _seed_pre_propid_frame(db_path, "sinistro", "260101", "OpenA")

    # Corrupt a page well past the header so integrity_check has something to
    # actually detect (a header-only tweak just fails to open the file).
    with open(db_path, "r+b") as f:
        f.seek(4096)
        f.write(b"\xff" * 256)

    with pytest.raises(RuntimeError, match="integrity_check"):
        backfill_propid_for_instrument("sinistro", db_path=db_path, sleep_s=0)


# ── CLI-level tests: the operator runs `muscat-db backfill-propid`, not the
# function directly, so the command's own wiring (choice validation, --db,
# --dry-run, exit codes) has to be covered too.
def _invoke(*args, **env):
    from typer.testing import CliRunner
    from muscat_db.cli import app
    return CliRunner().invoke(
        app, ["backfill-propid", *args], env={"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200", **env},
    )


def test_cli_dry_run_reports_eagerly_against_an_existing_db(
    tmp_obslog, tmp_data, tmp_home_temp, tmp_path,
):
    db_path = str(tmp_path / "muscat.db")
    _seed_pre_propid_frame(db_path, "sinistro", "260101", "OpenA")

    result = _invoke("sinistro", "--db", db_path, "--dry-run")

    assert result.exit_code == 0, result.output
    assert "1 date(s) would be rescanned" in result.output
    # Nothing was ingested: the frame row still carries the pre-PROPID default.
    assert _proposal_ids(db_path, "sinistro", "260101") == [""]


def test_cli_refuses_a_non_lco_instrument():
    result = _invoke("muscat")
    assert result.exit_code != 0
    assert "muscat3" in result.output  # the choice help lists the valid LCO instruments
    assert "sinistro" in result.output


def test_cli_all_expands_to_every_propid_instrument(
    tmp_obslog, tmp_data, tmp_home_temp, tmp_path,
):
    db_path = str(tmp_path / "muscat.db")
    _seed_pre_propid_frame(db_path, "sinistro", "260101", "OpenA")

    result = _invoke("all", "--db", db_path, "--dry-run")

    assert result.exit_code == 0, result.output
    for inst in PROPID_INSTRUMENTS:
        assert f"Backfilling PROPID for {inst}" in result.output
