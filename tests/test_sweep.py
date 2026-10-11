"""Periodic backfill sweep (issue #196, part 2).

``scan-yesterday`` looks at each date once, so a date that fails is never
retried. ``sweep`` rescans every date ``scan-missing`` would pick up, then
retries open scan failures -- while staying out of the way of photometry
jobs, never touching dates where a rescan would destroy rows (#197, #198),
and not rescanning, week after week, dates whose raw files have not changed.
"""

from __future__ import annotations

import fcntl
import os

import pytest

from muscat_db import scan_failures, scanner, sweep
from muscat_db.instruments import INSTRUMENTS


class Env:
    def __init__(self, tmp_path, monkeypatch):
        self.data = tmp_path / "data"
        self.obslog = tmp_path / "obslog"
        self.obslog.mkdir()
        monkeypatch.setenv("MUSCAT_DATA_DIR", str(self.data))
        monkeypatch.setattr(scanner, "OBSLOG_BASE", str(self.obslog))
        monkeypatch.setattr(sweep, "OBSLOG_BASE", str(self.obslog))
        monkeypatch.setattr(sweep, "_active_jobs", lambda: [])
        self.missing: dict[str, list[str]] = {}
        self.rescans: list[tuple[str, str, int | None]] = []
        monkeypatch.setattr(sweep, "missing_dates", lambda inst, year, force=False: list(self.missing.get(inst, [])))
        monkeypatch.setattr(sweep, "scan_date", self._scan_date)

    def _scan_date(self, inst, obsdate, max_workers=None, progress=None, data_root=None):
        self.rescans.append((inst, obsdate, max_workers))
        scan_failures.clear(str(self.obslog), inst, obsdate)
        return {"total": 1, "per_ccd": {0: 1}}

    def raw(self, inst: str, obsdate: str, names: list[str]) -> None:
        d = self.data / INSTRUMENTS[inst].data_subdir / obsdate
        d.mkdir(parents=True, exist_ok=True)
        for n in names:
            (d / n).write_bytes(b"")

    def csv(self, inst: str, obsdate: str, ccd: int, rows: int) -> None:
        d = self.obslog / inst / obsdate
        d.mkdir(parents=True, exist_ok=True)
        lines = ["FRAME,OBJECT"] + [f"F{i},TOI-1" for i in range(rows)]
        (d / f"obslog-{inst}-{obsdate}-ccd{ccd}.csv").write_text("\n".join(lines) + "\n")

    def rescanned(self) -> list[tuple[str, str]]:
        return [(i, d) for i, d, _ in self.rescans]


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


# -- what gets rescanned ------------------------------------------------------


def test_rescans_missing_dates_of_every_instrument_with_capped_workers(env):
    env.raw("muscat", "250101", ["MSCT0_2501010001.fits"])
    env.raw("qhy600", "250102", ["coj0m416-sq36-20250102-0001-e91.fits"])
    env.missing = {"muscat": ["250101"], "qhy600": ["250102"]}

    result = sweep.run_sweep(max_workers=8)

    assert env.rescanned() == [("muscat", "250101"), ("qhy600", "250102")]
    assert {w for *_, w in env.rescans} == {8}
    assert result.scanned == {"muscat": ["250101"], "qhy600": ["250102"]}


def test_default_worker_cap_leaves_most_of_the_host_free(env):
    env.raw("muscat", "250101", ["MSCT0_2501010001.fits"])
    env.missing = {"muscat": ["250101"]}

    sweep.run_sweep()

    assert {w for *_, w in env.rescans} == {sweep.DEFAULT_WORKERS}
    assert sweep.DEFAULT_WORKERS <= 24 // 2


def test_an_unchanged_raw_directory_is_not_rescanned_next_week(env):
    """Unreadable headers keep a date "incomplete" however often it is
    rescanned; only a change on disk can make another rescan worth doing."""
    env.raw("muscat", "250101", ["MSCT0_2501010001.fits"])
    env.missing = {"muscat": ["250101"]}

    sweep.run_sweep()
    result = sweep.run_sweep()

    assert env.rescanned() == [("muscat", "250101")]
    assert result.unchanged == 1

    raw_dir = env.data / "MuSCAT" / "250101"
    (raw_dir / "MSCT0_2501010002.fits").write_bytes(b"")  # a late delivery
    st = raw_dir.stat()
    os.utime(raw_dir, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))  # coarse-mtime filesystems

    sweep.run_sweep()

    assert env.rescanned() == [("muscat", "250101"), ("muscat", "250101")]


def test_a_rescan_that_would_shrink_existing_rows_is_held(env):
    """The #198 safeguard, for any date: fewer raw matches than CSV rows means
    a rescan would overwrite good rows with fewer or none."""
    env.raw("muscat", "250101", ["MSCT0_2501010001.fits"])  # 1 match left on disk
    env.csv("muscat", "250101", 0, rows=40)
    env.missing = {"muscat": ["250101"]}

    result = sweep.run_sweep()

    assert env.rescans == []
    ((inst, obsdate, why),) = result.held
    assert (inst, obsdate) == ("muscat", "250101") and "ccd0" in why and "40" in why


def test_rows_may_grow_but_never_shrink(env):
    env.raw("muscat", "250101", [f"MSCT0_25010100{i}.fits" for i in range(5)])
    env.csv("muscat", "250101", 0, rows=3)
    env.missing = {"muscat": ["250101"]}

    sweep.run_sweep()

    assert env.rescanned() == [("muscat", "250101")]


def test_a_scan_that_matches_nothing_is_not_reported_or_counted_as_changed(env, monkeypatch):
    """A zero-match scan writes no CSVs, so it must not read as a successful
    rescan or make the CLI rebuild, but its signature is still remembered so
    later sweeps skip it until the directory changes."""
    env.raw("muscat", "250101", ["MSCT0_2501010001.fits"])
    env.missing = {"muscat": ["250101"]}
    monkeypatch.setattr(
        sweep, "scan_date",
        lambda inst, obsdate, max_workers=None, progress=None, data_root=None: {},
    )

    result = sweep.run_sweep()

    assert result.scanned == {}
    assert result.changed is False
    assert result.unchanged == 0
    assert sweep.run_sweep().unchanged == 1


def test_known_destructive_dates_are_held_from_both_steps(env):
    env.raw("muscat3", "250722", ["ogg2m001-ep02-20250722-0001-e91.fits.fz"])
    env.missing = {"muscat3": ["250722"]}
    scan_failures.record(str(env.obslog), "muscat3", "210110", "OSError: boom")

    result = sweep.run_sweep()

    assert env.rescans == []
    assert sorted(result.held) == [("muscat3", "210110", "#197"), ("muscat3", "250722", "#198")]
    assert [(e["instrument"], e["obsdate"]) for e in scan_failures.pending(str(env.obslog))] == [
        ("muscat3", "210110")]


def test_the_213_duplicate_dates_are_held_without_any_csv(env):
    """Regression for the review (#213): after the 2026-10-08 cleanup the
    duplicate raw copies have no obslog CSVs left, so the shrink guard — which
    only compares against existing rows — cannot protect them. Only the hold
    list can, so a first sweep must not rescan them."""
    for obsdate in ("260729", "260727", "250704", "260716", "260723"):
        env.raw("muscat3", obsdate, [f"ogg2m001-ep02-2026{obsdate[-4:]}-0001-e91.fits"])
    env.raw("sinistro", "260722", ["coj0m416-01-20260721-0001-e91.fits"])
    env.missing = {
        "muscat3": ["260729", "260727", "250704", "260716", "260723"],
        "sinistro": ["260722"],
    }

    result = sweep.run_sweep()

    assert env.rescans == []
    held = {f"{i} {d}": why for i, d, why in result.held}
    assert held == {
        "muscat3 250704": "#213", "muscat3 260716": "#213",
        "muscat3 260723": "#213", "muscat3 260727": "#213",
        "muscat3 260729": "#213", "sinistro 260722": "#213",
    }


# -- retrying the failure ledger ------------------------------------------------


def test_retries_open_scan_failures_and_clears_them(env):
    scan_failures.record(str(env.obslog), "muscat2", "250310", "OSError: boom")

    result = sweep.run_sweep()

    assert env.rescanned() == [("muscat2", "250310")]
    assert result.retried == [("muscat2", "250310")]
    assert scan_failures.pending(str(env.obslog)) == []


def test_a_failing_retry_does_not_stop_the_others(env, monkeypatch):
    scan_failures.record(str(env.obslog), "muscat", "250101", "x")
    scan_failures.record(str(env.obslog), "muscat2", "250102", "x")
    done: list[str] = []

    def flaky(inst, obsdate, **kw):
        if inst == "muscat":
            raise OSError("still broken")
        done.append(obsdate)
        return {"total": 1}

    monkeypatch.setattr(sweep, "scan_date", flaky)

    result = sweep.run_sweep()

    assert done == ["250102"]
    assert result.retried == [("muscat2", "250102")]
    assert result.failed == [("muscat", "250101")]


# -- staying out of the way ------------------------------------------------------


def test_skips_entirely_while_photometry_jobs_are_active(env, monkeypatch):
    env.raw("muscat", "250101", ["MSCT0_2501010001.fits"])
    env.missing = {"muscat": ["250101"]}
    scan_failures.record(str(env.obslog), "muscat2", "250310", "x")
    monkeypatch.setattr(sweep, "_active_jobs", lambda: [{"key": "phot-1", "state": "running"}])

    result = sweep.run_sweep()

    assert env.rescans == []
    assert "active" in result.skipped


def test_an_unreadable_job_store_counts_as_busy(env, monkeypatch):
    def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(sweep, "_active_jobs", boom)

    result = sweep.run_sweep()

    assert "could not check" in result.skipped


def test_a_second_sweep_does_not_run_alongside_the_first(env):
    env.missing = {"muscat": ["250101"]}
    with open(env.obslog / sweep.LOCK_NAME, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

        result = sweep.run_sweep()

    assert env.rescans == []
    assert "already running" in result.skipped


# -- CLI ----------------------------------------------------------------------


def _invoke(*args):
    from typer.testing import CliRunner

    from muscat_db.cli import app

    return CliRunner().invoke(app, [*args], env={"NO_COLOR": "1", "COLUMNS": "200"})


@pytest.fixture
def builds(monkeypatch, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr("muscat_db.database.build_db", lambda db, progress=None: calls.append(db) or 0)
    db = tmp_path / "muscat.db"
    db.write_bytes(b"")
    return calls, str(db)


def test_cli_build_db_runs_only_when_the_sweep_rescanned_something(env, builds):
    calls, db = builds

    r = _invoke("sweep", "--build-db", "--db", db)
    assert r.exit_code == 0, r.output
    assert calls == []  # nothing rescanned: no rebuild

    env.raw("muscat", "250101", ["MSCT0_2501010001.fits"])
    env.missing = {"muscat": ["250101"]}
    r = _invoke("sweep", "--build-db", "--db", db)
    assert r.exit_code == 0, r.output
    assert calls == [db]
    assert "muscat" in r.output and "250101" in r.output


def test_cli_skipped_sweep_never_rebuilds(env, monkeypatch, builds):
    calls, db = builds
    env.raw("muscat", "250101", ["MSCT0_2501010001.fits"])
    env.missing = {"muscat": ["250101"]}
    monkeypatch.setattr(sweep, "_active_jobs", lambda: [{"key": "fit-1", "state": "pending"}])

    r = _invoke("sweep", "--build-db", "--db", db)

    assert r.exit_code == 0, r.output
    assert calls == []
    assert "skipped" in r.output.lower()
