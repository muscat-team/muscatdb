"""Product records (issue #208, first PR of #228): every photometry, transit-fit
and TTV run stores who made it, from what, with which settings and versions.

Nothing here tests visibility: this PR only records provenance.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from muscat_db import photometry as phot
from muscat_db import product_record as pr
from muscat_db import transit_fit as fit
from muscat_db import ttv_fit as ttv
from muscat_db.database import SCHEMA
from muscat_db.product_backfill import backfill_products

INST = "sinistro"
DATE = "260101"
TARGET = "TOI-1234"


@pytest.fixture
def db(tmp_path, monkeypatch) -> str:
    path = tmp_path / "muscat.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO frames (instrument, obsdate, ccd, filename, object, proposal_id) "
        "VALUES (?, ?, 1, ?, ?, ?)",
        [
            (INST, DATE, "f1.fits", "TOI-1234", "KEY2026B-001"),
            (INST, DATE, "f2.fits", "TOI-1234", "KEY2026B-001"),
            (INST, DATE, "f3.fits", "TOI-1234", "OTHER-9"),
            (INST, DATE, "f4.fits", "WASP-1", "KEY2026B-001"),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("MUSCAT_DB_PATH", str(path))
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path / "prose"))
    monkeypatch.setenv("MUSCAT_TIMER_DIR", str(tmp_path / "timer"))
    monkeypatch.setenv("MUSCAT_TTV_DIR", str(tmp_path / "harmonic"))
    monkeypatch.setattr(phot, "prose_version", lambda: "9.9.9")
    monkeypatch.setattr(fit, "_timer_version", lambda: "8.8.8")
    monkeypatch.setattr(ttv, "_harmonic_version", lambda: "7.7.7")
    return str(path)


def _insert_job(db: str, **kw) -> None:
    row = {
        "key": f"{kw['type']}:{kw['instrument']}/{kw['obsdate']}/{kw['target']}/{kw.get('run_id', '')}",
        "state": "done", "returncode": 0, "elapsed": 1, "started_at": 1_700_000_000.0,
        "run_type": "full", "params": "{}", "run_id": "", "run_name": "", "user_name": "",
    }
    row.update(kw)
    cols = ", ".join(row)
    with sqlite3.connect(db) as conn:
        conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({', '.join('?' * len(row))})", list(row.values()))


# -- record file --------------------------------------------------------------


def test_record_roundtrips(tmp_path):
    rec = pr.build_record("photometry", author=" alice ", inputs=[], settings={"a": 1})
    assert pr.write_record(tmp_path, rec)
    got = pr.read_record(tmp_path)
    assert got["author"] == "alice"
    assert got["inputs"] == []
    assert got["settings"] == {"a": 1}


def test_undetermined_inputs_stay_distinct_from_empty(tmp_path):
    pr.write_record(tmp_path, pr.build_record("ttv_fit", author="", inputs=None))
    assert pr.read_record(tmp_path)["inputs"] is None


def test_unreadable_or_foreign_record_reads_as_absent(tmp_path):
    assert pr.read_record(tmp_path) is None
    (tmp_path / pr.RECORD_NAME).write_text("{not json")
    assert pr.read_record(tmp_path) is None
    (tmp_path / pr.RECORD_NAME).write_text(json.dumps({"schema": 99, "level": "photometry"}))
    assert pr.read_record(tmp_path) is None
    (tmp_path / pr.RECORD_NAME).write_text(json.dumps({"schema": 1, "level": "bogus", "inputs": []}))
    assert pr.read_record(tmp_path) is None


def test_unknown_level_is_rejected():
    with pytest.raises(ValueError):
        pr.build_record("nonsense", author="", inputs=[])


def test_write_failure_is_reported_not_raised(tmp_path):
    assert pr.write_record(tmp_path / "missing" / "dir", pr.build_record("photometry", author="", inputs=[])) is False


# -- frames input -------------------------------------------------------------


def test_frame_input_collects_proposals_and_count(db):
    entry = pr.frame_input(db, INST, DATE, "toi-1234")  # name matched ignoring case
    assert entry["resolved"] is True
    assert entry["n_frames"] == 3
    assert entry["proposal_ids"] == ["KEY2026B-001", "OTHER-9"]


def test_frame_input_unresolved_target_is_flagged(db):
    entry = pr.frame_input(db, INST, DATE, "NOPE")
    assert entry["resolved"] is False
    assert entry["n_frames"] == 0
    assert entry["proposal_ids"] == []


# -- transit-fit light curves -------------------------------------------------


def test_lightcurve_input_reads_run_id_from_path(tmp_path):
    runs = tmp_path / "_runs" / "TOI-1234"
    csv = runs / "coj-1m0-full" / "TOI-1234_sinistro_g.csv"
    entry = pr.lightcurve_input(INST, DATE, TARGET, csv, runs)
    assert entry["run_id"] == "coj-1m0-full"
    assert entry["file"] == "TOI-1234_sinistro_g.csv"
    legacy = pr.lightcurve_input(INST, DATE, TARGET, tmp_path / "TOI-1234_sinistro_g.csv", runs)
    assert legacy["run_id"] == ""


# -- TTV points ---------------------------------------------------------------


def _point(**kw):
    base = {"kind": "product", "instrument": INST, "date": DATE, "target": TARGET, "run_id": "r1"}
    base.update(kw)
    return base


def test_ttv_points_collapse_to_distinct_fits():
    inputs, err = pr.ttv_inputs_from_points([_point(), _point(), _point(run_id="r2"), {"kind": "manual", "note": "lit"}])
    assert err is None
    assert [i["run_id"] for i in inputs if i["kind"] == "product"] == ["r1", "r2"]
    assert all(i["level"] == "transit_fit" for i in inputs if i["kind"] == "product")
    assert inputs[-1] == {"kind": "manual", "note": "lit"}


@pytest.mark.parametrize(
    "points",
    [
        "nope",
        [1],
        [{"kind": "frames"}],
        [_point(instrument="")],
        [_point(date=None)],
        [_point(run_id=5)],
        [_point()] * (pr.MAX_INPUTS + 1),
    ],
)
def test_ttv_points_reject_malformed_input(points):
    _, err = pr.ttv_inputs_from_points(points)
    assert err


# -- writers ------------------------------------------------------------------


def test_photometry_run_records_author_inputs_settings_versions(db, tmp_path):
    rdir = tmp_path / "run"
    rdir.mkdir()
    phot._write_run_product(
        rdir, inst=INST, date=DATE, target=TARGET, opts={"aperture": 1.5}, test_run=False,
        user_name="alice", site="coj", telescope="", mode="", run_name="r",
    )
    rec = pr.read_record(rdir)
    assert rec["level"] == "photometry"
    assert rec["author"] == "alice"
    assert rec["backfilled"] is False
    assert rec["settings"]["options"] == {"aperture": 1.5}
    assert rec["tool_versions"]["prose"] == "9.9.9"
    assert rec["inputs"][0]["proposal_ids"] == ["KEY2026B-001", "OTHER-9"]


def test_transit_fit_records_each_selected_lightcurve(db, tmp_path):
    runs = phot.output_base() / INST / DATE / "_runs" / "TOI-1234"
    csv = runs / "coj-full" / "TOI-1234_sinistro_g.csv"
    csv.parent.mkdir(parents=True)
    csv.write_text("x\n")
    rdir = tmp_path / "fit"
    rdir.mkdir()
    fit._write_fit_product(rdir, INST, DATE, TARGET, [csv], {"degree": 2}, False, "bob")
    rec = pr.read_record(rdir)
    assert rec["level"] == "transit_fit"
    assert rec["author"] == "bob"
    assert rec["tool_versions"]["timer"] == "8.8.8"
    assert rec["inputs"] == [{
        "kind": "product", "level": "photometry", "instrument": INST, "obsdate": DATE,
        "target": TARGET, "run_id": "coj-full", "file": "TOI-1234_sinistro_g.csv",
    }]


def _ttv_options(**kw):
    opts = {
        "csv_content": "planet,epoch,tc,tc_unc\n0,0,2459000.1,0.001\n",
        "ini_content": "[INIT]\n", "planet_letters": "b",
    }
    opts.update(kw)
    return opts


def test_ttv_fit_records_submitted_inputs_and_author(db, tmp_path):
    rdir = tmp_path / "ttv"
    rdir.mkdir()
    ttv.write_ttv_inputs(rdir, "c", "i", _ttv_options(inputs=[_point()]), user_name="carol")
    rec = pr.read_record(rdir)
    assert rec["level"] == "ttv_fit"
    assert rec["author"] == "carol"
    assert rec["tool_versions"]["harmonic"] == "7.7.7"
    assert len(rec["inputs"]) == 1 and rec["inputs"][0]["run_id"] == "r1"
    assert "csv_content" not in rec["settings"] and "inputs" not in rec["settings"]


def test_ttv_fit_without_inputs_is_undetermined_not_empty(db, tmp_path):
    rdir = tmp_path / "ttv"
    rdir.mkdir()
    ttv.write_ttv_inputs(rdir, "c", "i", _ttv_options(), user_name="carol")
    assert pr.read_record(rdir)["inputs"] is None


def test_ttv_options_reject_malformed_inputs():
    assert ttv.validate_ttv_options(_ttv_options(inputs="nope"))
    assert ttv.validate_ttv_options(_ttv_options(inputs=[_point()])) is None


# -- backfill -----------------------------------------------------------------


def test_backfill_photometry_from_job_row(db):
    _insert_job(
        db, type="photometry", instrument=INST, obsdate=DATE, target=TARGET, run_id="coj-full",
        user_name="alice", params=json.dumps({"options": {"aperture": 2.0}, "test_run": False}),
    )
    rdir = phot.run_output_dir(INST, DATE, TARGET, "coj-full")
    rdir.mkdir(parents=True)
    stats = backfill_products(db)
    assert (stats.written, stats.determined) == (1, 1)
    rec = pr.read_record(rdir)
    assert rec["backfilled"] is True
    assert rec["author"] == "alice"
    assert rec["settings"]["options"] == {"aperture": 2.0}
    assert rec["tool_versions"] == {}  # never today's versions
    assert rec["inputs"][0]["n_frames"] == 3


def test_backfill_photometry_with_no_matching_frames_is_undetermined(db):
    _insert_job(db, type="photometry", instrument=INST, obsdate=DATE, target="GHOST", run_id="r")
    phot.run_output_dir(INST, DATE, "GHOST", "r").mkdir(parents=True)
    stats = backfill_products(db)
    assert stats.undetermined == 1
    assert pr.read_record(phot.run_output_dir(INST, DATE, "GHOST", "r"))["inputs"] is None


def test_backfill_transit_fit_uses_selected_csvs_only(db):
    night = phot.output_base() / INST / DATE
    csv = night / "_runs" / "TOI-1234" / "coj-full" / "TOI-1234_sinistro_g.csv"
    params = json.dumps({"options": {}, "selected_csvs": [str(csv)], "test_run": False})
    _insert_job(db, type="transit_fit", instrument=INST, obsdate=DATE, target=TARGET,
                run_id="coj-full", user_name="bob", params=params)
    _insert_job(db, type="transit_fit", instrument=INST, obsdate=DATE, target=TARGET,
                run_id="all-csvs", params=json.dumps({"options": {}}))
    fit.fit_output_dir(INST, DATE, TARGET, "coj-full").mkdir(parents=True)
    fit.fit_output_dir(INST, DATE, TARGET, "all-csvs").mkdir(parents=True)
    stats = backfill_products(db)
    assert (stats.determined, stats.undetermined) == (1, 1)
    good = pr.read_record(fit.fit_output_dir(INST, DATE, TARGET, "coj-full"))
    assert good["inputs"][0]["run_id"] == "coj-full"
    unknown = pr.read_record(fit.fit_output_dir(INST, DATE, TARGET, "all-csvs"))
    assert unknown["inputs"] is None and unknown["note"]


def test_backfill_transit_fit_rejects_csv_from_another_night(db, tmp_path):
    stray = tmp_path / "elsewhere" / "TOI-1234_sinistro_g.csv"
    _insert_job(db, type="transit_fit", instrument=INST, obsdate=DATE, target=TARGET, run_id="r",
                params=json.dumps({"selected_csvs": [str(stray)]}))
    fit.fit_output_dir(INST, DATE, TARGET, "r").mkdir(parents=True)
    backfill_products(db)
    assert pr.read_record(fit.fit_output_dir(INST, DATE, TARGET, "r"))["inputs"] is None


def test_backfill_ttv_is_always_undetermined(db):
    _insert_job(db, type="ttv_fit", instrument="_", obsdate="_", target=TARGET, run_id="base",
                run_name="base", user_name="carol",
                params=json.dumps({"options": {"csv_content": "x", "walkers": 50}}))
    ttv.ttv_output_dir(TARGET, "base").mkdir(parents=True)
    backfill_products(db)
    rec = pr.read_record(ttv.ttv_output_dir(TARGET, "base"))
    assert rec["inputs"] is None
    assert rec["author"] == "carol"
    assert rec["settings"] == {"walkers": 50}


def test_backfill_is_idempotent_and_never_overwrites(db):
    _insert_job(db, type="photometry", instrument=INST, obsdate=DATE, target=TARGET, run_id="r1", user_name="old")
    rdir = phot.run_output_dir(INST, DATE, TARGET, "r1")
    rdir.mkdir(parents=True)
    pr.write_record(rdir, pr.build_record("photometry", author="real-author", inputs=[]))
    stats = backfill_products(db)
    assert stats.already == 1 and stats.written == 0
    assert pr.read_record(rdir)["author"] == "real-author"


def test_backfill_dry_run_writes_nothing(db):
    _insert_job(db, type="photometry", instrument=INST, obsdate=DATE, target=TARGET, run_id="r1")
    rdir = phot.run_output_dir(INST, DATE, TARGET, "r1")
    rdir.mkdir(parents=True)
    stats = backfill_products(db, dry_run=True)
    assert stats.determined == 1 and stats.written == 0
    assert pr.read_record(rdir) is None


def test_backfill_skips_deleted_run_dirs(db):
    _insert_job(db, type="photometry", instrument=INST, obsdate=DATE, target=TARGET, run_id="gone")
    assert backfill_products(db).missing_dir == 1


# -- wiring through the real start functions ----------------------------------


class _Proc:
    pid = 4321

    def poll(self):
        return None


def test_start_fit_writes_record_with_author(db, tmp_path, monkeypatch):
    run_dir = tmp_path / "rundir"
    run_dir.mkdir()
    csv = phot.output_base() / INST / DATE / "_runs" / "TOI-1234" / "coj-full" / "TOI-1234_sinistro_g.csv"
    csv.parent.mkdir(parents=True)
    csv.write_text("time,flux\n")
    monkeypatch.setattr(fit, "fit_output_dir", lambda *_a: run_dir)
    monkeypatch.setattr(fit, "get_csv_lightcurves", lambda *_a: [csv])
    monkeypatch.setattr(fit, "_timer_prefix", lambda: ["timer-fit"])
    monkeypatch.setattr(fit.subprocess, "Popen", lambda *_a, **_k: _Proc())
    monkeypatch.setattr(fit, "_FIT_JOBS", {})
    monkeypatch.setattr("muscat_db.database.save_job", lambda **_k: None)

    result = fit.start_fit(INST, DATE, TARGET, {}, test_run=True, user_name="dana")
    try:
        assert result["ok"] is True
        rec = pr.read_record(run_dir)
        assert rec["author"] == "dana"
        assert rec["inputs"][0]["run_id"] == "coj-full"
    finally:
        for job in fit._FIT_JOBS.values():
            job.logf.close()


def test_start_ttv_fit_writes_record_with_author_and_inputs(db, monkeypatch):
    monkeypatch.setattr(ttv, "_harmonic_prefix", lambda: ["harmonic"])
    monkeypatch.setattr(ttv.subprocess, "Popen", lambda *_a, **_k: _Proc())
    monkeypatch.setattr(ttv, "_TTV_JOBS", {})
    monkeypatch.setattr("muscat_db.database.save_job", lambda **_k: None)

    result = ttv.start_ttv_fit(TARGET, _ttv_options(inputs=[_point()], run_name="r1"), "erin")
    try:
        assert result["ok"] is True
        rec = pr.read_record(ttv.ttv_output_dir(TARGET, "r1"))
        assert rec["author"] == "erin"
        assert rec["inputs"][0]["run_id"] == "r1"
    finally:
        for job in ttv._TTV_JOBS.values():
            job.logf.close()
