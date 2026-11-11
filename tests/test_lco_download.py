"""Archive file safety and fail-closed execution, without an external service."""

import io
import json
from pathlib import Path
import threading
import time
from unittest.mock import Mock

import pytest

from muscat_db import lco, lco_download, lco_queue, lco_rate


@pytest.fixture
def lease():
    queue = Mock()
    queue.update.return_value = {"cancel_requested": False}
    job = {"job_id": "a" * 16, "lease_token": "attempt1", "download_attempts": 1}
    with lco_download.Lease(queue, job) as active:
        yield active


def test_transfer_stops_on_lost_lease_and_preserves_completed_file(tmp_path, monkeypatch, lease):
    dest = tmp_path / "frame.fits"
    dest.write_bytes(b"previous complete frame")
    class Stream(io.BytesIO):
        def read(self, n):
            lease.stop.set()
            return super().read(n)
    monkeypatch.setattr(lco, "_open_download_url", lambda *a, **k: Stream(b"replacement"))
    with pytest.raises(lco_download.LeaseLost):
        lco._download_to_file("https://archive-api.lco.global/frame", dest,
                              check=lease.check, temp_suffix=".attempt1", deadline_s=120)
    assert dest.read_bytes() == b"previous complete frame"
    assert not list(tmp_path.glob("*.part*"))


def test_attempt_temporary_paths_do_not_delete_a_sibling_attempt(tmp_path, monkeypatch, lease):
    dest = tmp_path / "frame.fits"
    sibling = tmp_path / "frame.fits.part.attempt2"
    sibling.write_bytes(b"sibling in progress")
    monkeypatch.setattr(lco, "_open_download_url", lambda *a, **k: io.BytesIO(b"complete"))
    lco._download_to_file("https://archive-api.lco.global/frame", dest,
                          check=lease.check, temp_suffix=".attempt1", deadline_s=120)
    assert dest.read_bytes() == b"complete"
    assert sibling.read_bytes() == b"sibling in progress"


def test_deadline_prevents_slow_trickle_transfer(tmp_path, monkeypatch):
    monkeypatch.setattr(lco.time, "monotonic", Mock(side_effect=[0, 121]))
    monkeypatch.setattr(lco, "_open_download_url", lambda *a, **k: io.BytesIO(b"partial"))
    dest = tmp_path / "frame.fits"
    with pytest.raises(TimeoutError):
        lco._download_to_file("url", dest, check=lambda: None, deadline_s=120)
    assert not dest.exists()


def test_short_response_is_not_published_as_an_existing_frame(tmp_path, monkeypatch, lease):
    class Stream(io.BytesIO):
        headers = {"Content-Length": "100"}
    monkeypatch.setattr(lco, "_open_download_url", lambda *a, **k: Stream(b"partial"))
    dest = tmp_path / "frame.fits"
    with pytest.raises(OSError, match="incomplete"):
        lco._download_to_file("url", dest, check=lease.check, deadline_s=120)
    assert not dest.exists()


@pytest.mark.parametrize("response", [None, {"cancel_requested": True}])
def test_lost_or_cancelled_lease_fails_closed(lease, response):
    lease.queue.update.return_value = response
    with pytest.raises(lco_download.LeaseLost):
        lease.checkpoint({"phase": "funpacking"})
    with pytest.raises(lco_download.LeaseLost):
        lease.check()


def test_database_outage_and_suspended_process_are_fenced(lease, monkeypatch):
    lease.queue.update.side_effect = OSError("private database detail")
    with pytest.raises(lco_download.LeaseLost, match="control plane unavailable"):
        lease.checkpoint({})
    lease.stop.clear()
    monkeypatch.setattr(lco_download.time, "monotonic", lambda: lease.ack + lco_queue.LEASE_S)
    with pytest.raises(lco_download.LeaseLost):
        lease.check()


def test_funpack_cannot_publish_after_losing_lease(tmp_path, monkeypatch, lease):
    source = tmp_path / "frame.fits.fz"
    source.write_bytes(b"compressed")
    monkeypatch.setattr(lco.shutil, "which", lambda name: "funpack")
    def unpack(cmd, **kwargs):
        Path(cmd[2]).write_bytes(b"unpacked")
        lease.stop.set()
        return Mock(returncode=0)
    monkeypatch.setattr(lco.subprocess, "run", unpack)
    with pytest.raises(lco_download.LeaseLost):
        lco._funpack_file(source, check=lease.check, temp_suffix=".attempt1")
    assert not (tmp_path / "frame.fits").exists()
    assert not list(tmp_path.glob("*.part*"))


def test_private_spool_refuses_symlinks_and_shared_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSCAT_LCO_DOWNLOAD_SPOOL", str(tmp_path / "spool"))
    path = lco_download._path("a" * 16)
    other = tmp_path / "credential"
    other.write_text("secret")
    path.symlink_to(other)
    with pytest.raises(OSError):
        lco_download._read_manifest("a" * 16)
    path.unlink()
    path.write_text('{}')
    path.chmod(0o644)
    with pytest.raises(RuntimeError, match="mode 600"):
        lco_download._read_manifest("a" * 16)


def test_public_snapshot_hides_lease_and_accounts_for_missing_results():
    snapshot = lco_download.public({"job_id": "abc", "state": "error", "lease_token": "secret",
                                    "filenames": ["one", "two"], "results": [{"filename": "one", "status": "exists"}]})
    assert "secret" not in json.dumps(snapshot)
    assert snapshot["results"][1] == {"filename": "two", "status": "error", "error": "Download failed"}


def test_results_use_shared_alias_and_drop_exception_text(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    result = lco_download._safe_result({"src": str(real / "frame.fits.fz"),
                                       "dest": str(alias / "frame.fits"),
                                       "error": "HTTP Error 403: https://host/?signature=secret"}, alias)
    assert result["src"] == str(alias / "frame.fits.fz")
    assert result["dest"] == str(alias / "frame.fits")
    assert result["error"] == "Archive HTTP 403"


def test_frame_window_is_bounded_and_cancellation_starts_no_more(monkeypatch):
    lease = Mock()
    lease.job = {"lease_token": "attempt"}
    # Progress saves are throttled, so cancellation arrives through the check
    # the heartbeat trips rather than through a per-frame database write.
    lease.check.side_effect = lco_download.LeaseLost("cancelled")
    arrived = threading.Barrier(lco_queue.FRAME_WORKERS)
    calls = []
    def frame(data, manifest, lease):
        calls.append(data)
        arrived.wait(timeout=5)
        return {"filename": data, "status": "exists"}
    monkeypatch.setattr(lco_download, "_frame", frame)
    with pytest.raises(lco_download.LeaseLost):
        lco_download._download({"frames": list(map(str, range(100))), "root": "/data"}, lease)
    assert len(calls) == lco_queue.FRAME_WORKERS


def test_unpacking_overlaps_downloads_and_runs_in_parallel(monkeypatch, tmp_path):
    lease = Mock()
    lease.job = {"lease_token": "attempt"}
    unpack_started = threading.Event()
    last = "9"
    def frame(data, manifest, lease):
        if data == last:
            # The final transfer only finishes once earlier frames are unpacking.
            assert unpack_started.wait(timeout=5), "unpacking waited for every download"
        return {"filename": data, "dest": str(tmp_path / f"{data}.fits.fz"), "status": "downloaded"}
    pair = threading.Barrier(2)
    def funpack(path, *, check, temp_suffix):
        unpack_started.set()
        pair.wait(timeout=5)  # breaks unless two unpacks run at once
        return {"filename": path.name, "src": str(path), "dest": str(path)[:-3], "status": "unpacked"}
    monkeypatch.setattr(lco_download, "_frame", frame)
    monkeypatch.setattr(lco, "_funpack_file", funpack)
    monkeypatch.setattr(lco, "_ARCHIVE_FUNPACK_WORKERS", 2)
    error, retryable = lco_download._download({"frames": list(map(str, range(10))), "root": str(tmp_path)}, lease)
    assert error is None and not retryable
    final = lease.checkpoint.call_args_list[-1].args[0]
    assert final["funpack_done"] == 10
    assert {r["status"] for r in final["funpack_results"]} == {"unpacked"}


def test_lease_loss_cancels_queued_unpacks(monkeypatch, tmp_path):
    lease = Mock()
    lease.job = {"lease_token": "attempt"}
    lost = threading.Event()
    def frame(data, manifest, lease):
        if data == "20":
            lost.set()
            raise lco_download.LeaseLost("cancelled")
        return {"filename": data, "dest": str(tmp_path / f"{data}.fits.fz"), "status": "downloaded"}
    started = []
    def funpack(path, **kwargs):
        started.append(path)
        lost.wait(timeout=5)
        time.sleep(0.2)  # the job ends while these two are still running
        return {"filename": path.name, "src": str(path), "dest": str(path)[:-3], "status": "unpacked"}
    monkeypatch.setattr(lco_download, "_frame", frame)
    monkeypatch.setattr(lco, "_funpack_file", funpack)
    monkeypatch.setattr(lco, "_ARCHIVE_FUNPACK_WORKERS", 2)
    with pytest.raises(lco_download.LeaseLost):
        lco_download._download({"frames": list(map(str, range(30))), "root": str(tmp_path)}, lease)
    # Twenty unpacks were queued; only the two already running may finish.
    assert len(started) == 2


def test_progress_reports_the_unpack_total_before_downloads_finish(monkeypatch, tmp_path):
    # A cancelled fleet job showed funpack "21/0": the total was only written
    # after the last transfer, so a job that ended earlier never got one.
    lease = Mock()
    lease.job = {"lease_token": "attempt"}
    monkeypatch.setattr(lco_download, "_CHECKPOINT_INTERVAL_S", 0)
    def frame(data, manifest, lease):
        if data == "20":
            raise lco_download.LeaseLost("cancelled")
        return {"filename": data, "dest": str(tmp_path / f"{data}.fits.fz"), "status": "downloaded"}
    def funpack(path, **kwargs):
        return {"filename": path.name, "src": str(path), "dest": str(path)[:-3], "status": "unpacked"}
    monkeypatch.setattr(lco_download, "_frame", frame)
    monkeypatch.setattr(lco, "_funpack_file", funpack)
    with pytest.raises(lco_download.LeaseLost):
        lco_download._download({"frames": list(map(str, range(30))), "root": str(tmp_path)}, lease)
    saves = [c.args[0] for c in lease.checkpoint.call_args_list if "funpack_done" in c.args[0]]
    assert saves, "no progress was saved"
    assert all(s.get("funpack_total", 0) >= s["funpack_done"] for s in saves)


@pytest.fixture
def stopping(monkeypatch):
    monkeypatch.setattr(lco_download, "_STOPPING", threading.Event())
    monkeypatch.setattr(lco_download, "_EXECUTION_THREADS", {})


def test_worker_stop_hands_the_running_job_back(monkeypatch, tmp_path, stopping):
    # Canary 2026-11-10: a restarted worker left its job claimed until the
    # 300 s lease expired. shutdown() must release it while the process lives.
    queue = Mock()
    queue.update.return_value = {"cancel_requested": False}
    job = {"job_id": "b" * 16, "lease_token": "t1", "download_attempts": 1, "stage": "download"}
    monkeypatch.setattr(lco_download, "_read_manifest", lambda job_id: {
        "root": str(tmp_path), "auto_ingest": False, "frames": ["0"], "overwrite": False, "token": "x"})
    monkeypatch.setattr(lco, "download_root", lambda: tmp_path)
    started = threading.Event()
    def frame(data, manifest, lease):
        started.set()
        while True:
            lease.check()
            time.sleep(0.01)
    monkeypatch.setattr(lco_download, "_frame", frame)
    thread = threading.Thread(target=lco_download._execute, args=(queue, job), daemon=True)
    lco_download._EXECUTION_THREADS["download"] = thread
    thread.start()
    assert started.wait(5)
    lco_download.shutdown(timeout=5)
    assert not thread.is_alive()
    queue.release.assert_called_once_with(job["job_id"], "t1")
    queue.finish.assert_not_called()


def test_a_stopping_process_claims_no_new_job(monkeypatch, stopping):
    queue = Mock()
    monkeypatch.setattr(lco_queue, "enabled", lambda: True)
    monkeypatch.setattr(lco_queue, "ArchiveQueue", lambda: queue)
    monkeypatch.setattr(lco_download.job_store, "current_owner", lambda: "worker")
    lco_download.shutdown(timeout=0)
    lco_download.sync_jobs()
    queue.claim.assert_not_called()


def test_retry_count_is_clamped_and_backoff_checks_lease(monkeypatch, tmp_path, lease):
    monkeypatch.setattr(lco, "_ARCHIVE_DOWNLOAD_FRAME_RETRIES", 999)
    monkeypatch.setattr(lco, "frame_destination", lambda frame: ("muscat3", "date", tmp_path / "frame.fits"))
    transfer = Mock(side_effect=lco.LcoError("temporary", status=503))
    monkeypatch.setattr(lco, "_download_to_file", transfer)
    waits = []
    def wait(delay, check):
        check()
        waits.append(delay)
    monkeypatch.setattr(lco_rate, "wait", wait)
    monkeypatch.setattr(lco_rate.random, "uniform", lambda lo, hi: hi)
    result = lco_download._frame({"filename": "frame.fits", "url": "signed-secret"}, {"overwrite": False}, lease)
    assert transfer.call_count == 3
    assert waits == [2, 4]
    assert result["retryable"]
    assert "signed-secret" not in json.dumps(result)


def test_refresh_cooldown_receives_lease_check(monkeypatch, tmp_path, lease):
    monkeypatch.setattr(lco, "frame_destination", lambda frame: ("muscat3", "date", tmp_path / "frame.fits"))
    def refresh(url, **kwargs):
        assert kwargs["token"] == "private-token"
        assert kwargs["check"] == lease.check
        lease.stop.set()
        kwargs["check"]()
    monkeypatch.setattr(lco, "_lco_api_request", refresh)
    transfer = Mock()
    monkeypatch.setattr(lco, "_download_to_file", transfer)
    with pytest.raises(lco_download.LeaseLost):
        lco_download._frame({"filename": "frame.fits"}, {"overwrite": False, "token": "private-token"}, lease)
    transfer.assert_not_called()


def test_filesystem_error_before_transfer_is_retried(monkeypatch, tmp_path, lease):
    dest = tmp_path / "night" / "frame.fits"
    outcomes = [OSError("stale NFS handle"), ("muscat3", "date", dest)]
    def destination(frame):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(lco, "frame_destination", destination)
    monkeypatch.setattr(lco_rate, "wait", lambda delay, check: None)
    transfer = Mock()
    monkeypatch.setattr(lco, "_download_to_file", transfer)
    result = lco_download._frame({"filename": "frame.fits", "url": "signed"}, {"overwrite": False}, lease)
    assert result["status"] == "downloaded"
    transfer.assert_called_once()


def test_execution_failures_retry_filesystem_errors_and_log_no_exception_text(monkeypatch, caplog):
    queue = Mock()
    queue.update.return_value = {"cancel_requested": False}
    queue.finish.return_value = {"state": "pending"}
    job = {"job_id": "a" * 16, "lease_token": "attempt", "stage": "download", "download_attempts": 1}
    def unreadable(job_id):
        raise OSError("/private/spool?signature=secret")
    monkeypatch.setattr(lco_download, "_read_manifest", unreadable)
    with caplog.at_level("WARNING", logger="muscat_db.lco_download"):
        lco_download._execute(queue, job)
    assert queue.finish.call_args.kwargs["retryable"] is True
    assert "OSError" in caplog.text and job["job_id"] in caplog.text
    assert "signature" not in caplog.text


def test_transfer_deadline_grows_with_the_declared_response_size():
    assert lco._transfer_deadline(120, None) == 120
    assert lco._transfer_deadline(120, 1) == 120
    large = 360 * lco._MIN_TRANSFER_BYTES_PER_S
    assert lco._transfer_deadline(120, large) == pytest.approx(360)


def test_large_response_outlives_the_shortest_deadline(tmp_path, monkeypatch):
    class Stream(io.BytesIO):
        headers = {"Content-Length": "7"}
    monkeypatch.setattr(lco, "_MIN_TRANSFER_BYTES_PER_S", 0.01)
    monkeypatch.setattr(lco.time, "monotonic", Mock(side_effect=[0, 121, 121]))
    monkeypatch.setattr(lco, "_open_download_url", lambda *a, **k: Stream(b"partial"))
    dest = tmp_path / "frame.fits"
    lco._download_to_file("url", dest, check=lambda: None, deadline_s=120)
    assert dest.read_bytes() == b"partial"


def test_malformed_content_length_does_not_fail_a_complete_transfer(tmp_path, monkeypatch):
    class Stream(io.BytesIO):
        headers = {"Content-Length": "oops"}
    monkeypatch.setattr(lco, "_open_download_url", lambda *a, **k: Stream(b"complete"))
    dest = tmp_path / "frame.fits"
    lco._download_to_file("url", dest, check=lambda: None, deadline_s=120)
    assert dest.read_bytes() == b"complete"


def test_job_row_details_show_readable_phases():
    from muscat_db import web

    row = web._lco_archive_download_row({"job_id": "a" * 16, "state": "pending", "phase": "awaiting_ingest",
                                         "dest_dirs": ["/data/night"]})
    assert row["details"] == "awaiting ingest: /data/night"


def test_progress_saves_are_throttled_but_the_final_save_is_complete(monkeypatch):
    lease = Mock()
    lease.job = {"lease_token": "attempt"}
    monkeypatch.setattr(lco_download, "_frame", lambda data, manifest, lease: {"filename": data, "status": "exists"})
    monkeypatch.setattr(lco, "_funpack_paths", lambda results: [])
    lco_download._download({"frames": list(map(str, range(200))), "root": "/data"}, lease)
    saves = [call.args[0] for call in lease.checkpoint.call_args_list if "results" in call.args[0]]
    assert len(saves) < 10
    assert saves[-1]["frames_done"] == 200
    assert len(saves[-1]["results"]) == 200


def test_archive_sync_failure_cannot_stop_target_refresh(monkeypatch):
    from muscat_db import web

    for module in (web.phot, web.fit, web.ttv):
        monkeypatch.setattr(module, "sync_jobs", lambda: None)
    monkeypatch.setattr(web, "_lco_archive_download_rows", lambda: [])
    monkeypatch.setattr(web, "uses_shared_control_plane", lambda: True)
    monkeypatch.setattr(web, "get_job_store", Mock())
    refresh = Mock()
    monkeypatch.setattr(web, "refresh_worker_finished_targets", refresh)
    monkeypatch.setattr(lco_download, "sync_jobs", Mock(side_effect=lco.LcoError("spool mode", status=503)))
    web._reconcile_all_jobs()
    refresh.assert_called_once()


def test_cleanup_failure_does_not_stop_claims_or_repeat_every_pass(monkeypatch):
    queue = Mock()
    queue.claim.return_value = None
    monkeypatch.setattr(lco_queue, "enabled", lambda: True)
    monkeypatch.setattr(lco_queue, "ArchiveQueue", lambda: queue)
    monkeypatch.setattr(lco_download.job_store, "current_owner", lambda: "web")
    monkeypatch.setattr(lco_download, "_EXECUTION_THREADS", {})
    monkeypatch.setattr(lco_download, "_LAST_CLEANUP", -1e12)
    cleanup = Mock(side_effect=lco.LcoError("spool mode", status=503))
    monkeypatch.setattr(lco_download, "cleanup", cleanup)
    lco_download.sync_jobs()
    lco_download.sync_jobs()
    assert queue.claim.call_count == 2
    cleanup.assert_called_once()


def test_queue_accepts_jobs_only_when_explicitly_enabled(monkeypatch):
    monkeypatch.setattr(lco_queue.job_store, "uses_shared_control_plane", lambda: True)
    monkeypatch.delenv("MUSCAT_LCO_FLEET_DOWNLOADS", raising=False)
    assert lco_queue.enabled() is True  # Existing rows stay readable after rollback.
    assert lco_queue.accepting() is False
    monkeypatch.setenv("MUSCAT_LCO_FLEET_DOWNLOADS", "1")
    assert lco_queue.accepting() is True
    monkeypatch.setattr(lco_queue.job_store, "uses_shared_control_plane", lambda: False)
    assert lco_queue.accepting() is False


def test_merge_without_the_flag_keeps_the_local_download_path(monkeypatch, tmp_path):
    monkeypatch.setattr(lco_queue.job_store, "uses_shared_control_plane", lambda: True)
    monkeypatch.delenv("MUSCAT_LCO_FLEET_DOWNLOADS", raising=False)
    monkeypatch.setenv("MUSCAT_LCO_DIR", str(tmp_path))
    monkeypatch.setattr(lco, "_ARCHIVE_DOWNLOAD_JOBS", {})
    executor = Mock()
    monkeypatch.setattr(lco, "_ARCHIVE_DOWNLOAD_EXECUTOR", executor)
    submit = Mock()
    monkeypatch.setattr(lco_download, "submit", submit)
    frame = {"filename": "coj1m003-fa19-20261101-0001-e91.fits", "INSTRUME": "fa19", "SITEID": "coj",
             "TELID": "1m0", "OBJECT": "TOI-1", "url": "https://archive-lco-global.s3.amazonaws.com/frame"}
    lco.start_archive_download([frame], user_name="alice")
    submit.assert_not_called()
    executor.submit.assert_called_once()


def test_startup_warnings_name_missing_settings_but_never_their_values(monkeypatch):
    monkeypatch.setattr(lco_queue.job_store, "uses_shared_control_plane", lambda: True)
    monkeypatch.delenv("MUSCAT_LCO_RATE_DSN", raising=False)
    monkeypatch.delenv("MUSCAT_LCO_FLEET_DOWNLOADS", raising=False)
    assert any("MUSCAT_LCO_RATE_DSN" in line for line in lco_download.config_warnings())
    monkeypatch.setenv("MUSCAT_LCO_RATE_DSN", "postgresql://user:secret@host/db")
    monkeypatch.setenv("MUSCAT_LCO_FLEET_DOWNLOADS", "1")
    monkeypatch.delenv("MUSCAT_LCO_DOWNLOAD_SPOOL", raising=False)
    warnings = lco_download.config_warnings()
    assert any("MUSCAT_LCO_DOWNLOAD_SPOOL" in line for line in warnings)
    assert "secret" not in " ".join(warnings)


def test_admission_refusal_keeps_retry_information(monkeypatch, tmp_path):
    monkeypatch.setattr(lco, "frame_destination", lambda frame: ("muscat3", "date", tmp_path / "frame.fits"))
    monkeypatch.setattr(lco, "_validate_download_url", lambda url: url)
    monkeypatch.setattr(lco, "_download_to_file", Mock(side_effect=lco.LcoError(
        "Shared LCO request budget or cooldown active", status=429, retry_after=30)))
    result = lco._download_frame({"filename": "frame.fits", "url": "signed"})
    assert result["status"] == "error"
    assert result["retry_after"] == 30


def test_admission_wait_spends_no_frame_attempt(monkeypatch):
    refused = {"filename": "f", "status": "error", "error": "Shared LCO cooldown", "retry_after": 60}
    download = Mock(side_effect=[refused, refused, {"filename": "f", "status": "downloaded"}])
    monkeypatch.setattr(lco, "_download_frame", download)
    monkeypatch.setattr(lco, "_ARCHIVE_DOWNLOAD_FRAME_RETRIES", 1)
    sleeps = []
    monkeypatch.setattr(lco.time, "sleep", sleeps.append)
    assert lco._download_frame_with_retry({"filename": "f"})["status"] == "downloaded"
    assert sleeps == [60, 60]


def test_long_admission_wait_returns_to_the_caller(monkeypatch):
    refused = {"filename": "f", "status": "error", "error": "Shared LCO budget", "retry_after": 3600}
    monkeypatch.setattr(lco, "_download_frame", Mock(return_value=refused))
    sleeps = []
    monkeypatch.setattr(lco.time, "sleep", sleeps.append)
    assert lco._download_frame_with_retry({"filename": "f"})["retry_after"] == 3600
    assert sleeps == []


@pytest.mark.parametrize("dsn", ["", "postgresql://user:secret@127.0.0.1:1/none"])
def test_missing_or_unreachable_coordinator_fails_the_frame_at_once(monkeypatch, tmp_path, dsn):
    # Not a rate limit: no retry_after, so the frame is not slept on 5 x 60 s.
    monkeypatch.setenv("MUSCAT_LCO_RATE_DSN", dsn)
    lco_rate._coordinator.cache_clear()
    monkeypatch.setattr(lco, "frame_destination", lambda frame: ("muscat3", "date", tmp_path / "frame.fits"))
    monkeypatch.setattr(lco, "_ARCHIVE_DOWNLOAD_FRAME_RETRIES", 3)
    sleeps = []
    monkeypatch.setattr(lco.time, "sleep", sleeps.append)
    frame = {"filename": "frame.fits", "url": "https://archive-lco-global.s3.amazonaws.com/frame.fits.fz"}
    result = lco._download_frame_with_retry(frame)
    lco_rate._coordinator.cache_clear()
    assert result["status"] == "error"
    assert "coordinator" in result["error"]
    assert "retry_after" not in result
    assert sleeps == []
    assert "secret" not in json.dumps(result)
