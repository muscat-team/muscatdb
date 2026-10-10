"""Scan failures are surfaced and recorded instead of swallowed (issue #196, part 1).

``scan_date_for_all_inst`` -- what the nightly ``scan-yesterday`` runs -- used
to log a failing instrument at DEBUG, which nothing prints, so one transient
error silently dropped that instrument's whole night. Failures now log at
WARNING and land in a ledger (``$OBSLOG_BASE/.scan-failures.jsonl``) that a
later sweep can retry from; a clean rescan clears the entry.
"""

from __future__ import annotations

import datetime
import logging

import pytest

# cli is imported here, before any fixture patches scanner._find_fits_files:
# obsdate_normalize binds that name at import time, so a first import of cli
# from inside a test would keep the fake bound for the rest of the session.
from muscat_db import cli, scan_failures, scanner  # noqa: F401
from muscat_db.instruments import INSTRUMENTS


@pytest.fixture
def obslog(tmp_path, monkeypatch):
    base = tmp_path / "obslog"
    base.mkdir()
    monkeypatch.setattr(scanner, "OBSLOG_BASE", str(base))
    return base


def _row(fname: str) -> dict[str, str]:
    return {"FRAME": fname, "OBJECT": "TOI-1234"}


@pytest.fixture
def fake_files(monkeypatch):
    """Stub the filesystem side of scan_date: ``files[(inst, ccd)]`` lists the
    paths _find_fits_files returns; ``broken`` instruments raise from it."""
    files: dict[tuple[str, int], list[str]] = {}
    broken: set[str] = set()

    def find(inst, obsdate, ccd, data_root=None):
        if inst.name in broken:
            raise OSError(f"stale NFS handle under {inst.name}")
        return files.get((inst.name, ccd), [])

    monkeypatch.setattr(scanner, "_find_fits_files", find)
    monkeypatch.setattr(
        scanner, "_process_single_file",
        lambda fp, inst: _row(fp.rsplit("/", 1)[-1].removesuffix(".fits")),
    )
    return files, broken


def _keys(base) -> set[tuple[str, str]]:
    return {(e["instrument"], e["obsdate"]) for e in scan_failures.pending(str(base))}


# -- scan_date_for_all_inst / scan-yesterday ----------------------------------


def test_failing_instrument_is_logged_at_warning_and_recorded(obslog, fake_files, caplog):
    files, broken = fake_files
    files[("muscat", 0)] = ["/data/MuSCAT/260514/MSCT0_2605140001.fits"]
    broken.add("muscat2")

    with caplog.at_level(logging.WARNING, logger="muscat_db.scanner"):
        scanned = scanner.scan_date_for_all_inst("260514", max_workers=1)

    assert scanned == ["muscat"]  # one failure does not stop the others
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("muscat2" in r.getMessage() and "260514" in r.getMessage() for r in warnings)
    assert any(r.exc_info for r in warnings), "traceback must be kept for diagnosis"
    assert _keys(obslog) == {("muscat2", "260514")}
    (entry,) = scan_failures.pending(str(obslog))
    assert "stale NFS handle" in entry["reason"]


def test_scan_yesterday_cli_names_failed_instruments_but_exits_zero(obslog, fake_files, monkeypatch):
    """Exit 0 on purpose: the cron chains ``scan-yesterday && build-db``, and
    one instrument's failure must not also skip every other instrument's rebuild."""
    from typer.testing import CliRunner

    from muscat_db.cli import app

    _, broken = fake_files
    broken.add("sinistro")
    fake_date = type("D", (), {"today": staticmethod(lambda: datetime.date(2026, 5, 15))})
    monkeypatch.setattr(scanner, "date", fake_date)

    r = CliRunner().invoke(app, ["scan-yesterday"], env={"NO_COLOR": "1", "COLUMNS": "200"})

    assert r.exit_code == 0, r.output
    assert "sinistro" in r.output and "failed" in r.output.lower()
    assert _keys(obslog) == {("sinistro", "260514")}


# -- scan_date: record on failure, clear on success ---------------------------


def test_scan_date_records_failure_from_any_caller_and_reraises(obslog, fake_files):
    _, broken = fake_files
    broken.add("muscat3")

    with pytest.raises(OSError):
        scanner.scan_date("muscat3", "260101", max_workers=1)

    assert _keys(obslog) == {("muscat3", "260101")}


def test_clean_rescan_clears_the_failure(obslog, fake_files):
    files, broken = fake_files
    broken.add("muscat")
    with pytest.raises(OSError):
        scanner.scan_date("muscat", "260101", max_workers=1)
    assert _keys(obslog) == {("muscat", "260101")}  # else the clear below proves nothing
    broken.clear()
    files[("muscat", 0)] = ["/d/MSCT0_2601010001.fits"]

    assert scanner.scan_date("muscat", "260101", max_workers=1)

    assert _keys(obslog) == set()


def test_repeated_failures_keep_one_entry_and_count_attempts(obslog, fake_files):
    _, broken = fake_files
    broken.add("muscat")
    for _ in range(3):
        with pytest.raises(OSError):
            scanner.scan_date("muscat", "260101", max_workers=1)

    (entry,) = scan_failures.pending(str(obslog))
    assert entry["attempts"] == 3
    assert entry["first_failed"] <= entry["last_failed"]


def test_partial_csv_write_is_recorded_not_silently_kept(obslog, fake_files):
    """A CCD whose CSV cannot be written leaves the date with the other CCDs'
    CSVs, which _obsdate_dir_is_complete accepts -- so scan-missing would never
    retry it. That has to land in the ledger."""
    files, _ = fake_files
    files[("muscat", 0)] = ["/d/MSCT0_2601010001.fits"]
    files[("muscat", 1)] = ["/d/MSCT1_2601010001.fits"]
    # A directory where CCD1's CSV should go makes open(..., "w") fail.
    (obslog / "muscat" / "260101" / "obslog-muscat-260101-ccd1.csv").mkdir(parents=True)

    scanner.scan_date("muscat", "260101", max_workers=1)

    assert (obslog / "muscat" / "260101" / "obslog-muscat-260101-ccd0.csv").is_file()
    (entry,) = scan_failures.pending(str(obslog))
    assert (entry["instrument"], entry["obsdate"]) == ("muscat", "260101")
    assert "ccd1" in entry["reason"]


def test_unwritable_obslog_dir_is_recorded_and_reads_as_no_data(obslog, fake_files, monkeypatch):
    """The date has files but nothing could be written. Callers treat a truthy
    result as "this instrument had data" (and ``muscat-db scan`` indexes
    ``result["per_ccd"]``), so it must still come back falsy."""
    files, _ = fake_files
    files[("muscat", 0)] = ["/d/MSCT0_2601010001.fits"]
    real_makedirs = scanner.os.makedirs

    def deny(path, *a, **k):
        if path.startswith(str(obslog / "muscat")):
            raise PermissionError(13, "Permission denied", path)
        return real_makedirs(path, *a, **k)

    monkeypatch.setattr(scanner.os, "makedirs", deny)

    assert scanner.scan_date("muscat", "260101", max_workers=1) == {}

    (entry,) = scan_failures.pending(str(obslog))
    assert "cannot create" in entry["reason"]


def test_empty_result_leaves_an_existing_failure_alone(obslog, fake_files):
    """Zero files found is not proof the earlier failure is resolved (the date
    directory may be gone, #198), so the entry stays visible."""
    _, broken = fake_files
    broken.add("muscat")
    with pytest.raises(OSError):
        scanner.scan_date("muscat", "260101", max_workers=1)
    broken.clear()

    assert not scanner.scan_date("muscat", "260101", max_workers=1)

    assert _keys(obslog) == {("muscat", "260101")}


# -- the ledger itself ---------------------------------------------------------


def test_pending_is_empty_without_a_ledger(tmp_path):
    assert scan_failures.pending(str(tmp_path)) == []


def test_unwritable_ledger_warns_but_never_breaks_the_scan(obslog, fake_files, monkeypatch, caplog):
    files, _ = fake_files
    files[("muscat", 0)] = ["/d/MSCT0_2601010001.fits"]

    def boom(*a, **k):
        raise PermissionError("read-only obslog mount")

    monkeypatch.setattr(scan_failures, "_rewrite", boom)
    _, broken = fake_files
    broken.add("muscat2")

    with caplog.at_level(logging.WARNING):
        with pytest.raises(OSError, match="stale NFS"):  # the scan's own error, not the ledger's
            scanner.scan_date("muscat2", "260101", max_workers=1)
        assert scanner.scan_date("muscat", "260101", max_workers=1)

    assert any("ledger" in r.getMessage() for r in caplog.records)


def test_corrupt_ledger_line_is_skipped_with_a_warning(tmp_path, caplog):
    ledger = tmp_path / scan_failures.LEDGER_NAME
    ledger.write_text('{"instrument": "muscat", "obsdate": "260101", "attempts": 1}\nnot json\n')

    with caplog.at_level(logging.WARNING):
        entries = scan_failures.pending(str(tmp_path))

    assert [(e["instrument"], e["obsdate"]) for e in entries] == [("muscat", "260101")]
    assert any("not json" in r.getMessage() or "line 2" in r.getMessage() for r in caplog.records)


def test_ledger_is_not_picked_up_as_an_instrument_or_date(obslog, fake_files):
    """It sits at the obslog root, beside the per-instrument directories that
    build_db and scan-missing walk."""
    _, broken = fake_files
    broken.add("muscat")
    with pytest.raises(OSError):
        scanner.scan_date("muscat", "260101", max_workers=1)

    assert (obslog / scan_failures.LEDGER_NAME).is_file()
    assert scan_failures.LEDGER_NAME.startswith(".")
    assert scan_failures.LEDGER_NAME not in INSTRUMENTS


# -- muscat-db scan-failures ---------------------------------------------------


def test_scan_failures_cli_lists_pending_entries(obslog, fake_files):
    from typer.testing import CliRunner

    from muscat_db.cli import app

    _, broken = fake_files
    broken.add("qhy600")
    with pytest.raises(OSError):
        scanner.scan_date("qhy600", "260301", max_workers=1)
    r = CliRunner().invoke(app, ["scan-failures"], env={"NO_COLOR": "1", "COLUMNS": "200"})

    assert r.exit_code == 0, r.output
    assert "qhy600" in r.output and "260301" in r.output and "stale NFS" in r.output
