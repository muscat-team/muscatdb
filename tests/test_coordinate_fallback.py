"""Shared target coordinates remain usable when CDS name resolution fails."""
from types import SimpleNamespace

import pytest

from muscat_db import catalog, exofop, exposure


@pytest.fixture(autouse=True)
def isolated_resolution(monkeypatch):
    catalog._CATALOG_CACHE.clear()
    exofop._exofop_cache.clear()
    monkeypatch.setattr(catalog, "_query_target_coordinates", lambda name: None)
    monkeypatch.setattr(exposure, "resolve_target_coords", lambda name: None)
    yield
    catalog._CATALOG_CACHE.clear()
    exofop._exofop_cache.clear()


def response(ra=117.929, dec=12.536):
    return {"basic_info": {"tic_id": "12345"}, "coordinates": {"ra": ra, "dec": dec}}


def test_shared_resolver_uses_exofop_and_preserves_unknown_epoch(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(json=lambda: response())

    monkeypatch.setattr(catalog, "_sync_get", get)
    expected = {"ra": 117.929, "dec": 12.536, "source": "exofop", "epoch": None,
                "pmra": None, "pmdec": None}
    assert catalog.resolve_target_astrometry("TOI-12345") == expected
    assert catalog._resolve_archive_coords("TOI-12345") == (117.929, 12.536, "exofop")
    assert len(calls) == 1
    assert calls[0][1]["timeout"] == 5.0


@pytest.mark.parametrize("ra,dec", [(float("nan"), 0), (1, float("inf")), (-1, 0), (360, 0), (1, 91), (None, 1), (True, 1)])
def test_exofop_invalid_coordinates_do_not_resolve(monkeypatch, ra, dec):
    monkeypatch.setattr(catalog, "_sync_get", lambda *a, **k: SimpleNamespace(json=lambda: response(ra, dec)))
    assert catalog.resolve_target_astrometry("invalid") is None


@pytest.mark.parametrize("source", ["nasa", "simbad"])
def test_exofop_does_not_override_existing_positions(monkeypatch, source):
    if source == "nasa":
        monkeypatch.setattr(catalog, "_query_target_coordinates", lambda name: {"ra": 10, "dec": -20, "source": source})
    else:
        monkeypatch.setattr(exposure, "resolve_target_coords", lambda name: (10, -20))
    monkeypatch.setattr(catalog, "_sync_get", lambda *a, **k: pytest.fail("ExoFOP should not be queried"))
    assert catalog.resolve_target_astrometry("known")["source"] == source


def test_exofop_failure_cache_expires_and_success_is_reused(monkeypatch):
    calls = []

    def get(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise TimeoutError("offline")
        return SimpleNamespace(json=lambda: response())

    monkeypatch.setattr(catalog, "_sync_get", get)
    assert exofop.fetch_target_by_name("new", _now=100) is None
    assert exofop.fetch_target_by_name("new", _now=110) is None
    data = exofop.fetch_target_by_name("new", _now=131)
    assert data["coordinates"]["ra"] == 117.929
    data["coordinates"]["ra"] = 0
    assert exofop.fetch_target_by_name("new", _now=200)["coordinates"]["ra"] == 117.929
    assert len(calls) == 2


def test_sesame_outage_is_cached_briefly_and_retried(monkeypatch):
    now = [100.0]
    calls = []
    monkeypatch.setattr(catalog.time, "monotonic", lambda: now[0])

    def sesame(name):
        calls.append(name)
        return (10, 20) if len(calls) > 1 else None

    monkeypatch.setattr(exposure, "resolve_target_coords", sesame)
    monkeypatch.setattr(catalog, "_sync_get", lambda *a, **k: SimpleNamespace(json=lambda: response()))
    assert catalog.resolve_target_astrometry("new")["source"] == "exofop"
    now[0] = 110
    assert catalog.resolve_target_astrometry("new")["source"] == "exofop"
    assert len(calls) == 1
    now[0] = 131
    assert catalog.resolve_target_astrometry("new")["source"] == "simbad"


def test_sesame_transport_has_timeout_and_encodes_names(monkeypatch):
    import httpx
    from muscat_db import http_client
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, text="%J 117.929 +12.536\n", request=httpx.Request("GET", url))

    monkeypatch.setattr(http_client, "get_sync_client", lambda: SimpleNamespace(get=get))
    coords = exposure._query_sesame_coords("TOI 123&extra")
    assert coords.ra.deg == 117.929
    assert calls[0][0].endswith("TOI%20123%26extra")
    assert calls[0][1]["timeout"] == 5.0


def test_sesame_primary_timeout_still_tries_the_existing_mirror(monkeypatch):
    import httpx
    from muscat_db import http_client
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        assert kwargs["timeout"] == 5.0
        if len(calls) == 1:
            raise httpx.ConnectTimeout("CDS unavailable")
        return httpx.Response(200, text="%J 117.929 +12.536\n", request=httpx.Request("GET", url))

    monkeypatch.setattr(http_client, "get_sync_client", lambda: SimpleNamespace(get=get))
    assert exposure._query_sesame_coords("TOI-12345").ra.deg == 117.929
    assert "vizier.cfa.harvard.edu" in calls[1]
