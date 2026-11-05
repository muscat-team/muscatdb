"""Real transactional claims, restarts and fenced retries (isolated PG in CI)."""

import concurrent.futures
import io
import json
import threading

import pytest

from muscat_db import database, job_store, lco, lco_download, lco_queue
from tests.test_job_store_postgres import _DSN, _postgres_reachable

pytestmark = pytest.mark.skipif(not _postgres_reachable(_DSN), reason="isolated PostgreSQL unavailable")


@pytest.fixture
def queue(monkeypatch, tmp_path):
    store = job_store.PostgresJobStore(dsn=_DSN)
    with store._pool.connection() as conn:
        conn.execute("TRUNCATE jobs, job_concurrency_slots")
    monkeypatch.setattr(job_store, "_STORE", store)
    monkeypatch.setattr(job_store, "uses_shared_control_plane", lambda: True)
    monkeypatch.setattr(job_store, "get_job_store", lambda: store)
    monkeypatch.setattr(job_store, "_OWNER", "web")
    monkeypatch.setenv("MUSCAT_LCO_DOWNLOAD_SPOOL", str(tmp_path / "spool"))
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("MUSCAT_LCO_DIR", str(root))
    monkeypatch.setattr(lco, "_get_lco_api_token", lambda user: "private-token-" + str(user))
    yield lco_queue.ArchiveQueue(store)
    store.close()


def frame(n=0):
    return {"filename": f"coj1m003-fa19-20261101-{n:04d}-e91.fits",
            "INSTRUME": "fa19", "SITEID": "coj", "TELID": "1m0", "OBJECT": "TOI-1",
            "url": "https://archive-lco-global.s3.amazonaws.com/frame?signature=private-signed-url"}


def enqueue(queue, user="alice", **kwargs):
    return lco.start_archive_download([frame()], user_name=user, **kwargs)


def expire(queue, job):
    with queue.store._pool.connection() as conn:
        payload = queue.get(job["job_id"])
        payload["lease_until"] = 1
        conn.execute("UPDATE jobs SET params=%s WHERE key=%s", (json.dumps(payload), lco_queue.key(job["job_id"])))


def make_retry_due(queue, job):
    with queue.store._pool.connection() as conn:
        payload = queue.get(job["job_id"])
        payload["retry_after"] = 0
        conn.execute("UPDATE jobs SET params=%s WHERE key=%s", (json.dumps(payload), lco_queue.key(job["job_id"])))


def test_atomic_fleet_claim_and_independent_cpu_capacity(queue):
    for i in range(8):
        enqueue(queue, user=f"u{i}")
    assert queue.store.claim_slot("photometry", "busy", 1)
    barrier = threading.Barrier(8)

    def claim(i):
        barrier.wait()
        return queue.claim("download", f"h{i}")

    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        claims = [j for j in pool.map(claim, range(8)) if j]
    assert len(claims) == 2
    assert len({j["job_id"] for j in claims}) == 2
    for job in claims:
        row = queue.store.get(lco_queue.key(job["job_id"]))
        assert row["instance_id"].startswith(job["host"] + ":")
    assert queue.store.count_claimed("photometry") == 1
    assert queue.store.count_claimed(lco_queue.TYPE) == 0


def test_per_host_capacity_is_separate(queue, monkeypatch):
    enqueue(queue)
    enqueue(queue, user="bob")
    monkeypatch.setattr(job_store, "_WORKER_MAX_SLOTS", 0)
    assert queue.claim("download", "h1")
    assert queue.claim("download", "h1") is None
    assert queue.claim("download", "h2")


def test_atomic_admission_double_click(queue):
    barrier = threading.Barrier(5)

    def submit(_):
        barrier.wait()
        try:
            return enqueue(queue, request_id="123")
        except lco.LcoError as exc:
            return exc.status

    with concurrent.futures.ThreadPoolExecutor(5) as pool:
        results = list(pool.map(submit, range(5)))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert results.count(409) == 4
    assert len(list(lco_download._spool().glob("*.json"))) == 1


def test_reclaim_fences_old_worker_and_has_attempt_limit(queue):
    submitted = enqueue(queue)
    old = queue.claim("download", "h1")
    expire(queue, old)
    new = queue.claim("download", "h2")
    assert new["job_id"] == old["job_id"]
    assert new["lease_token"] != old["lease_token"]
    assert queue.update(old["job_id"], old["lease_token"], {"state": "done"}) is None
    assert queue.finish(old["job_id"], old["lease_token"]) is None
    expire(queue, new)
    third = queue.claim("download", "h3")
    expire(queue, third)
    assert queue.claim("download", "h4") is None
    assert queue.get(submitted["job_id"])["state"] == "error"
    lco_download.cleanup()
    assert not lco_download._path(submitted["job_id"]).exists()


def test_retry_backoff_resume_budget_and_cancel(queue):
    enqueue(queue)
    first = queue.claim("download", "h1")
    queue.finish(first["job_id"], first["lease_token"], error="temporary", retryable=True)
    assert queue.claim("download", "h2") is None
    make_retry_due(queue, first)
    second = queue.claim("download", "h2")
    assert second["download_attempts"] == 2
    queue.cancel(second["job_id"])
    row = queue.update(second["job_id"], second["lease_token"], {"results": [{"status": "wrong"}]})
    assert row["cancel_requested"] and not row["results"]
    queue.finish(second["job_id"], second["lease_token"])
    assert queue.get(second["job_id"])["state"] == "cancelled"


def test_restart_jobs_page_cannot_clobber_queue_or_expose_secrets(queue, monkeypatch):
    from muscat_db import web

    submitted = enqueue(queue)
    claimed = queue.claim("download", "h1")
    before = queue.get(submitted["job_id"])
    # Simulate no local registry, including an empty archive listing during a
    # process restart. Persisted durable rows must remain live.
    monkeypatch.setattr(lco, "archive_download_jobs", lambda: [])
    monkeypatch.setattr(web, "get_job_store", lambda: queue.store)
    rows = web._jobs_with_lco_archive_rows()
    assert rows[0]["state"] == "running"
    web._persist_lco_archive_download_row(rows[0])
    assert queue.get(submitted["job_id"]) == before
    other = job_store.PostgresJobStore(dsn=_DSN)
    try:
        assert lco_queue.ArchiveQueue(other).get(submitted["job_id"])["lease_token"] == claimed["lease_token"]
    finally:
        other.close()
    persisted = json.dumps(queue.store.all())
    assert "private-token" not in persisted and "private-signed-url" not in persisted
    assert "https://archive" not in persisted
    assert lco_download._path(submitted["job_id"]).stat().st_mode & 0o777 == 0o600
    assert lco_download._spool().stat().st_mode & 0o777 == 0o700


def test_worker_download_resume_and_control_plane_ingest(queue, monkeypatch):
    submitted = enqueue(queue, auto_ingest=True, overwrite=True)
    first = queue.claim("download", "h1")
    dest = lco.frame_destination(frame())[2]
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"completed-before-crash")
    expire(queue, first)
    resumed = queue.claim("download", "h2")
    monkeypatch.setattr(job_store, "_OWNER", "worker")
    monkeypatch.setattr(lco, "_open_download_url", lambda *a, **k: pytest.fail("existing frame downloaded again"))
    database.forbid_catalog("test worker isolation")
    try:
        lco_download._execute(queue, resumed)
    finally:
        database.allow_catalog()
    status = lco.archive_download_status(submitted["job_id"])
    assert status["state"] == "pending" and status["phase"] == "awaiting_ingest"
    assert status["results"][0]["status"] == "exists"
    assert queue.claim("download", "h3") is None
    monkeypatch.setattr(job_store, "_OWNER", "web")
    calls = []
    monkeypatch.setattr("muscat_db.scanner.scan_date", lambda *a, **kw: {"total": 1})
    monkeypatch.setattr(database, "ingest_date", lambda *a: calls.append(a) or 1)
    registration = queue.claim("ingest", "control")
    lco_download._execute(queue, registration)
    assert len(calls) == 1
    status = lco.archive_download_status(submitted["job_id"])
    assert status["state"] == "done" and status["photometry_url"]
    assert not lco_download._path(submitted["job_id"]).exists()


def test_worker_transfer_errors_never_persist_secrets(queue, monkeypatch, caplog):
    submitted = enqueue(queue)
    monkeypatch.setattr(job_store, "_OWNER", "worker")
    def fail(*a, **kw):
        raise ValueError("private-token-alice https://s3.invalid/?private-signed-url")
    monkeypatch.setattr(lco, "_open_download_url", fail)
    lco_download._execute(queue, queue.claim("download", "h1"))
    assert queue.get(submitted["job_id"])["state"] == "error"
    text = json.dumps(queue.store.all()) + caplog.text
    assert "private-token" not in text and "private-signed-url" not in text


def test_fresh_transfer_and_expired_url_refresh(queue, monkeypatch):
    import urllib.error

    submitted = enqueue(queue)
    monkeypatch.setattr(job_store, "_OWNER", "worker")
    opened, tokens = [], []
    def download(url, timeout):
        opened.append(url)
        if len(opened) == 1:
            raise urllib.error.HTTPError(url, 403, "Expired", {}, None)
        return io.BytesIO(b"FITS data")
    def refresh(url, **kwargs):
        assert "basename_exact=" in url and ".fits" not in url
        tokens.append(kwargs["token"])
        return {"results": [dict(frame(), url="https://archive-api.lco.global/fresh")]}
    monkeypatch.setattr(lco, "_open_download_url", download)
    monkeypatch.setattr(lco, "_lco_api_request", refresh)
    lco_download._execute(queue, queue.claim("download", "h1"))
    assert tokens == ["private-token-alice"]
    assert lco.frame_destination(frame())[2].read_bytes() == b"FITS data"
    assert lco.archive_download_status(submitted["job_id"])["state"] == "done"


def test_monitor_public_failure_accounts_for_uncheckpointed_frames(queue):
    from muscat_db.lco_monitor import _prepared_downloads

    submitted = enqueue(queue)
    claimed = queue.claim("download", "h1")
    queue.finish(claimed["job_id"], claimed["lease_token"], error="worker failed")
    ready, errors = _prepared_downloads(lco.archive_download_status(submitted["job_id"]))
    assert not ready
    assert frame()["filename"] in errors


def test_wrong_spool_permissions_fail_closed(queue):
    spool = lco_download._spool()
    spool.chmod(0o755)
    with pytest.raises(lco.LcoError, match="mode 700"):
        enqueue(queue)
    assert queue.all() == []


def test_lease_loss_prevents_publication(queue, monkeypatch):
    submitted = enqueue(queue)
    claimed = queue.claim("download", "h1")
    dest = lco.frame_destination(frame())[2]
    dest.parent.mkdir(parents=True)
    def response(url, timeout):
        class Stream(io.BytesIO):
            def read(self, n):
                expire(queue, claimed)
                assert queue.claim("download", "replacement")
                lease.checkpoint({})
        return Stream(b"partial")
    monkeypatch.setattr(lco, "_open_download_url", response)
    with lco_download.Lease(queue, claimed) as lease:
        with pytest.raises(lco_download.LeaseLost):
            lco_download._frame(frame(), lco_download._read_manifest(submitted["job_id"]), lease)
    assert not dest.exists()
    assert not list(dest.parent.glob("*.part*"))


def test_ingest_cannot_run_on_worker(queue, monkeypatch):
    submitted = enqueue(queue)
    claimed = queue.claim("download", "h1")
    monkeypatch.setattr(job_store, "_OWNER", "worker")
    with lco_download.Lease(queue, claimed) as lease:
        with pytest.raises(RuntimeError, match="never register"):
            lco_download._ingest(lco_download._read_manifest(submitted["job_id"]), lease)
