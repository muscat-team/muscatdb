"""Pure window calculations for opt-in LCO automatic scheduling."""

from __future__ import annotations

import datetime as dt
import math


def _timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _interval(window: dict) -> tuple[dt.datetime, dt.datetime]:
    start, end = _timestamp(window["start"]), _timestamp(window["end"])
    if end <= start:
        raise ValueError("Window end must be after its start")
    return start, end


def _threshold(value: float | None, name: str, maximum: float | None = None) -> float:
    number = 0.0 if value is None else float(value)
    if not math.isfinite(number) or number < 0 or (maximum is not None and number > maximum):
        raise ValueError(f"Invalid {name}")
    return number


def replacement_window(
    original_windows: list[dict],
    *,
    now: str,
    min_fraction: float | None = None,
    min_minutes: float | None = None,
    observable_windows: list[dict] | None = None,
    conflicts: list[dict] | None = None,
    observed_until: str | None = None,
    overhead_seconds: float = 0,
) -> dict | None:
    """Largest future observable piece meeting every enabled threshold.

    ``observed_until`` includes the last exposure's duration. ``conflicts``
    must contain verified spans, never spans inferred from a request state.
    Fractions use each original alternative window's length, before clipping.
    Equal-length pieces prefer the earlier start for deterministic planning.
    """
    fraction = _threshold(min_fraction, "minimum fraction", 1)
    minutes = _threshold(min_minutes, "minimum minutes")
    overhead = dt.timedelta(seconds=_threshold(overhead_seconds, "setup overhead"))
    if not fraction and not minutes:
        return None
    earliest = _timestamp(now)
    if observed_until:
        earliest = max(earliest, _timestamp(observed_until))
    blocked = [_interval(w) for w in conflicts or []]
    observable = None if observable_windows is None else [_interval(w) for w in observable_windows]
    candidates = []
    for original in original_windows:
        start, end = _interval(original)
        required = max((end - start).total_seconds() * fraction, minutes * 60)
        for visible_start, visible_end in observable if observable is not None else [(start, end)]:
            pieces = [(max(start, earliest, visible_start), min(end, visible_end))]
            for conflict_start, conflict_end in blocked:
                left, right = conflict_start - overhead, conflict_end + overhead
                remaining = []
                for a, b in pieces:
                    if right <= a or left >= b:
                        remaining.append((a, b))
                    else:
                        remaining.extend([(a, min(b, left)), (max(a, right), b)])
                pieces = remaining
            candidates.extend(
                (a, b) for a, b in pieces if b > a and (b - a).total_seconds() >= required
            )
    if not candidates:
        return None
    start, end = min(
        candidates, key=lambda piece: (-(piece[1] - piece[0]).total_seconds(), piece[0])
    )
    return {
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
    }


def rescue_policy(settings: dict | None, proposal: str) -> dict | None:
    """Validate opt-in thresholds against the administrator's proposal budget.

    Budgets are cumulative minutes per proposal, shared by all submitting users
    and never reset by a monitor restart. Raising a budget requires an explicit
    server configuration change; a scheduling form cannot grant allocation.
    """
    import json
    import os

    if not settings:
        return None
    if not isinstance(settings, dict):
        raise ValueError("Auto-resubmit settings must be an object")
    fraction = _threshold(settings.get("min_fraction"), "minimum fraction", 1)
    minutes = _threshold(settings.get("min_minutes"), "minimum minutes")
    if not fraction and not minutes:
        return None
    if settings.get("max_attempts", 1) != 1:
        raise ValueError("Only one automatic replacement per original request is allowed")
    try:
        budgets = json.loads(os.environ.get("MUSCAT_LCO_RESCUE_BUDGET_MINUTES", "{}"))
        budget = _threshold(budgets.get(proposal), "proposal rescue budget")
        if budget <= 0:
            raise ValueError("A positive proposal rescue budget is required")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("A valid positive proposal rescue budget is required") from exc
    return {
        "min_fraction": fraction,
        "min_minutes": minutes,
        "max_attempts": 1,
        "budget_seconds": budget * 60,
    }
