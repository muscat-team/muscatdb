from __future__ import annotations

import datetime
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from muscat_db import lco, lco_sync
from muscat_db.cli import app

UTC = datetime.timezone.utc


def _frame(number: int, *, night: str = "20261004", date_obs: str = "2026-10-05T08:00:00Z",
           obj: str = "TOI-123", tel: str = "0m463", instrume: str = "sq35") -> dict:
    return {
        "id": f"{night}-{number}",
        "filename": f"ogg{tel}-{instrume}-{night}-{number:04d}-e91.fits.fz",
        "SITEID": "ogg",
        "TELID": tel,
        "INSTRUME": instrume,
        "OBJECT": obj,
        "DATE_OBS": date_obs,
        "url": "https://archive-api.lco.global/frames/x.fits.fz",
    }


def _night(night: str, n: int, date_obs: str) -> list[dict]:
    return [_frame(i, night=night, date_obs=date_obs) for i in range(1, n + 1)]


@pytest.fixture
def roots(tmp_path, monkeypatch):
    data = tmp_path / "data"
    obslog = tmp_path / "obslog"
    data.mkdir()
    obslog.mkdir()
    monkeypatch.setenv("MUSCAT_LCO_DIR", str(data))
    monkeypatch.setattr("muscat_db.instruments.OBSLOG_BASE", str(obslog))
    return data, obslog


def _paths(frame) -> tuple[Path, Path]:
    """(packed .fz, unpacked .fits) destinations of *frame*."""
    _inst, _date, dest = lco.frame_destination(frame)
    return dest, dest.with_name(dest.name[: -len(".fz")])


def _touch(path: Path, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _in_range(frame, filters) -> bool:
    when = lco_sync._parse_utc(frame["DATE_OBS"], "x")
    return (lco_sync._parse_utc(filters["start"], "x") <= when
            < lco_sync._parse_utc(filters["end"], "x"))


@pytest.fixture
def fakes(monkeypatch):
    """Archive, download, funpack, scan and ingest stand-ins that log events."""
    calls = {"archive": [], "queries": [], "events": [], "fail": set()}

    def fake_search(filters, user_name=None):
        calls["queries"].append((filters, user_name))
        rows = [f for f in calls["archive"] if _in_range(f, filters)]
        return {"count": len(rows), "results": rows, "truncated": False}

    def fake_download(frame, overwrite=False):
        name = frame["filename"]
        calls["events"].append(("download", name))
        if name in calls["fail"]:
            return {"filename": name, "status": "error", "error": "HTTP 403"}
        packed, _ = _paths(frame)
        if packed.exists():
            return {"filename": name, "status": "exists", "dest": str(packed)}
        _touch(packed)
        return {"filename": name, "status": "downloaded", "dest": str(packed)}

    def fake_funpack(path):
        calls["events"].append(("funpack", path.name))
        _touch(path.with_name(path.name[: -len(".fz")]))
        return {"filename": path.name, "status": "unpacked"}

    def fake_scan(instrument, obsdate, max_workers=None, data_root=None):
        calls["events"].append(("scan", instrument, obsdate, data_root))
        return {"total": 3, "per_ccd": {0: 3}}

    def fake_ingest(db, instrument, obsdate):
        calls["events"].append(("ingest", db, instrument, obsdate))
        return 3

    monkeypatch.setattr("muscat_db.lco.archive_search_all", fake_search)
    monkeypatch.setattr("muscat_db.lco._download_frame_with_retry", fake_download)
    monkeypatch.setattr("muscat_db.lco._funpack_file", fake_funpack)
    monkeypatch.setattr("muscat_db.scanner.scan_date", fake_scan)
    monkeypatch.setattr("muscat_db.database.ingest_date", fake_ingest)
    return calls


def _events(calls, kind):
    return [e[1:] for e in calls["events"] if e[0] == kind]


def _sync(data, **kwargs):
    defaults = dict(start="2026-10-01 00:00", end="2026-10-07 00:00", data_root=data,
                    db="test.db", workers=1, log=lambda _m: None)
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

def test_query_pads_window_by_two_days_in_daily_chunks(fakes):
    fakes["archive"] = [_frame(1, date_obs="2026-09-30T08:00:00Z")]
    frames = lco_sync.query_frames("KEY2026B-001", "2026-10-01 00:00", "2026-10-03 00:00", "alice",
                                   log=lambda _m: None)
    assert [f["filename"] for f in frames] == [fakes["archive"][0]["filename"]]
    windows = [(f["start"], f["end"]) for f, _user in fakes["queries"]]
    assert windows[0] == ("2026-09-29 00:00", "2026-09-30 00:00")
    assert windows[-1] == ("2026-10-04 00:00", "2026-10-05 00:00")
    assert len(windows) == 6
    filters, user = fakes["queries"][0]
    assert user == "alice"
    assert (filters["proposal_id"], filters["reduction_level"], filters["OBSTYPE"]) == (
        "KEY2026B-001", 91, "EXPOSE",
    )


def test_query_refuses_a_truncated_chunk(monkeypatch):
    monkeypatch.setattr(
        "muscat_db.lco.archive_search_all",
        lambda filters, user_name=None: {"count": 20000, "results": [], "truncated": True},
    )
    with pytest.raises(lco_sync.SyncError, match="partial listing"):
        lco_sync.query_frames("KEY2026B-001", "2026-10-01 00:00", "2026-10-02 00:00",
                              log=lambda _m: None)


# --- grouping into datasets -------------------------------------------------

def test_night_cut_by_window_start_is_kept_whole(roots):
    # DAY-OBS 261004 runs from 23:00 UTC on 10-04 into 10-05; the window opens
    # mid-night, so its first frame lies before `start`.
    early = _frame(1, date_obs="2026-10-04T23:00:00Z")
    late = _frame(2, date_obs="2026-10-05T03:00:00Z")
    datasets, _eng, _bad = lco_sync.group_datasets([early, late], "2026-10-05 00:00", "2026-10-06 00:00")
    assert [(d.label, len(d.frames)) for d in datasets] == [("qhy600 261004", 2)]


def test_night_seen_only_through_padding_is_dropped(roots):
    outside = _frame(1, night="20261001", date_obs="2026-10-01T08:00:00Z")
    inside = _frame(1, night="20261004", date_obs="2026-10-05T08:00:00Z")
    datasets, _eng, _bad = lco_sync.group_datasets([outside, inside], "2026-10-03 00:00", "2026-10-06 00:00")
    assert [d.obsdate for d in datasets] == ["261004"]


def test_grouping_skips_engineering_and_reports_unplaceable(roots):
    unplaceable = {**_frame(3), "TELID": "0m4", "INSTRUME": "zz99",
                   "filename": "ogg0m4-zz99-20261004-0003-e91.fits.fz"}
    datasets, engineering, bad = lco_sync.group_datasets(
        [_frame(1), _frame(1), _frame(2, obj="auto_focus"), unplaceable],
        "2026-10-01 00:00", "2026-10-07 00:00",
    )
    assert [len(d.frames) for d in datasets] == [1]  # duplicate listing collapsed
    assert engineering == 1
    assert len(bad) == 1 and "zz99" in bad[0]


def test_packed_frame_whose_funpack_never_finished_counts_as_missing(roots):
    frame = _frame(1)
    packed, _ = _paths(frame)
    _touch(packed)  # .fz on disk, no .fits next to it
    datasets, _e, _b = lco_sync.group_datasets([frame], "2026-10-01 00:00", "2026-10-07 00:00")
    assert [p.filename for p in datasets[0].missing] == [frame["filename"]]


# --- syncing ----------------------------------------------------------------

def test_each_night_is_finished_before_the_next_starts(roots, fakes):
    data, _ = roots
    fakes["archive"] = (_night("20261002", 2, "2026-10-03T08:00:00Z")
                        + _night("20261004", 2, "2026-10-05T08:00:00Z"))
    report = _sync(data)
    assert report.ok
    kinds = [(e[0], e[1] if e[0] != "scan" else e[2]) for e in fakes["events"]
             if e[0] in {"download", "scan"}]
    first_scan = kinds.index(("scan", "261002"))
    assert all("20261002" in name for kind, name in kinds[:first_scan])
    assert all("20261004" in name for kind, name in kinds[first_scan + 1:] if kind == "download")
    assert _events(fakes, "ingest") == [("test.db", "qhy600", "261002"), ("test.db", "qhy600", "261004")]
    assert all(d.complete for d in report.datasets)


def test_rerun_after_kill_fetches_only_missing_frames_and_rescans(roots, fakes):
    data, obslog = roots
    night = _night("20261004", 4, "2026-10-05T08:00:00Z")
    fakes["archive"] = night
    # State left by a run killed mid-night: two frames done, one downloaded but
    # never unpacked, one cut off mid-download, and an obslog from an earlier run.
    _touch(obslog / "qhy600" / "261004" / "obslog-qhy600-261004-ccd0.csv", mtime=1_000)
    for frame in night[:2]:
        _touch(_paths(frame)[1], mtime=500)
    _touch(_paths(night[2])[0])
    stale_part = _touch(_paths(night[3])[0].with_name(night[3]["filename"] + ".part"),
                        mtime=time.time() - 2 * 3600)
    report = _sync(data)
    assert report.ok
    assert sorted(_events(fakes, "download")) == [(night[2]["filename"],), (night[3]["filename"],)]
    assert (night[2]["filename"],) in _events(fakes, "funpack")
    assert not stale_part.exists()
    assert [e[1] for e in _events(fakes, "scan")] == ["261004"]
    assert report.datasets[0].missing_before == 2 and report.datasets[0].complete


def test_fresh_part_file_is_left_for_a_live_download(roots, fakes):
    data, _ = roots
    frame = _frame(1)
    fakes["archive"] = [frame]
    live = _touch(_paths(frame)[0].with_name("other-frame.fits.fz.part"))
    _sync(data)
    assert live.exists()


def test_complete_night_with_current_obslog_is_left_alone(roots, fakes):
    data, obslog = roots
    frame = _frame(1)
    fakes["archive"] = [frame]
    _touch(_paths(frame)[1], mtime=500)
    _touch(obslog / "qhy600" / "261004" / "obslog-qhy600-261004-ccd0.csv", mtime=1_000)
    report = _sync(data)
    assert report.ok and fakes["events"] == []


def test_complete_night_with_stale_obslog_is_rescanned(roots, fakes):
    data, obslog = roots
    frame = _frame(1)
    fakes["archive"] = [frame]
    _touch(_paths(frame)[1], mtime=2_000)  # downloaded by a run that died before scanning
    _touch(obslog / "qhy600" / "261004" / "obslog-qhy600-261004-ccd0.csv", mtime=1_000)
    _sync(data)
    assert _events(fakes, "download") == []
    assert [e[1] for e in _events(fakes, "scan")] == ["261004"]


def test_failed_frame_marks_night_incomplete_and_retries_next_run(roots, fakes):
    data, _ = roots
    good, bad = _night("20261004", 2, "2026-10-05T08:00:00Z")
    fakes["archive"] = [good, bad]
    fakes["fail"] = {bad["filename"]}
    report = _sync(data)
    assert not report.ok
    night = report.datasets[0]
    assert night.failures == (f"{bad['filename']}: HTTP 403",)
    assert not night.complete
    assert night.scanned == 3  # the frames that did arrive are still usable
    fakes["fail"] = set()
    fakes["events"].clear()
    assert _sync(data).ok
    assert _events(fakes, "download") == [(bad["filename"],)]


def test_scan_failure_is_reported_not_raised(roots, fakes, monkeypatch):
    data, _ = roots
    fakes["archive"] = [_frame(1)]
    monkeypatch.setattr("muscat_db.scanner.scan_date", lambda *a, **k: {})
    report = _sync(data)
    assert not report.ok
    assert "no reduced FITS" in report.datasets[0].error
    assert _events(fakes, "ingest") == []


def test_without_db_scans_but_does_not_ingest(roots, fakes):
    data, _ = roots
    fakes["archive"] = [_frame(1)]
    report = _sync(data, db=None)
    assert report.datasets[0].ingested is None
    assert _events(fakes, "scan") and _events(fakes, "ingest") == []


def test_max_nights_defers_whole_newer_nights(roots, fakes):
    data, _ = roots
    fakes["archive"] = (_night("20261004", 3, "2026-10-05T08:00:00Z")
                        + _night("20261002", 3, "2026-10-03T08:00:00Z"))
    report = _sync(data, max_nights=1)
    assert [(d.obsdate, d.deferred) for d in report.datasets] == [("261002", False), ("261004", True)]
    assert all("20261002" in name for (name,) in _events(fakes, "download"))
    assert len(_events(fakes, "download")) == 3


def test_dry_run_touches_nothing(roots, fakes):
    data, _ = roots
    fakes["archive"] = [_frame(1)]
    report = _sync(data, dry_run=True)
    assert report.datasets[0].missing_before == 1
    assert fakes["events"] == []


def test_interrupt_cancels_queued_frames_instead_of_draining_them(roots, fakes, monkeypatch):
    data, _ = roots
    fakes["archive"] = _night("20261004", 20, "2026-10-05T08:00:00Z")
    started = []

    def interrupted(planned):
        started.append(planned.filename)
        raise KeyboardInterrupt

    monkeypatch.setattr(lco_sync, "_fetch_one", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _sync(data, workers=2)
    assert len(started) < 20


def test_sync_lock_refuses_overlapping_run(tmp_path):
    with lco_sync.sync_lock(tmp_path):
        with pytest.raises(lco_sync.SyncError, match="another lco-sync"):
            with lco_sync.sync_lock(tmp_path):
                pass
    with lco_sync.sync_lock(tmp_path):  # released on exit
        pass


# --- CLI --------------------------------------------------------------------

def _flat(output: str) -> str:
    return " ".join(output.split())


def test_cli_rejects_malformed_proposal_id(roots):
    result = CliRunner().invoke(app, ["lco-sync", "../x", "--dry-run"])
    assert result.exit_code == 1
    assert "not a valid LCO proposal ID" in result.output


def test_cli_dry_run_reports_each_night(roots, fakes):
    fakes["archive"] = [_frame(1)]
    result = CliRunner().invoke(app, [
        "lco-sync", "KEY2026B-001", "--start", "2026-10-01", "--end", "2026-10-07",
        "--dry-run", "--user", "alice",
    ])
    assert result.exit_code == 0, result.output
    assert "qhy600 261004: 1 frames, 1 missing" in result.output
    assert fakes["queries"][0][1] == "alice"
    assert fakes["events"] == []


def test_cli_banner_reports_the_window_actually_requested(roots, fakes):
    result = CliRunner().invoke(app, [
        "lco-sync", "KEY2026B-001", "--start", "2026-09-01", "--end", "2026-09-10", "--dry-run",
    ])
    assert result.exit_code == 0, result.output
    assert "lco-sync KEY2026B-001 --start 2026-09-01 --end 2026-09-10 --dry-run" in _flat(result.output)
    assert "--days" not in result.output
    assert "querying the LCO archive for 2026-08-30 00:00 .. 2026-09-12 00:00 UTC" in result.output


def test_cli_exits_nonzero_when_a_download_fails(roots, fakes, tmp_path):
    fakes["archive"] = [_frame(1)]
    fakes["fail"] = {_frame(1)["filename"]}
    result = CliRunner().invoke(app, [
        "lco-sync", "KEY2026B-001", "--start", "2026-10-01", "--end", "2026-10-07",
        "--no-ingest", "--db", str(tmp_path / "x.db"),
    ])
    assert result.exit_code == 1
    assert "0/1 nights complete" in result.output and "1 failed" in result.output


def test_cli_interrupt_exits_130_with_resume_hint(roots, fakes, monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(lco_sync, "sync_proposal", interrupted)
    result = CliRunner().invoke(app, ["lco-sync", "KEY2026B-001", "--no-ingest", "--db", "x.db"])
    assert result.exit_code == 130
    assert "Re-run the same command to resume" in result.output


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
    assert "KEY2026B-001: 0/0 nights complete" in _flat(result.output)
