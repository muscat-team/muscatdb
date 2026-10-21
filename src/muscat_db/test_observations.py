"""Planning and persistence for closed-loop LCO test observations.

The planner is deliberately pure and deterministic.  Network-derived FOV,
catalogue, visibility, and capability results are inputs with provenance; this
keeps plans reproducible and makes stale/approximate evidence visible.

Lifecycle: ``draft -> validated -> submitted`` is driven by the schedule page.
After submission :func:`advance` (run by the LCO observation monitor) follows the
monitored requests through ``pending -> downloading -> analyzing`` and ends at
``complete``/``partial`` with a recommendation, or ``failed``.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Callable

from muscat_db.database import connect, db_path

logger = logging.getLogger(__name__)

ANALYSIS_VERSION = "test-observation-v2"
# qhy600 is schedulable (LCO instrument_type "0M4-SCICAM-QHY600", confirmed
# via LCO's live https://observe.lco.global/api/instruments/ list) and gets
# the single-filter exposure-plan shape (nccd=1, same as sinistro). sbig is
# deliberately excluded: it's archival-only -- no live LCO instrument_type
# code exists to submit a test observation request for it.
SUPPORTED = {"muscat3", "muscat4", "sinistro", "qhy600"}
# nccd=1 kinds: one filter per exposure, so the exposure plan uses a single
# {filter: exposure_time} pair rather than muscat3/muscat4's simultaneous
# {g,r,i,z: exposure_time} dict.
SINGLE_FILTER_KINDS = {"sinistro", "qhy600"}
STATES = {"draft", "validated", "submitted", "pending", "downloading", "analyzing", "complete", "partial", "failed"}
# States :func:`advance` still has to move forward after submission.
ACTIVE_STATES = ("submitted", "pending", "downloading", "analyzing")

# Per-exposure readout overhead (s) by readout mode, from LCO's live instrument
# capabilities (observe.lco.global/api/instruments/, modes.readout.modes[].overhead,
# checked 2026-10-21). Keyed by kind family because "full_frame" means a
# different camera (and overhead) on Sinistro and QHY600.
READOUT_OVERHEAD_S = {
    "muscat": {"MUSCAT_FAST": 6.0, "MUSCAT_SLOW": 46.0},
    "sinistro": {"full_frame": 28.0, "central_2k_2x2": 9.0},
    "qhy600": {"central30x30": 4.0, "full_frame": 9.0},
}
# LCO's default readout mode per kind family (same source as above).
DEFAULT_READOUT_MODE = {"muscat": "MUSCAT_FAST", "sinistro": "central_2k_2x2", "qhy600": "central30x30"}
# Front padding LCO reserves per request for slew/acquisition; same value the
# science scheduler deducts (lco._REPEAT_EXPOSE_SETUP_OVERHEAD_S).
SETUP_OVERHEAD_S = 180.0

EXPOSURE_FACTORS = (0.67, 1.0, 1.5)
MIN_REPEATS = 3
# Two FOV candidates closer than this are the same pointing: testing both
# would spend the budget twice on identical frames.
SAME_POINTING_ARCSEC = 5.0

# Test window modes. "today" books the next 24 h; "lead" books from now until
# LEAD_TIME_S before the first selected transit so the result is in hand with
# time to adjust the science request.
WINDOW_TODAY = "today"
WINDOW_LEAD = "lead"
TODAY_WINDOW_S = 24 * 3600.0
LEAD_TIME_S = 1 * 86400.0
# A shorter window is unlikely to contain a schedulable slot at all.
MIN_TEST_WINDOW_S = 3600.0

# Plan keys the editable draft may not override: they carry the guarded
# provenance the request is built and validated from.
PROTECTED_PLAN_KEYS = frozenset({
    "version", "kind", "target", "target_coord", "fov_candidates", "window", "window_mode",
    "science_windows", "exposure_budget_s", "readout_overhead_s",
})
# Per-configuration fields the draft may edit. Pointing, PA, focus and FOV stay
# as planned: they are what the guards above checked.
EDITABLE_CONFIG_KEYS = frozenset({"exposure_times", "exposure_time", "repeats"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS test_observations (
 id TEXT PRIMARY KEY, target TEXT NOT NULL, instrument TEXT NOT NULL, site TEXT NOT NULL,
 transit_json TEXT NOT NULL DEFAULT '{}', plan_json TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
 request_ids_json TEXT NOT NULL DEFAULT '[]', payload_hash TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL DEFAULT 'draft', analysis_version TEXT NOT NULL,
 recommendation_json TEXT NOT NULL DEFAULT '{}', failure_detail TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT ''
);
"""


class TestObservationError(ValueError):
    __test__ = False
    pass


def kind_family(kind: str) -> str:
    return "muscat" if kind.startswith("muscat") else kind


def readout_overhead_s(kind: str, mode: str | None) -> float:
    family = kind_family(kind)
    modes = READOUT_OVERHEAD_S.get(family, {})
    mode = mode or DEFAULT_READOUT_MODE.get(family)
    if mode not in modes:
        raise TestObservationError(f"unknown readout mode {mode!r} for {kind}")
    return modes[mode]


def _number(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise TestObservationError(f"{label} must be numeric") from exc
    if not math.isfinite(result) or result <= 0:
        raise TestObservationError(f"{label} must be positive and finite")
    return result


def _parse_utc(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        try:
            parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise TestObservationError(f"invalid timestamp: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _spans(windows: list[dict]) -> list[tuple[dt.datetime, dt.datetime]]:
    spans = []
    for window in windows or []:
        if not window.get("start") or not window.get("end"):
            continue
        spans.append((_parse_utc(window["start"]), _parse_utc(window["end"])))
    return sorted(spans)


def assert_no_overlap(window: dict, science_windows: list[dict]) -> None:
    start, end = _parse_utc(window["start"]), _parse_utc(window["end"])
    for s_start, s_end in _spans(science_windows):
        if start < s_end and s_start < end:
            raise TestObservationError(
                f"test window {_iso(start)}–{_iso(end)} overlaps science window {_iso(s_start)}–{_iso(s_end)}"
            )


def plan_test_window(mode: str, science_windows: list[dict], now: dt.datetime) -> dict:
    """The window a test observation may run in, never overlapping a science window.

    ``today`` runs from now for 24 h, cut short at the next science window.
    ``lead`` runs from now until ``LEAD_TIME_S`` before the first upcoming
    science window, so the test result arrives with time to adjust the science
    request.
    """
    mode = mode or WINDOW_TODAY
    if mode not in (WINDOW_TODAY, WINDOW_LEAD):
        raise TestObservationError(f"unknown test window mode: {mode!r}")
    now = _parse_utc(now).replace(microsecond=0)
    spans = _spans(science_windows)
    for s_start, s_end in spans:
        if s_start <= now < s_end:
            raise TestObservationError("a selected science window is already open; a test cannot precede it")
    upcoming = [s_start for s_start, _ in spans if s_start > now]
    first = min(upcoming) if upcoming else None
    if mode == WINDOW_TODAY:
        end = now + dt.timedelta(seconds=TODAY_WINDOW_S)
        if first is not None and first < end:
            end = first
    else:
        if first is None:
            raise TestObservationError("the 1-day lead option needs an upcoming transit window")
        end = first - dt.timedelta(seconds=LEAD_TIME_S)
    if (end - now).total_seconds() < MIN_TEST_WINDOW_S:
        hint = "the first transit is too close for a 1-day lead" if mode == WINDOW_LEAD else "the next transit window starts too soon"
        raise TestObservationError(f"test window shorter than {MIN_TEST_WINDOW_S / 3600:g} h: {hint}")
    window = {"start": _iso(now), "end": _iso(end)}
    assert_no_overlap(window, science_windows)
    return window


def _separation_arcsec(a: dict, b: dict) -> float:
    ra1, dec1, ra2, dec2 = (math.radians(float(v)) for v in (a["center_ra"], a["center_dec"], b["center_ra"], b["center_dec"]))
    cos_sep = math.sin(dec1) * math.sin(dec2) + math.cos(dec1) * math.cos(dec2) * math.cos(ra1 - ra2)
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_sep)))) * 3600.0


def _fov_candidates(payload: dict) -> tuple[list[dict], list[str]]:
    """The optimized and target-centred FOVs, PA fixed at 0, de-duplicated."""
    fovs = list(payload.get("fov_candidates") or [])
    if len(fovs) != 2:
        raise TestObservationError("exactly two FOV candidates are required: optimized and target-centred")
    for fov in fovs:
        if float(fov.get("pa_deg") or 0.0) != 0.0:
            raise TestObservationError("FOV rotation is not allowed: every candidate must have PA 0")
        if fov.get("center_ra") is None or fov.get("center_dec") is None:
            raise TestObservationError("FOV candidates need center_ra and center_dec")
    if _separation_arcsec(fovs[0], fovs[1]) < SAME_POINTING_ARCSEC:
        return [fovs[0]], ["optimized pointing equals the target-centred pointing; testing one FOV"]
    return fovs, []


def _predicted_saturated(exposure: dict[str, float], model: dict) -> bool:
    """Whether the target's predicted peak reaches full well in any band.

    ``model`` maps a plan band key to ``{"rate": peak e-/s, "level": full well
    e-}`` from the exposure calculator. A test frame with a saturated target
    measures nothing, so such a combination is not worth its budget.
    """
    for band, seconds in exposure.items():
        entry = model.get(band)
        if entry and entry["rate"] * seconds >= entry["level"]:
            return True
    return False


def generate_plan(payload: dict) -> dict:
    kind = str(payload.get("kind") or "").lower()
    if kind not in SUPPORTED:
        raise TestObservationError(f"test observations support {', '.join(sorted(SUPPORTED))}")
    from muscat_db import lco

    fovs, warnings = _fov_candidates(payload)
    repeats = max(MIN_REPEATS, int(payload.get("repeats") or MIN_REPEATS))
    budget = _number(payload.get("exposure_budget_s", 600), "exposure budget")
    focus = float(payload.get("defocus_mm") or 0.0)
    limit_mm = lco._DEFOCUS_LIMIT_MM[kind]
    if abs(focus) > limit_mm:
        raise TestObservationError(f"defocus must be within ±{limit_mm:g} mm for {kind}")
    readout_s = readout_overhead_s(kind, payload.get("readout_mode"))
    limits = payload.get("exposure_limits") or {}
    minimum, maximum = float(limits.get("min", 0.1)), float(limits.get("max", 600.0))
    window = plan_test_window(payload.get("test_window_mode"), payload.get("science_windows") or [], _parse_utc(payload.get("now") or dt.datetime.now(dt.timezone.utc)))
    removed: list[dict] = []

    if kind in SINGLE_FILTER_KINDS:
        nominal: dict[str, float] = {str(payload.get("filter") or "rp"): _number(payload.get("exposure_time"), "exposure time")}
    else:
        raw = payload.get("exposure_times") or {}
        nominal = {b: _number(raw.get(b), f"{b} exposure time") for b in ("g", "r", "i", "z")}

    exposures = []
    for factor in EXPOSURE_FACTORS:
        values = {band: round(max(minimum, min(maximum, value * factor)), 6) for band, value in nominal.items()}
        if values in exposures:
            removed.append({"reason": "instrument-limit clipping produced duplicate", "factor": factor})
        else:
            exposures.append(values)

    saturation_model = payload.get("saturation_model") or {}
    candidates = []
    for fov_index, fov in enumerate(fovs):
        for exposure in exposures:
            factor = min(exposure[b] / nominal[b] for b in nominal)
            if _predicted_saturated(exposure, saturation_model):
                removed.append({"reason": "target predicted to saturate", "fov_index": fov_index, "exposure_times": exposure})
                continue
            candidates.append({
                "fov_index": fov_index, "center_ra": fov.get("center_ra"), "center_dec": fov.get("center_dec"),
                "pa_deg": 0, "exposure_times": exposure,
                "exposure_time": max(exposure.values()), "defocus_mm": focus, "repeats": repeats,
                "priority": (0 if abs(factor - 1) < 0.02 else 1, fov_index),
            })
    candidates.sort(key=lambda c: c["priority"])
    kept, used = [], 0.0
    for candidate in candidates:
        candidate.pop("priority")
        cost = candidate["exposure_time"] * repeats
        if used + cost <= budget:
            kept.append(candidate)
            used += cost
        else:
            removed.append({**candidate, "reason": "exposure budget exceeded"})
    if not kept or {c["fov_index"] for c in kept} != set(range(len(fovs))):
        raise TestObservationError(
            f"exposure budget ({budget:g} s) cannot retain a test for every FOV; "
            "raise the budget or lower the nominal exposure"
        )
    wall_clock = _wall_clock_s(kept, readout_s)
    _check_fits_window(window, wall_clock)
    warnings.extend(w for w in payload.get("plan_warnings") or [] if w)
    return {
        "version": ANALYSIS_VERSION, "kind": kind, "target": payload.get("target_name"), "site": payload.get("site"),
        "target_coord": {"ra": payload.get("ra"), "dec": payload.get("dec")},
        "transit": payload.get("transit") or {}, "window": window, "window_mode": payload.get("test_window_mode") or WINDOW_TODAY,
        "science_windows": list(payload.get("science_windows") or []),
        "fov_candidates": fovs, "configurations": kept, "removed_combinations": removed,
        "exposure_budget_s": budget, "estimated_exposure_s": round(used, 6),
        "readout_overhead_s": readout_s, "estimated_wall_clock_s": round(wall_clock, 6), "warnings": warnings,
        "provenance": payload.get("provenance") or {},
    }


def _exposure_s(configs: list[dict]) -> float:
    return sum(float(c["exposure_time"]) * int(c["repeats"]) for c in configs)


def _wall_clock_s(configs: list[dict], readout_s: float) -> float:
    return SETUP_OVERHEAD_S + _exposure_s(configs) + readout_s * sum(int(c["repeats"]) for c in configs)


def _check_fits_window(window: dict, wall_clock: float) -> None:
    if (_parse_utc(window["end"]) - _parse_utc(window["start"])).total_seconds() < wall_clock:
        raise TestObservationError("test window is shorter than the estimated test duration")


def _edited_configuration(stored: dict, edited: Any, index: int) -> dict:
    """One draft configuration: only exposures and repeats may differ from the plan."""
    if not isinstance(edited, dict):
        raise TestObservationError(f"configuration {index} must be an object")
    changed = sorted(k for k in set(stored) | set(edited)
                     if k not in EDITABLE_CONFIG_KEYS and edited.get(k) != stored.get(k))
    if changed:
        raise TestObservationError(
            f"draft may not change {', '.join(changed)} of configuration {index}; create a new draft instead"
        )
    times = edited.get("exposure_times")
    if not isinstance(times, dict) or set(times) != set(stored["exposure_times"]):
        raise TestObservationError(f"configuration {index} must keep the bands {', '.join(sorted(stored['exposure_times']))}")
    times = {band: _number(value, f"configuration {index} {band} exposure time") for band, value in times.items()}
    exposure_time = max(times.values())
    if abs(_number(edited.get("exposure_time"), f"configuration {index} exposure time") - exposure_time) > 1e-9:
        raise TestObservationError(f"configuration {index}: exposure_time must equal the longest of exposure_times")
    try:
        repeats = int(edited.get("repeats"))
    except (TypeError, ValueError) as exc:
        raise TestObservationError(f"configuration {index}: repeats must be an integer") from exc
    if repeats < MIN_REPEATS or repeats != edited.get("repeats"):
        raise TestObservationError(f"configuration {index}: repeats must be an integer of at least {MIN_REPEATS}")
    return {**stored, "exposure_times": times, "exposure_time": exposure_time, "repeats": repeats}


def merge_plan_override(plan: dict, override: dict | None) -> dict:
    """Overlay the editable draft on the stored plan, refusing protected keys.

    Configurations may only change their exposures and repeats; the result is
    re-checked against the exposure budget and the test window, and its
    estimates are recomputed so the stored plan matches what is submitted.
    """
    if not override:
        return plan
    changed = sorted(k for k in override if k in PROTECTED_PLAN_KEYS and override[k] != plan.get(k))
    if changed:
        raise TestObservationError(f"draft may not change {', '.join(changed)}; create a new draft instead")
    if plan.get("version") != ANALYSIS_VERSION:
        raise TestObservationError("draft predates the current test planner; create a new draft")
    configs = plan["configurations"]
    edited = override.get("configurations", configs)
    if not isinstance(edited, list) or len(edited) != len(configs):
        raise TestObservationError(
            f"draft must keep its {len(configs)} configuration(s); create a new draft to change them"
        )
    configs = [_edited_configuration(stored, item, i) for i, (stored, item) in enumerate(zip(configs, edited))]
    used = _exposure_s(configs)
    budget = float(plan["exposure_budget_s"])
    if used > budget + 1e-9:
        raise TestObservationError(f"draft exposures total {used:g} s, over the {budget:g} s test budget")
    readout_s = float(plan["readout_overhead_s"])
    wall_clock = _wall_clock_s(configs, readout_s)
    _check_fits_window(plan["window"], wall_clock)
    return {
        **plan, **override, "configurations": configs,
        "estimated_exposure_s": round(used, 6), "estimated_wall_clock_s": round(wall_clock, 6),
    }


def request_window(plan: dict, now: dt.datetime | None = None) -> dict:
    """The stored test window, re-checked against the science windows and the clock."""
    window = plan.get("window") or {}
    if not window.get("start") or not window.get("end"):
        raise TestObservationError("draft has no test window; create a new draft")
    assert_no_overlap(window, plan.get("science_windows") or [])
    now = now or dt.datetime.now(dt.timezone.utc)
    if (_parse_utc(window["end"]) - now).total_seconds() < MIN_TEST_WINDOW_S:
        raise TestObservationError("test window has expired; create a new draft")
    return {"start": window["start"], "end": window["end"]}


def request_configurations(plan: dict, base: dict) -> list[dict]:
    result = []
    for item in plan["configurations"]:
        override = {"type": "EXPOSE", "exposure_count": item["repeats"], "defocus": item["defocus_mm"]}
        if plan["kind"] in SINGLE_FILTER_KINDS:
            override.update(exposure_time=item["exposure_time"], filter=next(iter(item["exposure_times"])))
        else:
            override["exposure_times"] = item["exposure_times"]
        # Pointing is represented by the verified ICRS target coordinates used
        # by LCO, not unverified offset fields.
        override.update(ra=item["center_ra"], dec=item["center_dec"])
        result.append(override)
    return result


def _connect(path=None):
    conn = connect(path or db_path())
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(test_observations)")}
    if "created_by" not in columns:
        # Tables created before ownership was recorded; their rows stay ownerless.
        conn.execute("ALTER TABLE test_observations ADD COLUMN created_by TEXT NOT NULL DEFAULT ''")
    return conn


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def create_record(plan: dict, path=None, *, created_by: str = "") -> dict:
    identifier = str(uuid.uuid4())
    now = _now_iso()
    with _connect(path) as conn:
        conn.execute("INSERT INTO test_observations (id,target,instrument,site,transit_json,plan_json,analysis_version,created_at,updated_at,created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (identifier, plan.get("target") or "", plan["kind"], plan.get("site") or "", json.dumps(plan.get("transit") or {}, sort_keys=True), json.dumps(plan, sort_keys=True), ANALYSIS_VERSION, now, now, created_by or ""))
    return get_record(identifier, path)


def _row_to_record(row: sqlite3.Row) -> dict:
    result = dict(row)
    for key in ("transit_json", "plan_json", "result_json", "request_ids_json", "recommendation_json"):
        result[key.removesuffix("_json")] = json.loads(result.pop(key))
    return result


def get_record(identifier: str, path=None) -> dict:
    with _connect(path) as conn:
        row = conn.execute("SELECT * FROM test_observations WHERE id=?", (identifier,)).fetchone()
    if row is None:
        raise KeyError(identifier)
    return _row_to_record(row)


def list_records(*, target: str | None = None, limit: int = 20, path=None) -> list[dict]:
    query, args = "SELECT * FROM test_observations", []
    if target:
        query += " WHERE target=?"
        args.append(target)
    query += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    with _connect(path) as conn:
        rows = conn.execute(query, args).fetchall()
    return [_row_to_record(row) for row in rows]


def update_record(identifier: str, *, state: str, payload_hash: str | None = None, request_ids=None,
                  failure_detail: str = "", result: dict | None = None, recommendation: dict | None = None,
                  plan: dict | None = None, path=None) -> dict:
    if state not in STATES:
        raise TestObservationError(f"invalid lifecycle state: {state}")
    now = _now_iso()

    def encoded(value):
        return json.dumps(value, sort_keys=True) if value is not None else None

    with _connect(path) as conn:
        changed = conn.execute(
            "UPDATE test_observations SET state=?, payload_hash=COALESCE(?,payload_hash), "
            "request_ids_json=COALESCE(?,request_ids_json), result_json=COALESCE(?,result_json), "
            "recommendation_json=COALESCE(?,recommendation_json), plan_json=COALESCE(?,plan_json), "
            "failure_detail=?, updated_at=? WHERE id=?",
            (state, payload_hash, encoded(request_ids), encoded(result), encoded(recommendation), encoded(plan),
             failure_detail, now, identifier),
        )
        if not changed.rowcount:
            raise KeyError(identifier)
    return get_record(identifier, path)


# --- post-submission lifecycle ------------------------------------------------

_MONITOR_TERMINAL = {"complete", "terminal_no_data"}
_MONITOR_TRANSFER = {"downloading", "processing"}


def _monitor_rows(conn, request_ids: list[int]) -> list[sqlite3.Row]:
    marks = ",".join("?" for _ in request_ids)
    return conn.execute(
        f"SELECT request_id, request_state, monitor_state FROM lco_observation_requests WHERE request_id IN ({marks})",
        request_ids,
    ).fetchall()


def ingested_frame_paths(conn, request_ids: list[int]) -> list[Path]:
    """Local paths of the reduced frames the monitor ingested for these requests."""
    from muscat_db import lco

    marks = ",".join("?" for _ in request_ids)
    rows = conn.execute(
        f"SELECT metadata_json FROM lco_observation_frames WHERE request_id IN ({marks}) AND state='ingested'",
        request_ids,
    ).fetchall()
    paths = []
    for row in rows:
        _instrument, _obsdate, dest = lco.frame_destination(json.loads(row[0]))
        # The monitor funpacks .fz products next to the original.
        for candidate in (Path(dest), Path(str(dest).removesuffix(".fz"))):
            if candidate.exists():
                paths.append(candidate)
                break
    return sorted(set(paths))


def _next_state(record: dict, rows: list[sqlite3.Row]) -> str | None:
    monitor_states = {row["monitor_state"] for row in rows}
    if monitor_states <= _MONITOR_TERMINAL:
        return "analyzing"
    if monitor_states & _MONITOR_TRANSFER:
        return "downloading"
    return "pending"


def advance(*, path=None, analyze: Callable[[dict, list[Path]], tuple[dict, dict]] | None = None) -> int:
    """Move submitted test observations forward from the monitored request state.

    Returns the number of records whose state changed. Analysis failures mark
    the record ``failed`` with the error; the monitor loop itself never sees
    them.
    """
    if analyze is None:
        from muscat_db.test_analysis import analyze_record as analyze
    marks = ",".join("?" for _ in ACTIVE_STATES)
    with _connect(path) as conn:
        records = [_row_to_record(r) for r in conn.execute(
            f"SELECT * FROM test_observations WHERE state IN ({marks})", ACTIVE_STATES).fetchall()]
    changed = 0
    for record in records:
        ids = [int(i) for i in record["request_ids"]]
        if not ids:
            continue
        with _connect(path) as conn:
            rows = _monitor_rows(conn, ids)
            if len(rows) < len(ids):
                continue  # not yet registered with the monitor
            state = _next_state(record, rows)
            frames = ingested_frame_paths(conn, ids) if state == "analyzing" else []
        if state != "analyzing":
            if state != record["state"]:
                update_record(record["id"], state=state, path=path)
                changed += 1
            continue
        if not frames:
            lco_states = ", ".join(sorted({row["request_state"] for row in rows}))
            update_record(record["id"], state="failed", failure_detail=f"no reduced frames were ingested (LCO state: {lco_states})", path=path)
            changed += 1
            continue
        update_record(record["id"], state="analyzing", path=path)
        try:
            result, recommendation = analyze(record, frames)
        except Exception as exc:  # noqa: BLE001 - recorded on the record, not swallowed
            logger.exception("test observation %s analysis failed", record["id"])
            update_record(record["id"], state="failed", failure_detail=f"analysis failed: {exc}", path=path)
        else:
            final = "partial" if result.get("missing_configurations") or recommendation.get("incomplete") else "complete"
            update_record(record["id"], state=final, result=result, recommendation=recommendation, path=path)
        changed += 1
    return changed


def reanalyze(identifier: str, *, path=None, analyze: Callable[[dict, list[Path]], tuple[dict, dict]] | None = None) -> dict:
    """Re-run the analysis of a finished test observation on its ingested frames."""
    if analyze is None:
        from muscat_db.test_analysis import analyze_record as analyze
    record = get_record(identifier, path)
    if record["state"] not in {"complete", "partial", "failed"}:
        raise TestObservationError(f"cannot re-analyze a test observation in state {record['state']}")
    ids = [int(i) for i in record["request_ids"]]
    with _connect(path) as conn:
        frames = ingested_frame_paths(conn, ids) if ids else []
    if not frames:
        raise TestObservationError("no ingested frames to analyze")
    result, recommendation = analyze(record, frames)
    final = "partial" if result.get("missing_configurations") or recommendation.get("incomplete") else "complete"
    return update_record(identifier, state=final, result=result, recommendation=recommendation, path=path)
