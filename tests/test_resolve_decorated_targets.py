"""LCO key-project names like ``TIC 434398831.11 (TOI 32.11)`` must resolve
through whichever half the catalogs / SIMBAD actually know."""
from __future__ import annotations

import pytest

from muscat_db import catalog

TIC_COORDS = {"ra": 93.9592639, "dec": 16.0238459, "source": "nasa"}


@pytest.fixture(autouse=True)
def _clean_cache():
    catalog._CATALOG_CACHE.clear()
    yield
    catalog._CATALOG_CACHE.clear()


def _key(name: str) -> str:
    return name.replace(" ", "").replace("-", "").upper()


def test_candidates_alias_then_primary_with_and_without_candidate_suffix():
    assert catalog._lco_key_project_candidates("TIC 434398831.11 (TOI 32.11)") == [
        "TOI 32.11", "TOI 32", "TIC 434398831.11", "TIC 434398831",
    ]


def test_candidates_of_plain_name_is_just_the_name():
    assert catalog._lco_key_project_candidates("WASP-12") == ["WASP-12"]


def test_falls_back_to_tic_when_toi_alias_is_unknown_to_the_catalogs(monkeypatch):
    seen = []

    def fake_query(name):
        seen.append(name)
        return TIC_COORDS if _key(name) == "TIC434398831" else None

    monkeypatch.setattr(catalog, "_query_target_coordinates", fake_query)
    monkeypatch.setattr(catalog.exp_calc, "resolve_target_coords", lambda n: None)
    res = catalog._resolve_archive_coords("TIC 434398831.11 (TOI 32.11)")
    assert res == (93.9592639, 16.0238459, "nasa")
    assert seen[0] == "TIC 434398831.11 (TOI 32.11)"


def test_falls_back_to_tic_on_simbad_when_catalogs_have_nothing(monkeypatch):
    monkeypatch.setattr(catalog, "_query_target_coordinates", lambda n: None)
    asked = []

    def fake_simbad(name):
        asked.append(name)
        return (93.96, 16.02) if _key(name) == "TIC434398831" else None

    monkeypatch.setattr(catalog.exp_calc, "resolve_target_coords", fake_simbad)
    res = catalog._resolve_archive_coords("TIC 434398831.11 (TOI 32.11)")
    assert res == (93.96, 16.02, "simbad")
    assert "TOI 32" in asked and "TIC 434398831" in asked


def test_unresolvable_decorated_name_is_none(monkeypatch):
    monkeypatch.setattr(catalog, "_query_target_coordinates", lambda n: None)
    monkeypatch.setattr(catalog.exp_calc, "resolve_target_coords", lambda n: None)
    assert catalog._resolve_archive_coords("TIC 1.01 (TOI 2.01)") is None
