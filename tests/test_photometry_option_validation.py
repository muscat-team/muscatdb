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
