"""Deterministic replacement-window rules; no telescope time is submitted."""

import pytest

from muscat_db import lco_autoschedule


def window(start, end):
    return {"start": f"2026-11-01T{start}:00Z", "end": f"2026-11-01T{end}:00Z"}


@pytest.mark.parametrize(
    ("conflict", "expected"),
    [
        (window("10:00", "10:30"), window("10:32", "12:00")),
        (window("11:30", "12:00"), window("10:00", "11:28")),
        (window("10:30", "10:40"), window("10:42", "12:00")),
        (window("10:50", "11:10"), None),
    ],
)
def test_conflict_selects_largest_acceptable_piece(conflict, expected):
    assert (
        lco_autoschedule.replacement_window(
            [window("10:00", "12:00")],
            now="2026-11-01T09:00:00Z",
            conflicts=[conflict],
            overhead_seconds=120,
            min_fraction=0.5,
            min_minutes=30,
        )
        == expected
    )


def test_threshold_exactly_met_and_observed_time_excluded():
    assert lco_autoschedule.replacement_window(
        [window("10:00", "12:00")],
        now="2026-11-01T10:30:00Z",
        observed_until="2026-11-01T11:00:00Z",
        min_fraction=0.5,
        min_minutes=60,
    ) == window("11:00", "12:00")


def test_elapsed_window_and_disabled_thresholds_do_not_resubmit():
    original = [window("10:00", "12:00")]
    assert (
        lco_autoschedule.replacement_window(
            original,
            now="2026-11-01T12:00:00Z",
            min_minutes=1,
        )
        is None
    )
    assert (
        lco_autoschedule.replacement_window(
            original,
            now="2026-11-01T09:00:00Z",
        )
        is None
    )


def test_clipped_window_keeps_original_fraction_denominator():
    assert (
        lco_autoschedule.replacement_window(
            [window("10:00", "12:00")],
            now="2026-11-01T09:00:00Z",
            observable_windows=[window("10:00", "10:45")],
            min_fraction=0.5,
        )
        is None
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1])
def test_invalid_thresholds_are_rejected(value):
    with pytest.raises(ValueError):
        lco_autoschedule.replacement_window(
            [window("10:00", "12:00")],
            now="2026-11-01T09:00:00Z",
            min_minutes=value,
        )


def test_rescue_policy_requires_threshold_and_proposal_budget(monkeypatch):
    monkeypatch.setenv("MUSCAT_LCO_RESCUE_BUDGET_MINUTES", '{"P1": 120}')
    assert lco_autoschedule.rescue_policy({}, "P1") is None
    policy = lco_autoschedule.rescue_policy({"min_minutes": 30}, "P1")
    assert policy == {
        "min_minutes": 30.0,
        "min_fraction": 0.0,
        "max_attempts": 1,
        "budget_seconds": 7200.0,
    }
    with pytest.raises(ValueError, match="budget"):
        lco_autoschedule.rescue_policy({"min_minutes": 30}, "P2")
    with pytest.raises(ValueError, match="one"):
        lco_autoschedule.rescue_policy({"min_minutes": 30, "max_attempts": 2}, "P1")


@pytest.mark.parametrize(
    "budgets", ["{}", '{"P1": 0}', '{"P1": -1}', '{"P1": "NaN"}', "[]", "invalid"]
)
def test_missing_or_invalid_budget_cannot_enable_rescue(monkeypatch, budgets):
    monkeypatch.setenv("MUSCAT_LCO_RESCUE_BUDGET_MINUTES", budgets)
    with pytest.raises(ValueError, match="budget"):
        lco_autoschedule.rescue_policy({"min_fraction": 0.5}, "P1")
