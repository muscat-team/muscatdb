from __future__ import annotations

from muscat_db import scanner


def test_scan_missing_dates_skips_non_date_directories(tmp_path, monkeypatch):
    """scan_missing_dates() must only treat canonical YYMMDD directories as
    scannable dates. Legacy/adjacent directories that live alongside real date
    dirs in the raw data tree (calibration copies, monitoring campaigns, org
    notes, ...) have no obslog counterpart either, so without a date-format
    check they get misread as "missing dates" and scanned in their place."""
    data_dir = tmp_path / "MuSCAT"
    for name in ("250101", "250101_calibrated", "dummy", "230711.org", "TIC1445_monitoring_zs_2025"):
        (data_dir / name).mkdir(parents=True)
    obslog_dir = tmp_path / "obslog" / "muscat"
    obslog_dir.mkdir(parents=True)

    monkeypatch.setenv("MUSCAT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(scanner, "OBSLOG_BASE", str(tmp_path / "obslog"))

    scanned_dates: list[str] = []
    monkeypatch.setattr(
        scanner, "scan_date",
        lambda inst_name, obsdate, max_workers=None, progress=None: scanned_dates.append(obsdate),
    )

    result = scanner.scan_missing_dates("muscat", "all", max_workers=1, progress=None)

    assert result == ["250101"]
    assert scanned_dates == ["250101"]
