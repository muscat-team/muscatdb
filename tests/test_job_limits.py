"""Per-user caps on running and queued jobs (issue #265).

The store-level behaviour (atomic claim and enqueue, on both backends) lives in
tests/_job_store_contract.py; this file covers how the caps are resolved, set
from the CLI, handed to the job store by the pipelines, and reported over HTTP.
"""

from __future__ import annotations

import pathlib

import pytest
from typer.testing import CliRunner

from muscat_db import job_limits, photometry, transit_fit, ttv_fit, web
from muscat_db.cli import app as cli_app
from muscat_db.job_limits import JobLimitExceeded, UserLimits


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "muscat.db"
    monkeypatch.setenv("MUSCAT_DB_PATH", str(path))
    monkeypatch.delenv("MUSCAT_USER_MAX_RUNNING", raising=False)
    monkeypatch.delenv("MUSCAT_USER_MAX_QUEUED", raising=False)
    return str(path)


# --- resolution ----------------------------------------------------------


def test_site_defaults_are_one_running_and_five_queued(db):
    assert job_limits.site_defaults() == UserLimits(running=1, queued=5)


def test_site_defaults_come_from_the_environment(db, monkeypatch):
    monkeypatch.setenv("MUSCAT_USER_MAX_RUNNING", "3")
    monkeypatch.setenv("MUSCAT_USER_MAX_QUEUED", "0")
    assert job_limits.site_defaults() == UserLimits(running=3, queued=0)


def test_a_malformed_environment_value_falls_back_to_the_default(db, monkeypatch):
    monkeypatch.setenv("MUSCAT_USER_MAX_RUNNING", "lots")
    assert job_limits.site_defaults().running == 1


def test_unknown_user_gets_the_site_defaults(db):
    assert job_limits.limits_for("nobody") == UserLimits(running=1, queued=5)


def test_a_job_with_no_user_is_not_capped(db):
    assert job_limits.limits_for(None) is None
    assert job_limits.limits_for("") is None
    assert job_limits.claim_caps(None) == {"user_name": None, "max_user_slots": None}
    assert job_limits.enqueue_caps("") == {"max_queued": None}


def test_per_user_override_beats_the_default_and_null_means_default(db):
    job_limits.set_limits(db, "ann", running=4)
    assert job_limits.limits_for("ann") == UserLimits(running=4, queued=5)
    job_limits.set_limits(db, "ann", queued=9)
    assert job_limits.limits_for("ann") == UserLimits(running=4, queued=9)


def test_zero_override_means_unlimited_not_default(db):
    job_limits.set_limits(db, "ann", running=0, queued=0)
    assert job_limits.limits_for("ann") == UserLimits(running=0, queued=0)


def test_reset_clears_the_overrides(db):
    job_limits.set_limits(db, "ann", running=4, queued=9)
    assert job_limits.set_limits(db, "ann", reset=True) == UserLimits(running=1, queued=5)


def test_negative_limit_is_rejected(db):
    with pytest.raises(ValueError):
        job_limits.set_limits(db, "ann", running=-1)


def test_admins_get_the_default_too(db):
    from muscat_db import database

    with database.get_conn(db) as conn:
        database._apply_schema(conn)
        conn.execute("INSERT INTO users (username, display_name, is_admin) VALUES ('root', 'root', 1)")
        conn.commit()
    assert job_limits.limits_for("root") == UserLimits(running=1, queued=5)


# --- CLI -----------------------------------------------------------------


def test_cli_sets_shows_and_resets_limits(db):
    runner = CliRunner()
    result = runner.invoke(cli_app, ["access", "limit", "ann", "--running", "2", "--queued", "0"])
    assert result.exit_code == 0, result.output
    assert "running 2, queued unlimited" in result.output

    shown = runner.invoke(cli_app, ["access", "limit", "ann"])
    assert "running 2, queued unlimited" in shown.output

    reset = runner.invoke(cli_app, ["access", "limit", "ann", "--reset"])
    assert "running 1, queued 5" in reset.output


def test_cli_rejects_a_negative_limit(db):
    result = CliRunner().invoke(cli_app, ["access", "limit", "ann", "--running", "-1"])
    assert result.exit_code != 0


# --- pipelines hand the caps to the store --------------------------------


class _RecordingStore:
    def __init__(self, *, claim, enqueue_error=None):
        self._claim = claim
        self._enqueue_error = enqueue_error
        self.claims: list[dict] = []
        self.enqueues: list[dict] = []

    def claim_slot(self, pipeline, holder_key, max_slots, **kwargs):
        self.claims.append(kwargs)
        return self._claim

    def enqueue(self, **kwargs):
        self.enqueues.append(kwargs)
        if self._enqueue_error:
            raise self._enqueue_error

    def release_slot(self, *args):
        pass


def _ttv_launch(monkeypatch, store, user):
    monkeypatch.setattr(ttv_fit, "get_job_store", lambda: store)
    monkeypatch.setattr(ttv_fit, "_MAX_FULL_JOBS", 1)
    monkeypatch.setattr(ttv_fit, "validate_ttv_options", lambda options: None)
    return ttv_fit.start_ttv_fit("TOI-123", options={"run_name": "r1"}, user_name=user)


def _photometry_launch(monkeypatch, store, user, tmp_path):
    monkeypatch.setattr(photometry, "get_job_store", lambda: store)
    monkeypatch.setattr(photometry, "_MAX_FULL_JOBS", 1)
    monkeypatch.setattr(photometry, "_JOBS", {})
    monkeypatch.setattr(photometry, "raw_data_dir", lambda inst, date: tmp_path)
    monkeypatch.setattr(photometry, "run_output_dir", lambda *a, **k: tmp_path / "run")
    monkeypatch.setattr(photometry, "_detect_process_running", lambda rdir: False)
    monkeypatch.setattr(photometry, "_reduction_products_exist", lambda *a, **k: False)
    return photometry.start_run("muscat3", "260720", "TOI-123", options={}, test_run=False, user_name=user)


def _transit_fit_launch(monkeypatch, store, user, tmp_path):
    monkeypatch.setattr(transit_fit, "get_job_store", lambda: store)
    monkeypatch.setattr(transit_fit, "_MAX_FULL_JOBS", 1)
    monkeypatch.setattr(transit_fit, "_FIT_JOBS", {})
    monkeypatch.setattr(transit_fit, "validate_fit_options", lambda options: None)
    monkeypatch.setattr(
        transit_fit, "get_csv_lightcurves",
        lambda *a, **k: [pathlib.Path("TOI-123_muscat3_g.csv")],
    )
    monkeypatch.setattr(transit_fit, "validate_no_duplicate_datasets", lambda *a, **k: None)
    monkeypatch.setattr(transit_fit, "fit_output_dir", lambda *a, **k: tmp_path / "run")
    monkeypatch.setattr(transit_fit, "_fit_reduction_exists", lambda *a, **k: False)
    return transit_fit.start_fit(
        "muscat3", "260720", "TOI-123", options={"run_mode": "new"}, test_run=False, user_name=user,
    )


@pytest.mark.parametrize(
    "launch",
    [
        pytest.param(_photometry_launch, id="photometry"),
        pytest.param(_transit_fit_launch, id="transit_fit"),
        pytest.param(lambda mp, store, user, _tmp: _ttv_launch(mp, store, user), id="ttv_fit"),
    ],
)
def test_launch_passes_the_users_caps_to_claim_and_enqueue(launch, db, monkeypatch, tmp_path):
    job_limits.set_limits(db, "ann", running=2, queued=3)
    store = _RecordingStore(claim=False)

    result = launch(monkeypatch, store, "ann", tmp_path)

    assert result["queued"] is True
    assert store.claims == [{"user_name": "ann", "max_user_slots": 2}]
    assert store.enqueues[0]["max_queued"] == 3


def test_a_refused_enqueue_is_reported_as_a_limit_not_a_database_error(db, monkeypatch):
    store = _RecordingStore(claim=False, enqueue_error=JobLimitExceeded("ann already has 5 queued job(s)"))

    result = _ttv_launch(monkeypatch, store, "ann")

    assert result["ok"] is False
    assert result["limit_exceeded"] is True
    assert "5 queued" in result["error"]


# --- HTTP ----------------------------------------------------------------


def test_limit_refusal_is_http_429_and_other_refusals_stay_400():
    refused = web._launch_response(job_limits.limit_refusal(JobLimitExceeded("too many")))
    assert refused.status_code == 429
    assert b"too many" in refused.body

    assert web._launch_response({"ok": False, "error": "bad"}).status_code == 400
    assert web._launch_response({"ok": True}).status_code == 200
