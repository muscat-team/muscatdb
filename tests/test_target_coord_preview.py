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

    monkeypatch.setattr(phot.catalog_lookup, "resolve_target_astrometry", resolve)
    # A remote lookup would run in a thread that swallows exceptions, so it is
    # recorded as a call with allow_remote=None and the tests assert on that.
    monkeypatch.setattr(phot, "_resolve_archive_coords", lambda name: calls.append((name, None)))
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


def test_command_preview_does_not_go_online(monkeypatch):
    """Opening a dataset builds the command twice (page and /api/photometry/command)."""
    # The lookup runs in a worker thread that swallows exceptions, so record
    # the calls instead of raising from the stub.
    remote = []
    monkeypatch.setattr(phot.catalog_lookup, "resolve_target_astrometry", lambda name, allow_remote=True: None)
    monkeypatch.setattr(phot, "_resolve_archive_coords", lambda name: remote.append(name))
    command = phot.command_str("muscat3", "261001", "NoSuchStar", test_run=False)
    assert "--target_coord" not in command
    assert remote == []


def test_launch_command_still_resolves_online(monkeypatch):
    monkeypatch.setattr(phot.catalog_lookup, "resolve_target_astrometry", lambda name, allow_remote=True: None)
    monkeypatch.setattr(phot, "_resolve_archive_coords", lambda name: (10.5, -20.25, "simbad"))
    argv = phot.build_command("muscat3", "261001", "NoSuchStar", run_id="r1")
    i = argv.index("--target_coord")
    assert argv[i + 1:i + 3] == ["10.500000", " -20.250000"]


@pytest.mark.parametrize("headers,expected", [
    (["BJD_TDB,Flux,Err", "BJD_TDB,Flux,Err"], "column named BJD_TDB (column 1)"),
    # Before jpdeleon/prose2@bce5f807 the column was appended, not first.
    (["GJD_UTC,Flux,Err,BJD_TDB", "GJD_UTC,Flux,Err,BJD_TDB"], "column named BJD_TDB (column 4)"),
    (["BJD_TDB,Flux", "GJD_UTC,Flux,BJD_TDB"], "its position differs between the files"),
    (["GJD_UTC,Flux,Err"], "no BJD_TDB column"),
])
def test_time_note_reads_the_csv_headers(tmp_path, headers, expected):
    files = []
    for i, header in enumerate(headers):
        path = tmp_path / f"T_muscat3_b{i}_261001.csv"
        path.write_text(header + "\n1,2,3,4\n")
        files.append(path)
    assert expected in phot.csv_time_note(files)


def test_time_note_is_empty_without_csvs(tmp_path):
    assert phot.csv_time_note([]) == ""
    assert phot.csv_time_note([tmp_path / "missing.csv"]) == ""
