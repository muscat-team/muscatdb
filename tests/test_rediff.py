"""Tests for re-differencing a run from its flux datacube (muscat_db.rediff).

prose2's ``rediff_lightcurves`` is tested in prose2; here the subprocess is
replaced by a stand-in, and what is under test is the muscat-db layer:
request validation, the command line, the undo snapshot, its layering with
the post-process clip, and the /api/photometry/rediff endpoints.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from muscat_db import postprocess as pp
from muscat_db import rediff as rd

INST = "muscat4"
DATE = "250512"
TARGET = "TOI-6715"
RUN = "first_half"
SEL = [{"band": "gp", "comparisons": [3, 5], "aperture": 1}]
GP_CSV = f"{TARGET}_{INST}_gp_{DATE}.csv"
RP_CSV = f"{TARGET}_{INST}_rp_{DATE}.csv"


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    base = tmp_path / "prose"
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(base))
    rdir = base / INST / DATE / "_runs" / TARGET / RUN
    rdir.mkdir(parents=True)
    (rdir / "_webrun_meta.json").write_text(json.dumps(
        {"run_id": RUN, "site": "lsc", "telescope": "1m0-05", "run_type": "full"}
    ))
    for name in (GP_CSV, RP_CSV):
        (rdir / name).write_text("BJD_TDB,Flux,Err\n2460807.84,1.0001,0.002\n2460807.85,0.9998,0.002\n")
    monkeypatch.setattr("muscat_db.photometry.job_status", lambda *a, **k: {"state": "done"})
    return rdir


def _band_report(applied=False):
    part = {"comparisons": [3, 5], "aperture": 1, "aper_radius": 6.0, "weighting": "equal",
            "metrics": {"n": 2, "std_ppt": 1.0, "p2p_ppt": 0.9}}
    return {"ok": True, "applied": applied, "summary_png": "x_lightcurves.png" if applied else None,
            "bands": {"gp": {"default": {**part, "comparisons": [1, 2], "weighting": "broeg"},
                             "custom": part, "internal": "dropped"}}}


def _fake_prose(rdir: Path, calls: list):
    """Stand-in for rediff_lightcurves: --apply rewrites the gp CSV."""
    def run(args):
        calls.append(args)
        if "--info" in args:
            return {"ok": True, "bands": {"gp": {"aper_radii": [4.0, 6.0]}}}
        if "--apply" in args:
            (rdir / GP_CSV).write_text("BJD_TDB,Flux,Err\n2460807.84,1.01,0.003\n2460807.85,0.99,0.003\n")
            (rdir / f"{TARGET}_{INST}_{DATE}_lightcurves.png").write_bytes(b"rediff-png")
            return _band_report(applied=True)
        return _band_report()
    return run


def _fake_clip(rdir: Path):
    def run(args):
        p = rdir / GP_CSV
        p.write_text("\n".join(p.read_text().splitlines()[:2]) + "\n")
        return {"ok": True, "applied": True, "files": []}
    return run


def _files(rdir):
    return {p.name: p.read_bytes() for p in sorted(rdir.glob("*.csv"))}


@pytest.mark.parametrize("raw, message", [
    (None, "at least one band"),
    ([], "at least one band"),
    (["gp"], "needs a band"),
    ([{"band": "g p", "comparisons": [1], "aperture": 0}], "invalid or repeated band"),
    ([{"band": "gp=1", "comparisons": [1], "aperture": 0}], "invalid or repeated band"),
    ([SEL[0], SEL[0]], "invalid or repeated band"),
    ([{"band": "gp", "comparisons": [], "aperture": 0}], "select 1 to"),
    ([{"band": "gp", "comparisons": ["a"], "aperture": 0}], "integers"),
    ([{"band": "gp", "comparisons": [1], "aperture": None}], "integers"),
    ([{"band": "gp", "comparisons": [-1], "aperture": 0}], "out of range"),
    ([{"band": "gp", "comparisons": [1, 1], "aperture": 0}], "duplicate"),
])
def test_parse_selections_rejects_bad_requests(raw, message):
    sel, err = rd.parse_selections(raw)
    assert sel == [] and message in err


def test_parse_selections_accepts_numeric_strings():
    sel, err = rd.parse_selections([{"band": "gp", "comparisons": ["3", 5], "aperture": "1"}])
    assert err is None and sel == SEL


def test_info_reports_bands_and_state(run_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(pp, "_run_sync", _fake_prose(run_dir, calls))
    res = rd.info(INST, DATE, TARGET, RUN)
    assert res["ok"] and res["bands"]["gp"]["aper_radii"] == [4.0, 6.0]
    assert res["restorable"] is False and res["clip_active"] is False
    assert calls[0][-2:] == [str(run_dir), "--info"]
    assert "prose.scripts.rediff_lightcurves" in calls[0]


def test_test_mode_builds_select_and_preview_and_writes_nothing(run_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(pp, "_run_sync", _fake_prose(run_dir, calls))
    monkeypatch.setattr(pp, "_preview_path", lambda: "/nonexistent/preview.png")
    monkeypatch.setattr(pp, "_read_preview", lambda path: "data:image/png;base64,QQ==")
    before = _files(run_dir)
    res = rd.rediff(INST, DATE, TARGET, RUN, SEL, apply=False)
    assert res["ok"] and not res["applied"]
    assert res["preview_png"].startswith("data:image/png")
    assert "internal" not in res["bands"]["gp"]
    args = calls[0]
    assert args[args.index("--select") + 1] == "gp=3,5@1"
    assert args[args.index("--preview") + 1] == "/nonexistent/preview.png"
    assert "--apply" not in args
    assert _files(run_dir) == before and not (run_dir / rd._BACKUP_DIRNAME).exists()


def test_apply_passes_run_context_snapshots_and_records_selection(run_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(pp, "_run_sync", _fake_prose(run_dir, calls))
    res = rd.rediff(INST, DATE, TARGET, RUN, SEL, apply=True)
    assert res["ok"] and res["applied"] and res["restorable"]
    args = calls[0]
    for flag, value in (("--target", TARGET), ("--inst", INST), ("--date", DATE),
                        ("--site", "lsc"), ("--telescope", "1m0-05"), ("--confmode", "full")):
        assert args[args.index(flag) + 1] == value
    assert res["applied_selection"]["gp"]["comparisons"] == [3, 5]
    assert rd.info(INST, DATE, TARGET, RUN)["applied_selection"]["gp"]["aperture"] == 1


def test_apply_then_restore_returns_the_pipeline_files(run_dir, monkeypatch):
    original = _files(run_dir)
    monkeypatch.setattr(pp, "_run_sync", _fake_prose(run_dir, []))
    rd.rediff(INST, DATE, TARGET, RUN, SEL, apply=True)
    assert _files(run_dir) != original

    res = rd.restore_rediff(INST, DATE, TARGET, RUN)

    assert res["ok"], res
    assert _files(run_dir) == original
    assert not (run_dir / f"{TARGET}_{INST}_{DATE}_lightcurves.png").exists()  # created by the apply
    assert not (run_dir / rd._BACKUP_DIRNAME).exists()
    assert not (run_dir / rd._APPLIED_NAME).exists()


def test_second_apply_keeps_the_pipeline_snapshot(run_dir, monkeypatch):
    original = _files(run_dir)
    monkeypatch.setattr(pp, "_run_sync", _fake_prose(run_dir, []))
    rd.rediff(INST, DATE, TARGET, RUN, SEL, apply=True)
    rd.rediff(INST, DATE, TARGET, RUN, [{**SEL[0], "comparisons": [4]}], apply=True)
    assert rd.restore_rediff(INST, DATE, TARGET, RUN)["ok"]
    assert _files(run_dir) == original


def test_apply_and_restore_are_refused_while_a_clip_is_active(run_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(pp, "_run_sync", _fake_prose(run_dir, calls))
    rd.rediff(INST, DATE, TARGET, RUN, SEL, apply=True)
    monkeypatch.setattr(pp, "_run_sync", _fake_clip(run_dir))
    assert pp.postprocess(INST, DATE, TARGET, RUN, 5.0, 2, 5, apply=True)["ok"]
    clipped = _files(run_dir)

    monkeypatch.setattr(pp, "_run_sync", _fake_prose(run_dir, calls))
    applied = rd.rediff(INST, DATE, TARGET, RUN, SEL, apply=True)
    restored = rd.restore_rediff(INST, DATE, TARGET, RUN)

    assert not applied["ok"] and "Restore pre-clip" in applied["error"]
    assert applied["clip_active"] is True
    assert not restored["ok"] and "Restore pre-clip" in restored["error"]
    assert _files(run_dir) == clipped and len(calls) == 1  # only the first apply ran


def test_clip_restore_returns_to_the_rediff_then_rediff_restore_to_the_pipeline(run_dir, monkeypatch):
    original = _files(run_dir)
    monkeypatch.setattr(pp, "_run_sync", _fake_prose(run_dir, []))
    rd.rediff(INST, DATE, TARGET, RUN, SEL, apply=True)
    rediffed = _files(run_dir)
    monkeypatch.setattr(pp, "_run_sync", _fake_clip(run_dir))
    pp.postprocess(INST, DATE, TARGET, RUN, 5.0, 2, 5, apply=True)
    assert _files(run_dir) != rediffed

    assert pp.restore_clip(INST, DATE, TARGET, RUN)["ok"]
    assert _files(run_dir) == rediffed
    assert rd.restore_rediff(INST, DATE, TARGET, RUN)["ok"]
    assert _files(run_dir) == original


def test_apply_is_refused_while_the_job_runs(run_dir, monkeypatch):
    monkeypatch.setattr("muscat_db.photometry.job_status", lambda *a, **k: {"state": "running"})
    monkeypatch.setattr(pp, "_run_sync", lambda a: pytest.fail("must not run prose"))
    res = rd.rediff(INST, DATE, TARGET, RUN, SEL, apply=True)
    assert not res["ok"] and "running" in res["error"]
    assert not (run_dir / rd._BACKUP_DIRNAME).exists()


def test_restore_without_backup_and_missing_run_are_errors(run_dir):
    assert "nothing to restore" in rd.restore_rediff(INST, DATE, TARGET, RUN)["error"]
    assert "no such run" in rd.info(INST, DATE, TARGET, "nope")["error"]


def test_prose_failure_is_reported_with_its_message(run_dir, monkeypatch):
    monkeypatch.setattr(pp, "_run_sync", lambda a: {"ok": False, "error": "star 0 is the target"})
    monkeypatch.setattr(pp, "_read_preview", lambda path: None)
    res = rd.rediff(INST, DATE, TARGET, RUN, SEL, apply=False)
    assert not res["ok"] and res["error"] == "star 0 is the target"


class TestEndpoints:
    def _post(self, path, body):
        from muscat_db import web

        return TestClient(web.app).post(f"/api/photometry/{path}", json=body)

    def test_rediff_endpoint_passes_selections_and_apply(self, mock_db, monkeypatch):
        from muscat_db import web

        seen = {}

        def fake(inst, date, target, run, selections, *, apply):
            seen.update(key=(inst, date, target, run), selections=selections, apply=apply)
            return {"ok": True, "applied": False, "bands": {}, "preview_png": "data:image/png;base64,QQ=="}

        monkeypatch.setattr(web.rediff_mod, "rediff", fake)
        r = self._post("rediff", {"inst": INST, "date": DATE, "target": TARGET, "run": RUN,
                                  "selections": SEL, "apply": False})
        assert r.status_code == 200 and r.json()["preview_png"]
        assert seen == {"key": (INST, DATE, TARGET, RUN), "selections": SEL, "apply": False}

    def test_bad_selection_is_400_with_reason(self, mock_db, run_dir):
        r = self._post("rediff", {"inst": INST, "date": DATE, "target": TARGET, "run": RUN,
                                  "selections": [{"band": "gp", "comparisons": [], "aperture": 0}]})
        assert r.status_code == 400 and "select 1 to" in r.json()["error"]

    @pytest.mark.parametrize("path", ["rediff/info", "rediff", "rediff/restore"])
    def test_run_is_required(self, mock_db, path):
        r = self._post(path, {"inst": INST, "date": DATE, "target": TARGET})
        assert r.status_code == 400 and "run" in r.json()["error"]

    def test_info_and_restore_endpoints(self, mock_db, monkeypatch):
        from muscat_db import web

        monkeypatch.setattr(web.rediff_mod, "info", lambda *a: {
            "ok": True, "bands": {"gp": {}}, "restorable": True, "clip_active": False,
            "applied_selection": {}})
        monkeypatch.setattr(web.rediff_mod, "restore_rediff", lambda *a: {
            "ok": False, "error": "no re-diff backup for this run; nothing to restore"})
        body = {"inst": INST, "date": DATE, "target": TARGET, "run": RUN}
        info = self._post("rediff/info", body)
        assert info.status_code == 200 and info.json()["restorable"] is True
        restore = self._post("rediff/restore", body)
        assert restore.status_code == 400 and "nothing to restore" in restore.json()["error"]
