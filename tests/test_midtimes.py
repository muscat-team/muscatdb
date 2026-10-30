"""Parsing an uploaded list of mid-transit times (/lco/schedule, #350).

The list is typed or pasted by an observer, so the parser is the system
boundary: every malformed line has to be named rather than skipped, and a time
that is plausibly in the wrong system (a reduced BJD) must be refused instead
of being scheduled thousands of days off.
"""

from __future__ import annotations

import pytest

from muscat_db import midtimes
from muscat_db.midtimes import MidtimeError, parse_midtimes


def test_bare_bjd_column_without_header():
    parsed = parse_midtimes("2461000.5\n2461002.55\n")

    assert [r.bjd for r in parsed.rows] == [2461000.5, 2461002.55]
    assert all(r.utc_jd is None and r.unc_min is None for r in parsed.rows)
    assert parsed.time_systems == {"bjd_tdb": 2, "utc": 0}


@pytest.mark.parametrize("separator", [",", " ", "\t", "  "])
def test_columns_use_days(separator):
    parsed = parse_midtimes(separator.join(["tc_err", "tc"]) + "\n" + separator.join(["0.001", "2461000.5"]))
    assert parsed.rows[0].unc_min == pytest.approx(1.44)
    assert parsed.rows[0].bjd == 2461000.5


@pytest.mark.parametrize("column", ["epoch", "tc_err_min"])
def test_removed_columns_are_rejected(column):
    with pytest.raises(MidtimeError, match="unknown column"):
        parse_midtimes(f"tc,{column}\n2461000.5,1")


def test_iso_utc_times_are_kept_as_utc():
    parsed = parse_midtimes("tc\n2026-11-03T04:12:00Z\n2026-11-05T00:00:00\n")

    assert [r.bjd for r in parsed.rows] == [None, None]
    assert parsed.rows[0].utc_jd == pytest.approx(2461347.675)
    assert parsed.time_systems == {"bjd_tdb": 0, "utc": 2}


def test_offset_iso_time_is_converted_to_utc():
    a = parse_midtimes("2026-11-03T13:12:00+09:00").rows[0]
    b = parse_midtimes("2026-11-03T04:12:00Z").rows[0]
    assert a.utc_jd == pytest.approx(b.utc_jd)


def test_comments_and_blank_lines_are_ignored():
    parsed = parse_midtimes("# predicted by photodynamics\n\n2461000.5  # first\n")
    assert len(parsed.rows) == 1


def test_reduced_bjd_is_refused_with_a_hint():
    with pytest.raises(MidtimeError) as exc:
        parse_midtimes("3000.5\n")
    assert "line 1" in str(exc.value)
    assert "full BJD_TDB" in str(exc.value)


def test_every_bad_line_is_named():
    with pytest.raises(MidtimeError) as exc:
        parse_midtimes("2461000.5\nabc\n2461002.5, -1\nnan\n")
    errors = exc.value.errors
    assert [e.split(":")[0] for e in errors] == ["line 2", "line 3", "line 4"]


def test_unknown_header_column_is_refused():
    with pytest.raises(MidtimeError, match="unknown column 'depth'"):
        parse_midtimes("tc,depth\n2461000.5,0.01\n")


def test_header_without_tc_is_refused():
    with pytest.raises(MidtimeError, match="no 'tc' column"):
        parse_midtimes("tc_err\n0.001\n")


def test_epochs_must_be_all_or_none():
    with pytest.raises(MidtimeError, match="epoch"):
        parse_midtimes("tc,epoch\n2461000.5,1\n2461002.5,\n")


def test_non_integer_epoch_is_refused():
    with pytest.raises(MidtimeError, match="line 1"):
        parse_midtimes("2461000.5, 0.001, 1.5\n")


def test_duplicate_times_are_dropped_and_counted():
    parsed = parse_midtimes("2461000.5\n2461002.5\n2461000.5\n")
    assert [r.bjd for r in parsed.rows] == [2461000.5, 2461002.5]
    assert parsed.duplicates_dropped == 1


def test_third_positional_column_is_refused():
    with pytest.raises(MidtimeError, match="expected at most 2"):
        parse_midtimes("2461000.5,0,5\n2461002.5,0,5\n")


def test_empty_input_is_refused():
    with pytest.raises(MidtimeError, match="no mid-transit times"):
        parse_midtimes("# only a comment\n\n")


def test_row_cap_bounds_the_work(monkeypatch):
    monkeypatch.setattr(midtimes, "MAX_ROWS", 3)
    with pytest.raises(MidtimeError, match="at most 3"):
        parse_midtimes("\n".join(str(2461000.5 + i) for i in range(4)))


def test_text_size_cap(monkeypatch):
    monkeypatch.setattr(midtimes, "MAX_TEXT_CHARS", 20)
    with pytest.raises(MidtimeError, match="too large"):
        parse_midtimes("2461000.5\n" * 5)


def test_error_list_is_truncated_but_counted(monkeypatch):
    monkeypatch.setattr(midtimes, "MAX_REPORTED_ERRORS", 2)
    with pytest.raises(MidtimeError) as exc:
        parse_midtimes("a\nb\nc\nd\n")
    assert len(exc.value.errors) == 3
    assert exc.value.errors[-1] == "... and 2 more"
