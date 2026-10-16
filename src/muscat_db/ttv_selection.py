"""Rebuild a TTV fit's data and recorded inputs from the O-C points a client picked.

The ephemeris page used to send both the CSV of transit times and a list of the
transit fits behind it. Nothing tied the two together, so a client that sent
the wrong list made the product record understate where the fit's data came
from, and visibility computed from that record would be wrong (#221).

Now the client names the points it selected (which transit fit and planet, or a
manually entered time) and the server reads each transit time from the stored
fit itself. The CSV and the inputs are produced together, from the same loop,
so they cannot disagree.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

from muscat_db import ephemeris_math, product_record

MAX_SELECTION = product_record.MAX_INPUTS
CSV_HEADER = "planet,epoch,tc,tc_unc"


@dataclass(frozen=True)
class Resolved:
    csv_content: str
    inputs: list[dict]


def _finite(value: Any) -> float | None:
    """*value* as a float, or None unless it is a real finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def _lookup(mapping: dict, key: str) -> Any:
    """``mapping[key]`` ignoring the case of the planet letter."""
    for k, v in mapping.items():
        if str(k).lower() == key:
            return v
    return None


def _epoch_of(tc: float, ephem: Any) -> int | None:
    if not isinstance(ephem, dict):
        return None
    t0, period = _finite(ephem.get("t0")), _finite(ephem.get("period"))
    if t0 is None or period is None or period <= 0:
        return None
    return ephemeris_math.assign_epoch(tc, t0, period)


def _product_row(
    i: int,
    item: dict,
    planet: str,
    ephem: dict,
    fitted_params: Callable[[str, str, str, str], dict],
    is_available: Callable[[str, str, str, str], bool],
    is_hidden: Callable[[str, str, str], bool],
) -> tuple[tuple[int, float, float] | None, dict | None, str | None]:
    """``((epoch, tc, unc), input, error)`` for one selected transit fit."""
    fields = {}
    for key in ("instrument", "date", "target"):
        val = item.get(key)
        if not isinstance(val, str) or not val.strip():
            return None, None, f"selection[{i}].{key} is required"
        fields[key] = val.strip()
    run_id = item.get("run_id", "")
    if not isinstance(run_id, str):
        return None, None, f"selection[{i}].run_id must be a string"
    run_id = run_id.strip()
    inst, date, target = fields["instrument"], fields["date"], fields["target"]
    # A hidden night and a missing fit read the same, so the message does not
    # confirm that a night the viewer may not see exists.
    unavailable = f"selection[{i}]: no completed transit fit for this night and run"
    if is_hidden(inst, date, target) or not is_available(inst, date, target, run_id):
        return None, None, unavailable
    stored = _lookup(fitted_params(inst, date, target, run_id) or {}, planet) or {}
    tc, unc = _finite(stored.get("tc")), _finite(stored.get("unc"))
    if tc is None or unc is None or unc <= 0:
        return None, None, f"selection[{i}]: the fit has no usable transit time for planet {planet}"
    epoch = _epoch_of(tc, _lookup(ephem, planet))
    if epoch is None:
        return None, None, f"selection[{i}]: no valid ephemeris for planet {planet}"
    entry = {"kind": "product", "instrument": inst, "date": date, "target": target, "run_id": run_id}
    return (epoch, tc, unc), entry, None


def _manual_row(i: int, item: dict) -> tuple[tuple[int, float, float] | None, dict | None, str | None]:
    epoch, tc, unc = item.get("epoch"), _finite(item.get("tc")), _finite(item.get("tc_unc"))
    if isinstance(epoch, bool) or not isinstance(epoch, int):
        return None, None, f"selection[{i}].epoch must be an integer"
    if tc is None or unc is None or unc <= 0:
        return None, None, f"selection[{i}]: tc and tc_unc must be finite, with tc_unc > 0"
    return (epoch, tc, unc), {"kind": "manual", "note": str(item.get("note") or "")[:500]}, None


def resolve_selection(
    selection: Any,
    planet_letters: str,
    planets_ephem: dict,
    *,
    fitted_params: Callable[[str, str, str, str], dict],
    is_available: Callable[[str, str, str, str], bool],
    is_hidden: Callable[[str, str, str], bool],
) -> tuple[Resolved | None, str | None]:
    """``(resolved, error)`` for the O-C points a client selected.

    *fitted_params* reads a stored transit fit's per-planet ``tc``/``unc``,
    *is_available* says whether a completed transit fit exists for the night
    and run, and *is_hidden* whether the viewer may not see that night. The
    planet column of the CSV is the index of the letter in *planet_letters*.
    """
    if not isinstance(selection, list):
        return None, "selection must be a list"
    if not selection:
        return None, "no transit times selected"
    if len(selection) > MAX_SELECTION:
        return None, f"too many inputs (max {MAX_SELECTION})"
    letters = sorted(str(planet_letters or "").lower())
    ephem = planets_ephem if isinstance(planets_ephem, dict) else {}

    rows: set[tuple[int, int, float, float]] = set()
    points: list[dict] = []
    for i, item in enumerate(selection):
        if not isinstance(item, dict):
            return None, f"selection[{i}] must be an object"
        kind = item.get("kind")
        if kind not in ("product", "manual"):
            return None, f"selection[{i}].kind must be one of product, manual"
        planet = str(item.get("planet") or "").strip().lower()
        if planet not in letters:
            return None, f"selection[{i}].planet is not one of the selected planets"
        if kind == "product":
            row, entry, err = _product_row(
                i, item, planet, ephem, fitted_params, is_available, is_hidden,
            )
        else:
            row, entry, err = _manual_row(i, item)
        if err:
            return None, err
        rows.add((letters.index(planet), *row))
        points.append(entry)

    inputs, err = product_record.ttv_inputs_from_points(points)
    if err:
        return None, err
    lines = [CSV_HEADER] + [f"{p},{e},{tc!r},{unc!r}" for p, e, tc, unc in sorted(rows)]
    return Resolved("\n".join(lines) + "\n", inputs), None
