"""The nightly looks back over recent dates for late or partial transfers (#500).

``scan-yesterday`` runs while raw frames are still arriving, so a night can be
scanned half-delivered. ``rescan_recent`` revisits the dates before yesterday:
once unconditionally, then again whenever the raw directory's ctime changes.
The transfer preserves mtimes, so only the ctime shows that something arrived.
"""

from __future__ import annotations

import csv
import datetime
import os
import time

import pytest

from muscat_db import scan_failures, scanner, sweep
from muscat_db.instruments import INSTRUMENTS

TODAY = datetime.date(2026, 11, 22)
RECENT = "261119"  # three days before TODAY


@pytest.fixture
def env(tmp_path, monkeypatch):
    obslog = tmp_path / "obslog"
    obslog.mkdir()
    monkeypatch.setenv("MUSCAT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(scanner, "OBSLOG_BASE", str(obslog))
    monkeypatch.setattr(sweep, "OBSLOG_BASE", str(obslog))
    monkeypatch.setattr(sweep, "known_issues", lambda: {})
    # Header parsing is not under test: every frame yields a valid row.
    monkeypatch.setattr(
        scanner, "_process_single_file",
        lambda fp, inst: {"FRAME": os.path.basename(fp).removesuffix(".fits"), "OBJECT": "TOI-1"},
    )
    return obslog


def _raw_dir(obsdate: str = RECENT) -> str:
    return os.path.join(INSTRUMENTS["muscat2"].data_dir, obsdate)


def _arrive(*numbers: int, obsdate: str = RECENT, ccd: int = 0) -> None:
    """Deliver frames the way the transfer does: the directory keeps an old mtime."""
    # File-system timestamps are coarse; keep successive deliveries distinct.
    time.sleep(0.05)
    directory = _raw_dir(obsdate)
    os.makedirs(directory, exist_ok=True)
    for n in numbers:
        open(os.path.join(directory, f"MCT2{ccd}_{obsdate}{n:04d}.fits"), "w").close()
    os.utime(directory, (0, 0))


def _rows(obslog, obsdate: str = RECENT, ccd: int = 0) -> int:
    path = obslog / "muscat2" / obsdate / f"obslog-muscat2-{obsdate}-ccd{ccd}.csv"
    with open(path, newline="") as f:
        return sum(1 for _ in csv.DictReader(f))


def _run():
    return sweep.rescan_recent(max_workers=1, today=TODAY)


def test_partial_obslog_is_completed(env):
    _arrive(1)
    scanner.scan_date("muscat2", RECENT, max_workers=1)
    _arrive(2, 3)

    result = _run()

    assert result.scanned == {"muscat2": [RECENT]}
    assert _rows(env) == 3


def test_late_delivery_with_no_obslog_is_scanned(env):
    _arrive(1)

    _run()

    assert _rows(env) == 1


def test_unchanged_date_is_not_rescanned(env):
    _arrive(1)
    _run()

    result = _run()

    assert result.scanned == {}
    assert result.unchanged == 1


def test_arrival_is_noticed_although_the_directory_mtime_is_preserved(env):
    _arrive(1)
    _run()
    _arrive(2)

    _run()

    assert _rows(env) == 2


def test_frame_arriving_between_listing_and_csv_write_is_picked_up_next_time(env, monkeypatch):
    _arrive(1)
    parse = scanner._process_single_file

    def parse_while_transfer_continues(fp, inst):
        _arrive(2)
        return parse(fp, inst)

    monkeypatch.setattr(scanner, "_process_single_file", parse_while_transfer_continues)
    _run()
    assert _rows(env) == 1
    monkeypatch.setattr(scanner, "_process_single_file", parse)

    _run()

    assert _rows(env) == 2


def test_rescan_that_would_lose_rows_is_held(env):
    _arrive(1, 2)
    scanner.scan_date("muscat2", RECENT, max_workers=1)
    os.remove(os.path.join(_raw_dir(), f"MCT20_{RECENT}0002.fits"))

    result = _run()

    assert [(i, d) for i, d, _ in result.held] == [("muscat2", RECENT)]
    assert _rows(env) == 2


def test_failed_scan_is_retried_although_nothing_changed(env, monkeypatch):
    _arrive(1)
    real = sweep.scan_date

    def broken(*args, **kwargs):
        raise OSError("stale NFS handle")

    monkeypatch.setattr(sweep, "scan_date", broken)
    assert _run().failed == [("muscat2", RECENT)]
    monkeypatch.setattr(sweep, "scan_date", real)

    assert _run().scanned == {"muscat2": [RECENT]}


def test_partly_written_scan_is_retried(env, monkeypatch):
    _arrive(1)
    monkeypatch.setattr(
        sweep, "scan_date",
        lambda *a, **kw: {"total": 1, "per_ccd": {0: 1}, "write_errors": ["cannot write ccd1"]},
    )
    assert _run().failed == [("muscat2", RECENT)]

    assert _run().failed == [("muscat2", RECENT)]


@pytest.mark.parametrize(
    "obsdate,expected",
    [("261121", False), ("261120", True), ("261115", True), ("261114", False)],
)
def test_window_is_the_seven_dates_ending_yesterday_minus_yesterday(env, obsdate, expected):
    """Yesterday (261121) belongs to the plain nightly scan."""
    _arrive(1, obsdate=obsdate)

    assert bool(_run().scanned) is expected


def test_nightly_command_looks_back_and_reports(env, monkeypatch):
    from typer.testing import CliRunner

    from muscat_db.cli import app

    fake_date = type("D", (datetime.date,), {"today": classmethod(lambda cls: TODAY)})
    monkeypatch.setattr(scanner, "date", fake_date)
    monkeypatch.setattr(sweep, "date", fake_date)
    _arrive(1)

    r = CliRunner().invoke(app, ["scan-yesterday", "-w", "1"], env={"NO_COLOR": "1", "COLUMNS": "200"})

    assert r.exit_code == 0, r.output
    assert f"muscat2 {RECENT}" in r.output
    assert _rows(env) == 1
    assert scan_failures.pending(str(env)) == []


def test_nightly_command_still_exits_zero_when_the_lookback_breaks(env, monkeypatch):
    """The cron chains ``scan-yesterday && build-db``."""
    from typer.testing import CliRunner

    from muscat_db.cli import app

    def broken(*args, **kwargs):
        raise RuntimeError("lookback broke")

    monkeypatch.setattr(sweep, "known_issues", broken)

    r = CliRunner().invoke(app, ["scan-yesterday"], env={"NO_COLOR": "1", "COLUMNS": "200"})

    assert r.exit_code == 0, r.output
    assert "lookback" in r.output.lower()
