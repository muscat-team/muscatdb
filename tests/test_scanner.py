from __future__ import annotations

import csv

from muscat_db import scanner

_MUSCAT_HEADER = ["FRAME", "OBJECT", "JD-STRT", "UT-STRT", "EXPTIME (s)", "READ_MODE",
                   "FILTER", "RA", "DEC", "SECZ", "FOCUS (mm)", "PA (deg)"]


def _write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_MUSCAT_HEADER)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _good_row(frame="MSCT0001"):
    return {"FRAME": frame, "OBJECT": "TOI-1234", "JD-STRT": "9000.123456",
             "UT-STRT": "10:00:00", "EXPTIME (s)": "30", "READ_MODE": "high",
             "FILTER": "g", "RA": "10:00:00", "DEC": "+20:00:00", "SECZ": "1.2",
             "FOCUS (mm)": "12.5", "PA (deg)": "0.0"}


def _corrupt_header_row(frame="MSCT0002"):
    """A row written when the FITS header couldn't be parsed -- see
    _read_fits_header_raw's corrupt/truncated-header fallback, which returns
    blank values for every requested key."""
    return {"FRAME": frame, "OBJECT": "", "JD-STRT": "-49999.500000",
             "UT-STRT": "", "EXPTIME (s)": "", "READ_MODE": "", "FILTER": "",
             "RA": "", "DEC": "", "SECZ": "", "FOCUS (mm)": "", "PA (deg)": ""}


def _setup(tmp_path, monkeypatch):
    data_dir = tmp_path / "MuSCAT"
    obslog_dir = tmp_path / "obslog" / "muscat"
    obslog_dir.mkdir(parents=True)
    monkeypatch.setenv("MUSCAT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(scanner, "OBSLOG_BASE", str(tmp_path / "obslog"))
    scanned_dates: list[str] = []
    monkeypatch.setattr(
        scanner, "scan_date",
        lambda inst_name, obsdate, max_workers=None, progress=None: scanned_dates.append(obsdate),
    )
    return data_dir, obslog_dir, scanned_dates


def test_scan_missing_dates_rescans_empty_marker_directory(tmp_path, monkeypatch):
    """A date dir created (by a killed/crashed scan) with zero CSVs inside is
    indistinguishable from an untouched raw date, and must be retried -- this
    is #157's root cause."""
    data_dir, obslog_dir, scanned_dates = _setup(tmp_path, monkeypatch)
    (data_dir / "250101").mkdir(parents=True)
    (obslog_dir / "250101").mkdir()  # marker dir exists, no CSV inside

    result = scanner.scan_missing_dates("muscat", "all", max_workers=1, progress=None)

    assert result == ["250101"]
    assert scanned_dates == ["250101"]


def test_scan_missing_dates_rescans_csv_with_no_data_rows(tmp_path, monkeypatch):
    """A CSV with only a header (scan killed before any row was written) must
    be retried, not treated as a completed scan."""
    data_dir, obslog_dir, scanned_dates = _setup(tmp_path, monkeypatch)
    (data_dir / "250101").mkdir(parents=True)
    _write_csv(obslog_dir / "250101" / "obslog-muscat-250101-ccd0.csv", [])

    result = scanner.scan_missing_dates("muscat", "all", max_workers=1, progress=None)

    assert result == ["250101"]
    assert scanned_dates == ["250101"]


def test_scan_missing_dates_rescans_csv_with_malformed_target_name(tmp_path, monkeypatch):
    """A row with a blank OBJECT means its FITS header couldn't be parsed --
    the same signal as an empty CSV, just discovered a layer deeper."""
    data_dir, obslog_dir, scanned_dates = _setup(tmp_path, monkeypatch)
    (data_dir / "250101").mkdir(parents=True)
    _write_csv(
        obslog_dir / "250101" / "obslog-muscat-250101-ccd0.csv",
        [_good_row(), _corrupt_header_row()],
    )

    result = scanner.scan_missing_dates("muscat", "all", max_workers=1, progress=None)

    assert result == ["250101"]
    assert scanned_dates == ["250101"]


def test_scan_missing_dates_leaves_complete_csvs_alone(tmp_path, monkeypatch):
    """A date whose CSVs all have rows with real target names is genuinely
    done and must not be rescanned."""
    data_dir, obslog_dir, scanned_dates = _setup(tmp_path, monkeypatch)
    (data_dir / "250101").mkdir(parents=True)
    _write_csv(
        obslog_dir / "250101" / "obslog-muscat-250101-ccd0.csv",
        [_good_row("MSCT0001"), _good_row("MSCT0002")],
    )

    result = scanner.scan_missing_dates("muscat", "all", max_workers=1, progress=None)

    assert result == []
    assert scanned_dates == []


def test_scan_missing_dates_skips_non_date_directories(tmp_path, monkeypatch):
    """scan_missing_dates() must only treat canonical YYMMDD directories as
    scannable dates. Legacy/adjacent directories that live alongside real date
    dirs in the raw data tree (calibration copies, monitoring campaigns, org
    notes, ...) have no obslog counterpart either, so without a date-format
    check they get misread as "missing dates" and scanned in their place."""
    data_dir, obslog_dir, scanned_dates = _setup(tmp_path, monkeypatch)
    for name in ("250101", "250101_calibrated", "dummy", "230711.org", "TIC1445_monitoring_zs_2025"):
        (data_dir / name).mkdir(parents=True)

    result = scanner.scan_missing_dates("muscat", "all", max_workers=1, progress=None)

    assert result == ["250101"]
    assert scanned_dates == ["250101"]
