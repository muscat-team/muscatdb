"""Tests for the synchronous post-processing pass (muscat_db.postprocess).

The prose CLI itself is exercised directly by prose2's own test suite and only
smoke-tested here on the host; the muscat-db layer under test is the
``postprocess()`` orchestration (context resolution, command building, job
guarding, preview plumbing) and the /api/photometry/postprocess endpoint.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from muscat_db import postprocess as pp


INST = "muscat4"
DATE = "250512"
TARGET = "TOI-6715"
RUN = "first_half"


@pytest.fixture
def prose_dir(tmp_path, monkeypatch):
    base = tmp_path / "prose"
    base.mkdir()
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(base))
    return base


def _make_run_dir(base: Path, target: str = TARGET, run: str = RUN) -> Path:
    rdir = base / INST / DATE / "_runs" / target.replace(" ", "") / run
    rdir.mkdir(parents=True)
    (rdir / "_webrun_meta.json").write_text(
        '{"run_id":"' + run + '","run_name":"' + run + '",'
        '"site":"lsc","mode":"full","telescope":"1m0-05","run_type":"full"}'
    )
    stem = f"{target}_{INST}_gp_{DATE}"
    (rdir / (stem + ".csv")).write_text(
        "BJD_TDB,Flux,Err\n2460807.84,1.0001,0.0019\n2460807.85,0.9998,0.0020\n"
    )
    (rdir / (stem + "_rp.csv")).write_text(
        "BJD_TDB,Flux,Err\n2460807.84,0.999,0.004\n2460807.85,1.001,0.004\n"
    )
    return rdir


def _ok_report() -> dict:
    return {
        "ok": True,
        "results_dir": str(Path("/prose")),
        "sigma": 5.0,
        "degree": 2,
        "iterations": 5,
        "applied": False,
        "n_files": 2,
        "files": [
            {
                "file": f"{TARGET}_{INST}_gp_{DATE}.csv",
                "n": 2,
                "n_clipped": 0,
                "n_kept": 2,
                "kept_fraction": 1.0,
                "sigma": 5.0,
                "degree": 2,
                "iterations": 5,
            },
            {
                "file": f"{TARGET}_{INST}_rp_{DATE}.csv",
                "n": 2,
                "n_clipped": 1,
                "n_kept": 1,
                "kept_fraction": 0.5,
                "sigma": 5.0,
                "degree": 2,
                "iterations": 5,
            },
        ],
        "preview": None,
        "summary_png": None,
        "written": [],
    }


class TestValidateParams:
    @pytest.mark.parametrize(
        "sigma,degree,iterations",
        [(5.0, 2, 5), (0.1, 0, 1), (100.0, 6, 50), (3.5, 3, 12)],
    )
    def test_accepts_in_range(self, sigma, degree, iterations):
        assert pp.validate_params(sigma, degree, iterations) is None

    @pytest.mark.parametrize(
        "sigma,degree,iterations",
        [
            ("abc", 2, 5),
            (0, 2, 5),
            (101, 2, 5),
            (-1, 2, 5),
            (5.0, "x", 5),
            (5.0, -1, 5),
            (5.0, 7, 5),
            (5.0, 2, 0),
            (5.0, 2, 51),
        ],
    )
    def test_rejects_out_of_range(self, sigma, degree, iterations):
        assert pp.validate_params(sigma, degree, iterations) is not None

    @pytest.mark.parametrize(
        "before,after",
        [(None, None), ("", ""), (2460877.0, None), (None, 2460877.0), (2460870.0, 2460877.0)],
    )
    def test_accepts_valid_jd_exclusion(self, before, after):
        assert pp.validate_params(5.0, 2, 5, before, after) is None

    def test_rejects_non_numeric_jd_exclusion(self):
        assert pp.validate_params(5.0, 2, 5, "not-a-number", None) is not None
        assert pp.validate_params(5.0, 2, 5, None, "not-a-number") is not None

    @pytest.mark.parametrize("bad", ["inf", "-inf", "nan", "1e999", float("inf"), float("nan")])
    def test_rejects_non_finite_jd_exclusion(self, bad):
        # inf/nan parse as floats; with apply=true an infinite bound would
        # rewrite every band CSV in place with zero rows.
        assert pp.validate_params(5.0, 2, 5, bad, None) is not None
        assert pp.validate_params(5.0, 2, 5, None, bad) is not None

    def test_rejects_before_not_less_than_after(self):
        err = pp.validate_params(5.0, 2, 5, 2460877.0, 2460877.0)
        assert err is not None and "before" in err and "after" in err
        assert pp.validate_params(5.0, 2, 5, 2460880.0, 2460870.0) is not None

    @pytest.mark.parametrize("sigma", [None, "", "  "])
    def test_blank_sigma_disables_clipping_without_error(self, sigma):
        assert pp.validate_params(sigma, 2, 5) is None

    def test_non_numeric_sigma_still_rejected(self):
        assert pp.validate_params("abc", 2, 5) is not None


class TestRunContext:
    def test_named_run_dir_and_meta(self, prose_dir):
        _make_run_dir(prose_dir)
        ctx = pp._run_context(INST, DATE, TARGET, RUN)
        assert ctx["results_dir"] == str(
            prose_dir / INST / DATE / "_runs" / TARGET / RUN
        )
        assert ctx["site"] == "lsc"
        assert ctx["telescope"] == "1m0-05"
        # run_type full -> confmode "full"
        assert ctx["confmode"] == "full"

    def test_missing_run_dir_raises(self, prose_dir):
        with pytest.raises(pp.PostprocessError):
            pp._run_context(INST, DATE, TARGET, "nope")

    def test_legacy_dir_when_run_id_empty(self, prose_dir):
        base_dir = prose_dir / INST / DATE
        base_dir.mkdir(parents=True)
        ctx = pp._run_context(INST, DATE, TARGET, "")
        assert ctx["results_dir"] == str(base_dir)

    def test_command_shape(self, prose_dir):
        _make_run_dir(prose_dir)
        ctx = pp._run_context(INST, DATE, TARGET, RUN)
        cmd = pp._command(ctx, sigma=3.5, degree=2, iterations=5, apply=False, preview_path="/tmp/x.png")
        assert cmd[-8:] == ["--sigma", "3.5", "--degree", "2", "--iterations", "5", "--preview", "/tmp/x.png"]
        assert "--exclude-before-jd" not in cmd and "--exclude-after-jd" not in cmd
        apply_cmd = pp._command(ctx, sigma=3.5, degree=2, iterations=5, apply=True)
        assert "--apply" in apply_cmd
        assert "--target" in apply_cmd and "--inst" in apply_cmd and "--date" in apply_cmd
        assert "--site" in apply_cmd and "--confmode" in apply_cmd and "--telescope" in apply_cmd
        assert "--preview" not in apply_cmd

    def test_command_includes_jd_exclusion_when_given(self, prose_dir):
        _make_run_dir(prose_dir)
        ctx = pp._run_context(INST, DATE, TARGET, RUN)
        cmd = pp._command(
            ctx, sigma=3.5, degree=2, iterations=5, apply=False,
            exclude_before_jd=2460870.0, exclude_after_jd=2460877.5,
        )
        assert "--exclude-before-jd" in cmd
        assert cmd[cmd.index("--exclude-before-jd") + 1] == "2460870.0"
        assert "--exclude-after-jd" in cmd
        assert cmd[cmd.index("--exclude-after-jd") + 1] == "2460877.5"

    def test_command_sigma_none_maps_to_sigma_none_flag(self, prose_dir):
        _make_run_dir(prose_dir)
        ctx = pp._run_context(INST, DATE, TARGET, RUN)
        cmd = pp._command(ctx, sigma=None, degree=2, iterations=5, apply=False)
        assert cmd[cmd.index("--sigma") + 1] == "none"


class TestRunSyncReport:
    @staticmethod
    def _run(monkeypatch, stdout):
        import subprocess

        monkeypatch.setattr(
            pp.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=stdout, stderr=""),
        )
        return pp._run_sync(["prose"])

    def test_single_line_report(self, monkeypatch):
        assert self._run(monkeypatch, 'log line\n{"ok": true, "n": 1}\n') == {"ok": True, "n": 1}

    def test_pretty_printed_report(self, monkeypatch):
        import json

        report = {"ok": True, "files": [{"name": "a.csv", "rows": 3}], "nested": {"k": [1, 2]}}
        out = "progress {not json}\n" + json.dumps(report, indent=2) + "\n"
        assert self._run(monkeypatch, out) == report

    def test_pretty_report_after_early_json_log_line(self, monkeypatch):
        import json

        report = {"ok": True, "files": [{"name": "a.csv", "rows": 3}]}
        out = '{"event": "start"}\nworking...\n' + json.dumps(report, indent=2) + "\n"
        assert self._run(monkeypatch, out) == report

    def test_pretty_report_with_compact_inner_objects(self, monkeypatch):
        report = {"ok": True, "files": [{"name": "a.csv", "rows": 3}]}
        out = (
            '{\n  "ok": true,\n  "files": [\n'
            '    {"name": "a.csv", "rows": 3}\n  ]\n}\n'
        )
        assert self._run(monkeypatch, out) == report

    def test_single_line_report_followed_by_log_text(self, monkeypatch):
        out = 'early log\n{"ok": true, "n": 1}\nshutting down\n'
        assert self._run(monkeypatch, out) == {"ok": True, "n": 1}

    def test_indented_single_line_report(self, monkeypatch):
        assert self._run(monkeypatch, 'log\n   {"ok": true, "n": 2}\n') == {"ok": True, "n": 2}

    def test_no_json_falls_through(self, monkeypatch):
        assert self._run(monkeypatch, "just text\n")["ok"] is False


class TestPostprocess:
    def test_test_preview_mode(self, prose_dir, monkeypatch):
        _make_run_dir(prose_dir)
        calls: list[list[str]] = []
        monkeypatch.setattr(
            pp, "_run_sync", lambda args: calls.append(args) or _ok_report()
        )
        monkeypatch.setattr(pp, "_read_preview", lambda path: "data:image/png;base64,QQ==")
        monkeypatch.setattr(pp, "_preview_path", lambda: "/tmp/preview.png")

        res = pp.postprocess(INST, DATE, TARGET, RUN, 5.0, 2, 5, apply=False)

        assert res["ok"] and not res["applied"]
        assert res["preview_png"] == "data:image/png;base64,QQ=="
        assert [c["file"] for c in res["files"]] == [
            f"{TARGET}_{INST}_gp_{DATE}.csv",
            f"{TARGET}_{INST}_rp_{DATE}.csv",
        ]
        assert len(calls) == 1
        assert "--apply" not in calls[0]
        assert "--preview" in calls[0]

    def test_apply_mode_overwrites_and_guards_active_job(self, prose_dir, monkeypatch):
        _make_run_dir(prose_dir)
        monkeypatch.setattr("muscat_db.photometry.job_status", lambda *a, **k: {"state": "done"})
        calls: list[list[str]] = []
        report = _ok_report()
        report["applied"] = True
        report["summary_png"] = f"{TARGET}_{INST}_{DATE}_lightcurves.png"
        report["written"] = [f"{TARGET}_{INST}_gp_{DATE}.csv: dropped 1 rows"]
        monkeypatch.setattr(pp, "_run_sync", lambda args: calls.append(args) or report)

        res = pp.postprocess(INST, DATE, TARGET, RUN, 3.0, 2, 5, apply=True)

        assert res["ok"] and res["applied"]
        assert res["summary_png"] == f"{TARGET}_{INST}_{DATE}_lightcurves.png"
        assert len(calls) == 1
        assert "--apply" in calls[0]

    def test_apply_blocked_while_job_running(self, prose_dir, monkeypatch):
        _make_run_dir(prose_dir)
        monkeypatch.setattr("muscat_db.photometry.job_status", lambda *a, **k: {"state": "running"})

        res = pp.postprocess(INST, DATE, TARGET, RUN, 5.0, 2, 5, apply=True)

        assert not res["ok"]
        assert "wait for it to finish" in res["error"]

    def test_apply_allowed_when_job_state_none(self, prose_dir, monkeypatch):
        _make_run_dir(prose_dir)
        monkeypatch.setattr("muscat_db.photometry.job_status", lambda *a, **k: {"state": "none"})
        report = _ok_report()
        report["applied"] = True
        monkeypatch.setattr(pp, "_run_sync", lambda args: report)

        res = pp.postprocess(INST, DATE, TARGET, RUN, 5.0, 2, 5, apply=True)

        assert res["ok"] and res["applied"]

    def test_characterization_sigma_missing_run(self, prose_dir):
        res = pp.postprocess(INST, DATE, TARGET, "absent", 5.0, 2, 5, apply=False)
        assert not res["ok"]
        assert "no such run directory" in res["error"]

    def test_invalid_params_return_error_not_raise(self, prose_dir):
        res = pp.postprocess(INST, DATE, TARGET, RUN, 5.0, 99, 5, apply=False)
        assert not res["ok"]
        assert "poly degree" in res["error"]

    def test_invalid_jd_exclusion_returns_error_not_raise(self, prose_dir):
        _make_run_dir(prose_dir)
        res = pp.postprocess(
            INST, DATE, TARGET, RUN, 5.0, 2, 5, apply=False,
            exclude_before_jd=2460877.0, exclude_after_jd=2460870.0,
        )
        assert not res["ok"]
        assert "before" in res["error"] and "after" in res["error"]

    def test_forwards_jd_exclusion_to_command_and_blank_means_unset(self, prose_dir, monkeypatch):
        _make_run_dir(prose_dir)
        calls: list[list[str]] = []
        monkeypatch.setattr(
            pp, "_run_sync", lambda args: calls.append(args) or _ok_report()
        )
        monkeypatch.setattr(pp, "_read_preview", lambda path: None)
        monkeypatch.setattr(pp, "_preview_path", lambda: "/tmp/preview.png")

        pp.postprocess(
            INST, DATE, TARGET, RUN, 5.0, 2, 5, apply=False,
            exclude_before_jd="2460870.0", exclude_after_jd="",
        )
        assert "--exclude-before-jd" in calls[0]
        assert "--exclude-after-jd" not in calls[0]

    def test_blank_sigma_forwards_sigma_none_and_succeeds(self, prose_dir, monkeypatch):
        _make_run_dir(prose_dir)
        calls: list[list[str]] = []
        monkeypatch.setattr(
            pp, "_run_sync", lambda args: calls.append(args) or _ok_report()
        )
        monkeypatch.setattr(pp, "_read_preview", lambda path: None)
        monkeypatch.setattr(pp, "_preview_path", lambda: "/tmp/preview.png")

        res = pp.postprocess(
            INST, DATE, TARGET, RUN, "", 2, 5, apply=False,
            exclude_after_jd="2460877.0",
        )
        assert res["ok"]
        assert calls[0][calls[0].index("--sigma") + 1] == "none"
        assert "--exclude-after-jd" in calls[0]


class TestEndpoint:
    def test_postprocess_endpoint_rejects_bad_payload(self, mock_db):
        client = TestClient(__import__("muscat_db.web", fromlist=["app"]).app)
        r = client.post(
            "/api/photometry/postprocess",
            json={"inst": "nope", "date": "250512", "target": TARGET, "run": RUN, "apply": False},
        )
        assert r.status_code == 400
        assert r.json()["ok"] is False

    def test_postprocess_endpoint_requires_run(self, mock_db):
        client = TestClient(__import__("muscat_db.web", fromlist=["app"]).app)
        r = client.post(
            "/api/photometry/postprocess",
            json={"inst": INST, "date": DATE, "target": TARGET, "run": "", "apply": False},
        )
        assert r.status_code == 400
        assert r.json()["error"] == "a run is required"

    def test_postprocess_endpoint_forwards_payload(self, mock_db, monkeypatch):
        from muscat_db import web

        calls: dict = {}
        def fake_postprocess(inst, date, target, run, sigma, degree, iterations, *,
                              apply, exclude_before_jd=None, exclude_after_jd=None):
            calls.update(inst=inst, date=date, target=target, run=run,
                         sigma=sigma, degree=degree, iterations=iterations, apply=apply,
                         exclude_before_jd=exclude_before_jd, exclude_after_jd=exclude_after_jd)
            return {
                "ok": True,
                "applied": False,
                "sigma": sigma,
                "degree": degree,
                "iterations": iterations,
                "n_files": 0,
                "files": [],
                "preview_png": "data:image/png;base64,QQ==",
            }

        monkeypatch.setattr(web.postproc, "postprocess", fake_postprocess)
        client = TestClient(web.app)
        r = client.post(
            "/api/photometry/postprocess",
            json={"inst": INST, "date": DATE, "target": TARGET, "run": RUN,
                  "sigma": 4.0, "degree": 1, "iterations": 3, "apply": False,
                  "exclude_before_jd": 2460870.0, "exclude_after_jd": ""},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] and body["preview_png"].startswith("data:image/png")
        assert calls == {
            "inst": INST, "date": DATE, "target": TARGET, "run": RUN,
            "sigma": 4.0, "degree": 1, "iterations": 3, "apply": False,
            "exclude_before_jd": 2460870.0, "exclude_after_jd": "",
        }

    def test_postprocess_endpoint_surfaces_subprocess_failure(self, mock_db, monkeypatch):
        from muscat_db import web

        monkeypatch.setattr(
            web.postproc,
            "postprocess",
            lambda *a, **k: {"ok": False, "error": "no band light-curve CSVs found"},
        )
        client = TestClient(web.app)
        r = client.post(
            "/api/photometry/postprocess",
            json={"inst": INST, "date": DATE, "target": TARGET, "run": RUN, "apply": False},
        )
        assert r.status_code == 400
        assert "no band light-curve" in r.json()["error"]

def _fake_prose_clip(rdir: Path, *, drop_png: bool = True):
    """Stand-in for prose2's apply_clip: halves every band CSV in place."""
    def run(args):
        for p in sorted(rdir.glob("*.csv")):
            head, *rows = p.read_text().splitlines()
            p.write_text("\n".join([head, *rows[:1]]) + "\n")
        if drop_png:
            (rdir / f"{TARGET}_{INST}_{DATE}_lightcurves.png").write_bytes(b"clipped-png")
        report = _ok_report()
        report["applied"] = True
        return report
    return run


class TestClipBackupRestore:
    @pytest.fixture
    def run_dir(self, prose_dir, monkeypatch):
        rdir = _make_run_dir(prose_dir)
        monkeypatch.setattr("muscat_db.photometry.job_status", lambda *a, **k: {"state": "done"})
        return rdir

    def _original(self, rdir):
        return {p.name: p.read_bytes() for p in rdir.glob("*.csv")}

    def test_apply_backs_up_then_restore_recovers_originals(self, run_dir, monkeypatch):
        original = self._original(run_dir)
        monkeypatch.setattr(pp, "_run_sync", _fake_prose_clip(run_dir))

        res = pp.postprocess(INST, DATE, TARGET, RUN, 5.0, 2, 5, apply=True)

        assert res["ok"] and res["restorable"] is True
        assert self._original(run_dir) != original  # the clip really rewrote them

        out = pp.restore_clip(INST, DATE, TARGET, RUN)

        assert out["ok"], out
        assert self._original(run_dir) == original
        assert not (run_dir / ".clip_backup").exists()
        # the summary figure the clip created did not exist before: removed
        assert not (run_dir / f"{TARGET}_{INST}_{DATE}_lightcurves.png").exists()

    def test_second_apply_keeps_the_first_snapshot(self, run_dir, monkeypatch):
        original = self._original(run_dir)
        monkeypatch.setattr(pp, "_run_sync", _fake_prose_clip(run_dir))
        pp.postprocess(INST, DATE, TARGET, RUN, 5.0, 2, 5, apply=True)
        # a second clip runs on the already-clipped files
        pp.postprocess(INST, DATE, TARGET, RUN, 3.0, 2, 5, apply=True)

        assert pp.restore_clip(INST, DATE, TARGET, RUN)["ok"]
        assert self._original(run_dir) == original

    def test_apply_is_refused_when_backup_cannot_be_taken(self, run_dir, monkeypatch):
        calls = []
        monkeypatch.setattr(pp, "_run_sync", lambda a: calls.append(a) or _ok_report())

        def boom(rdir):
            raise OSError("disk full")

        monkeypatch.setattr(pp, "_snapshot_unclipped", boom)
        res = pp.postprocess(INST, DATE, TARGET, RUN, 5.0, 2, 5, apply=True)

        assert res["ok"] is False and "back up" in res["error"]
        assert calls == []  # prose2 never ran, so nothing was overwritten

    def test_preview_takes_no_backup(self, run_dir, monkeypatch):
        monkeypatch.setattr(pp, "_run_sync", lambda a: _ok_report())
        monkeypatch.setattr(pp, "_read_preview", lambda path: None)
        res = pp.postprocess(INST, DATE, TARGET, RUN, 5.0, 2, 5, apply=False)
        assert res["restorable"] is False
        assert not (run_dir / ".clip_backup").exists()

    def test_restore_without_backup_is_an_error(self, run_dir):
        out = pp.restore_clip(INST, DATE, TARGET, RUN)
        assert out["ok"] is False and "nothing to restore" in out["error"]

    def test_restore_refused_when_files_changed_after_clip(self, run_dir, monkeypatch):
        monkeypatch.setattr(pp, "_run_sync", _fake_prose_clip(run_dir))
        pp.postprocess(INST, DATE, TARGET, RUN, 5.0, 2, 5, apply=True)
        # photometry re-run in the same run dir regenerates the CSV
        fresh = next(iter(sorted(run_dir.glob("*.csv"))))
        fresh.write_text("BJD_TDB,Flux,Err\n2460900.1,1.0,0.001\n2460900.2,1.0,0.001\n")

        out = pp.restore_clip(INST, DATE, TARGET, RUN)

        assert out["ok"] is False and "stale" in out["error"]
        assert fresh.read_text().startswith("BJD_TDB,Flux,Err\n2460900.1")

    def test_apply_after_rereduction_replaces_stale_snapshot(self, run_dir, monkeypatch):
        monkeypatch.setattr(pp, "_run_sync", _fake_prose_clip(run_dir))
        pp.postprocess(INST, DATE, TARGET, RUN, 5.0, 2, 5, apply=True)
        for p in run_dir.glob("*.csv"):
            p.write_text("BJD_TDB,Flux,Err\n2460900.1,1.0,0.001\n2460900.2,1.0,0.001\n")
        reduced = self._original(run_dir)

        pp.postprocess(INST, DATE, TARGET, RUN, 5.0, 2, 5, apply=True)

        assert pp.restore_clip(INST, DATE, TARGET, RUN)["ok"]
        assert self._original(run_dir) == reduced

    def test_failed_apply_then_rereduction_replaces_stale_snapshot(self, run_dir, monkeypatch):
        # an apply that snapshots and then fails leaves a backup with no clip manifest
        monkeypatch.setattr(pp, "_run_sync", lambda a: _ok_report())  # applied: False
        pp.postprocess(INST, DATE, TARGET, RUN, 5.0, 2, 5, apply=True)
        assert (run_dir / ".clip_backup").is_dir()
        # the run is re-reduced (v2), then clipped successfully
        for p in run_dir.glob("*.csv"):
            p.write_text("BJD_TDB,Flux,Err\n2460900.1,1.0,0.001\n2460900.2,1.0,0.001\n")
        reduced = self._original(run_dir)
        monkeypatch.setattr(pp, "_run_sync", _fake_prose_clip(run_dir))
        pp.postprocess(INST, DATE, TARGET, RUN, 5.0, 2, 5, apply=True)

        out = pp.restore_clip(INST, DATE, TARGET, RUN)

        assert out["ok"], out
        assert self._original(run_dir) == reduced  # v2, never the pre-re-reduction v1

    def _clip_then_fail(self, rdir):
        """prose2 stand-in that rewrites the CSVs and then reports failure."""
        clip = _fake_prose_clip(rdir)

        def run(args):
            clip(args)
            return {"ok": False, "error": "figure step failed"}

        return run

    def test_restore_works_after_apply_that_rewrote_files_then_failed(
        self, run_dir, monkeypatch
    ):
        original = self._original(run_dir)
        monkeypatch.setattr(pp, "_run_sync", self._clip_then_fail(run_dir))

        res = pp.postprocess(INST, DATE, TARGET, RUN, 5.0, 2, 5, apply=True)

        assert res["ok"] is False
        assert self._original(run_dir) != original  # the failed apply did rewrite them

        out = pp.restore_clip(INST, DATE, TARGET, RUN)

        assert out["ok"], out
        assert self._original(run_dir) == original

    def test_retry_after_failed_rewriting_apply_keeps_the_original_snapshot(
        self, run_dir, monkeypatch
    ):
        original = self._original(run_dir)
        monkeypatch.setattr(pp, "_run_sync", self._clip_then_fail(run_dir))
        pp.postprocess(INST, DATE, TARGET, RUN, 5.0, 2, 5, apply=True)

        monkeypatch.setattr(pp, "_run_sync", _fake_prose_clip(run_dir))
        pp.postprocess(INST, DATE, TARGET, RUN, 3.0, 2, 5, apply=True)

        assert pp.restore_clip(INST, DATE, TARGET, RUN)["ok"]
        assert self._original(run_dir) == original

    def test_restore_blocked_while_job_running(self, run_dir, monkeypatch):
        monkeypatch.setattr(pp, "_run_sync", _fake_prose_clip(run_dir))
        pp.postprocess(INST, DATE, TARGET, RUN, 5.0, 2, 5, apply=True)
        monkeypatch.setattr("muscat_db.photometry.job_status", lambda *a, **k: {"state": "running"})
        out = pp.restore_clip(INST, DATE, TARGET, RUN)
        assert out["ok"] is False and "running" in out["error"]
        assert (run_dir / ".clip_backup").is_dir()

    def test_backup_dir_is_invisible_to_band_csv_globs(self, run_dir, monkeypatch):
        monkeypatch.setattr(pp, "_run_sync", _fake_prose_clip(run_dir))
        pp.postprocess(INST, DATE, TARGET, RUN, 5.0, 2, 5, apply=True)
        top_level = {p.name for p in run_dir.glob("*.csv")}
        assert top_level == {f"{TARGET}_{INST}_gp_{DATE}.csv", f"{TARGET}_{INST}_gp_{DATE}_rp.csv"}


class TestRestoreEndpoint:
    def test_rejects_missing_run(self, mock_db):
        client = TestClient(__import__("muscat_db.web", fromlist=["app"]).app)
        r = client.post(
            "/api/photometry/postprocess/restore",
            json={"inst": INST, "date": DATE, "target": TARGET, "run": ""},
        )
        assert r.status_code == 400 and r.json()["error"] == "a run is required"

    def test_forwards_to_restore_clip(self, mock_db, monkeypatch):
        from muscat_db import web

        seen = {}
        def fake(inst, date, target, run):
            seen.update(inst=inst, date=date, target=target, run=run)
            return {"ok": True, "restored": ["a.csv"], "n_files": 1}

        monkeypatch.setattr(web.postproc, "restore_clip", fake)
        client = TestClient(web.app)
        r = client.post(
            "/api/photometry/postprocess/restore",
            json={"inst": INST, "date": DATE, "target": TARGET, "run": RUN},
        )
        assert r.status_code == 200 and r.json()["n_files"] == 1
        assert seen == {"inst": INST, "date": DATE, "target": TARGET, "run": RUN}

    def test_surfaces_restore_failure_as_400(self, mock_db, monkeypatch):
        from muscat_db import web

        monkeypatch.setattr(
            web.postproc, "restore_clip",
            lambda *a, **k: {"ok": False, "error": "no pre-clip backup for this run; nothing to restore"},
        )
        client = TestClient(web.app)
        r = client.post(
            "/api/photometry/postprocess/restore",
            json={"inst": INST, "date": DATE, "target": TARGET, "run": RUN},
        )
        assert r.status_code == 400 and r.json()["ok"] is False
