"""Launch-time resolution stays bounded and recovers after a CDS outage."""
import threading
import time
from contextvars import ContextVar

import pytest
from astropy.coordinates import SkyCoord

from muscat_db import catalog, exposure, photometry as phot


@pytest.fixture(autouse=True)
def isolated_resolution(monkeypatch):
    catalog._CATALOG_CACHE.clear()
    monkeypatch.setattr(catalog, "_query_target_coordinates", lambda name: None)
    monkeypatch.setattr(phot, "_prose_prefix", lambda: ["python", "-m", "photometry"])
    monkeypatch.setattr(phot, "_COORD_LOOKUP_TIMEOUT_S", 0.03, raising=False)
    yield
    catalog._CATALOG_CACHE.clear()


@pytest.mark.parametrize("entry", ["command", "note", "api"])
def test_slow_resolver_bounds_the_whole_lookup(monkeypatch, mock_db, entry):
    release, finished = threading.Event(), threading.Event()
    calls = []

    def resolve(name):
        calls.append(name)
        try:
            release.wait(0.3)
            return None
        finally:
            finished.set()

    monkeypatch.setattr(phot, "_resolve_archive_coords", resolve)
    from fastapi.testclient import TestClient
    from muscat_db import web

    client = TestClient(web.app)
    try:
        start = time.monotonic()
        if entry == "command":
            assert "--target_coord" not in phot.build_command("qhy600", "260825", "DS Tuc b")
        elif entry == "note":
            assert "not resolved" in phot.target_coord_note("DS Tuc b")
        else:
            response = client.post("/api/photometry/command", json={
                "inst": "qhy600", "date": "260825", "target": "DS Tuc b",
            })
            assert response.status_code == 200
            assert response.json()["error"] is None
            assert "--target_coord" not in response.json()["command"]
        assert time.monotonic() - start < 0.2
        assert calls == ["DS Tuc"]
    finally:
        release.set()
        assert finished.wait(1)
        # Let the bounded worker exit before monkeypatch restores its dependencies.
        time.sleep(0.01)


def test_resolver_exception_does_not_abort_launch_and_next_call_recovers(monkeypatch):
    def unavailable(name):
        raise TimeoutError("CDS unavailable")

    monkeypatch.setattr(phot, "_resolve_archive_coords", unavailable)
    assert "--target_coord" not in phot.build_command("qhy600", "260825", "DS Tuc b")
    monkeypatch.setattr(phot, "_resolve_archive_coords", lambda name: (354.914503, -69.195752, "simbad"))
    cmd = phot.build_command("qhy600", "260825", "DS Tuc b")
    i = cmd.index("--target_coord")
    assert cmd[i + 1:i + 3] == ["354.914503", " -69.195752"]


def test_failed_sesame_query_is_not_cached_for_process_lifetime(monkeypatch):
    available = False

    def from_name(name):
        if not available:
            raise TimeoutError("CDS unavailable")
        return SkyCoord(10, -20, unit="deg")

    monkeypatch.setattr(exposure.SkyCoord, "from_name", from_name)
    assert catalog._resolve_archive_coords("DS Tuc") is None
    assert "simbad_DS TUC" not in catalog._CATALOG_CACHE
    available = True
    assert catalog._resolve_archive_coords("DS Tuc") == (10.0, -20.0, "simbad")


def test_only_successful_remote_results_are_cached(monkeypatch):
    calls = []

    def resolve(name):
        calls.append(name)
        return None if len(calls) == 1 else (10.0, -20.0)

    monkeypatch.setattr(exposure, "resolve_target_coords", resolve)
    assert catalog._resolve_archive_coords("Unlisted") is None
    assert catalog._resolve_archive_coords("Unlisted") == (10.0, -20.0, "simbad")
    assert catalog._resolve_archive_coords("Unlisted") == (10.0, -20.0, "simbad")
    assert calls == ["Unlisted", "Unlisted"]


def test_expired_deadline_stops_spelling_variant_requests(monkeypatch):
    calls = []

    def from_name(name):
        calls.append(name)
        time.sleep(0.04)
        raise TimeoutError("CDS unavailable")

    deadline = ContextVar("test_deadline", default=time.monotonic() + 0.02)
    monkeypatch.setattr(exposure, "_COORD_RESOLUTION_DEADLINE", deadline, raising=False)
    monkeypatch.setattr(exposure.SkyCoord, "from_name", from_name)
    assert exposure.resolve_target_coords("DS Tuc") is None
    assert calls == ["DS Tuc"]


def test_inflight_lookup_is_shared_and_capacity_has_no_backlog(monkeypatch):
    release, finished = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(phot, "_COORD_LOOKUP_LIMIT", 1, raising=False)

    def resolve(name):
        calls.append(name)
        try:
            release.wait(0.3)
            return None
        finally:
            finished.set()

    monkeypatch.setattr(phot, "_resolve_archive_coords", resolve)
    try:
        assert phot._auto_target_coord("DS Tuc b") is None
        assert phot._auto_target_coord("DS Tuc b") is None
        assert phot._auto_target_coord("AD3116b") is None
        assert calls == ["DS Tuc"]
    finally:
        release.set()
        assert finished.wait(1)
        time.sleep(0.01)


def test_log_does_not_retry_resolution_absent_from_built_command(monkeypatch):
    calls = []

    def unexpected(name):
        calls.append(name)
        raise RuntimeError("log generation retried a timed-out command lookup")

    monkeypatch.setattr(phot, "_resolve_archive_coords", unexpected)
    assert "not resolved" in phot.target_coord_note("DS Tuc b", command=["photometry"])
    assert calls == []


def test_offline_catalog_resolution_does_not_need_a_remote_worker(monkeypatch):
    monkeypatch.setattr(phot, "_COORD_LOOKUP_LIMIT", 0)
    monkeypatch.setattr(catalog, "_query_target_coordinates", lambda name: {
        "ra": 354.914503, "dec": -69.195752, "source": "nasa",
    } if name == "DS Tuc" else None)
    assert phot._auto_target_coord("DS Tuc b") == (354.914503, -69.195752, "nasa", "DS Tuc")


def test_cached_remote_position_does_not_need_a_remote_worker(monkeypatch):
    monkeypatch.setattr(phot, "_COORD_LOOKUP_LIMIT", 0)
    catalog._CATALOG_CACHE["simbad_DS TUC"] = (354.914503, -69.195752, "simbad")
    assert phot._auto_target_coord("DS Tuc b") == (354.914503, -69.195752, "simbad", "DS Tuc")
