"""Tests for the job-store persistence seam (architecture audit C2).

DatabaseJobStore is exercised against a real temp SQLite DB. The
JobRepository/JobQueue/JobConcurrency conformance tests live in
tests/_job_store_contract.py and run against every backend (see also
tests/test_job_store_postgres.py); this file adds the seam's swap point
(set_job_store/get_job_store) checks, which are backend-agnostic.
"""

import pytest

from muscat_db import job_store
from muscat_db.job_store import (
    DatabaseJobStore,
    JobConcurrency,
    JobQueue,
    JobRepository,
    get_job_store,
    set_job_store,
)
from tests._job_store_contract import JobStoreContractTests


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "muscat.db"))
    return DatabaseJobStore()


class TestDatabaseJobStore(JobStoreContractTests):
    pass


class TestSeamSwap:
    def test_get_returns_installed_store(self):
        original = get_job_store()
        try:
            sentinel = object()
            set_job_store(sentinel)
            assert get_job_store() is sentinel
        finally:
            set_job_store(original)

    def test_database_store_satisfies_protocols(self):
        s = DatabaseJobStore()
        assert isinstance(s, JobRepository)
        assert isinstance(s, JobQueue)
        assert isinstance(s, JobConcurrency)

    def test_default_store_is_database_backed(self):
        assert isinstance(job_store.get_job_store(), DatabaseJobStore)
