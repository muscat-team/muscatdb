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
            ("muscat2", DATE, "m1.fits", "BLANKTGT", ""),
            ("muscat2", DATE, "m2.fits", "BLANKTGT", ""),
            ("muscat2", DATE, "m3.fits", "MIXEDTGT", ""),
            ("muscat2", DATE, "m4.fits", "MIXEDTGT", "KEY2026B-001"),
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


def test_frozen_photometry_records_configuration_digest(db, tmp_path, monkeypatch):
    from muscat_db.prose_config import SNAPSHOT_SHA256
    monkeypatch.setenv("MUSCAT_PROSE_FROZEN_CONFIG", "1")
    phot._write_run_product(
        tmp_path, inst=INST, date=DATE, target=TARGET, opts={}, test_run=False,
        user_name="alice", site="", telescope="", mode="", run_name="frozen",
    )
    assert pr.read_record(tmp_path)["tool_versions"]["prose_config_sha256"] == SNAPSHOT_SHA256


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


# -- backfill: run directories with no job row -------------------------------


def _write_fit_yaml(rdir, files):
    import yaml

    rdir.mkdir(parents=True, exist_ok=True)
    data = {band: {"band": band, "file": name, "format": "afphot"} for band, name in files.items()}
    (rdir / "fit.yaml").write_text(yaml.safe_dump({"data": data, "planets": "b"}))


def _make_lightcurve(night, run_id, name):
    d = night / "_runs" / TARGET / run_id if run_id else night
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("BJD_TDB,flux\n")


def test_backfill_disk_photometry_run_without_job_row(db):
    rdir = phot.run_output_dir(INST, DATE, TARGET, "old-run")
    rdir.mkdir(parents=True)
    stats = backfill_products(db)
    rec = pr.read_record(rdir)
    assert stats.from_disk == 1 and stats.written == 1
    assert rec["backfilled"] is True and not rec["author"]
    assert rec["inputs"][0]["proposal_ids"] == ["KEY2026B-001", "OTHER-9"]


def test_backfill_disk_photometry_with_no_matching_frames_is_undetermined(db):
    phot.run_output_dir(INST, DATE, "GHOST", "r").mkdir(parents=True)
    backfill_products(db)
    assert pr.read_record(phot.run_output_dir(INST, DATE, "GHOST", "r"))["inputs"] is None


def test_backfill_disk_pass_does_not_recount_runs_with_job_rows(db):
    _insert_job(db, type="photometry", instrument=INST, obsdate=DATE, target=TARGET, run_id="r1",
                user_name="alice")
    phot.run_output_dir(INST, DATE, TARGET, "r1").mkdir(parents=True)
    stats = backfill_products(db)
    assert (stats.written, stats.from_disk) == (1, 0)
    assert pr.read_record(phot.run_output_dir(INST, DATE, TARGET, "r1"))["author"] == "alice"


def test_backfill_disk_transit_fit_recovers_inputs_from_fit_yaml(db):
    night = phot.output_base() / INST / DATE
    _make_lightcurve(night, "coj-full", "lc_g.csv")
    rdir = fit.fit_output_dir(INST, DATE, TARGET, "default")
    _write_fit_yaml(rdir, {"g": "lc_g.csv"})
    stats = backfill_products(db)
    rec = pr.read_record(rdir)
    assert stats.from_disk == 2  # the fit, and the photometry run holding its light curve
    assert rec["level"] == "transit_fit" and not rec["author"]
    assert rec["inputs"] == [pr.lightcurve_input(
        INST, DATE, TARGET, night / "_runs" / TARGET / "coj-full" / "lc_g.csv",
        night / "_runs" / TARGET,
    )]


def test_backfill_disk_transit_fit_legacy_layout_and_night_level_csv(db):
    night = phot.output_base() / INST / DATE
    _make_lightcurve(night, "", "lc_g.csv")
    rdir = fit.fit_output_dir(INST, DATE, TARGET)
    _write_fit_yaml(rdir, {"g": "lc_g.csv"})
    backfill_products(db)
    assert pr.read_record(rdir)["inputs"][0]["run_id"] == ""


def test_backfill_disk_transit_fit_ambiguous_csv_lists_every_candidate(db):
    night = phot.output_base() / INST / DATE
    _make_lightcurve(night, "run-a", "lc_g.csv")
    _make_lightcurve(night, "run-b", "lc_g.csv")
    rdir = fit.fit_output_dir(INST, DATE, TARGET, "default")
    _write_fit_yaml(rdir, {"g": "lc_g.csv"})
    backfill_products(db)
    rec = pr.read_record(rdir)
    assert sorted(i["run_id"] for i in rec["inputs"]) == ["run-a", "run-b"]
    assert rec["note"]


@pytest.mark.parametrize("files", [{"g": "missing.csv"}, {"g": "lc_g.csv", "r": "missing.csv"}])
def test_backfill_disk_transit_fit_unlocatable_csv_is_undetermined(db, files):
    night = phot.output_base() / INST / DATE
    _make_lightcurve(night, "run-a", "lc_g.csv")
    rdir = fit.fit_output_dir(INST, DATE, TARGET, "default")
    _write_fit_yaml(rdir, files)
    backfill_products(db)
    rec = pr.read_record(rdir)
    assert rec["inputs"] is None and "missing.csv" in rec["note"]


def test_backfill_disk_transit_fit_unreadable_fit_yaml_is_undetermined(db):
    rdir = fit.fit_output_dir(INST, DATE, TARGET, "default")
    rdir.mkdir(parents=True)
    (rdir / "fit.yaml").write_text("data: [unclosed")
    backfill_products(db)
    assert pr.read_record(rdir)["inputs"] is None


def test_backfill_disk_ignores_out_subdirectory_and_dirs_without_fit_yaml(db):
    rdir = fit.fit_output_dir(INST, DATE, TARGET, "default")
    _write_fit_yaml(rdir, {"g": "lc_g.csv"})
    _write_fit_yaml(rdir / "out", {"g": "lc_g.csv"})
    (fit.fit_output_dir(INST, DATE, TARGET, "empty")).mkdir(parents=True)
    stats = backfill_products(db)
    assert stats.from_disk == 1
    assert pr.read_record(rdir / "out") is None
    assert pr.read_record(fit.fit_output_dir(INST, DATE, TARGET, "empty")) is None


def test_backfill_disk_legacy_fit_out_directory_is_not_a_run(db):
    # legacy layout: the target directory is the run and timer's ``out`` sits directly below it
    night = phot.output_base() / INST / DATE
    _make_lightcurve(night, "", "lc_g.csv")
    rdir = fit.fit_output_dir(INST, DATE, TARGET)
    _write_fit_yaml(rdir, {"g": "lc_g.csv"})
    _write_fit_yaml(rdir / "out", {"g": "lc_g.csv"})
    backfill_products(db)
    assert pr.read_record(rdir) is not None
    assert pr.read_record(rdir / "out") is None


@pytest.mark.parametrize("name", ["../lc_g.csv", "sub/lc_g.csv"])
def test_backfill_disk_transit_fit_rejects_light_curve_path_not_a_bare_name(db, name):
    night = phot.output_base() / INST / DATE
    _make_lightcurve(night, "", "lc_g.csv")
    (night / "sub").mkdir()
    (night / "sub" / "lc_g.csv").write_text("BJD_TDB,flux\n")
    (night.parent / "lc_g.csv").write_text("BJD_TDB,flux\n")  # what ``../lc_g.csv`` resolves to
    rdir = fit.fit_output_dir(INST, DATE, TARGET, "default")
    _write_fit_yaml(rdir, {"g": name})
    backfill_products(db)
    assert pr.read_record(rdir)["inputs"] is None


def test_backfill_disk_dry_run_writes_nothing_and_second_run_is_a_noop(db):
    rdir = phot.run_output_dir(INST, DATE, TARGET, "old-run")
    rdir.mkdir(parents=True)
    dry = backfill_products(db, dry_run=True)
    assert dry.from_disk == 1 and dry.written == 0 and pr.read_record(rdir) is None
    backfill_products(db)
    again = backfill_products(db)
    assert again.from_disk == 0 and again.written == 0 and again.already == 1


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


def test_start_ttv_fit_never_sends_prior_flags(db, monkeypatch):
    """harmonic upstream has no --mu-min-me/--mu-max-me/--z-max and its
    argparse exits on them; options saved before #77 still carry the keys."""
    launched = []
    monkeypatch.setattr(ttv, "_harmonic_prefix", lambda: ["harmonic"])
    monkeypatch.setattr(ttv, "_harmonic_version", lambda: "0.0.0")
    monkeypatch.setattr(ttv.subprocess, "Popen", lambda cmd, **_k: launched.append(cmd) or _Proc())
    monkeypatch.setattr(ttv, "_TTV_JOBS", {})
    monkeypatch.setattr("muscat_db.database.save_job", lambda **_k: None)

    options = _ttv_options(inputs=[_point()], run_name="r1")
    options.update(mu_min_me=1.0, mu_max_me=30.0, z_max=0.1)
    result = ttv.start_ttv_fit(TARGET, options, "erin")
    try:
        assert result["ok"] is True
        assert len(launched) == 1
        assert not {"--mu-min-me", "--mu-max-me", "--z-max"} & set(launched[0])
    finally:
        for job in ttv._TTV_JOBS.values():
            job.logf.close()


# -- review of #245: record contents must not over-expose ---------------------


def test_frame_input_keeps_blank_proposal_ids(db):
    """A blank PROPID is a real value (#230 treats it as denied for scoped
    viewers); dropping it makes 'every input visible' vacuously true."""
    blank = pr.frame_input(db, "muscat2", DATE, "BLANKTGT")
    assert blank["resolved"] is True
    assert blank["n_frames"] == 2
    assert blank["proposal_ids"] == [""]
    assert blank["blank_frames"] == 2
    mixed = pr.frame_input(db, "muscat2", DATE, "MIXEDTGT")
    assert mixed["proposal_ids"] == ["", "KEY2026B-001"]
    assert mixed["blank_frames"] == 1


def test_live_photometry_run_with_no_matching_frames_is_undetermined(db, tmp_path):
    rdir = tmp_path / "run"
    rdir.mkdir()
    phot._write_run_product(
        rdir, inst=INST, date=DATE, target="GHOST", opts={}, test_run=True,
        user_name="alice", site="", telescope="", mode="", run_name="",
    )
    rec = pr.read_record(rdir)
    assert rec["inputs"] is None
    assert rec["note"]


def test_ttv_empty_inputs_with_times_is_undetermined(db, tmp_path):
    rdir = tmp_path / "ttv"
    rdir.mkdir()
    ttv.write_ttv_inputs(rdir, "c", "i", _ttv_options(inputs=[]), user_name="carol")
    rec = pr.read_record(rdir)
    assert rec["inputs"] is None
    assert rec["note"]


def test_ttv_empty_inputs_for_a_fit_with_no_times_is_empty(db, tmp_path):
    rdir = tmp_path / "ttv"
    rdir.mkdir()
    opts = _ttv_options(inputs=[], csv_content="planet,epoch,tc,tc_unc\n# nothing\n")
    ttv.write_ttv_inputs(rdir, "c", "i", opts, user_name="carol")
    assert pr.read_record(rdir)["inputs"] == []


def test_prose_version_retries_after_a_failed_lookup(monkeypatch):
    calls = []

    class _R:
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out

    outcomes = [_R(1, ""), _R(0, "2.0.1\n")]

    def fake_run(*_a, **_k):
        calls.append(1)
        return outcomes[len(calls) - 1]

    monkeypatch.setattr(phot, "_PROSE_VERSION", None)
    monkeypatch.setattr(phot, "_PROSE_VERSION_FAILED_AT", None, raising=False)
    monkeypatch.setattr(phot, "_conda_env_python", lambda _env: "/bin/python")
    monkeypatch.setattr(phot.subprocess, "run", fake_run)
    t0 = 1000.0
    clock = iter([t0, t0 + 1, t0 + phot._PROSE_VERSION_RETRY_S + 1])
    monkeypatch.setattr(phot.time, "monotonic", lambda: next(clock))

    assert phot.prose_version() == "unknown"       # lookup fails
    assert phot.prose_version() == "unknown"       # inside the retry window: no new lookup
    assert len(calls) == 1
    assert phot.prose_version() == "2.0.1"         # after the window: retried and cached
    assert phot.prose_version() == "2.0.1"
    assert len(calls) == 2


def test_prose_version_rechecks_an_editable_checkout_every_time(monkeypatch):
    """A dev checkout can switch commits while the server runs; never cache it."""
    sha_a, sha_b = "a" * 40, "b" * 40
    outcomes = iter([f"3.4.0+git.{sha_a}.editable\n", f"3.4.0+git.{sha_b}.editable\n"])

    class _R:
        returncode = 0

        def __init__(self):
            self.stdout = next(outcomes)

    monkeypatch.setattr(phot, "_PROSE_VERSION", None)
    monkeypatch.setattr(phot, "_PROSE_VERSION_FAILED_AT", None, raising=False)
    monkeypatch.setattr(phot, "_conda_env_python", lambda _env: "/bin/python")
    monkeypatch.setattr(phot.subprocess, "run", lambda *_a, **_k: _R())

    assert phot.prose_version() == f"3.4.0+git.{sha_a}.editable"
    assert phot.prose_version() == f"3.4.0+git.{sha_b}.editable"


# -- target spelling (#221, #245) ---------------------------------------------

VARIANT_INST = "muscat3"
VARIANT_DATE = "260202"


@pytest.fixture
def variant_frames(db):
    """Frames filed as 'TOI 55.01'; runs name the same target 'TOI-55'."""
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO frames (instrument, obsdate, ccd, filename, object, proposal_id) "
        "VALUES (?, ?, 1, ?, ?, ?)",
        [(VARIANT_INST, VARIANT_DATE, f"v{i}.fits", "TOI 55.01", "P-55") for i in range(2)],
    )
    conn.commit()
    conn.close()
    return db


def test_live_photometry_resolves_a_spelling_variant_of_the_target(variant_frames, tmp_path):
    rdir = tmp_path / "run"
    rdir.mkdir()
    phot._write_run_product(
        rdir, inst=VARIANT_INST, date=VARIANT_DATE, target="TOI-55", opts={}, test_run=True,
        user_name="alice", site="", telescope="", mode="", run_name="",
    )
    rec = pr.read_record(rdir)
    assert rec["inputs"] is not None
    assert rec["inputs"][0]["proposal_ids"] == ["P-55"]
    assert rec["inputs"][0]["n_frames"] == 2


def test_backfill_photometry_resolves_a_spelling_variant_of_the_target(variant_frames):
    _insert_job(variant_frames, type="photometry", instrument=VARIANT_INST, obsdate=VARIANT_DATE,
                target="TOI-55", run_id="r1", user_name="alice")
    rdir = phot.run_output_dir(VARIANT_INST, VARIANT_DATE, "TOI-55", "r1")
    rdir.mkdir(parents=True)
    stats = backfill_products(variant_frames)
    assert (stats.determined, stats.undetermined) == (1, 0)
    assert pr.read_record(rdir)["inputs"][0]["proposal_ids"] == ["P-55"]


def test_target_normalizer_matches_the_catalog_name_rules(variant_frames):
    norm = pr.target_normalizer(variant_frames)
    assert norm("TOI-55") == norm("TOI 55.01")
