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
import threading

import pytest
from typer.testing import CliRunner

from muscat_db import cli, job_store, worker


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
        # _loop's wait goes through job_store.wait_for_work_or_sleep, which
        # (with MUSCAT_JOB_NOTIFY unset, the default here) calls
        # job_store.time.sleep -- patch it there, not on worker itself,
        # which no longer imports time at all.
        monkeypatch.setattr(job_store.time, "sleep", lambda s: None)
        flags = iter([False, False, True])
        worker._loop(
            [("x", lambda: calls.append(1))],
            interval=0,
            once=False,
            stop_requested=lambda: next(flags),
        )
        assert len(calls) == 3

    def test_waits_through_the_job_store_helper_not_a_blind_sleep(self, monkeypatch):
        """_loop's between-pass wait must go through
        job_store.wait_for_work_or_sleep (instant dispatch, architecture
        issue #51) rather than a bare time.sleep -- that helper is what
        applies the MUSCAT_JOB_NOTIFY gate and the never-return-early-
        without-a-signal floor."""
        waits: list[float] = []
        monkeypatch.setattr(
            job_store, "wait_for_work_or_sleep", lambda t: waits.append(t) or False,
        )
        flags = iter([False, False, True])
        worker._loop(
            [("x", lambda: None)],
            interval=1.5,
            once=False,
            stop_requested=lambda: next(flags),
        )
        assert waits == [1.5, 1.5]  # once per non-final pass, never after the last

    def test_does_not_wait_when_once(self, monkeypatch):
        waits: list[float] = []
        monkeypatch.setattr(
            job_store, "wait_for_work_or_sleep", lambda t: waits.append(t) or False,
        )
        worker._loop(
            [("x", lambda: None)],
            interval=1.5,
            once=True,
            stop_requested=lambda: False,
        )
        assert waits == []


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

    def test_run_restores_the_previous_owner_after_returning_once(self, monkeypatch):
        """run() tags every row it launches owner="worker" (job_store.py's
        _OWNER docstring), but that's process-role state, not something a
        single run() call should own permanently: leaving it stuck as
        "worker" after returning bit tests/test_worker_p2_proof.py enough
        that its own fixture snapshots/restores _OWNER around worker.run()
        calls as a workaround. Fix it at the source instead -- restore
        whatever _OWNER was before this call, not hardcoded back to "web",
        so this isn't just a reset-to-default in disguise. In production
        run() never returns except at process shutdown, so this changes no
        real behavior; it only closes the cross-test leak, which now matters
        for real since database.ingest_date's owner guard (architecture
        issue #51) makes _OWNER load-bearing for something other than
        orphan reconciliation."""
        from muscat_db import job_store

        monkeypatch.setattr(job_store, "_OWNER", "pre-existing-owner")
        monkeypatch.setattr("muscat_db.photometry.sync_jobs", lambda: None)
        worker.run("photometry", once=True)
        assert job_store.current_owner() == "pre-existing-owner"

    def test_run_restores_the_previous_owner_after_stopping(self, monkeypatch):
        from muscat_db import job_store

        monkeypatch.setattr(job_store, "_OWNER", "pre-existing-owner")

        def stub_sync_jobs() -> None:
            os.kill(os.getpid(), signal.SIGTERM)

        monkeypatch.setattr("muscat_db.photometry.sync_jobs", stub_sync_jobs)
        worker.run("photometry", interval=0.01, once=False)
        assert job_store.current_owner() == "pre-existing-owner"

    def test_run_restores_owner_even_when_signal_installation_fails(self, monkeypatch):
        """set_owner("worker") and signal installation must be one unit
        fully covered by the same try/finally: signal.signal() raises
        ValueError when called from anything but the main thread of the
        main interpreter, and if that happens *before* the try block
        starts, the finally that restores _OWNER never runs, leaking
        "worker" for the rest of the process. Exercised for real via a
        background thread (the actual condition that triggers the
        failure), not by mocking signal.signal."""
        from muscat_db import job_store

        monkeypatch.setattr(job_store, "_OWNER", "pre-existing-owner")
        monkeypatch.setattr("muscat_db.photometry.sync_jobs", lambda: None)

        errors: list[BaseException] = []

        def call_from_thread() -> None:
            try:
                worker.run("photometry", once=False)
            except BaseException as exc:
                errors.append(exc)

        t = threading.Thread(target=call_from_thread)
        t.start()
        t.join(timeout=5)

        assert not t.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], ValueError)
        assert job_store.current_owner() == "pre-existing-owner"

    def test_ingest_date_succeeds_after_a_worker_run_returns(self, monkeypatch, tmp_path):
        """The actual consequence proven end-to-end: without the owner
        restoration above, a worker.run() call anywhere earlier in the same
        process would permanently poison database.ingest_date's new
        owner-based guard (architecture issue #51) for the rest of the
        process's life, even for the unrelated web/CLI role that should
        always be allowed to ingest.

        Points OBSLOG_BASE at an empty temp dir (rather than pulling in
        test_main.py's tmp_obslog fixture) so "no CSVs found" is guaranteed
        rather than risking a real obslog tree on a dev host actually having
        data for muscat/260101; ingest_date applies its own schema
        (_apply_schema) to a fresh sqlite3.connect, so no prior build_db()
        call is needed either."""
        from muscat_db import database, job_store

        monkeypatch.setattr(database, "OBSLOG_BASE", str(tmp_path / "obslog"))
        monkeypatch.setattr(job_store, "_OWNER", "web")
        monkeypatch.setattr("muscat_db.photometry.sync_jobs", lambda: None)
        worker.run("photometry", once=True)

        db_path = str(tmp_path / "muscat.db")
        with pytest.raises(FileNotFoundError, match="No obslog CSVs found"):
            # Reaches the (expected, unrelated) "no CSVs" error rather than
            # the worker-owner RuntimeError -- proof the guard sees owner
            # "web" again, not a leaked "worker".
            database.ingest_date(db_path, "muscat", "260101")

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


def test_cli_worker_banner_reports_notify_state(tmp_path, monkeypatch):
    """The startup banner surfaces whether instant dispatch (architecture
    issue #51, MUSCAT_JOB_NOTIFY) is actually live, so an operator can tell
    from the log alone rather than having to know the env var was set."""
    monkeypatch.setattr("muscat_db.photometry.sync_jobs", lambda: None)
    monkeypatch.setattr("muscat_db.transit_fit.sync_jobs", lambda: None)
    monkeypatch.setattr("muscat_db.ttv_fit.sync_jobs", lambda: None)

    result_off = CliRunner().invoke(
        cli.app,
        ["worker", "--pipeline", "all", "--once", "--db", str(tmp_path / "muscat.db")],
    )
    assert result_off.exit_code == 0, result_off.output
    assert "notify=off" in result_off.output

    monkeypatch.setattr(job_store, "_NOTIFY_ENABLED", True)
    result_on = CliRunner().invoke(
        cli.app,
        ["worker", "--pipeline", "all", "--once", "--db", str(tmp_path / "muscat.db")],
    )
    assert result_on.exit_code == 0, result_on.output
    assert "notify=on" in result_on.output


def test_cli_worker_unknown_pipeline_exits_nonzero(tmp_path):
    result = CliRunner().invoke(
        cli.app,
        ["worker", "--pipeline", "nope", "--once", "--db", str(tmp_path / "muscat.db")],
    )
    assert result.exit_code != 0
    assert "unknown pipeline" in result.output
