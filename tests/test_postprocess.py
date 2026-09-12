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
        apply_cmd = pp._command(ctx, sigma=3.5, degree=2, iterations=5, apply=True)
        assert "--apply" in apply_cmd
        assert "--target" in apply_cmd and "--inst" in apply_cmd and "--date" in apply_cmd
        assert "--site" in apply_cmd and "--confmode" in apply_cmd and "--telescope" in apply_cmd
        assert "--preview" not in apply_cmd


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
        def fake_postprocess(inst, date, target, run, sigma, degree, iterations, *, apply):
            calls.update(inst=inst, date=date, target=target, run=run,
                         sigma=sigma, degree=degree, iterations=iterations, apply=apply)
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
                  "sigma": 4.0, "degree": 1, "iterations": 3, "apply": False},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] and body["preview_png"].startswith("data:image/png")
        assert calls == {
            "inst": INST, "date": DATE, "target": TARGET, "run": RUN,
            "sigma": 4.0, "degree": 1, "iterations": 3, "apply": False,
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