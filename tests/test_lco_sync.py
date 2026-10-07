from __future__ import annotations

import datetime
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from muscat_db import lco, lco_sync
from muscat_db.cli import app

UTC = datetime.timezone.utc


def _frame(number: int, *, night: str = "20261004", obj: str = "TOI-123", tel: str = "0m463",
           instrume: str = "sq35") -> dict:
    return {
        "id": number,
        "filename": f"ogg{tel}-{instrume}-{night}-{number:04d}-e91.fits.fz",
        "SITEID": "ogg",
        "TELID": tel,
        "INSTRUME": instrume,
        "OBJECT": obj,
        "DATE_OBS": "2026-10-05T12:00:00Z",
        "url": "https://archive-api.lco.global/frames/x.fits.fz",
    }


@pytest.fixture
def roots(tmp_path, monkeypatch):
    data = tmp_path / "data"
    obslog = tmp_path / "obslog"
    data.mkdir()
    obslog.mkdir()
    monkeypatch.setenv("MUSCAT_LCO_DIR", str(data))
    monkeypatch.setattr("muscat_db.instruments.OBSLOG_BASE", str(obslog))
    return data, obslog


def _unpacked(data, frame) -> Path:
    _inst, _date, dest = lco.frame_destination(frame)
    return dest.with_name(dest.name[: -len(".fz")])


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


@pytest.fixture
def fakes(monkeypatch):
    """Archive, download, funpack, scan and ingest stand-ins that record calls."""
    calls = {"pages": [], "downloads": [], "scans": [], "ingests": [], "fail": set()}

    def fake_search(filters, user_name=None):
        calls["filters"] = (filters, user_name)
        frames = calls["pages"]
        return {"count": len(frames), "results": frames, "truncated": False}

    def fake_download(frame, overwrite=False):
        name = frame["filename"]
        calls["downloads"].append(name)
        if name in calls["fail"]:
            return {"filename": name, "status": "error", "error": "HTTP 403"}
        _inst, _date, dest = lco.frame_destination(frame)
        _touch(dest)
        return {"filename": name, "status": "downloaded", "dest": str(dest)}

    def fake_funpack(path):
        _touch(path.with_name(path.name[: -len(".fz")]))
        return {"filename": path.name, "status": "unpacked"}

    def fake_scan(instrument, obsdate, max_workers=None, data_root=None):
        calls["scans"].append((instrument, obsdate, data_root))
        return {"total": 3, "per_ccd": {0: 3}}

    def fake_ingest(db, instrument, obsdate):
        calls["ingests"].append((db, instrument, obsdate))
        return 3

    monkeypatch.setattr("muscat_db.lco.archive_search_all", fake_search)
    monkeypatch.setattr("muscat_db.lco._download_frame_with_retry", fake_download)
    monkeypatch.setattr("muscat_db.lco._funpack_file", fake_funpack)
    monkeypatch.setattr("muscat_db.scanner.scan_date", fake_scan)
    monkeypatch.setattr("muscat_db.database.ingest_date", fake_ingest)
    return calls


def _sync(data, **kwargs):
    defaults = dict(start="2026-09-29 00:00", end="2026-10-06 00:00", data_root=data,
                    db="test.db", log=lambda _m: None)
    return lco_sync.sync_proposal("KEY2026B-001", **{**defaults, **kwargs})


# --- arguments --------------------------------------------------------------

def test_archive_window_defaults_to_lookback_ending_now():
    now = datetime.datetime(2026, 10, 6, 12, 30, tzinfo=UTC)
    assert lco_sync.archive_window(7, now=now) == ("2026-09-29 12:30", "2026-10-06 12:30")


def test_archive_window_explicit_start_overrides_days():
    assert lco_sync.archive_window(7, start="2026-01-01", end="2026-02-01T06:00Z") == (
        "2026-01-01 00:00", "2026-02-01 06:00",
    )


@pytest.mark.parametrize("kwargs", [
    {"days": 0},
    {"days": 7, "start": "yesterday"},
    {"days": 7, "start": "2026-02-01", "end": "2026-01-01"},
])
def test_archive_window_rejects_bad_input(kwargs):
    with pytest.raises(lco_sync.SyncError):
        lco_sync.archive_window(**kwargs)


@pytest.mark.parametrize("bad", ["", "KEY 2026", "../etc", "a/b", "-KEY"])
def test_validate_proposal_id_rejects_malformed(bad):
    with pytest.raises(lco_sync.SyncError):
        lco_sync.validate_proposal_id(bad)


def test_validate_proposal_id_accepts_lco_ids():
    assert lco_sync.validate_proposal_id(" KEY2026B-001 ") == "KEY2026B-001"


# --- archive query ----------------------------------------------------------

def test_query_frames_requests_reduced_science_for_proposal(fakes):
    fakes["pages"] = [_frame(1)]
    count, frames = lco_sync.query_frames("KEY2026B-001", "2026-09-29 00:00", "2026-10-06 00:00", "alice")
    filters, user = fakes["filters"]
    assert (count, len(frames), user) == (1, 1, "alice")
    assert filters["proposal_id"] == "KEY2026B-001"
    assert filters["reduction_level"] == 91
    assert filters["OBSTYPE"] == "EXPOSE"
    assert (filters["start"], filters["end"]) == ("2026-09-29 00:00", "2026-10-06 00:00")


def test_query_frames_refuses_truncated_listing(monkeypatch):
    monkeypatch.setattr(
        "muscat_db.lco.archive_search_all",
        lambda filters, user_name=None: {"count": 20000, "results": [], "truncated": True},
    )
    with pytest.raises(lco_sync.SyncError, match="narrow the window"):
        lco_sync.query_frames("KEY2026B-001", "a", "b")


# --- planning ---------------------------------------------------------------

def test_plan_frames_skips_unpacked_and_engineering_and_reports_unplaceable(roots):
    data, _ = roots
    done, fresh = _frame(1), _frame(2)
    _touch(_unpacked(data, done))
    unplaceable = {**_frame(3), "TELID": "0m4", "INSTRUME": "zz99",
                   "filename": "ogg0m4-zz99-20261004-0003-e91.fits.fz"}
    plan = lco_sync.plan_frames([done, fresh, fresh, _frame(4, obj="auto_focus"), unplaceable])
    assert [p.filename for p in plan.present] == [done["filename"]]
    assert [p.filename for p in plan.to_download] == [fresh["filename"]]
    assert plan.engineering == 1
    assert len(plan.errors) == 1 and "zz99" in plan.errors[0]


def test_plan_frames_requeues_packed_frame_whose_funpack_failed(roots):
    data, _ = roots
    frame = _frame(1)
    _inst, _date, packed = lco.frame_destination(frame)
    _touch(packed)  # .fz on disk, no .fits next to it
    plan = lco_sync.plan_frames([frame])
    assert [p.filename for p in plan.to_download] == [frame["filename"]]
    assert plan.present == ()


# --- full sync --------------------------------------------------------------

def test_sync_downloads_new_frames_then_scans_and_ingests_each_night(roots, fakes):
    data, _ = roots
    fakes["pages"] = [_frame(1), _frame(2), _frame(3, night="20261005")]
    report = _sync(data)
    assert report.ok
    assert sorted(report.downloaded) == sorted(f["filename"] for f in fakes["pages"])
    assert fakes["scans"] == [
        ("qhy600", "261004", str(data)),
        ("qhy600", "261005", str(data)),
    ]
    assert fakes["ingests"] == [("test.db", "qhy600", "261004"), ("test.db", "qhy600", "261005")]


def test_sync_second_run_downloads_nothing_and_skips_scanned_nights(roots, fakes):
    data, obslog = roots
    fakes["pages"] = [_frame(1)]
    _sync(data)
    _touch(obslog / "qhy600" / "261004" / "obslog-qhy600-261004-ccd0.csv")
    fakes["downloads"].clear()
    fakes["scans"].clear()
    report = _sync(data)
    assert report.ok
    assert fakes["downloads"] == []
    assert fakes["scans"] == []


def test_sync_rescans_local_night_that_never_got_an_obslog(roots, fakes):
    data, _ = roots
    frame = _frame(1)
    _touch(_unpacked(data, frame))  # downloaded by a run that died before scanning
    fakes["pages"] = [frame]
    report = _sync(data)
    assert fakes["downloads"] == []
    assert [d.obsdate for d in report.datasets] == ["261004"]


def test_sync_rescans_night_whose_obslog_predates_a_local_frame(roots, fakes):
    data, obslog = roots
    old, late = _frame(1), _frame(2)
    csv = _touch(obslog / "qhy600" / "261004" / "obslog-qhy600-261004-ccd0.csv")
    _touch(_unpacked(data, old))
    os.utime(csv, (1_000, 1_000))
    os.utime(_unpacked(data, old), (500, 500))
    # `late` was fetched by a run that died before rescanning: newer than the CSV.
    _touch(_unpacked(data, late))
    os.utime(_unpacked(data, late), (2_000, 2_000))
    fakes["pages"] = [old, late]
    report = _sync(data)
    assert fakes["downloads"] == []
    assert [d.obsdate for d in report.datasets] == ["261004"]


def test_sync_failed_download_marks_report_and_skips_its_night(roots, fakes):
    data, _ = roots
    good, bad = _frame(1), _frame(2, night="20261005")
    fakes["pages"] = [good, bad]
    fakes["fail"] = {bad["filename"]}
    report = _sync(data)
    assert not report.ok
    assert report.download_errors == (f"{bad['filename']}: HTTP 403",)
    assert [d.obsdate for d in report.datasets] == ["261004"]


def test_sync_scan_failure_is_reported_not_raised(roots, fakes, monkeypatch):
    data, _ = roots
    fakes["pages"] = [_frame(1)]
    monkeypatch.setattr("muscat_db.scanner.scan_date", lambda *a, **k: {})
    report = _sync(data)
    assert not report.ok
    assert "no reduced FITS" in report.datasets[0].error
    assert fakes["ingests"] == []


def test_sync_without_db_scans_but_does_not_ingest(roots, fakes):
    data, _ = roots
    fakes["pages"] = [_frame(1)]
    report = _sync(data, db=None)
    assert report.datasets[0].ingested is None
    assert fakes["scans"] and fakes["ingests"] == []


def test_sync_max_frames_defers_newest_frames(roots, fakes):
    data, _ = roots
    fakes["pages"] = [_frame(3, night="20261005"), _frame(1), _frame(2)]
    report = _sync(data, max_frames=2)
    assert report.deferred == 1
    assert sorted(fakes["downloads"]) == [_frame(1)["filename"], _frame(2)["filename"]]


def test_sync_dry_run_touches_nothing(roots, fakes):
    data, _ = roots
    fakes["pages"] = [_frame(1)]
    report = _sync(data, dry_run=True)
    assert len(report.plan.to_download) == 1
    assert fakes["downloads"] == [] and fakes["scans"] == [] and fakes["ingests"] == []


def test_sync_lock_refuses_overlapping_run(tmp_path):
    with lco_sync.sync_lock(tmp_path):
        with pytest.raises(lco_sync.SyncError, match="another lco-sync"):
            with lco_sync.sync_lock(tmp_path):
                pass
    with lco_sync.sync_lock(tmp_path):  # released on exit
        pass


# --- CLI --------------------------------------------------------------------

def test_cli_rejects_malformed_proposal_id(roots):
    result = CliRunner().invoke(app, ["lco-sync", "../x", "--dry-run"])
    assert result.exit_code == 1
    assert "not a valid LCO proposal ID" in result.output


def test_cli_dry_run_plans_without_downloading(roots, fakes):
    fakes["pages"] = [_frame(1)]
    result = CliRunner().invoke(app, ["lco-sync", "KEY2026B-001", "--dry-run", "--user", "alice"])
    assert result.exit_code == 0, result.output
    assert "would download 1 frames for qhy600 261004" in result.output
    assert fakes["filters"][1] == "alice"
    assert fakes["downloads"] == []


def test_cli_exits_nonzero_when_a_download_fails(roots, fakes, tmp_path):
    fakes["pages"] = [_frame(1)]
    fakes["fail"] = {_frame(1)["filename"]}
    result = CliRunner().invoke(
        app, ["lco-sync", "KEY2026B-001", "--no-ingest", "--db", str(tmp_path / "x.db")]
    )
    assert result.exit_code == 1
    assert "1 failed" in result.output


def test_cli_archive_error_on_one_proposal_does_not_stop_the_next(roots, fakes, monkeypatch):
    def search(filters, user_name=None):
        if filters["proposal_id"] == "BAD2026B-001":
            raise lco.LcoError("LCO API request failed with HTTP 400", status=400,
                               detail="field [bold]start[/bold] invalid")
        return {"count": 0, "results": [], "truncated": False}

    monkeypatch.setattr("muscat_db.lco.archive_search_all", search)
    result = CliRunner().invoke(
        app, ["lco-sync", "BAD2026B-001", "KEY2026B-001", "--no-ingest", "--db", "x.db"]
    )
    assert result.exit_code == 1
    assert "[bold]start[/bold]" in result.output  # markup in the error text is printed, not parsed
    assert "KEY2026B-001: 0 downloaded" in result.output


def test_cli_banner_reports_the_window_actually_requested(roots, fakes):
    result = CliRunner().invoke(app, [
        "lco-sync", "KEY2026B-001", "--start", "2026-09-01", "--end", "2026-09-10", "--dry-run",
    ])
    assert result.exit_code == 0, result.output
    assert "lco-sync KEY2026B-001 --start 2026-09-01 --end 2026-09-10 --dry-run" in " ".join(result.output.split())
    assert "--days" not in result.output
    assert "querying the LCO archive for 2026-09-01 00:00 .. 2026-09-10 00:00 UTC" in result.output
