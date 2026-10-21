"""LCO schedule coordinates: catalog CSVs first, SIMBAD fallback."""

from __future__ import annotations

from fastapi.testclient import TestClient

from muscat_db import web
from muscat_db.web import app


SIMBAD = {"ra": 10.5, "dec": -20.25, "source": "simbad", "epoch": 2000.0, "pmra": None, "pmdec": None}


def test_target_coordinates_returns_resolver_astrometry(monkeypatch):
    monkeypatch.setattr(web, "resolve_target_astrometry", lambda t: SIMBAD)
    assert web._target_coordinates("X") == SIMBAD


def test_target_coordinates_none_when_unresolved(monkeypatch):
    monkeypatch.setattr(web, "resolve_target_astrometry", lambda t: None)
    assert web._target_coordinates("X") is None


def test_target_coordinates_preserves_catalog_source(monkeypatch):
    monkeypatch.setattr(web, "resolve_target_astrometry", lambda t: {**SIMBAD, "source": "nasa", "epoch": 2015.5})
    assert web._target_coordinates("HIP67522")["source"] == "nasa"


def test_target_info_falls_back_to_simbad_when_catalog_misses(monkeypatch, tmp_path):
    # Catalog-less target: the planet resolvers return nothing, so coordinates
    # must come from the SIMBAD fallback rather than being null.
    monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "coords.db"))
    monkeypatch.setattr(web, "_query_target_planets_nasa", lambda t: {})
    monkeypatch.setattr(web, "_query_target_planets_toi", lambda t: {})
    monkeypatch.setattr(web, "_query_target_planets_catalog", lambda t: {})
    monkeypatch.setattr(web, "resolve_target_astrometry", lambda t: {**SIMBAD, "ra": 232.955, "dec": -34.270})
    client = TestClient(app)
    data = client.get("/api/ephemeris/target-info", params={"target": "TIC89071445"}).json()
    assert data["ok"] is True
    assert data["coordinates"] == {**SIMBAD, "ra": 232.955, "dec": -34.270}
