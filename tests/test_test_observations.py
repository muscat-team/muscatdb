import datetime as dt
import time
from pathlib import Path

import pytest

from muscat_db import database, lco, test_observations as subject
from muscat_db.access import object_hidden
from muscat_db.access_groups import Grant
from muscat_db.database import ScopedDenied, _apply_schema, connect

NOW = dt.datetime(2026, 10, 21, 0, 0, tzinfo=dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _transit(days: float, hours: float = 4.0) -> dict:
    start = NOW + dt.timedelta(days=days)
    return {"start": _iso(start), "end": _iso(start + dt.timedelta(hours=hours))}


def _payload(**overrides):
    payload = {
        "kind": "sinistro", "target_name": "WASP-12", "site": "coj", "filter": "rp", "ra": 1.0, "dec": 2.0,
        "exposure_time": 30, "exposure_budget_s": 600, "readout_mode": "central_2k_2x2",
        "fov_candidates": [
            {"center_ra": 1.01, "center_dec": 2.01, "pa_deg": 0, "edge_margin_arcsec": 40},
            {"center_ra": 1.0, "center_dec": 2.0, "pa_deg": 0, "edge_margin_arcsec": 35},
        ],
        "science_windows": [_transit(5)], "now": _iso(NOW), "provenance": {"calculator": "unit-test"},
    }
    payload.update(overrides)
    return payload


# --- test window ---------------------------------------------------------------

def test_today_window_runs_24_hours_from_now():
    window = subject.plan_test_window("today", [_transit(5)], NOW)
    assert window == {"start": _iso(NOW), "end": _iso(NOW + dt.timedelta(hours=24))}


def test_today_window_stops_at_a_transit_inside_the_next_24_hours():
    transit = _transit(0.5)
    window = subject.plan_test_window("today", [transit], NOW)
    assert window["end"] == transit["start"]


def test_lead_window_ends_two_days_before_the_first_transit():
    window = subject.plan_test_window("lead", [_transit(9), _transit(5)], NOW)
    assert window["end"] == _iso(NOW + dt.timedelta(days=4))


def test_lead_window_needs_room_before_the_transit():
    with pytest.raises(subject.TestObservationError, match="1-day lead"):
        subject.plan_test_window("lead", [_transit(1.01)], NOW)
    with pytest.raises(subject.TestObservationError, match="upcoming transit"):
        subject.plan_test_window("lead", [], NOW)


def test_window_refused_while_a_science_window_is_open():
    with pytest.raises(subject.TestObservationError, match="already open"):
        subject.plan_test_window("today", [_transit(-0.01)], NOW)


# --- plan ------------------------------------------------------------------------

def test_plan_is_deterministic_and_retains_both_fovs():
    first = subject.generate_plan(_payload())
    second = subject.generate_plan(_payload())
    assert first == second
    assert {item["fov_index"] for item in first["configurations"]} == {0, 1}
    assert all(item["repeats"] >= 3 for item in first["configurations"])
    assert all(item["pa_deg"] == 0 for item in first["configurations"])
    assert first["estimated_exposure_s"] <= 600
    assert first["window"] == {"start": _iso(NOW), "end": _iso(NOW + dt.timedelta(hours=24))}
    assert first["target_coord"] == {"ra": 1.0, "dec": 2.0}


def test_plan_wall_clock_counts_setup_and_readout_per_exposure():
    plan = subject.generate_plan(_payload())
    n_exposures = sum(c["repeats"] for c in plan["configurations"])
    assert plan["readout_overhead_s"] == 9.0  # LCO central_2k_2x2 overhead
    assert plan["estimated_wall_clock_s"] == pytest.approx(
        subject.SETUP_OVERHEAD_S + plan["estimated_exposure_s"] + 9.0 * n_exposures)


def test_plan_rejects_rotated_fov():
    fovs = _payload()["fov_candidates"]
    fovs[0]["pa_deg"] = 15
    with pytest.raises(subject.TestObservationError, match="rotation"):
        subject.generate_plan(_payload(fov_candidates=fovs))


def test_plan_requires_exactly_two_fovs():
    with pytest.raises(subject.TestObservationError, match="exactly two"):
        subject.generate_plan(_payload(fov_candidates=_payload()["fov_candidates"][:1]))


def test_identical_fovs_are_tested_once():
    same = [{"center_ra": 1.0, "center_dec": 2.0, "pa_deg": 0}, {"center_ra": 1.0, "center_dec": 2.0, "pa_deg": 0}]
    plan = subject.generate_plan(_payload(fov_candidates=same))
    assert {c["fov_index"] for c in plan["configurations"]} == {0}
    assert any("one FOV" in w for w in plan["warnings"])


def test_focus_is_held_at_the_nominal_defocus():
    plan = subject.generate_plan(_payload(defocus_mm=2))
    assert {item["defocus_mm"] for item in plan["configurations"]} == {2}


def test_defocus_beyond_instrument_limit_is_rejected():
    with pytest.raises(subject.TestObservationError, match="0.5"):
        subject.generate_plan(_payload(kind="qhy600", readout_mode="central30x30", defocus_mm=1.0))


def test_predicted_target_saturation_removes_the_long_exposure():
    # 30 s nominal: 45 s exposures reach 4000 e-/s * 45 s = 180000 >= 150000.
    plan = subject.generate_plan(_payload(saturation_model={"rp": {"rate": 4000.0, "level": 150000.0}}))
    assert all(c["exposure_time"] < 45 for c in plan["configurations"])
    assert any(r["reason"] == "target predicted to saturate" for r in plan["removed_combinations"])


def test_clipping_deduplicates_and_reports_removed():
    plan = subject.generate_plan(_payload(exposure_time=1, exposure_limits={"min": 1, "max": 1}))
    assert any("duplicate" in item["reason"] for item in plan["removed_combinations"])


def test_budget_error_names_the_actual_budget():
    with pytest.raises(subject.TestObservationError, match=r"exposure budget \(100 s\)"):
        subject.generate_plan(_payload(exposure_budget_s=100))


def test_plan_fails_when_the_window_cannot_hold_the_test():
    # 1.05 h window; two nominal 3x600 s tests plus setup/readout need ~1.07 h.
    with pytest.raises(subject.TestObservationError, match="shorter than the estimated"):
        subject.generate_plan(_payload(science_windows=[_transit(1.05 / 24)], exposure_time=600,
                                       exposure_budget_s=4000, exposure_limits={"max": 900}))


# --- draft override and request window --------------------------------------------

def test_draft_may_not_change_protected_keys():
    plan = subject.generate_plan(_payload())
    edited_window = {**plan["window"], "end": _transit(5)["end"]}
    with pytest.raises(subject.TestObservationError, match="window"):
        subject.merge_plan_override(plan, {"window": edited_window})
    with pytest.raises(subject.TestObservationError, match="exposure_budget_s"):
        subject.merge_plan_override(plan, {"exposure_budget_s": 6000})
    # Unchanged protected keys (the draft echoes them back) are accepted.
    assert subject.merge_plan_override(plan, {"window": plan["window"], "warnings": ["x"]})["warnings"] == ["x"]


def _edit_first(plan: dict, **changes) -> list[dict]:
    configs = [dict(c) for c in plan["configurations"]]
    configs[0] = {**configs[0], **changes}
    return configs


def test_draft_may_edit_exposures_and_repeats_and_estimates_follow():
    plan = subject.generate_plan(_payload())
    configs = _edit_first(plan, exposure_times={"rp": 25.0}, exposure_time=25.0, repeats=4)
    merged = subject.merge_plan_override(plan, {"configurations": configs})
    assert merged["configurations"][0]["exposure_times"] == {"rp": 25.0}
    assert merged["configurations"][0]["repeats"] == 4
    assert merged["estimated_exposure_s"] == pytest.approx(sum(c["exposure_time"] * c["repeats"] for c in configs))
    assert merged["estimated_wall_clock_s"] == pytest.approx(
        subject.SETUP_OVERHEAD_S + merged["estimated_exposure_s"]
        + plan["readout_overhead_s"] * sum(c["repeats"] for c in configs))
    assert plan["configurations"][0]["exposure_time"] == 30.0  # stored plan left intact


@pytest.mark.parametrize("changes, match", [
    ({"center_ra": 5.0}, "center_ra"),
    ({"center_dec": 5.0}, "center_dec"),
    ({"pa_deg": 90}, "pa_deg"),
    ({"defocus_mm": 3.0}, "defocus_mm"),
    ({"fov_index": 1}, "fov_index"),
    ({"exposure_times": {"ip": 30.0}}, "bands"),
    ({"exposure_time": 99.0}, "longest"),
    ({"repeats": 1}, "repeats"),
    ({"exposure_times": {"rp": 500.0}, "exposure_time": 500.0}, "budget"),
])
def test_draft_may_not_move_pointing_focus_or_break_the_budget(changes, match):
    plan = subject.generate_plan(_payload())
    with pytest.raises(subject.TestObservationError, match=match):
        subject.merge_plan_override(plan, {"configurations": _edit_first(plan, **changes)})


def test_draft_may_not_drop_configurations():
    plan = subject.generate_plan(_payload())
    with pytest.raises(subject.TestObservationError, match="keep its"):
        subject.merge_plan_override(plan, {"configurations": plan["configurations"][:1]})


def test_request_window_rejects_overlap_and_expiry():
    plan = subject.generate_plan(_payload())
    assert subject.request_window(plan, NOW) == plan["window"]
    with pytest.raises(subject.TestObservationError, match="expired"):
        subject.request_window(plan, NOW + dt.timedelta(hours=23.5))
    overlapping = {**plan, "science_windows": [_transit(0.5)]}
    with pytest.raises(subject.TestObservationError, match="overlaps"):
        subject.request_window(overlapping, NOW)


def test_test_request_uses_only_the_test_window(monkeypatch):
    from muscat_db import web

    plan = subject.generate_plan(_payload(now=_iso(dt.datetime.now(dt.timezone.utc))))
    record = {"id": "t1", "plan": plan}
    params = {"name": "X", "proposal": "P", "target_name": "WASP-12", "ra": 1.0, "dec": 2.0, "site": "coj",
              "windows": plan["science_windows"], "filter": "rp", "readout_mode": "central_2k_2x2"}
    monkeypatch.setattr(lco, "_clip_windows_to_observability", lambda *a, **k: None)
    rg, _plan = web._test_request(record, params)
    assert rg["requests"][0]["windows"] == [plan["window"]]
    assert rg["name"].startswith("TEST ")
    targets = {(c["target"]["ra"], c["target"]["dec"]) for c in rg["requests"][0]["configurations"]}
    assert targets == {(1.01, 2.01), (1.0, 2.0)}


def test_ordered_configs_leave_normal_payload_unchanged():
    params = {
        "name": "normal", "proposal": "P", "target_name": "T", "ra": 1, "dec": 2,
        "site": "coj", "windows": [], "exposure_time": 30, "filter": "rp",
        "readout_mode": "central_2k_2x2", "exposure_count": 3, "type": "EXPOSE",
    }
    normal = lco.build_requestgroup("sinistro", params)
    assert normal == lco.build_requestgroup("sinistro", dict(params))
    multi = lco.build_requestgroup("sinistro", params, configurations=[
        {"exposure_time": 10}, {"exposure_time": 20},
    ])
    assert [c["instrument_configs"][0]["exposure_time"] for c in multi["requests"][0]["configurations"]] == [10, 20]


# --- persistence and lifecycle --------------------------------------------------

@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.sqlite"
    with connect(path) as conn:
        _apply_schema(conn)
    return path


def _monitor(path, request_id: int, monitor_state: str, request_state: str = "PENDING"):
    now = time.time()
    with connect(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO lco_observation_requests (request_id, requestgroup_id, request_state, "
            "monitor_state, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (request_id, 1, request_state, monitor_state, now, now),
        )
        conn.commit()


def _submitted(path, ids=(11,)):
    record = subject.create_record(subject.generate_plan(_payload()), path)
    return subject.update_record(record["id"], state="submitted", request_ids=list(ids), path=path)


def test_record_round_trip_with_result_and_recommendation(db):
    created = subject.create_record(subject.generate_plan(_payload()), db)
    assert created["state"] == "draft"
    updated = subject.update_record(created["id"], state="complete", payload_hash="abc",
                                    result={"frames_analyzed": 3}, recommendation={"fov_index": 0}, path=db)
    assert updated["payload_hash"] == "abc"
    assert updated["result"] == {"frames_analyzed": 3}
    assert updated["recommendation"] == {"fov_index": 0}
    assert [r["id"] for r in subject.list_records(target="WASP-12", path=db)] == [created["id"]]
    assert subject.list_records(target="other", path=db) == []


def test_created_by_survives_the_nightly_rebuild(db, tmp_path, monkeypatch):
    # build_db recreates the image from database.SCHEMA and restores only the
    # columns that table has; an owner dropped there makes every test anonymous.
    monkeypatch.setattr(database, "OBSLOG_BASE", str(tmp_path / "no-obslog"))
    record = subject.create_record(subject.generate_plan(_payload()), db, created_by="bob")
    database.build_db(str(db))
    assert subject.get_record(record["id"], db)["created_by"] == "bob"


def _summary(path, target: str, proposal: str):
    with connect(path) as conn:
        conn.execute("INSERT INTO summaries (instrument, obsdate, ccd, object, proposal_id) VALUES (?,?,?,?,?)",
                     ("muscat3", "260101", "g", target, proposal))
        conn.commit()


def _record_on(path, target: str) -> dict:
    return subject.create_record({**subject.generate_plan(_payload()), "target": target}, path)


def test_list_limit_counts_only_records_the_viewer_may_see(db):
    _summary(db, "OPENTGT", "")
    _summary(db, "HIDDENTGT", "SECRET")
    visible = _record_on(db, "OPENTGT")
    for _ in range(3):  # newer than the visible one, so a LIMIT before filtering drops it
        _record_on(db, "HIDDENTGT")
    listed = subject.list_records(limit=1, denied=frozenset({"SECRET"}), path=db)
    assert [r["id"] for r in listed] == [visible["id"]]


def test_list_filter_agrees_with_object_hidden(db):
    _summary(db, "OPENTGT", "")
    _summary(db, "HIDDENTGT", "SECRET")
    _summary(db, "MIXEDTGT", "SECRET")
    _summary(db, "MIXEDTGT", "")
    denied = frozenset({"SECRET"})
    for target in ("OPENTGT", "HIDDENTGT", "MIXEDTGT", "UNOBSERVED"):
        _record_on(db, target)
    listed = {r["target"] for r in subject.list_records(denied=denied, path=db)}
    expected = {t for t in ("OPENTGT", "HIDDENTGT", "MIXEDTGT", "UNOBSERVED")
                if not object_hidden(str(db), t, denied)}
    assert listed == expected == {"OPENTGT", "MIXEDTGT", "UNOBSERVED"}


def test_list_filter_agrees_with_object_hidden_for_a_scoped_viewer(db):
    # A target grant reaches summaries columns (instrument, object) through the
    # subquery, not the test_observations columns of the same name.
    _summary(db, "OPENTGT", "OPENPROP")
    _summary(db, "HIDDENTGT", "SECRET")
    _summary(db, "GRANTEDTGT", "SECRET")
    grant = Grant(instrument="muscat3", target="GRANTEDTGT", objects=frozenset({"GRANTEDTGT"}))
    denied = ScopedDenied({"SECRET", ""}, {"OPENPROP"}, (grant,))
    targets = ("OPENTGT", "HIDDENTGT", "GRANTEDTGT", "UNOBSERVED")
    for target in targets:
        _record_on(db, target)
    listed = {r["target"] for r in subject.list_records(denied=denied, path=db)}
    expected = {t for t in targets if not object_hidden(str(db), t, denied)}
    assert listed == expected == {"OPENTGT", "GRANTEDTGT", "UNOBSERVED"}


def test_table_from_before_ownership_gains_created_by(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with connect(path) as conn:
        conn.execute("CREATE TABLE test_observations (id TEXT PRIMARY KEY, target TEXT NOT NULL, "
                     "instrument TEXT NOT NULL, site TEXT NOT NULL, transit_json TEXT NOT NULL DEFAULT '{}', "
                     "plan_json TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}', "
                     "request_ids_json TEXT NOT NULL DEFAULT '[]', payload_hash TEXT NOT NULL DEFAULT '', "
                     "state TEXT NOT NULL DEFAULT 'draft', analysis_version TEXT NOT NULL, "
                     "recommendation_json TEXT NOT NULL DEFAULT '{}', failure_detail TEXT NOT NULL DEFAULT '', "
                     "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        conn.execute("INSERT INTO test_observations (id, target, instrument, site, plan_json, analysis_version, "
                     "created_at, updated_at) VALUES ('old', 'T', 'muscat3', '', '{}', 'v', 'a', 'a')")
        conn.commit()
    assert subject.get_record("old", path)["created_by"] == ""
    created = subject.create_record(subject.generate_plan(_payload()), path, created_by="bob")
    assert created["created_by"] == "bob"


def _never_analyze(record, frames):
    raise AssertionError("must not analyze")


def test_advance_waits_until_the_monitor_registers_every_request(db):
    record = _submitted(db, ids=(11, 12))
    _monitor(db, 11, "monitoring")
    assert subject.advance(path=db, analyze=_never_analyze) == 0
    assert subject.get_record(record["id"], db)["state"] == "submitted"


def test_advance_follows_pending_then_downloading(db):
    record = _submitted(db)
    _monitor(db, 11, "monitoring")
    subject.advance(path=db, analyze=_never_analyze)
    assert subject.get_record(record["id"], db)["state"] == "pending"
    _monitor(db, 11, "downloading")
    subject.advance(path=db, analyze=_never_analyze)
    assert subject.get_record(record["id"], db)["state"] == "downloading"


def test_advance_analyzes_terminal_requests(db, monkeypatch):
    record = _submitted(db)
    _monitor(db, 11, "complete", "COMPLETED")
    monkeypatch.setattr(subject, "ingested_frame_paths", lambda conn, ids: [Path("a.fits")])
    seen = {}

    def analyze(rec, frames):
        seen["frames"] = frames
        return {"missing_configurations": []}, {"fov_index": 1}

    assert subject.advance(path=db, analyze=analyze) == 1
    done = subject.get_record(record["id"], db)
    assert done["state"] == "complete"
    assert done["recommendation"] == {"fov_index": 1}
    assert seen["frames"] == [Path("a.fits")]


def test_advance_marks_partial_when_configurations_are_missing(db, monkeypatch):
    record = _submitted(db)
    _monitor(db, 11, "complete", "COMPLETED")
    monkeypatch.setattr(subject, "ingested_frame_paths", lambda conn, ids: [Path("a.fits")])
    subject.advance(path=db, analyze=lambda rec, frames: ({"missing_configurations": [2]}, {}))
    assert subject.get_record(record["id"], db)["state"] == "partial"


def test_advance_fails_without_frames_and_records_analysis_errors(db, monkeypatch):
    empty = _submitted(db)
    _monitor(db, 11, "terminal_no_data", "WINDOW_EXPIRED")
    subject.advance(path=db, analyze=_never_analyze)
    failed = subject.get_record(empty["id"], db)
    assert failed["state"] == "failed"
    assert "WINDOW_EXPIRED" in failed["failure_detail"]

    broken = _submitted(db, ids=(21,))
    _monitor(db, 21, "complete", "COMPLETED")
    monkeypatch.setattr(subject, "ingested_frame_paths", lambda conn, ids: [Path("a.fits")])

    def explode(rec, frames):
        raise RuntimeError("boom")

    subject.advance(path=db, analyze=explode)
    record = subject.get_record(broken["id"], db)
    assert record["state"] == "failed"
    assert "boom" in record["failure_detail"]


def test_plan_route_fixes_pa_and_tests_optimized_and_centred_fovs(db, monkeypatch):
    from fastapi.testclient import TestClient

    from muscat_db import exposure, fov, web

    captured = {}

    def fake_optimize(kind, **kwargs):
        captured.update(kwargs)
        return fov.FovResult(ok=True, instrument=kind, ra=1.0, dec=2.0, center_ra=1.01, center_dec=2.01, pa_deg=0.0)

    monkeypatch.setattr(web.fov_opt, "optimize", fake_optimize)
    monkeypatch.setattr(exposure, "lookup_magnitudes_with_fallback", lambda ra, dec: ({"rp": 12.0}, "test", False))
    monkeypatch.setattr(exposure, "load_coeffs", lambda instrument: {})
    monkeypatch.setattr(subject, "db_path", lambda: str(db))
    body = {k: v for k, v in _payload().items() if k not in ("fov_candidates", "now", "science_windows")}
    body["windows"] = [_transit(400)]
    response = TestClient(web.app).post("/api/lco/test-observations/plan", json=body)
    assert response.status_code == 200, response.text
    plan = response.json()["record"]["plan"]
    assert captured["pa_step_deg"] == 180.0
    assert [(f["center_ra"], f["center_dec"], f["pa_deg"]) for f in plan["fov_candidates"]] == [(1.01, 2.01, 0), (1.0, 2.0, 0)]
    assert plan["science_windows"] == body["windows"]
    assert {c["fov_index"] for c in plan["configurations"]} == {0, 1}


def test_submit_stores_the_draft_edits_the_analysis_matches_against(db, monkeypatch):
    # A draft edit changes the exposure LCO takes; the stored plan has to
    # follow, or match_configuration compares frames with the old times.
    from fastapi.testclient import TestClient

    from muscat_db import web

    monkeypatch.setattr(subject, "db_path", lambda: str(db))
    monkeypatch.setattr(lco, "_clip_windows_to_observability", lambda *a, **k: None)
    monkeypatch.setattr(web.lco, "max_allowable_ipp", lambda rg, user: {})
    monkeypatch.setattr(web.lco, "submit_requestgroup", lambda rg, user: {"id": 1})
    monkeypatch.setattr(web, "_submitted_request_ids", lambda result: [11])
    monkeypatch.setattr(web, "_record_lco_submission", lambda *a, **k: {})
    plan = subject.generate_plan(_payload(now=_iso(dt.datetime.now(dt.timezone.utc))))
    record = subject.create_record(plan, db)
    edited = _edit_first(plan, exposure_times={"rp": 25.0}, exposure_time=25.0)
    params = {"name": "X", "proposal": "P", "target_name": "WASP-12", "ra": 1.0, "dec": 2.0, "site": "coj",
              "filter": "rp", "readout_mode": "central_2k_2x2", "plan_override": {"configurations": edited}}
    client = TestClient(web.app)
    url = f"/api/lco/test-observations/{record['id']}"
    dry = client.post(f"{url}/ipp", json=params)
    assert dry.status_code == 200, dry.text
    sent = client.post(f"{url}/submit", json={**params, "confirm": True, "dry_run_hash": dry.json()["payload_hash"]})
    assert sent.status_code == 200, sent.text
    stored = subject.get_record(record["id"], db)
    assert stored["state"] == "submitted"
    assert stored["plan"]["configurations"][0]["exposure_times"] == {"rp": 25.0}
    assert stored["plan"]["configurations"][0]["exposure_time"] == 25.0
