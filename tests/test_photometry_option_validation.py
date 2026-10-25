"""List-valued photometry options must stay plain numbers.

``build_command`` forwards each item of ``comparison_ids``, ``avoid_comparison_ids``
and the two ``exclude_*_jd`` lists as its own argv element, so an item that is not a
number could be read by prose2's argparse as a flag of its own and override
``--data_dir`` or ``--results_dir``. ``target_id`` and ``calib_dir`` reach the command
as single values and are held to the same rule.
"""

from __future__ import annotations

import pytest

from muscat_db import photometry as phot

INST = "sinistro"
DATE = "260101"
TARGET = "TOI-1234"

LIST_FIELDS = ("comparison_ids", "avoid_comparison_ids", "exclude_after_jd", "exclude_before_jd")
BAD_ITEMS = (
    "1,--data_dir,/elsewhere",
    "--results_dir",
    "1, --glob ,*",
    "1,2;rm",
    "1,x",
    "nan",
    "inf",
    "-1",
)


def _options(**extra) -> dict:
    # avoid_comparison_ids needs a reference band to pass the earlier check.
    return {"bands": ["gp"], "ref_band": "gp", **extra}


def _error(options: dict, inst: str = INST) -> str | None:
    return phot.validate_run_options(phot.normalize_run_options(options), inst=inst)


@pytest.mark.parametrize("field", LIST_FIELDS)
@pytest.mark.parametrize("bad", BAD_ITEMS)
def test_validation_rejects_non_numeric_list_items(field, bad):
    err = _error(_options(**{field: bad}))
    assert err is not None and "comma-separated" in err


@pytest.mark.parametrize(
    "field,value",
    [
        ("comparison_ids", "0,1,2"),
        ("comparison_ids", " 5 , 7 "),
        ("avoid_comparison_ids", "5,7,12"),
        ("exclude_after_jd", "2460423.10,2460423.60"),
        ("exclude_before_jd", "2460423.20"),
        ("comparison_ids", ""),
    ],
)
def test_validation_accepts_the_numbers_the_form_sends(field, value):
    assert _error(_options(**{field: value})) is None


@pytest.mark.parametrize("bad", ["--data_dir", "12 --glob x", "abc", "-3", "1.5"])
def test_validation_rejects_a_non_integer_target_id(bad):
    err = _error(_options(target_id=bad))
    assert err is not None and "target ID" in err


def test_validation_accepts_an_integer_target_id():
    assert _error(_options(target_id="42")) is None


def test_validation_rejects_a_calibration_directory():
    err = _error(_options(calib_dir="/home/victim/calibrated"), inst="muscat2")
    assert err is not None and "calibration directory" in err


@pytest.mark.parametrize("field", LIST_FIELDS)
def test_build_command_never_forwards_a_flag_from_a_list_item(field, monkeypatch, tmp_path):
    """Defense in depth: stored or queued options skip ``validate_run_options``."""
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
    argv = phot.build_command(
        INST, DATE, TARGET, _options(**{field: "1,--data_dir,/elsewhere,--results_dir,/x"}),
        test_run=False,
    )
    assert argv.count("--data_dir") == 1
    assert argv.count("--results_dir") == 1
    assert "/elsewhere" not in argv and "/x" not in argv


def test_build_command_drops_a_flag_like_target_id(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
    argv = phot.build_command(INST, DATE, TARGET, _options(target_id="--data_dir"), test_run=False)
    assert "--tID" not in argv
    assert argv.count("--data_dir") == 1


def test_build_command_ignores_a_requested_calibration_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
    argv = phot.build_command(
        "muscat2", DATE, TARGET, _options(calib_dir="/home/victim/calibrated"), test_run=False
    )
    value = argv[argv.index("--calib_dir") + 1]
    assert value == str(phot.results_dir("muscat2", DATE)) + "_calibrated"
    assert "/home/victim/calibrated" not in argv


# ── free-text options at build time (#273 item 7) ─────────────────────────
# Each of these reaches prose2 as an argv element. validate_run_options refuses
# a bad value on the form, but queued and stored options reach build_command
# without it (fleet worker, sync_jobs), so build_command checks them itself.

_TIME = {"bands": ["gp"], "ref_select": "time"}
_APER = {"bands": ["gp"], "aper_radii": "10,20,2", "annulus": "25,40", "cutout_size": 90}
FORWARDED_BAD = [
    ({"bands": ["gp", "--data_dir"]}, "bands"),
    ({"bands": ["gp"], "ref_band": "--glob"}, "ref_band"),
    ({**_TIME, "ref_time": "--data_dir"}, "ref_time"),
    ({**_APER, "aper_unit": "--data_dir"}, "aper_unit"),
    ({**_APER, "aper_radii": "--data_dir"}, "aper_radii"),
    ({**_APER, "annulus": "--data_dir"}, "annulus"),
    ({"bands": ["gp"], "ccd_trim": "--data_dir"}, "ccd_trim"),
    ({"bands": ["gp"], "wcs_method": "--data_dir"}, "wcs_method"),
    ({"bands": ["gp"], "centroid_method": "--data_dir"}, "centroid_method"),
    ({"bands": ["gp"], "site": "--data_dir"}, "site"),
    ({"bands": ["gp"], "telescope": "--data_dir"}, "telescope"),
    ({"bands": ["gp"], "mode": "--data_dir"}, "mode"),
    ({"bands": ["gp"], "cmap": "--data_dir"}, "cmap"),
    ({"bands": ["gp"], "nan_imputation_method": "--data_dir"}, "nan_imputation_method"),
    ({"bands": ["gp"], "target_coord": "--data_dir /elsewhere"}, "target_coord"),
    ({"bands": ["gp"], "target_coord": "10.5 --results_dir"}, "target_coord"),
    ({"bands": ["gp"], "target_coord": "10.5 -45 --glob x"}, "target_coord"),
    # --avoid_nearby_star takes an optional value (nargs="?"), so a flag-like
    # value becomes a new option that overrides the real one (#312 review).
    ({"bands": ["gp"], "avoid_nearby_star_mode": "custom", "avoid_nearby_star": "--results_dir=/elsewhere"},
     "avoid_nearby_star"),
    ({"bands": ["gp"], "avoid_nearby_star_mode": "custom", "avoid_nearby_star": "--data_dir=/elsewhere"},
     "avoid_nearby_star"),
    ({"bands": ["gp"], "avoid_nearby_star_mode": "unknown", "avoid_nearby_star": "--results_dir=/elsewhere"},
     "avoid_nearby_star"),
]


@pytest.mark.parametrize(("options", "field"), FORWARDED_BAD, ids=[f for _, f in FORWARDED_BAD])
def test_build_command_refuses_an_invalid_free_text_option(options, field, monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
    with pytest.raises(ValueError, match=field):
        phot.build_command(INST, DATE, TARGET, options, test_run=False)


@pytest.mark.parametrize(("mode", "value", "forwarded"), [
    ("custom", "5", ["--avoid_nearby_star", "5.0"]),
    ("custom", 2.5, ["--avoid_nearby_star", "2.5"]),
    ("custom", "", ["--avoid_nearby_star"]),
    ("auto", "--results_dir=/elsewhere", ["--avoid_nearby_star"]),
    ("off", "--results_dir=/elsewhere", []),
])
def test_build_command_checks_avoid_nearby_star_only_where_it_forwards_it(
    mode, value, forwarded, monkeypatch, tmp_path
):
    # Only a value build_command actually forwards is checked, so a queued run
    # whose unused value is junk still launches.
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
    options = {"bands": ["gp"], "avoid_nearby_star_mode": mode, "avoid_nearby_star": value}
    argv = phot.build_command(INST, DATE, TARGET, options, test_run=False)
    tail = argv[argv.index("--avoid_nearby_star"):][: len(forwarded)] if forwarded else []
    assert tail == forwarded
    assert ("--avoid_nearby_star" in argv) == bool(forwarded)
    assert "--results_dir=/elsewhere" not in argv


@pytest.mark.parametrize(("options", "field"), FORWARDED_BAD, ids=[f for _, f in FORWARDED_BAD])
def test_the_form_refuses_whatever_build_command_refuses(options, field):
    """Otherwise a run the form accepted would fail at launch."""
    assert _error(options) is not None


@pytest.mark.parametrize("coord", [
    "159.157968 -64.798231",
    "63.928339 29.166542",
    "19:34:55.87 +36:48:55.78",
    "15:13:47.515 -45:00:42.12",
    "12h34m56.7s -45d00m42s",
    "1.2e-05 +3.4E+01",
])
def test_build_command_forwards_the_coordinates_the_form_accepts(coord, monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
    options = {"bands": ["gp"], "target_coord": coord}
    assert _error(options) is None
    argv = phot.build_command(INST, DATE, TARGET, options, test_run=False)
    ra, dec = coord.split()
    i = argv.index("--target_coord")
    assert argv[i + 1] == ra and argv[i + 2].strip() == dec


def test_build_command_still_accepts_every_valid_choice(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
    options = {
        **_APER, "aper_unit": "fwhm", "ccd_trim": "10, 10", "wcs_method": "twirl",
        "centroid_method": phot.CENTROID_METHODS[-1], "site": phot.SINISTRO_SITES[0].upper(),
        "telescope": "1m0-05", "mode": phot.SINISTRO_MODES[0], "cmap": phot.CMAP_CHOICES[-1],
        "nan_imputation_method": phot.NAN_IMPUTATION_METHODS[-1],
    }
    assert _error(options) is None
    argv = phot.build_command(INST, DATE, TARGET, options, test_run=False)
    assert argv[argv.index("--aper_unit") + 1] == "fwhm"
    assert argv[argv.index("--site") + 1] == phot.SINISTRO_SITES[0].upper()
