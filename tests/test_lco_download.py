"""Archive file safety and fail-closed execution, without an external service."""

import io
import json
from pathlib import Path
import threading
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
    lease.checkpoint.side_effect = lco_download.LeaseLost("cancelled")
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
