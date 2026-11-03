"""Per-viewer proposal access control on jobs, photometry, transit-fit, TTV-fit
and ephemeris data (issue #144 PR5).

Derived data inherits the proposal of the night it came from: a photometry or
transit-fit run on (instrument, obsdate, target) is hidden when that target's
summaries on that night are all under denied proposals. A TTV fit spans
nights and no run records which ones it used (#208), so it is hidden when any
night of its target is: a fit over a denied night's timing would expose it.

Fixture DB (sinistro):

* 260101: ``OPENTGT`` (open);
* 260102: ``HIDDENTGT`` (restricted) -- a fully restricted night;
* 260103: ``OPENTGT`` (open) and ``HIDDENTGT`` (restricted) -- a mixed night;
* 260104 / 260105: ``MIXTGT`` restricted, then open -- a target with only some
  nights denied.

Products on disk carry the distinctive run id ``secretrun`` so a leak shows up
as that string in a response.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from muscat_db import access, database, web
from muscat_db import product_record
from muscat_db import photometry as phot
from muscat_db import transit_fit as fit
from muscat_db import ttv_fit as ttv
from muscat_db.access import NightVisibility, hidden_obsdates
from muscat_db.coord import CoordRepr
from muscat_db.database import SCHEMA, _insert_summary_rows, _populate_targets, _summary_rows
from muscat_db.job_store import get_job_store
from tests.test_access_control import (
    _OPEN,
    _PROXY_SECRET,
    _RESTRICTED,
    _headers,
    _set_admin,
)

pytestmark = pytest.mark.usefixtures("mock_target_coord_resolution")

INST = "sinistro"
RUN = "secretrun"
DENIED = frozenset({_RESTRICTED})

_FRAMES = [
    (INST, "260101", "SIN_2601010001", "OPENTGT", 1.0, _OPEN),
    (INST, "260102", "SIN_2601020001", "HIDDENTGT", 2.0, _RESTRICTED),
    (INST, "260103", "SIN_2601030001", "OPENTGT", 3.0, _OPEN),
    (INST, "260103", "SIN_2601030002", "HIDDEN TGT", 3.1, _RESTRICTED),
    (INST, "260104", "SIN_2601040001", "MIXTGT", 4.0, _RESTRICTED),
    (INST, "260105", "SIN_2601050001", "MIXTGT", 5.0, _OPEN),
]


def _build_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.create_aggregate("coord_repr", 2, CoordRepr)
    conn.executescript(SCHEMA)
    conn.executemany(
        """INSERT INTO frames
           (instrument, obsdate, ccd, filename, object, jd_start, ut_start,
            exptime, read_mode, filter, ra, declination, airmass, focus, pa,
            proposal_id)
           VALUES (?, ?, 0, ?, ?, ?, '00:00:00', 10, 'fast', 'gp', '', '', 1, 0, NULL, ?)""",
        _FRAMES,
    )
    _insert_summary_rows(conn, _summary_rows(conn))
    _populate_targets(conn)
    conn.commit()
    conn.close()
    access.restrict(db_path, _RESTRICTED.lower())


def _record(rdir: Path, level: str, inputs, author: str = "someone-else") -> None:
    rdir.mkdir(parents=True, exist_ok=True)
    assert product_record.write_record(
        rdir, product_record.build_record(level, author=author, inputs=inputs)
    )


def _lightcurve_input(date: str, target: str) -> dict:
    return {"kind": "product", "level": "photometry", "instrument": INST,
            "obsdate": date, "target": target, "run_id": RUN}


def _fit_input(date: str, target: str) -> dict:
    return {"kind": "product", "level": "transit_fit", "instrument": INST,
            "obsdate": date, "target": target, "run_id": RUN}


def _make_phot_run(db: str, date: str, target: str) -> Path:
    rdir = phot.run_output_dir(INST, date, target, RUN)
    rdir.mkdir(parents=True)
    inputs, _ = product_record.frames_inputs(db, INST, date, target)
    _record(rdir, "photometry", inputs)
    stem = f"{target}_{INST}_{date}"
    (rdir / f"{stem}_lightcurves.png").write_bytes(b"\x89PNG\r\n")
    (rdir / f"{target}_{INST}_gp_{date}.csv").write_text("BJD_TDB,Flux\n1,1\n")
    (rdir / "_webrun_meta.json").write_text(
        '{"run_id":"' + RUN + '","run_name":"' + RUN + '","site":"","mode":""}'
    )
    # A legacy (pre-run-dir) product directly under the night.
    (phot.results_dir(INST, date) / f"{stem}_legacy.png").write_bytes(b"\x89PNG\r\n")
    return rdir


def _make_fit_run(date: str, target: str) -> Path:
    rdir = fit.fit_output_dir(INST, date, target, RUN)
    _record(rdir, "transit_fit", [_lightcurve_input(date, target)])
    (rdir / "out").mkdir(parents=True)
    (rdir / "out" / "fit.png").write_bytes(b"\x89PNG\r\n")
    (rdir / "out" / "summary.csv").write_text("parameter,mean\nt0[0],1.0\n")
    (rdir / "timer-fit.log").write_text("fit log\n")
    return rdir


# The nights each TTV fixture run was fitted on. MIXTGT's run used one night
# the viewer may not see, which is what hides it.
_TTV_NIGHTS = {
    "HIDDENTGT": [("260102", "HIDDENTGT")],
    "MIXTGT": [("260103", "OPENTGT"), ("260103", "HIDDENTGT")],
    "OPENTGT": [("260103", "OPENTGT")],
}


def _make_ttv_run(target: str) -> Path:
    rdir = ttv.ttv_output_dir(target, RUN)
    rdir.mkdir(parents=True)
    _record(rdir, "ttv_fit", [_fit_input(d, t) for d, t in _TTV_NIGHTS[target]])
    (rdir / "data.csv").write_text("epoch,tc\n0,1\n")
    (rdir / "harmonic.log").write_text("ttv log\n")
    return rdir


def _save_job(type_: str, date: str, target: str, *, inst: str = INST, state: str = "done") -> None:
    get_job_store().save(
        type_=type_, inst=inst, date=date, target=target, state=state,
        returncode=0 if state == "done" else None, elapsed=1, started_at=time.time(),
        run_id=RUN if type_ != "lco_archive_download" else "job-1",
        run_name=RUN, run_type="full",
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "muscat.db"
    _build_db(str(db))
    monkeypatch.setenv("MUSCAT_DB_PATH", str(db))
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path / "prose"))
    monkeypatch.setenv("MUSCAT_TIMER_DIR", str(tmp_path / "timer"))
    monkeypatch.setenv("MUSCAT_TTV_DIR", str(tmp_path / "harmonic"))
    monkeypatch.setenv("MUSCAT_DATA_DIR", str(tmp_path / "raw"))
    monkeypatch.setenv("MUSCAT_TMPDIR", str(tmp_path / "muscat-tmp"))
    monkeypatch.setenv("MUSCAT_PROXY_SECRET", _PROXY_SECRET)
    monkeypatch.setattr(database, "refresh_target_status", lambda *a, **k: None)
    database.clear_all_caches()
    web._index_cache.clear()

    for date, target in (("260102", "HIDDENTGT"), ("260103", "HIDDENTGT"), ("260103", "OPENTGT")):
        _make_phot_run(str(db), date, target)
        _make_fit_run(date, target)
        _save_job("photometry", date, target)
        _save_job("transit_fit", date, target)
    for target in ("HIDDENTGT", "MIXTGT", "OPENTGT"):
        _make_ttv_run(target)
        _save_job("ttv_fit", "", target)
    return str(db)


@pytest.fixture
def client(env):
    _set_admin(env, "root")
    return TestClient(web.app, client=("127.0.0.1", 12345))


def _get(client, path, user="alice", **params):
    return client.get(path, headers=_headers(user), params=params)


def _post(client, path, payload, user="alice"):
    return client.post(path, headers=_headers(user), json=payload)


@pytest.mark.parametrize("endpoint", ["runs", "outputs"])
def test_photometry_discovery_respects_proposal_access(client, endpoint):
    params = dict(inst=INST, date="260102", target="HIDDENTGT")
    if endpoint == "outputs":
        params["run"] = RUN
    hidden = _get(client, f"/api/photometry/{endpoint}", **params)
    assert hidden.status_code == 200
    assert hidden.json() == ({"runs": []} if endpoint == "runs" else phot.empty_outputs())
    admin = _get(client, f"/api/photometry/{endpoint}", user="root", **params)
    assert admin.status_code == 200
    assert admin.json()["runs"] if endpoint == "runs" else admin.json()["has_any"]


def test_photometry_discovery_hides_a_restricted_run_on_a_visible_night(client, env):
    # The night is visible, but this named run includes restricted inputs.
    directory = phot.run_output_dir(INST, "260103", "OPENTGT", "mixed-inputs")
    inputs, _ = product_record.frames_inputs(env, INST, "260103", "HIDDENTGT")
    assert inputs[0]["resolved"] and inputs[0]["proposal_ids"] == [_RESTRICTED]
    _record(directory, "photometry", inputs)
    (directory / "OPENTGT_sinistro_gp_260103.csv").write_text("BJD_TDB,Flux\n1,1\n")
    params = dict(inst=INST, date="260103", target="OPENTGT")
    runs = _get(client, "/api/photometry/runs", **params)
    assert RUN in {r["run_id"] for r in runs.json()["runs"]}
    assert "mixed-inputs" not in runs.text
    outputs = _get(client, "/api/photometry/outputs", run="mixed-inputs", **params)
    assert outputs.json() == phot.empty_outputs()


# ── NightVisibility / hidden_obsdates ───────────────────────────────────


def test_night_visibility_rules(env):
    nights = NightVisibility(env, DENIED, normalize=lambda s: s.upper())
    assert not nights.hidden(INST, "260101", "OPENTGT")
    assert not nights.hidden(INST, "260101", "NEVERTGT")  # open night: unknown names unchanged
    assert nights.hidden(INST, "260102", "HIDDENTGT")
    assert nights.hidden(INST, "260103", "HIDDENTGT")  # spaces and case ignored
    assert nights.hidden(INST, "260103", "hidden tgt")
    assert not nights.hidden(INST, "260103", "OPENTGT")
    assert nights.hidden(INST, "260103", "NEVERTGT")  # mixed night: fail closed
    assert nights.hidden_objects(INST, "260103") == {"hiddentgt"}


def test_night_visibility_is_a_no_op_without_denials(env):
    nights = NightVisibility(env, frozenset())
    assert not nights.hidden(INST, "260102", "HIDDENTGT")
    assert nights.hidden_objects(INST, "260102") == set()


def test_hidden_obsdates(env):
    assert hidden_obsdates(env, INST, DENIED) == {"260102", "260104"}
    assert hidden_obsdates(env, INST, frozenset()) == set()


# ── /photometry and /transit-fit pages ──────────────────────────────────


@pytest.mark.parametrize("page", ["/photometry", "/transit-fit"])
def test_page_date_picker_drops_fully_restricted_night_even_with_products(client, page):
    html = _get(client, page, inst=INST).text
    assert "260103" in html
    assert "260102" not in html
    assert "260102" in _get(client, page, user="root", inst=INST).text


@pytest.mark.parametrize("page", ["/photometry", "/transit-fit"])
def test_page_target_picker_drops_hidden_object(client, page):
    html = _get(client, page, inst=INST, date="260103").text
    assert "OPENTGT" in html
    assert "HIDDENTGT" not in html


@pytest.mark.parametrize("page", ["/photometry", "/transit-fit"])
def test_page_for_hidden_target_shows_no_runs(client, page):
    hidden = _get(client, page, inst=INST, date="260103", target="HIDDENTGT").text
    assert RUN not in hidden
    admin = _get(client, page, user="root", inst=INST, date="260103", target="HIDDENTGT").text
    assert RUN in admin


def test_transit_fit_previous_fit_list_skips_hidden_nights(client):
    # OPENTGT's own page lists previous fits across nights; HIDDENTGT's
    # restricted-night fits must not appear on a page for it either.
    html = _get(client, "/transit-fit", inst=INST, date="260101", target="HIDDENTGT").text
    assert RUN not in html


# ── status endpoints ────────────────────────────────────────────────────

_NONE = {"state": "none", "log": "", "returncode": None, "elapsed": 0}


@pytest.mark.parametrize("prefix", ["/api/photometry", "/api/transit-fit"])
def test_status_of_hidden_run_looks_absent(client, prefix):
    params = {"inst": INST, "date": "260103", "target": "HIDDENTGT", "run": RUN}
    assert _get(client, f"{prefix}/status", **params).json() == _NONE
    assert _get(client, f"{prefix}/status", user="root", **params).json()["state"] == "done"


def test_status_batch_hides_only_hidden_runs(client):
    jobs = [
        {"inst": INST, "date": "260103", "target": t, "run": RUN}
        for t in ("HIDDENTGT", "OPENTGT")
    ]
    out = _post(client, "/api/photometry/status-batch", {"jobs": jobs}).json()["jobs"]
    assert out[0]["state"] == "none"
    assert out[1]["state"] == "done"


# ── files, downloads, logs ──────────────────────────────────────────────


@pytest.mark.parametrize("path", [
    f"/api/photometry/file/{INST}/260103/HIDDENTGT/run/{RUN}/HIDDENTGT_{INST}_260103_lightcurves.png",
    f"/api/photometry/file/{INST}/260103/HIDDENTGT_{INST}_260103_legacy.png",
    f"/api/photometry/download-all/{INST}/260103/HIDDENTGT/run/{RUN}",
    f"/api/transit-fit/file/{INST}/260103/HIDDENTGT/run/{RUN}/fit.png",
    f"/api/transit-fit/download-all/{INST}/260103/HIDDENTGT/run/{RUN}",
    f"/api/jobs/log/transit_fit/{INST}/260103/HIDDENTGT?run={RUN}",
])
def test_hidden_files_and_logs_are_404_but_admin_gets_them(client, path):
    assert client.get(path, headers=_headers("alice")).status_code == 404
    assert client.get(path, headers=_headers("root")).status_code == 200


def test_open_target_files_on_mixed_night_still_served(client):
    base = f"/api/photometry/file/{INST}/260103"
    assert client.get(f"{base}/OPENTGT_{INST}_260103_legacy.png", headers=_headers("alice")).status_code == 200
    assert client.get(
        f"{base}/OPENTGT/run/{RUN}/OPENTGT_{INST}_260103_lightcurves.png", headers=_headers("alice")
    ).status_code == 200


# ── writes ──────────────────────────────────────────────────────────────

_NIGHT = {"inst": INST, "date": "260103", "target": "HIDDENTGT", "run": RUN, "run_id": RUN}


@pytest.mark.parametrize("path", [
    "/api/photometry/run",
    "/api/photometry/cancel",
    "/api/photometry/delete",
    "/api/photometry/postprocess",
    "/api/transit-fit/run",
    "/api/transit-fit/logp",
    "/api/transit-fit/cancel",
    "/api/transit-fit/delete",
    "/api/jobs/lco-archive/scan",
    "/api/jobs/lco-archive/ingest-date",
])
def test_writes_on_hidden_run_are_404_and_touch_nothing(client, path):
    assert _post(client, path, _NIGHT).status_code == 404
    assert phot.run_output_dir(INST, "260103", "HIDDENTGT", RUN).is_dir()
    assert fit.fit_output_dir(INST, "260103", "HIDDENTGT", RUN).is_dir()


@pytest.fixture
def lco_calls(monkeypatch):
    """Record what the LCO archive scan/ingest would have run, without running it."""
    calls: list[tuple[str, str, str]] = []

    def _scan(inst, obsdate, *a, **k):
        calls.append(("scan", inst, obsdate))
        return {}

    def _ingest(db_path, inst, obsdate, *a, **k):
        calls.append(("ingest", inst, obsdate))
        return 0

    monkeypatch.setattr("muscat_db.scanner.scan_date", _scan)
    monkeypatch.setattr("muscat_db.database.ingest_date", _ingest)
    return calls


@pytest.mark.parametrize("path", [
    "/api/jobs/lco-archive/scan",
    "/api/jobs/lco-archive/ingest-date",
])
@pytest.mark.parametrize("date", ["260102", "260103", "260104"])
def test_lco_archive_write_refused_on_night_with_any_denied_object(client, lco_calls, path, date):
    """Whole-night actions 404 when any object on the night is restricted:
    fully restricted (260102), mixed (260103), or a restricted target whose
    later night is open (260104). Nothing runs."""
    resp = _post(client, path, {"inst": INST, "date": date})
    assert resp.status_code == 404
    assert lco_calls == []


@pytest.mark.parametrize("path", [
    "/api/jobs/lco-archive/scan",
    "/api/jobs/lco-archive/ingest-date",
])
def test_lco_archive_write_allowed_on_fully_open_night(client, lco_calls, path):
    assert _post(client, path, {"inst": INST, "date": "260101"}).status_code == 200
    assert _post(client, path, {"inst": INST, "date": "260105"}).status_code == 200
    assert len(lco_calls) == 2


@pytest.mark.parametrize("path", [
    "/api/jobs/lco-archive/scan",
    "/api/jobs/lco-archive/ingest-date",
])
def test_lco_archive_write_admin_gets_any_night(client, lco_calls, path):
    assert _post(client, path, {"inst": INST, "date": "260102"}, user="root").status_code == 200
    assert lco_calls and lco_calls[-1][2] == "260102"


def test_photometry_command_preview_skips_obslog_checks_for_hidden(client):
    resp = _post(client, "/api/photometry/command", {**_NIGHT, "options": {}})
    assert resp.status_code == 200
    assert resp.json()["error"] in (None, "")


def test_query_archive_previous_fit_of_hidden_night_looks_absent(client):
    resp = _get(
        client, "/api/transit-fit/query-archive",
        target="HIDDENTGT", source="previous", inst=INST, date="260103",
    ).json()
    assert resp["ok"] is False
    assert resp["error"].startswith("No previous fit runs found")


# ── jobs list ───────────────────────────────────────────────────────────


def _job_targets(rows) -> set[tuple[str, str, str]]:
    return {(r.get("type"), r.get("date"), r.get("target")) for r in rows}


def test_jobs_page_drops_hidden_rows(client):
    html = _get(client, "/jobs").text
    assert "OPENTGT" in html
    assert "HIDDENTGT" not in html
    assert "HIDDENTGT" in _get(client, "/jobs", user="root").text


def test_jobs_status_active_only_drops_hidden_rows(client):
    _save_job("photometry", "260103", "HIDDENTGT", state="running")
    _save_job("photometry", "260101", "OPENTGT", state="running")
    keys = {a["key"] for a in _get(client, "/api/jobs/status", active_only=True).json()["active"]}
    assert any("OPENTGT" in k for k in keys)
    assert not any("HIDDENTGT" in k for k in keys)


def test_jobs_status_counts_only_visible(client):
    alice = _get(client, "/api/jobs/status").json()["counts"]["done"]
    root = _get(client, "/api/jobs/status", user="root").json()["counts"]["done"]
    # Hidden: HIDDENTGT's 2 photometry + 2 transit-fit jobs, and the TTV jobs
    # of HIDDENTGT and MIXTGT.
    assert root - alice == 6


def test_archive_download_job_hidden_if_any_listed_night_is(client):
    _save_job("lco_archive_download", "260103", "OPENTGT, HIDDEN TGT")
    html = _get(client, "/jobs").text
    assert "HIDDEN TGT" not in html
    assert "HIDDEN TGT" in _get(client, "/jobs", user="root").text


def test_archive_download_naming_no_object_is_hidden_on_a_restricted_night(client, monkeypatch):
    """A finished download whose frames carry no OBJECT is listed under the
    placeholder target "LCO archive". On a night with restricted rows that name
    matches no visible object, so the row fails closed and its obslog link
    cannot reveal that the night (260102 holds only HIDDENTGT) was ingested.
    The same download of an open night keeps its row and link."""
    from muscat_db import lco

    snapshots = [
        {
            "job_id": job_id, "state": "done", "phase": "done", "frames_total": 1,
            "frames_done": 1, "funpack_total": 0, "funpack_done": 0,
            "instruments": [INST], "obsdates": [date], "objects": [], "dest_dirs": [],
            "started_at": time.time() - 5, "finished_at": time.time(),
            "error": None, "user_name": "bob",
        }
        for job_id, date in (("hiddennight000000", "260102"), ("opennight00000000", "260101"))
    ]
    monkeypatch.setattr(lco, "archive_download_jobs", lambda: snapshots)

    alice = _get(client, "/jobs").text
    root = _get(client, "/jobs", user="root").text
    assert 'data-key="lco_archive_download:hiddennight000000"' in root
    assert f'href="/{INST}/260102"' in root
    assert 'data-key="lco_archive_download:hiddennight000000"' not in alice
    assert f'href="/{INST}/260102"' not in alice
    assert 'data-key="lco_archive_download:opennight00000000"' in alice
    assert f'href="/{INST}/260101"' in alice


def test_rerun_of_hidden_job_is_404(client):
    key = f"photometry:{INST}/260103/HIDDENTGT/{RUN}"
    assert _post(client, "/api/jobs/rerun", {"key": key}).status_code == 404


# ── TTV fits ────────────────────────────────────────────────────────────


# MIXTGT's fit used one restricted night, so its record makes it hidden. A TTV
# run is hidden by what it was fitted on (#208), not by its target's name.
_TTV_HIDDEN = pytest.mark.parametrize("target", ["HIDDENTGT", "MIXTGT"])


@_TTV_HIDDEN
def test_ttv_reads_of_hidden_target_look_absent(client, target):
    assert _get(client, "/api/ttv-fit/runs", target=target).json()["runs"] == []
    outputs = _get(client, "/api/ttv-fit/outputs", target=target, run_name=RUN).json()
    assert outputs["outputs"] == ttv.empty_ttv_outputs()
    assert _get(client, "/api/ttv-fit/status", target=target, run_name=RUN).json() == _NONE
    model = _get(client, "/api/ttv-fit/model", target=target, run_name=RUN)
    never = _get(client, "/api/ttv-fit/model", target="NEVERTGT", run_name=RUN)
    assert (model.status_code, model.json()) == (never.status_code, never.json())
    assert _get(client, "/api/ttv-fit/output-file", target=target,
                run_name=RUN, file="data.csv").status_code == 404
    assert _get(client, "/api/ttv-fit/download-all", target=target,
                run_name=RUN).status_code == 404
    assert _get(client, f"/api/jobs/ttv-log/{target}", run=RUN).status_code == 404


def test_ttv_of_open_target_on_mixed_night_stays_visible(client):
    # OPENTGT shares 260103 with a restricted object, but none of its own
    # summaries are restricted.
    assert _get(client, "/api/ttv-fit/runs", target="OPENTGT").json()["runs"] != []


@_TTV_HIDDEN
def test_ttv_admin_sees_hidden_target(client, target):
    assert _get(client, "/api/ttv-fit/runs", user="root", target=target).json()["runs"] != []


@_TTV_HIDDEN
@pytest.mark.parametrize("path", ["/api/ttv-fit/cancel", "/api/ttv-fit/delete"])
def test_ttv_writes_on_hidden_run_are_404(client, path, target):
    assert _post(client, path, {"target": target, "run_name": RUN}).status_code == 404
    assert ttv.ttv_output_dir(target, RUN).is_dir()


def test_ttv_start_on_fully_hidden_target_is_404(client):
    assert _post(client, "/api/ttv-fit/start", {"target": "HIDDENTGT"}).status_code == 404


# The two consumers of a fitted TTV model besides /api/ttv-fit/*: the schedule
# page's run list and the windows built from a run's predicted mid-times (#261).
_MODEL = {"ok": True, "run_name": RUN, "t_offset": 0.0,
          "points": {"b": [{"tc": 2461000.5, "epoch": 0}]}}


@pytest.fixture
def ttv_model(monkeypatch):
    monkeypatch.setattr(ttv, "get_ttv_outputs", lambda t, r: {"has_any": True, "has_model": True})
    monkeypatch.setattr(ttv, "get_ttv_model", lambda t, r, end="": dict(_MODEL))


def _windows(client, target, user="alice"):
    return _post(client, "/api/lco/windows", {
        "source": "ttv", "target": target, "ttv_run": RUN, "planet": "b",
        "duration": 2.0, "range_start": "2025-01-01", "range_end": "2030-01-01",
    }, user=user)


@_TTV_HIDDEN
def test_ttv_windows_of_hidden_run_look_absent(client, ttv_model, target):
    assert _windows(client, target).status_code == 404
    assert _windows(client, target, user="root").status_code == 200


def test_ttv_windows_of_visible_run_unchanged(client, ttv_model):
    response = _windows(client, "OPENTGT")
    assert response.status_code == 200
    assert response.json()["windows"]


@_TTV_HIDDEN
def test_target_info_hides_ttv_runs_of_hidden_run(client, ttv_model, target):
    assert _get(client, "/api/ephemeris/target-info", target=target).json()["ttv_runs"] == []
    assert _get(client, "/api/ephemeris/target-info", user="root", target=target).json()["ttv_runs"] != []


def test_target_info_lists_visible_ttv_run(client, ttv_model):
    runs = _get(client, "/api/ephemeris/target-info", target="OPENTGT").json()["ttv_runs"]
    assert [r["run_name"] for r in runs] == [RUN]


# ── ephemeris (built from completed transit fits) ───────────────────────


def test_ephemeris_targets_exclude_fits_on_hidden_nights(client, monkeypatch):
    monkeypatch.setattr(fit, "sync_jobs", lambda: None)
    targets = _get(client, "/api/ephemeris/targets").json()["targets"]
    assert any("OPENTGT" in t for t in targets)
    assert not any("HIDDENTGT" in t for t in targets)
    root = _get(client, "/api/ephemeris/targets", user="root").json()["targets"]
    assert any("HIDDENTGT" in t for t in root)


# ── LCO archive / ExoFOP local cross-check ──────────────────────────────


def test_exofop_cross_check_receives_viewer_denied_set(client, monkeypatch):
    seen = {}

    def fake_report(target, *, denied=frozenset(), **_kw):
        seen["denied"] = denied
        return {"ok": True, "toi": 1, "time_series": [], "total": 0}

    monkeypatch.setattr(web.exofop, "build_time_series_report", fake_report)
    assert _get(client, "/api/lco/archive/exofop", user="alice", OBJECT="TOI-1").status_code == 200
    assert seen["denied"] == access.denied_proposal_ids_for(os.environ["MUSCAT_DB_PATH"], "alice")
    assert access.is_denied(_RESTRICTED, seen["denied"])
    assert _get(client, "/api/lco/archive/exofop", user="root", OBJECT="TOI-1").status_code == 200
    assert seen["denied"] == frozenset()


# ── reference-time slider routes (frame previews) ───────────────────────

_REF_TIME_JD = 2_450_003.0  # full JD of the 260103 frames (jd_start 3.0)


def _ref_time_paths(date: str, target: str) -> list[str]:
    base = f"/api/photometry/ref-time/{INST}/{date}/{target}"
    return [base, f"{base}/preview?jd={_REF_TIME_JD}"]


@pytest.mark.parametrize(
    ("date", "target"),
    [
        ("260102", "HIDDENTGT"),  # fully restricted night
        ("260103", "HIDDEN TGT"),  # restricted target in a mixed night
        ("260103", "NOSUCHTGT"),  # unknown target in a mixed night
    ],
)
def test_ref_time_routes_404_for_restricted_viewer(client, date, target):
    for path in _ref_time_paths(date, target):
        assert client.get(path, headers=_headers("alice")).status_code == 404, path


def test_ref_time_routes_open_target_visible_to_restricted_viewer(client):
    for path in _ref_time_paths("260103", "OPENTGT"):
        assert client.get(path, headers=_headers("alice")).status_code == 200, path


@pytest.mark.parametrize(
    ("date", "target"),
    [("260102", "HIDDENTGT"), ("260103", "HIDDEN TGT"), ("260103", "OPENTGT")],
)
def test_ref_time_routes_visible_to_admin(client, date, target):
    for path in _ref_time_paths(date, target):
        assert client.get(path, headers=_headers("root")).status_code == 200, path


# ── scoped viewer (issue #202) ──────────────────────────────────────────


def test_scoped_viewer_jobs_and_runs_follow_the_allowlist(client, env):
    from muscat_db import access

    access.set_scoped(env, "andrew")
    access.grant(env, "andrew", _OPEN, "admin")
    html = _get(client, "/jobs", user="andrew").text
    assert "OPENTGT" in html
    assert "HIDDENTGT" not in html
    picker = _get(client, "/photometry", user="andrew", inst=INST).text
    assert "260103" in picker
    assert "260102" not in picker
    hidden = _get(client, "/photometry", user="andrew", inst=INST, date="260103", target="HIDDENTGT").text
    assert RUN not in hidden


# ── deleting finished job records ───────────────────────────────────────


def _owned_job(owner: str, *, state: str = "done", target: str = "OPENTGT", date: str = "260101") -> str:
    """Store a photometry job for *owner* and return its row key."""
    get_job_store().save(
        type_="photometry", inst=INST, date=date, target=target, state=state,
        returncode=0 if state == "done" else None, elapsed=1, started_at=time.time(),
        run_id="owned-run", run_name="owned-run", run_type="full", user_name=owner,
    )
    return next(
        j["key"] for j in get_job_store().all()
        if j.get("run_id") == "owned-run" and j.get("date") == date and j.get("target") == target
    )


def _stored(key: str) -> bool:
    return any(j["key"] == key for j in get_job_store().all())


def test_job_owner_can_delete_finished_job(client):
    key = _owned_job("alice")
    r = _post(client, "/api/jobs/delete", {"key": key}, user="alice")
    assert r.status_code == 200
    assert not _stored(key)


def test_other_user_cannot_delete_job(client):
    key = _owned_job("alice")
    r = _post(client, "/api/jobs/delete", {"key": key}, user="bob")
    assert r.status_code == 403
    assert _stored(key)


def test_admin_can_delete_anyones_job(client):
    key = _owned_job("alice")
    r = _post(client, "/api/jobs/delete", {"key": key}, user="root")
    assert r.status_code == 200
    assert not _stored(key)


@pytest.mark.parametrize("state", ["running", "pending"])
def test_unfinished_job_cannot_be_deleted(client, state):
    key = _owned_job("alice", state=state)
    r = _post(client, "/api/jobs/delete", {"key": key}, user="root")
    assert r.status_code == 409
    assert _stored(key)


def test_delete_unknown_or_missing_key(client):
    assert _post(client, "/api/jobs/delete", {"key": "photometry:nope"}).status_code == 404
    assert _post(client, "/api/jobs/delete", {}).status_code == 400


def test_delete_does_not_expose_hidden_jobs(client):
    # HIDDENTGT's night is restricted: alice may not even learn the job exists.
    key = _owned_job("alice", target="HIDDENTGT", date="260102")
    r = _post(client, "/api/jobs/delete", {"key": key}, user="alice")
    assert r.status_code == 404
    assert _stored(key)


def test_jobs_page_offers_delete_only_to_owner_or_admin(client):
    key = _owned_job("alice")
    mine = _get(client, "/jobs", user="alice").text
    theirs = _get(client, "/jobs", user="bob").text
    admin = _get(client, "/jobs", user="root").text

    def button(html: str) -> str:
        i = html.index(f'data-key="{key}"')
        return html[i : i + 3000]

    assert 'data-job-action="delete" data-key="' + key + '"' in mine
    assert 'data-job-action="delete" data-key="' + key + '"' in admin
    assert 'data-job-action="delete" data-key="' + key + '"' not in theirs
    assert "Only the job&#39;s owner or an admin can delete it" in theirs or \
        "Only the job's owner or an admin can delete it" in theirs
    assert "data-job-action=\"hide\"" not in mine
    assert button(mine)


def test_running_job_row_has_no_delete_button(client):
    key = _owned_job("alice", state="running")
    html = _get(client, "/jobs", user="alice").text
    assert 'data-job-action="delete" data-key="' + key + '"' not in html


def test_deleting_live_lco_archive_download_drops_it_from_memory(client, monkeypatch):
    from muscat_db import lco

    monkeypatch.setattr(lco, "_ARCHIVE_DOWNLOAD_JOBS", {})
    job_id = "livejob0000000000"
    lco._ARCHIVE_DOWNLOAD_JOBS[job_id] = {
        "job_id": job_id, "state": "done", "frames": [], "frames_total": 0, "results": [],
        "funpack_results": [], "phase": "done", "started_at": time.time() - 5,
        "finished_at": time.time(), "error": None, "user_name": "alice",
    }
    key = f"lco_archive_download:{job_id}"
    assert _post(client, "/api/jobs/delete", {"key": key}, user="bob").status_code == 403
    assert job_id in lco._ARCHIVE_DOWNLOAD_JOBS
    assert _post(client, "/api/jobs/delete", {"key": key}, user="alice").status_code == 200
    assert job_id not in lco._ARCHIVE_DOWNLOAD_JOBS


# ── visibility read from the run's record (#144 PR 5b) ──────────────────


def _status(client, user, date, target, run=RUN, prefix="/api/photometry"):
    return _get(client, f"{prefix}/status", user=user, inst=INST, date=date, target=target, run=run).json()


def test_record_with_a_denied_input_hides_a_run_on_an_open_night(client, env):
    """The target is open, but this run's own record names restricted frames."""
    rdir = phot.run_output_dir(INST, "260103", "OPENTGT", RUN)
    _record(rdir, "photometry", [{
        "kind": "frames", "instrument": INST, "obsdate": "260103", "target": "OPENTGT",
        "proposal_ids": [_RESTRICTED], "n_frames": 1, "blank_frames": 0, "resolved": True,
    }])
    assert _status(client, "alice", "260103", "OPENTGT") == _NONE
    assert _status(client, "root", "260103", "OPENTGT")["state"] == "done"
    assert _get(client, "/api/photometry/file/sinistro/260101/OPENTGT/run/" + RUN + "/OPENTGT_sinistro_260101_lightcurves.png").status_code == 404


def test_author_sees_their_own_run_whose_inputs_are_denied(client, env):
    rdir = phot.run_output_dir(INST, "260103", "HIDDENTGT", RUN)
    inputs, _ = product_record.frames_inputs(env, INST, "260103", "HIDDENTGT")
    _record(rdir, "photometry", inputs, author="Alice")
    assert _status(client, "alice", "260103", "HIDDENTGT")["state"] == "done"
    assert _status(client, "bob", "260103", "HIDDENTGT") == _NONE


def test_run_with_undetermined_inputs_is_admin_only(client, env):
    _record(phot.run_output_dir(INST, "260103", "OPENTGT", RUN), "photometry", None)
    assert _status(client, "alice", "260103", "OPENTGT") == _NONE
    assert _status(client, "root", "260103", "OPENTGT")["state"] == "done"


def test_run_with_no_record_is_admin_only(client, env):
    (phot.run_output_dir(INST, "260103", "OPENTGT", RUN) / "_product.json").unlink()
    assert _status(client, "alice", "260103", "OPENTGT") == _NONE
    assert _status(client, "root", "260103", "OPENTGT")["state"] == "done"


def test_transit_fit_follows_its_light_curves(client, env):
    _record(phot.run_output_dir(INST, "260103", "OPENTGT", RUN), "photometry", None)
    assert _status(client, "alice", "260103", "OPENTGT", prefix="/api/transit-fit") == _NONE


def test_ttv_run_follows_the_fits_it_used(client, env):
    _record(fit.fit_output_dir(INST, "260103", "OPENTGT", RUN), "transit_fit", None)
    assert _get(client, "/api/ttv-fit/runs", target="OPENTGT").json()["runs"] == []
    assert _get(client, "/api/ttv-fit/runs", user="root", target="OPENTGT").json()["runs"] != []


def test_transit_fit_download_all_follows_the_record(client, env):
    url = f"/api/transit-fit/download-all/{INST}/260103/OPENTGT/run/{RUN}"
    assert _get(client, url).status_code == 200
    _record(fit.fit_output_dir(INST, "260103", "OPENTGT", RUN), "transit_fit", None)
    assert _get(client, url).status_code == 404
    assert _get(client, url, user="root").status_code == 200


def test_transit_fit_cannot_read_light_curves_from_a_hidden_run(client, env, monkeypatch):
    """A fit's author sees it, so it must never be built from a run they cannot see."""
    _record(phot.run_output_dir(INST, "260103", "OPENTGT", RUN), "photometry", None)
    seen = {}

    def fake_start(inst, date, target, options, **kw):
        seen["csvs"] = kw["selected_csvs"]
        return {"ok": True}

    monkeypatch.setattr(fit, "start_fit", fake_start)
    body = {"inst": INST, "date": "260103", "target": "OPENTGT", "options": {}}
    assert _post(client, "/api/transit-fit/run", body).status_code == 200
    assert seen["csvs"] == []
    assert _post(client, "/api/transit-fit/run", body, user="root").status_code == 200
    assert seen["csvs"] is None or len(seen["csvs"]) == 1


def test_postprocess_of_a_hidden_run_is_404(client, env):
    _record(phot.run_output_dir(INST, "260103", "OPENTGT", RUN), "photometry", None)
    body = {"inst": INST, "date": "260103", "target": "OPENTGT", "run": RUN}
    assert _post(client, "/api/photometry/postprocess", body).status_code == 404
    assert _post(client, "/api/photometry/postprocess/restore", body).status_code == 404


# ── queued runs ─────────────────────────────────────────────────────────
# A full job waiting for a free slot has no run directory and no record yet.
# Its owner must still see its status and be able to cancel it.

QUEUED = "queuedrun"


@pytest.fixture(autouse=False)
def slots_busy(monkeypatch):
    """Every full-job slot is taken, so a queued job stays queued."""
    monkeypatch.setattr(type(get_job_store()), "claim_slot", lambda *a, **k: False)


def _enqueue(type_: str, date: str, target: str, owner: str) -> None:
    get_job_store().enqueue(
        type_=type_, inst="_" if type_ == "ttv_fit" else INST,
        date="_" if type_ == "ttv_fit" else date, target=target,
        started_at=time.time(), run_type="full", run_id=QUEUED, run_name=QUEUED,
        user_name=owner,
    )


_QUEUED_ROUTES = [
    ("photometry", "/api/photometry", {"inst": INST, "date": "260103", "target": "OPENTGT", "run": QUEUED}),
    ("transit_fit", "/api/transit-fit", {"inst": INST, "date": "260103", "target": "OPENTGT", "run": QUEUED}),
    ("ttv_fit", "/api/ttv-fit", {"target": "OPENTGT", "run_name": QUEUED}),
]


@pytest.mark.parametrize(("type_", "prefix", "params"), _QUEUED_ROUTES)
def test_owner_sees_and_cancels_their_queued_run(client, slots_busy, type_, prefix, params):
    _enqueue(type_, "260103", "OPENTGT", "alice")
    assert _get(client, f"{prefix}/status", **params).json()["state"] == "pending"
    assert _post(client, f"{prefix}/cancel", params).status_code != 404


@pytest.mark.parametrize(("type_", "prefix", "params"), _QUEUED_ROUTES)
def test_queued_run_of_someone_else_follows_the_night_rule(client, slots_busy, type_, prefix, params):
    _enqueue(type_, "260103", "OPENTGT", "someone-else")
    seen = _get(client, f"{prefix}/status", **params).json()["state"]
    # Photometry and transit fit follow the night they will read, which is
    # open; a TTV job has no night, so it stays owner/admin-only.
    assert seen == ("none" if type_ == "ttv_fit" else "pending")
    assert _get(client, f"{prefix}/status", user="root", **params).json()["state"] == "pending"


def test_queued_run_with_no_job_row_is_hidden(client):
    params = {"inst": INST, "date": "260103", "target": "OPENTGT", "run": "nosuchrun"}
    assert _get(client, "/api/photometry/status", **params).json() == _NONE


# ── a hidden run on an open night ───────────────────────────────────────
# The night rule alone must not be what hides these: OPENTGT on 260101 is open.


def test_run_without_record_on_open_night_is_hidden_from_pages_and_status(client, env):
    rdir = phot.run_output_dir(INST, "260101", "OPENTGT", "orphanrun")
    rdir.mkdir(parents=True)
    (rdir / "_webrun_meta.json").write_text(
        '{"run_id":"orphanrun","run_name":"orphanrun","site":"","mode":""}'
    )
    (rdir / f"OPENTGT_{INST}_gp_260101.csv").write_text("BJD_TDB,Flux\n1,1\n")
    fdir = fit.fit_output_dir(INST, "260101", "OPENTGT", "orphanfit")
    _record(fdir, "transit_fit", [{
        "kind": "product", "level": "photometry", "instrument": INST,
        "obsdate": "260101", "target": "OPENTGT", "run_id": "orphanrun",
    }])
    (fdir / "out").mkdir()
    (fdir / "out" / "summary.csv").write_text("parameter,mean\nt0[0],1.0\n")

    params = {"inst": INST, "date": "260101", "target": "OPENTGT"}
    for page, name in (("/photometry", "orphanrun"), ("/transit-fit", "orphanrun"), ("/transit-fit", "orphanfit")):
        assert name not in _get(client, page, **params).text
        assert name in _get(client, page, user="root", **params).text
    assert _get(client, "/api/transit-fit/status", **params, run="orphanfit").json() == _NONE
    outputs = _get(client, "/api/transit-fit/outputs", **params, run="orphanfit").json()["outputs"]
    assert outputs == fit.empty_fit_outputs()
    assert _get(client, "/api/transit-fit/outputs", user="root", **params, run="orphanfit").json()["outputs"]["has_any"]


# The call sites that take a light curve or a fit as input, each checked on an
# open night so the night rule cannot be what refuses them.


def _hide_run_on_open_night() -> None:
    """OPENTGT on 260103 is open, but this run's record makes it undetermined."""
    _record(phot.run_output_dir(INST, "260103", "OPENTGT", RUN), "photometry", None)


def test_logp_reads_no_light_curve_from_a_hidden_run(client, env, monkeypatch):
    _hide_run_on_open_night()
    seen = {}
    monkeypatch.setattr(
        fit, "compute_logp", lambda *a, selected_csvs=None, **k: seen.update(csvs=selected_csvs) or {"ok": True}
    )
    body = {"inst": INST, "date": "260103", "target": "OPENTGT", "options": {}}
    assert _post(client, "/api/transit-fit/logp", body).status_code == 200
    assert seen["csvs"] == []
    assert _post(client, "/api/transit-fit/logp", body, user="root").status_code == 200
    assert seen["csvs"] is None or len(seen["csvs"]) == 1


def test_rerun_of_a_fit_reads_no_light_curve_from_a_hidden_run(client, env, monkeypatch):
    # The fit itself is visible (manual input); only its light curve is not.
    _record(fit.fit_output_dir(INST, "260103", "OPENTGT", RUN), "transit_fit", [{"kind": "manual", "note": ""}])
    _hide_run_on_open_night()
    seen = {}
    monkeypatch.setattr(
        fit, "start_fit", lambda *a, selected_csvs=None, **k: seen.update(csvs=selected_csvs) or {"ok": True}
    )
    key = f"transit_fit:{INST}/260103/OPENTGT/{RUN}"
    assert _post(client, "/api/jobs/rerun", {"key": key}).status_code == 200
    assert seen["csvs"] == []


def test_ttv_start_refuses_a_hidden_fit_on_an_open_night(client, env, monkeypatch):
    fdir = fit.fit_output_dir(INST, "260101", "OPENTGT", "orphanfit")
    (fdir / "out").mkdir(parents=True)  # no record: undetermined, so hidden
    started = {}
    monkeypatch.setattr(ttv, "start_ttv_fit", lambda t, options, user, **kw: started.update(options=options) or {"ok": True})
    monkeypatch.setattr(web, "_transit_fit_available", lambda: (lambda *a: True))
    monkeypatch.setattr(web, "_get_run_fitted_params", lambda *a: {"b": {"tc": 2460004.02, "unc": 0.002}})
    body = {"target": "OPENTGT", "options": {
        "planet_letters": "b",
        "planets_ephem": {"b": {"t0": 2460000.0, "period": 2.0}},
        "selection": [{"kind": "product", "planet": "b", "instrument": INST, "date": "260101",
                       "target": "OPENTGT", "run_id": "orphanfit"}],
    }}
    refused = _post(client, "/api/ttv-fit/start", body)
    assert refused.status_code == 400
    assert "options" not in started
    assert _post(client, "/api/ttv-fit/start", body, user="root").status_code == 200
