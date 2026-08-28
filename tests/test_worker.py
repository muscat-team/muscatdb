"""Tests for the standalone worker loop (architecture issue #51, step 1).

Exercises the parts that must work before a `muscatdb worker` process is
trustworthy outside the web server: pipeline-name resolution, per-pipeline
failure isolation within one pass, the `once` short-circuit, and that
SIGTERM/SIGINT actually stop the loop (and restore the prior handlers
afterward, so a test run never leaves the process's signal disposition
mutated for whatever runs next).
"""

from __future__ import annotations

import os
import signal

import pytest
from typer.testing import CliRunner

from muscat_db import cli, worker


class TestResolvePipelines:
    def test_all_returns_every_pipeline_in_a_stable_order(self):
        names = [n for n, _ in worker.resolve_pipelines("all")]
        assert names == ["photometry", "transit_fit", "ttv_fit"]

    def test_single_name(self):
        names = [n for n, _ in worker.resolve_pipelines("transit_fit")]
        assert names == ["transit_fit"]

    def test_comma_separated_names_with_whitespace(self):
        names = [n for n, _ in worker.resolve_pipelines("transit_fit, ttv_fit")]
        assert names == ["transit_fit", "ttv_fit"]

    def test_unknown_pipeline_raises_value_error(self):
        with pytest.raises(ValueError, match="unknown pipeline"):
            worker.resolve_pipelines("not_a_pipeline")

    def test_empty_selection_raises_value_error(self):
        with pytest.raises(ValueError, match="must name at least one"):
            worker.resolve_pipelines("")

    def test_resolved_functions_are_the_real_sync_jobs(self):
        from muscat_db import photometry

        fns = dict(worker.resolve_pipelines("photometry"))
        assert fns["photometry"] is photometry.sync_jobs


class TestRunPass:
    def test_calls_every_function(self):
        calls: list[str] = []
        fns = [("a", lambda: calls.append("a")), ("b", lambda: calls.append("b"))]
        worker.run_pass(fns)
        assert calls == ["a", "b"]

    def test_one_failure_does_not_stop_the_others(self):
        calls: list[str] = []

        def boom():
            raise RuntimeError("simulated pipeline failure")

        fns = [("bad", boom), ("good", lambda: calls.append("good"))]
        worker.run_pass(fns)  # must not raise
        assert calls == ["good"]


class TestLoop:
    def test_once_runs_a_single_pass_regardless_of_stop_requested(self):
        calls: list[int] = []
        worker._loop(
            [("x", lambda: calls.append(1))],
            interval=0,
            once=True,
            stop_requested=lambda: False,
        )
        assert calls == [1]

    def test_stops_after_the_pass_where_stop_becomes_true(self, monkeypatch):
        calls: list[int] = []
        monkeypatch.setattr(worker.time, "sleep", lambda s: None)
        flags = iter([False, False, True])
        worker._loop(
            [("x", lambda: calls.append(1))],
            interval=0,
            once=False,
            stop_requested=lambda: next(flags),
        )
        assert len(calls) == 3


class TestRun:
    def test_once_calls_each_selected_pipeline_exactly_once(self, monkeypatch):
        counts = {"photometry": 0, "transit_fit": 0, "ttv_fit": 0}
        for name in counts:
            monkeypatch.setattr(
                f"muscat_db.{name}.sync_jobs",
                lambda name=name: counts.__setitem__(name, counts[name] + 1),
            )
        worker.run("all", once=True)
        assert counts == {"photometry": 1, "transit_fit": 1, "ttv_fit": 1}

    def test_once_does_not_install_signal_handlers(self, monkeypatch):
        installed = []
        monkeypatch.setattr(worker.signal, "signal", lambda *a: installed.append(a))
        monkeypatch.setattr("muscat_db.photometry.sync_jobs", lambda: None)
        worker.run("photometry", once=True)
        assert installed == []

    def test_sigterm_stops_the_loop_and_restores_prior_handler(self, monkeypatch):
        prior = signal.getsignal(signal.SIGTERM)
        calls: list[int] = []

        def stub_sync_jobs() -> None:
            calls.append(1)
            os.kill(os.getpid(), signal.SIGTERM)

        monkeypatch.setattr("muscat_db.photometry.sync_jobs", stub_sync_jobs)
        try:
            worker.run("photometry", interval=0.01, once=False)
        finally:
            assert signal.getsignal(signal.SIGTERM) == prior
        assert len(calls) == 1

    def test_unknown_pipeline_raises_before_touching_signals(self, monkeypatch):
        installed = []
        monkeypatch.setattr(worker.signal, "signal", lambda *a: installed.append(a))
        with pytest.raises(ValueError):
            worker.run("nope", once=False)
        assert installed == []


def test_cli_worker_once_smoke(tmp_path, monkeypatch):
    monkeypatch.setattr("muscat_db.photometry.sync_jobs", lambda: None)
    monkeypatch.setattr("muscat_db.transit_fit.sync_jobs", lambda: None)
    monkeypatch.setattr("muscat_db.ttv_fit.sync_jobs", lambda: None)

    result = CliRunner().invoke(
        cli.app,
        ["worker", "--pipeline", "all", "--once", "--db", str(tmp_path / "muscat.db")],
    )

    assert result.exit_code == 0, result.output
    assert "worker started" in result.output
    assert "photometry" in result.output and "transit_fit" in result.output


def test_cli_worker_unknown_pipeline_exits_nonzero(tmp_path):
    result = CliRunner().invoke(
        cli.app,
        ["worker", "--pipeline", "nope", "--once", "--db", str(tmp_path / "muscat.db")],
    )
    assert result.exit_code != 0
    assert "unknown pipeline" in result.output
