"""Per-viewer proposal access control on jobs, photometry, transit-fit, TTV-fit
and ephemeris data (issue #144 PR5).

Derived data inherits the proposal of the night it came from: a photometry or
transit-fit run on (instrument, obsdate, target) is hidden when that target's
summaries on that night are all under denied proposals. A TTV fit spans
nights, so it is hidden only when every night of its target is.

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

import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from muscat_db import database, web
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
    conn.execute(
        "INSERT INTO restricted_proposals (proposal_id) VALUES (?)", (_RESTRICTED.lower(),)
    )
    conn.commit()
    conn.close()


def _make_phot_run(date: str, target: str) -> Path:
    rdir = phot.run_output_dir(INST, date, target, RUN)
    rdir.mkdir(parents=True)
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
    (rdir / "out").mkdir(parents=True)
    (rdir / "out" / "fit.png").write_bytes(b"\x89PNG\r\n")
    (rdir / "out" / "summary.csv").write_text("parameter,mean\nt0[0],1.0\n")
    (rdir / "timer-fit.log").write_text("fit log\n")
    return rdir


def _make_ttv_run(target: str) -> Path:
    rdir = ttv.ttv_output_dir(target, RUN)
    rdir.mkdir(parents=True)
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
        _make_phot_run(date, target)
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
])
def test_writes_on_hidden_run_are_404_and_touch_nothing(client, path):
    assert _post(client, path, _NIGHT).status_code == 404
    assert phot.run_output_dir(INST, "260103", "HIDDENTGT", RUN).is_dir()
    assert fit.fit_output_dir(INST, "260103", "HIDDENTGT", RUN).is_dir()


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
    # Hidden: HIDDENTGT's 2 photometry + 2 transit-fit jobs and its TTV job.
    assert root - alice == 5


def test_archive_download_job_hidden_if_any_listed_night_is(client):
    _save_job("lco_archive_download", "260103", "OPENTGT, HIDDEN TGT")
    html = _get(client, "/jobs").text
    assert "HIDDEN TGT" not in html
    assert "HIDDEN TGT" in _get(client, "/jobs", user="root").text


def test_rerun_of_hidden_job_is_404(client):
    key = f"photometry:{INST}/260103/HIDDENTGT/{RUN}"
    assert _post(client, "/api/jobs/rerun", {"key": key}).status_code == 404


# ── TTV fits ────────────────────────────────────────────────────────────


def test_ttv_reads_of_fully_hidden_target_look_absent(client):
    assert _get(client, "/api/ttv-fit/runs", target="HIDDENTGT").json()["runs"] == []
    outputs = _get(client, "/api/ttv-fit/outputs", target="HIDDENTGT", run_name=RUN).json()
    assert outputs["outputs"] == ttv.empty_ttv_outputs()
    assert _get(client, "/api/ttv-fit/status", target="HIDDENTGT", run_name=RUN).json() == _NONE
    model = _get(client, "/api/ttv-fit/model", target="HIDDENTGT", run_name=RUN)
    never = _get(client, "/api/ttv-fit/model", target="NEVERTGT", run_name=RUN)
    assert (model.status_code, model.json()) == (never.status_code, never.json())
    assert _get(client, "/api/ttv-fit/output-file", target="HIDDENTGT",
                run_name=RUN, file="data.csv").status_code == 404
    assert _get(client, "/api/ttv-fit/download-all", target="HIDDENTGT",
                run_name=RUN).status_code == 404
    assert _get(client, "/api/jobs/ttv-log/HIDDENTGT", run=RUN).status_code == 404


def test_ttv_of_partly_hidden_target_stays_visible(client):
    assert _get(client, "/api/ttv-fit/runs", target="MIXTGT").json()["runs"] != []


def test_ttv_admin_sees_hidden_target(client):
    assert _get(client, "/api/ttv-fit/runs", user="root", target="HIDDENTGT").json()["runs"] != []


@pytest.mark.parametrize("path", ["/api/ttv-fit/start", "/api/ttv-fit/cancel", "/api/ttv-fit/delete"])
def test_ttv_writes_on_hidden_target_are_404(client, path):
    assert _post(client, path, {"target": "HIDDENTGT", "run_name": RUN}).status_code == 404
    assert ttv.ttv_output_dir("HIDDENTGT", RUN).is_dir()


# ── ephemeris (built from completed transit fits) ───────────────────────


def test_ephemeris_targets_exclude_fits_on_hidden_nights(client, monkeypatch):
    monkeypatch.setattr(fit, "sync_jobs", lambda: None)
    targets = _get(client, "/api/ephemeris/targets").json()["targets"]
    assert any("OPENTGT" in t for t in targets)
    assert not any("HIDDENTGT" in t for t in targets)
    root = _get(client, "/api/ephemeris/targets", user="root").json()["targets"]
    assert any("HIDDENTGT" in t for t in root)
