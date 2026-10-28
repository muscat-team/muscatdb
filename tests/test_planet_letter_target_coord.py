"""A planet-lettered OBJECT name ("DS Tuc b") resolves through its host star.

Neither MAST nor SIMBAD -- prose2's own resolvers -- place "DS Tuc b" or "DS
Tuc c", so every qhy600 run on DS Tuc aborted before photometry. muscat-db now
resolves every target itself, host name first, passes --target_coord, and
records in the run log where the coordinates came from.
"""

import io

import pytest

from muscat_db import photometry as phot
from muscat_db.target_names import strip_planet_letter

DS_TUC = (354.914503, -69.195752, "nasa")


@pytest.mark.parametrize(
    ("raw", "host"),
    [
        ("DS Tuc b", "DS Tuc"),
        ("DS Tuc c", "DS Tuc"),
        ("TOI-1224 b", "TOI-1224"),
        ("V1298Tau-c", "V1298Tau"),
        ("V1298tau_c", "V1298tau"),
        ("WASP-12b", "WASP-12"),
        ("HD149026b", "HD149026"),
        ("Kepler-90h", "Kepler-90"),
        ("TOI06908.01c", "TOI06908.01"),
        ("WASP-77Ab", "WASP-77A"),
        ("XO-2Nb", "XO-2N"),
        ("55 Cnc e", "55 Cnc"),
    ],
)
def test_strips_planet_letter(raw, host):
    assert strip_planet_letter(raw) == host


@pytest.mark.parametrize(
    "raw",
    [
        # The closing letter is part of the star's name.
        "DS Tuc", "DSTuc", "AU Mic", "AUMic", "V1298Tauc", "TOI-1224",
        # It tells distinct transients or minor planets apart.
        "WFST240506b", "EP240627b", "98943b", "2019AV4b",
        # Not a planet letter at all.
        "KELT-19As", "M31N2008-12a", "kepler-412becho",
        "TIC245728942.01(TOI5012.01)",
    ],
)
def test_keeps_names_that_are_not_planet_lettered(raw):
    assert strip_planet_letter(raw) == raw


def _resolver(known):
    calls = []

    def resolve(name):
        calls.append(name)
        return known.get(name)

    return resolve, calls


def test_planet_lettered_target_gets_its_host_coordinates(monkeypatch):
    resolve, calls = _resolver({"DS Tuc": DS_TUC})
    monkeypatch.setattr(phot, "_resolve_archive_coords", resolve)
    cmd = phot.build_command("qhy600", "260825", "DS Tuc b")
    # The name still selects the frames by OBJECT; only the position changes.
    assert cmd[cmd.index("--target_name") + 1] == "DS Tuc b"
    i = cmd.index("--target_coord")
    assert cmd[i + 1 : i + 3] == ["354.914503", " -69.195752"]
    assert calls[0] == "DS Tuc"


def test_plain_name_is_resolved_too(monkeypatch):
    resolve, calls = _resolver({"TOI-1": (100.0, 10.0, "toi")})
    monkeypatch.setattr(phot, "_resolve_archive_coords", resolve)
    cmd = phot.build_command("muscat3", "260101", "TOI-1")
    i = cmd.index("--target_coord")
    assert cmd[i + 1 : i + 3] == ["100.000000", "10.000000"]
    assert calls == ["TOI-1"]


def test_falls_back_to_the_full_name_when_the_host_does_not_resolve(monkeypatch):
    resolve, calls = _resolver({"M51b": (202.47, 47.19, "simbad")})
    monkeypatch.setattr(phot, "_resolve_archive_coords", resolve)
    cmd = phot.build_command("muscat3", "260101", "M51b")
    assert "--target_coord" in cmd
    assert calls == ["M51", "M51b"]


def test_unresolvable_name_leaves_resolution_to_prose(monkeypatch):
    resolve, _ = _resolver({})
    monkeypatch.setattr(phot, "_resolve_archive_coords", resolve)
    cmd = phot.build_command("qhy600", "260825", "DS Tuc b")
    assert "--target_coord" not in cmd


def test_coordinates_entered_on_the_page_are_never_overridden(monkeypatch):
    calls = []

    def _boom(name):
        calls.append(name)
        raise AssertionError("must not resolve when target_coord is given")

    monkeypatch.setattr(phot, "_resolve_archive_coords", _boom)
    cmd = phot.build_command("qhy600", "260825", "DS Tuc b", {"target_coord": "1.5 -2.5"})
    i = cmd.index("--target_coord")
    assert cmd[i + 1 : i + 3] == ["1.5", " -2.5"]
    assert calls == []


@pytest.mark.parametrize(
    ("known", "options", "expected"),
    [
        (
            {"DS Tuc": DS_TUC}, {},
            "target coordinates: 354.914503 -69.195752 deg from the NASA Exoplanet Archive catalog, "
            "resolved as 'DS Tuc' for OBJECT 'DS Tuc b'",
        ),
        (
            {}, {},
            "target coordinates: not resolved by muscat-db (tried 'DS Tuc', 'DS Tuc b'); "
            "prose resolves --target_name itself",
        ),
        ({}, {"target_coord": "1.5 -2.5"}, "target coordinates: 1.5 -2.5 (entered on the photometry page)"),
    ],
)
def test_log_header_records_where_the_coordinates_came_from(monkeypatch, known, options, expected):
    resolve, _ = _resolver(known)
    monkeypatch.setattr(phot, "_resolve_archive_coords", resolve)
    log = io.StringIO()
    phot._write_log_header(log, ["photometry"], phot.target_coord_note("DS Tuc b", options))
    assert expected + "\n" in log.getvalue()


def test_log_header_names_no_resolved_alias_when_the_name_resolved_as_is(monkeypatch):
    resolve, _ = _resolver({"TOI-1": (100.0, 10.0, "toi")})
    monkeypatch.setattr(phot, "_resolve_archive_coords", resolve)
    assert phot.target_coord_note("TOI-1", {}) == (
        "target coordinates: 100.000000 10.000000 deg from the TOI catalog"
    )
