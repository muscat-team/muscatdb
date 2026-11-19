"""The photometry page says what a run sends as --target_coord (issue #457)."""
import pytest

from muscat_db import photometry as phot


@pytest.fixture
def lookups(monkeypatch):
    """Record local lookups and fail the test if a remote one starts."""
    calls = []
    known = {"TOI-1000": {"ra": 117.929, "dec": 12.536, "source": "toi"}}

    def resolve(name, allow_remote=True):
        calls.append((name, allow_remote))
        return known.get(name)

    def remote(name):
        raise AssertionError(f"page preview started a remote lookup for {name!r}")

    monkeypatch.setattr(phot.catalog_lookup, "resolve_target_astrometry", resolve)
    monkeypatch.setattr(phot, "_resolve_archive_coords", remote)
    return calls


def test_preview_gives_the_coordinate_and_its_source(lookups):
    assert phot.target_coord_preview("TOI-1000") == "117.929000 12.536000 deg from the TOI catalog"
    assert all(allow_remote is False for _, allow_remote in lookups)


def test_preview_uses_the_words_of_the_run_log(lookups):
    assert phot.target_coord_note("TOI-1000") == "target coordinates: " + phot.target_coord_preview("TOI-1000")


def test_preview_names_the_host_it_resolved(lookups):
    assert phot.target_coord_preview("TOI-1000b") == (
        "117.929000 12.536000 deg from the TOI catalog, resolved as 'TOI-1000' for OBJECT 'TOI-1000b'"
    )


def test_preview_of_an_unknown_target_does_not_go_online(lookups):
    text = phot.target_coord_preview("NoSuchStar")
    assert text.startswith("not in the local catalogs")
    assert lookups and all(allow_remote is False for _, allow_remote in lookups)


def test_preview_is_empty_without_a_target(lookups):
    assert phot.target_coord_preview("") == ""
    assert lookups == []
