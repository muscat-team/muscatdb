"""MuSCAT2 clock offsets (alex2 lost NTP, 2026-07-23..2026-10-22; issue #422).

The reporter defines ``diff = Time_correct - Time_FITS`` per night, so the
correction is *added* to the recorded time.
"""

from __future__ import annotations

import logging
import pathlib

import pytest

from muscat_db import time_correction as tc
from muscat_db.transit_fit import _copy_lightcurve_for_timer, _write_fit_inputs

_HEADER = "BJD_TDB,Flux,Err,Bkg(ADU)"
_ROWS = ["2461000.5,1.0,0.001,100", "2461000.6,0.99,0.001,101"]


def _csv(tmp_path: pathlib.Path, rows=_ROWS) -> pathlib.Path:
    src = tmp_path / "in.csv"
    src.write_text("\n".join([_HEADER, *rows]) + "\n")
    return src


def _bjd(path: pathlib.Path) -> list[float]:
    return [float(line.split(",")[0]) for line in path.read_text().splitlines()[1:]]


def test_table_matches_the_report():
    assert len(tc.MUSCAT2_CLOCK_OFFSETS) == 38
    assert tc.MUSCAT2_CLOCK_OFFSETS["260723"] == -0.1
    assert tc.MUSCAT2_CLOCK_OFFSETS["260804"] == 0.2
    assert tc.MUSCAT2_CLOCK_OFFSETS["260825"] == -39.3
    assert tc.MUSCAT2_CLOCK_OFFSETS["261021"] == -98.2


def test_listed_night_returns_the_offset():
    assert tc.clock_offset_seconds("muscat2", "261021") == -98.2


@pytest.mark.parametrize("inst", ["muscat", "muscat3", "muscat4", "sinistro", "sbig", "qhy600"])
def test_other_instruments_are_never_corrected(inst):
    assert tc.clock_offset_seconds(inst, "261021") == 0.0
    assert not tc.is_uncorrected_night(inst, "260806")


@pytest.mark.parametrize("date", ["260722", "261023", "250101"])
def test_muscat2_outside_the_window_is_untouched(date):
    assert tc.clock_offset_seconds("muscat2", date) == 0.0
    assert not tc.is_uncorrected_night("muscat2", date)


def test_unlisted_night_inside_the_window_is_flagged_not_guessed():
    # 260806 falls between the listed 260804 and 260808.
    assert tc.clock_offset_seconds("muscat2", "260806") == 0.0
    assert tc.is_uncorrected_night("muscat2", "260806")
    assert not tc.is_uncorrected_night("muscat2", "261021")


def test_copy_adds_the_offset_to_bjd_tdb(tmp_path):
    dst = tmp_path / "out.csv"
    _copy_lightcurve_for_timer(_csv(tmp_path), dst, time_offset_s=-98.2)
    got = _bjd(dst)
    assert got == pytest.approx([2461000.5 - 98.2 / 86400, 2461000.6 - 98.2 / 86400], abs=1e-9)


def test_copy_without_offset_leaves_times_unchanged(tmp_path):
    dst = tmp_path / "out.csv"
    _copy_lightcurve_for_timer(_csv(tmp_path), dst)
    assert _bjd(dst) == [2461000.5, 2461000.6]


def test_copy_never_modifies_the_source_csv(tmp_path):
    src = _csv(tmp_path)
    before = src.read_text()
    _copy_lightcurve_for_timer(src, tmp_path / "out.csv", time_offset_s=-98.2)
    assert src.read_text() == before


def test_missing_time_cell_is_kept_blank(tmp_path):
    dst = tmp_path / "out.csv"
    _copy_lightcurve_for_timer(_csv(tmp_path, [",1.0,0.001,100", *_ROWS]), dst, time_offset_s=10.0)
    lines = dst.read_text().splitlines()[1:]
    assert lines[0].split(",")[0] == ""
    assert float(lines[1].split(",")[0]) == pytest.approx(2461000.5 + 10.0 / 86400, abs=1e-9)


def test_write_fit_inputs_shifts_muscat2_lightcurves(tmp_path):
    rdir = tmp_path / "run"
    rdir.mkdir()
    _write_fit_inputs(rdir, "muscat2", "261021", "T", [_csv(tmp_path)], {})
    assert _bjd(rdir / "in.csv")[0] == pytest.approx(2461000.5 - 98.2 / 86400, abs=1e-9)


def test_write_fit_inputs_leaves_other_instruments_alone(tmp_path):
    rdir = tmp_path / "run"
    rdir.mkdir()
    _write_fit_inputs(rdir, "muscat3", "261021", "T", [_csv(tmp_path)], {})
    assert _bjd(rdir / "in.csv")[0] == 2461000.5


def test_write_fit_inputs_warns_on_unlisted_night(tmp_path, caplog):
    rdir = tmp_path / "run"
    rdir.mkdir()
    with caplog.at_level(logging.WARNING):
        _write_fit_inputs(rdir, "muscat2", "260806", "T", [_csv(tmp_path)], {})
    assert _bjd(rdir / "in.csv")[0] == 2461000.5
    assert "260806" in caplog.text and "uncorrected" in caplog.text
