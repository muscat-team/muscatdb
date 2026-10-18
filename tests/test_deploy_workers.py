"""deploy/workers.py: the worker-fleet config, unit rendering and up/down logic.

The ssh and systemd calls go through ``Runner.run``; the tests replace it with a
recorder so nothing leaves the machine.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "deploy" / "workers.py"
_spec = importlib.util.spec_from_file_location("deploy_workers", _PATH)
w = importlib.util.module_from_spec(_spec)
sys.modules["deploy_workers"] = w
_spec.loader.exec_module(w)


def _data(**over):
    base = {
        "defaults": {"pg_host": "10.0.0.1", "database": "muscatdb_dev", "repo": "/srv/muscat"},
        "web": {"queue_only": True},
        "hosts": {
            "ut3": {"ssh": "muscat-ut3", "pipelines": ["transit_fit", "photometry"]},
            "ut5": {"pipelines": ["ttv_fit"], "max_slots": 2},
        },
    }
    base.update(over)
    return base


# -- config -------------------------------------------------------------------


def test_parse_config_applies_defaults_and_overrides():
    cfg = w.parse_config(_data())
    assert cfg.hosts["ut3"].ssh == "muscat-ut3"
    assert cfg.hosts["ut5"].ssh == "ut5"  # defaults to the host key
    assert (cfg.hosts["ut3"].max_slots, cfg.hosts["ut5"].max_slots) == (1, 2)
    assert cfg.hosts["ut3"].job_threads == 8
    assert cfg.queue_only_web is True


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["hosts"]["ut3"].update(pipelines=["nope"]), "unknown pipeline"),
        (lambda d: d["hosts"]["ut3"].update(pipelines=[]), "at least one pipeline"),
        (lambda d: d["hosts"]["ut3"].update(pipelines=["ttv_fit", "ttv_fit"]), "duplicate"),
        (lambda d: d["hosts"]["ut3"].update(max_slots=-1), "max_slots"),
        (lambda d: d["hosts"]["ut3"].update(job_threads=0), "job_threads"),
        (lambda d: d["defaults"].update(max_full_jobs=0), "max_full_jobs"),
        (lambda d: d["defaults"].pop("database"), "database"),
        (lambda d: d["hosts"].update({"ut-9": {"pipelines": ["ttv_fit"]}}), "must not contain '-'"),
    ],
)
def test_parse_config_rejects_bad_input(mutate, message):
    data = _data()
    mutate(data)
    with pytest.raises(w.ConfigError, match=message):
        w.parse_config(data)


def test_shipped_config_parses():
    cfg = w.load_config(w.DEFAULT_CONFIG)
    assert cfg.hosts, "deploy/workers.toml must define at least one host"


# -- profiles -----------------------------------------------------------------


def _profiled(**over):
    data = _data()
    data["defaults"]["profile"] = "dev"
    data["profiles"] = {
        "dev": {},
        "prod": {"database": "muscatdb_prod", "repo": "/srv/deploy/main", "queue_only": False},
    }
    data.update(over)
    return data


def test_profile_overrides_defaults_as_one_unit():
    cfg = w.parse_config(_profiled(), profile="prod")
    assert (cfg.profile, cfg.database, cfg.repo) == ("prod", "muscatdb_prod", Path("/srv/deploy/main"))
    assert cfg.queue_only_web is False
    assert cfg.pg_host == "10.0.0.1"  # not overridden: inherited from [defaults]


def test_default_profile_applies_when_none_is_selected():
    cfg = w.parse_config(_profiled())
    assert (cfg.profile, cfg.database, cfg.repo) == ("dev", "muscatdb_dev", Path("/srv/muscat"))
    assert cfg.queue_only_web is True


def test_no_profiles_keeps_the_flat_config_working():
    cfg = w.parse_config(_data())
    assert cfg.profile is None


@pytest.mark.parametrize(
    ("profile", "mutate", "message"),
    [
        ("staging", lambda d: None, "unknown profile 'staging'"),
        (None, lambda d: d["defaults"].update(profile="nope"), "unknown profile 'nope'"),
        ("prod", lambda d: d["profiles"]["prod"].update(databse="x"), "unknown key"),
        ("prod", lambda d: d["profiles"]["prod"].update(max_full_jobs=0), "max_full_jobs"),
    ],
)
def test_profile_rejects_bad_selection_or_keys(profile, mutate, message):
    data = _profiled()
    mutate(data)
    with pytest.raises(w.ConfigError, match=message):
        w.parse_config(data, profile=profile)


def test_profile_without_any_profiles_table_is_an_error():
    with pytest.raises(w.ConfigError, match="no \\[profiles"):
        w.parse_config(_data(), profile="prod")


def test_profile_switch_changes_the_worker_env_so_up_restarts_units():
    dev = w.parse_config(_profiled(), profile="dev")
    prod = w.parse_config(_profiled(), profile="prod")
    a = w.render_env(dev, dev.hosts["ut3"], "muscat-ut3")
    b = w.render_env(prod, prod.hosts["ut3"], "muscat-ut3")
    assert "MUSCAT_WORKER_PG_DATABASE=muscatdb_prod\n" in b
    assert "MUSCAT_WORKER_PROFILE=prod\n" in b and "MUSCAT_WORKER_PROFILE=dev\n" in a
    assert a != b


def test_shipped_prod_profile_pairs_control_database_with_the_deploy_checkout():
    dev = w.load_config(w.DEFAULT_CONFIG, profile="dev")
    prod = w.load_config(w.DEFAULT_CONFIG, profile="prod")
    assert dev.database == "muscatdb_dev"
    assert prod.database == "muscatdb_prod"
    assert prod.repo.parts[-3:] == ("deploy", "main", "app")
    assert prod.repo != dev.repo


@pytest.mark.parametrize("profile", ["dev", "prod"])
def test_no_shipped_profile_points_at_the_pytest_database(profile):
    # tests/test_job_store_postgres.py truncates muscatdb_test; a fleet on it loses its queue
    assert w.load_config(w.DEFAULT_CONFIG, profile=profile).database != "muscatdb_test"


def test_engine_envs_reach_both_web_and_workers():
    data = _profiled()
    data["profiles"]["prod"]["engine_envs"] = {
        "prose": "prose-prod-01234567", "timer": "timer-prod-89abcdef",
    }
    prod = w.parse_config(data, profile="prod")
    worker = w.render_env(prod, prod.hosts["ut3"], "muscat-ut3")
    web = w.web_env(prod)
    for key, name in (("PROSE", "prose-prod-01234567"), ("TIMER", "timer-prod-89abcdef")):
        assert f"MUSCAT_{key}_CONDA_ENV={name}\n" in worker
        assert f"export MUSCAT_{key}_CONDA_ENV={name}\n" in web
    assert "CONDA_ENV" not in w.web_env(w.parse_config(data, profile="dev"))


@pytest.mark.parametrize("envs", [{"unknown": "foo"}, {"prose": "bad\nINJECT=1"}, []])
def test_engine_envs_reject_unknown_engines_and_unsafe_names(envs):
    data = _profiled()
    data["profiles"]["prod"]["engine_envs"] = envs
    with pytest.raises(w.ConfigError, match="engine_envs"):
        w.parse_config(data, profile="prod")


# -- rendering ----------------------------------------------------------------


def test_unit_is_host_guarded_and_kills_the_whole_cgroup():
    unit = w.render_unit(Path("/srv/muscat"))
    assert "ExecStart=/srv/muscat/deploy/worker-run.sh %i" in unit
    assert "EnvironmentFile=%h/.config/muscatdb/workers/%i.env" in unit
    assert "KillMode=control-group" in unit
    # $$ so systemd hands a literal $ to the shell instead of expanding it
    assert '$$(hostname)' in unit and "$$MUSCAT_WORKER_EXPECT_HOST" in unit


def test_env_file_pins_the_host_and_holds_no_secret():
    cfg = w.parse_config(_data())
    text = w.render_env(cfg, cfg.hosts["ut5"], "muscat-ut5")
    assert "MUSCAT_WORKER_EXPECT_HOST=muscat-ut5\n" in text
    assert "MUSCAT_WORKER_MAX_SLOTS=2\n" in text
    assert "MUSCAT_WORKER_PG_DATABASE=muscatdb_dev\n" in text
    assert "POSTGRES_DSN" not in text and "password" not in text.lower()


def test_web_env_follows_queue_only_switch():
    on = w.parse_config(_data())
    off = w.parse_config(_data(web={"queue_only": False}))
    assert "export MUSCAT_WORKER_MAX_SLOTS=0" in w.web_env(on)
    assert "export" not in w.web_env(off)


def test_max_full_jobs_is_unset_unless_configured():
    cfg = w.parse_config(_data())
    assert cfg.max_full_jobs is None
    assert "MAX_FULL_JOBS" not in w.render_env(cfg, cfg.hosts["ut3"], "muscat-ut3")
    assert "MAX_FULL_JOBS" not in w.web_env(cfg)


def test_max_full_jobs_reaches_workers_and_web_app():
    data = _data()
    data["defaults"]["max_full_jobs"] = 6
    cfg = w.parse_config(data)
    assert cfg.max_full_jobs == 6
    assert "MUSCAT_MAX_FULL_JOBS=6\n" in w.render_env(cfg, cfg.hosts["ut3"], "muscat-ut3")
    assert "export MUSCAT_MAX_FULL_JOBS=6" in w.web_env(cfg)
    # exported even when the web app runs jobs itself: it claims the same slots
    data["web"] = {"queue_only": False}
    assert "export MUSCAT_MAX_FULL_JOBS=6" in w.web_env(w.parse_config(data))


def test_shipped_prod_profile_allows_one_full_run_per_host_plus_one():
    cfg = w.load_config(w.DEFAULT_CONFIG, "prod")
    assert cfg.max_full_jobs == len(cfg.hosts) + 1


def test_shipped_dev_profile_caps_the_dev_server_at_two_full_runs():
    cfg = w.load_config(w.DEFAULT_CONFIG, "dev")
    assert cfg.max_full_jobs == 2


# -- up / down ----------------------------------------------------------------


class RecordingRunner(w.Runner):
    def __init__(self, hostnames, existing=None, linger="yes"):
        super().__init__(dry_run=False)
        self.hostnames = hostnames
        self.existing = existing or {}
        self.linger = linger
        self.calls: list[tuple[str, str]] = []
        self.slept: list[float] = []

    def run(self, ssh, script, readonly=False):
        self.calls.append((ssh, script))
        if script == "hostname":
            return 0, self.hostnames[ssh]
        if "Linger" in script:
            return 0, self.linger
        if script.startswith("test -r"):
            return 0, ""
        if "list-units" in script:
            return 0, "\n".join(f"{u} loaded active running x" for u in self.existing.get(ssh, []))
        return 0, ""

    def sleep(self, seconds):
        self.slept.append(seconds)

    def mutating(self, ssh):
        return [s for h, s in self.calls if h == ssh and "systemctl --user" in s and "list-units" not in s]


@pytest.fixture
def conf(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSCAT_WORKERS_CONF_DIR", str(tmp_path))
    return tmp_path


class FakeDb:
    """Stands in for ``make_db_query``: running rows per database name."""

    def __init__(self, **rows_by_database):
        self.rows = rows_by_database
        self.queried: list[str | None] = []

    def __call__(self, database=None):
        self.queried.append(database)
        if database not in self.rows:
            raise RuntimeError(f"job query failed: no database {database!r}")
        return self.rows[database]


def no_db(database=None):
    raise AssertionError(f"the database must not be queried here (asked for {database!r})")


def install(cfg, host_key, hostname, conf):
    """Write the env files ``up`` would have, as an earlier run under *cfg*."""
    for pipe in cfg.hosts[host_key].pipelines:
        path = conf / "muscatdb" / "workers" / f"{w.instance(host_key, pipe)}.env"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(w.render_env(cfg, cfg.hosts[host_key], hostname))


def job(host, type_, n=1):
    return {"instance_id": f"{host}:{n}:abcd", "type": type_}


def test_up_installs_unit_and_env_then_starts_each_pipeline_staggered(conf):
    cfg = w.parse_config(_data())
    runner = RecordingRunner({"muscat-ut3": "muscat-ut3"})

    rc = w.cmd_up(cfg, [cfg.hosts["ut3"]], runner, no_db)

    assert rc == 0
    assert (conf / "systemd/user/muscatdb-worker@.service").is_file()
    env = (conf / "muscatdb/workers/ut3-photometry.env").read_text()
    assert "MUSCAT_WORKER_EXPECT_HOST=muscat-ut3" in env
    cmds = runner.mutating("muscat-ut3")
    assert cmds[0] == "systemctl --user daemon-reload"
    assert any("enable muscatdb-worker@ut3-transit_fit.service" in c for c in cmds)
    assert any("enable muscatdb-worker@ut3-photometry.service" in c for c in cmds)
    # one gap between the two workers, none after the last
    assert runner.slept == [cfg.stagger_s]


def test_up_restarts_only_workers_whose_env_changed(conf):
    cfg = w.parse_config(_data())
    w.cmd_up(cfg, [cfg.hosts["ut5"]], RecordingRunner({"ut5": "muscat-ut5"}), no_db)

    second = RecordingRunner({"ut5": "muscat-ut5"})
    w.cmd_up(cfg, [cfg.hosts["ut5"]], second, no_db)
    assert any(" start muscatdb-worker@ut5-ttv_fit.service" in c for c in second.mutating("ut5"))
    assert not any(" restart " in c for c in second.mutating("ut5"))

    changed = w.parse_config(_data(hosts={"ut5": {"pipelines": ["ttv_fit"], "max_slots": 3}}))
    third = RecordingRunner({"ut5": "muscat-ut5"})
    w.cmd_up(changed, [changed.hosts["ut5"]], third, FakeDb(muscatdb_dev=[]))
    assert any(" restart muscatdb-worker@ut5-ttv_fit.service" in c for c in third.mutating("ut5"))


def test_up_stops_instances_no_longer_in_the_config(conf):
    cfg = w.parse_config(_data())
    runner = RecordingRunner(
        {"muscat-ut3": "muscat-ut3"},
        existing={"muscat-ut3": ["muscatdb-worker@ut3-ttv_fit.service", "muscatdb-worker@ut3-photometry.service"]},
    )

    w.cmd_up(cfg, [cfg.hosts["ut3"]], runner, FakeDb(muscatdb_dev=[]))

    stops = [c for c in runner.mutating("muscat-ut3") if "disable --now" in c]
    assert stops == ["systemctl --user disable --now muscatdb-worker@ut3-ttv_fit.service"]


def test_up_reports_an_unreachable_host_and_continues(conf):
    cfg = w.parse_config(_data())

    class Down(RecordingRunner):
        def run(self, ssh, script, readonly=False):
            if ssh == "muscat-ut3" and script == "hostname":
                return 255, "ssh: connect timed out"
            return super().run(ssh, script, readonly)

    runner = Down({"ut5": "muscat-ut5"})
    rc = w.cmd_up(cfg, [cfg.hosts["ut3"], cfg.hosts["ut5"]], runner, no_db)

    assert rc == 1
    assert any("ut5-ttv_fit" in c for c in runner.mutating("ut5"))
    assert runner.mutating("muscat-ut3") == []


def test_down_refuses_while_jobs_run_on_the_host(conf, capsys):
    cfg = w.parse_config(_data())
    runner = RecordingRunner({"muscat-ut3": "muscat-ut3"})
    rows = lambda database=None: [{"instance_id": "muscat-ut3:123:abcd", "type": "transit_fit"}]  # noqa: E731

    rc = w.cmd_down(cfg, [cfg.hosts["ut3"]], runner, rows, force=False)

    assert rc == 1
    assert runner.mutating("muscat-ut3") == []
    assert "--force" in capsys.readouterr().err


def test_down_ignores_jobs_on_other_hosts_and_stops_every_instance(conf):
    cfg = w.parse_config(_data())
    runner = RecordingRunner({"muscat-ut3": "muscat-ut3"})
    rows = lambda database=None: [{"instance_id": "muscat-ut5:9:ffff", "type": "ttv_fit"}]  # noqa: E731

    rc = w.cmd_down(cfg, [cfg.hosts["ut3"]], runner, rows, force=False)

    assert rc == 0
    stopped = sorted(c.split()[-1] for c in runner.mutating("muscat-ut3"))
    assert stopped == [
        "muscatdb-worker@ut3-photometry.service",
        "muscatdb-worker@ut3-transit_fit.service",
    ]


def test_down_force_stops_despite_running_jobs(conf):
    cfg = w.parse_config(_data())
    runner = RecordingRunner({"muscat-ut3": "muscat-ut3"})
    rows = lambda database=None: [{"instance_id": "muscat-ut3:123:abcd", "type": "transit_fit"}]  # noqa: E731

    assert w.cmd_down(cfg, [cfg.hosts["ut3"]], runner, rows, force=True) == 0
    assert runner.mutating("muscat-ut3")


def test_down_force_does_not_need_the_database(conf):
    # the database may be gone or renamed, which is exactly when --force is wanted
    cfg = w.parse_config(_data())
    runner = RecordingRunner({"muscat-ut3": "muscat-ut3"})

    def unreachable(database=None):
        raise RuntimeError("database does not exist")

    assert w.cmd_down(cfg, [cfg.hosts["ut3"]], runner, unreachable, force=True) == 0
    assert runner.mutating("muscat-ut3")


def test_select_rejects_unknown_host():
    cfg = w.parse_config(_data())
    with pytest.raises(w.ConfigError, match="unknown host"):
        w._select(cfg, ["ut9"])
    assert [h.key for h in w._select(cfg, [])] == ["ut3", "ut5"]


def test_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MUSCAT_WORKERS_CONF_DIR", str(tmp_path))
    r = w.Runner(dry_run=True)
    assert r.write(tmp_path / "x" / "f.env", "a=1\n") is True
    assert not (tmp_path / "x").exists()
    assert r.run("host", "systemctl --user stop x") == (0, "")
    assert "[dry-run]" in capsys.readouterr().out


# -- safety guards: the installed fleet, not the selected profile, decides -------


def _prod_installed(conf):
    prod = w.parse_config(_profiled(), profile="prod")
    install(prod, "ut3", "muscat-ut3", conf)
    return prod


def test_down_refuses_when_the_installed_profile_is_not_the_selected_one(conf, capsys):
    _prod_installed(conf)
    dev = w.parse_config(_profiled(), profile="dev")
    runner = RecordingRunner({"muscat-ut3": "muscat-ut3"})
    db = FakeDb(muscatdb_dev=[], muscatdb_prod=[job("muscat-ut3", "photometry")])

    rc = w.cmd_down(dev, [dev.hosts["ut3"]], runner, db, force=False)

    assert rc == 1
    assert runner.mutating("muscat-ut3") == []
    err = capsys.readouterr().err
    assert "prod" in err and "--profile prod" in err and "--force" in err


def test_down_checks_the_database_the_workers_are_installed_against(conf, capsys):
    prod = _prod_installed(conf)
    runner = RecordingRunner({"muscat-ut3": "muscat-ut3"})
    # same profile name, config since pointed elsewhere: the running job is in the old database
    moved = w.parse_config(_profiled(profiles={
        "dev": {}, "prod": {"database": "muscatdb_prod2", "repo": "/srv/deploy/main"},
    }), profile="prod")
    assert moved.database != prod.database
    db = FakeDb(muscatdb_prod=[job("muscat-ut3", "photometry")], muscatdb_prod2=[])

    rc = w.cmd_down(moved, [moved.hosts["ut3"]], runner, db, force=False)

    assert rc == 1
    assert runner.mutating("muscat-ut3") == []
    assert set(db.queried) == {"muscatdb_prod"}


def test_down_force_stops_a_mismatched_fleet_without_the_database(conf):
    _prod_installed(conf)
    dev = w.parse_config(_profiled(), profile="dev")
    runner = RecordingRunner({"muscat-ut3": "muscat-ut3"})

    assert w.cmd_down(dev, [dev.hosts["ut3"]], runner, no_db, force=True) == 0
    assert runner.mutating("muscat-ut3")


def test_down_reports_an_unverifiable_host_instead_of_stopping_it(conf, capsys):
    prod = _prod_installed(conf)
    runner = RecordingRunner({"muscat-ut3": "muscat-ut3"})

    rc = w.cmd_down(prod, [prod.hosts["ut3"]], runner, FakeDb(), force=False)

    assert rc == 1
    assert runner.mutating("muscat-ut3") == []
    assert "--force" in capsys.readouterr().err


def test_status_counts_jobs_in_the_installed_database_and_flags_a_mismatch(conf, capsys):
    _prod_installed(conf)
    dev = w.parse_config(_profiled(), profile="dev")
    runner = RecordingRunner({"muscat-ut3": "muscat-ut3"})
    db = FakeDb(muscatdb_dev=[], muscatdb_prod=[job("muscat-ut3", "photometry")])

    assert w.cmd_status(dev, [dev.hosts["ut3"]], runner, db) == 0

    out = capsys.readouterr().out
    assert "running jobs on this host: 1" in out
    assert "installed profile prod" in out and "selected dev" in out
    assert "muscatdb_dev" not in db.queried


def _changed(cfg):
    """*cfg* with a different job_threads, so ``up`` sees new env text."""
    data = _data(hosts={"ut3": {"ssh": "muscat-ut3", "pipelines": ["transit_fit", "photometry"], "job_threads": 4}})
    return w.parse_config(data)


def test_up_keeps_a_busy_worker_running_and_leaves_its_env_for_the_next_run(conf, capsys):
    cfg = w.parse_config(_data())
    install(cfg, "ut3", "muscat-ut3", conf)
    new = _changed(cfg)
    env = conf / "muscatdb/workers/ut3-photometry.env"
    before = env.read_text()
    runner = RecordingRunner({"muscat-ut3": "muscat-ut3"})
    db = FakeDb(muscatdb_dev=[job("muscat-ut3", "photometry")])

    rc = w.cmd_up(new, [new.hosts["ut3"]], runner, db)

    cmds = runner.mutating("muscat-ut3")
    assert rc == 1
    assert not any("ut3-photometry" in c for c in cmds)  # not restarted, not even started
    assert any(" restart muscatdb-worker@ut3-transit_fit.service" in c for c in cmds)  # idle: restarted
    assert env.read_text() == before  # unchanged on disk, so the next `up` still sees the diff
    assert "restart pending" in capsys.readouterr().err

    later = RecordingRunner({"muscat-ut3": "muscat-ut3"})
    w.cmd_up(new, [new.hosts["ut3"]], later, FakeDb(muscatdb_dev=[]))
    assert any(" restart muscatdb-worker@ut3-photometry.service" in c for c in later.mutating("muscat-ut3"))


def test_up_force_restarts_a_busy_worker(conf):
    cfg = w.parse_config(_data())
    install(cfg, "ut3", "muscat-ut3", conf)
    new = _changed(cfg)
    runner = RecordingRunner({"muscat-ut3": "muscat-ut3"})
    db = FakeDb(muscatdb_dev=[job("muscat-ut3", "photometry")])

    assert w.cmd_up(new, [new.hosts["ut3"]], runner, db, force=True) == 0
    assert any(" restart muscatdb-worker@ut3-photometry.service" in c for c in runner.mutating("muscat-ut3"))


def test_up_profile_switch_checks_the_database_being_left(conf):
    cfg = w.parse_config(_profiled(), profile="dev")
    install(cfg, "ut3", "muscat-ut3", conf)
    prod = w.parse_config(_profiled(), profile="prod")
    runner = RecordingRunner({"muscat-ut3": "muscat-ut3"})
    db = FakeDb(muscatdb_dev=[job("muscat-ut3", "transit_fit")], muscatdb_prod=[])

    rc = w.cmd_up(prod, [prod.hosts["ut3"]], runner, db)

    cmds = runner.mutating("muscat-ut3")
    assert rc == 1
    assert not any("ut3-transit_fit" in c for c in cmds)
    assert "muscatdb_dev" in db.queried and "muscatdb_prod" not in db.queried


def test_up_does_not_stop_a_stale_unit_that_is_running_a_job(conf, capsys):
    cfg = w.parse_config(_data())
    runner = RecordingRunner(
        {"muscat-ut3": "muscat-ut3"},
        existing={"muscat-ut3": ["muscatdb-worker@ut3-ttv_fit.service"]},
    )
    db = FakeDb(muscatdb_dev=[job("muscat-ut3", "ttv_fit")])

    rc = w.cmd_up(cfg, [cfg.hosts["ut3"]], runner, db)

    assert rc == 1
    assert not any("disable --now" in c for c in runner.mutating("muscat-ut3"))
    assert "ut3-ttv_fit" in capsys.readouterr().err

    forced = RecordingRunner(
        {"muscat-ut3": "muscat-ut3"},
        existing={"muscat-ut3": ["muscatdb-worker@ut3-ttv_fit.service"]},
    )
    w.cmd_up(cfg, [cfg.hosts["ut3"]], forced, db, force=True)
    assert any("disable --now muscatdb-worker@ut3-ttv_fit.service" in c for c in forced.mutating("muscat-ut3"))


def test_up_fails_closed_when_it_cannot_check_for_jobs(conf):
    cfg = w.parse_config(_data())
    install(cfg, "ut3", "muscat-ut3", conf)
    new = _changed(cfg)
    runner = RecordingRunner({"muscat-ut3": "muscat-ut3"})

    rc = w.cmd_up(new, [new.hosts["ut3"]], runner, FakeDb())

    assert rc == 1
    assert not any(" restart " in c for c in runner.mutating("muscat-ut3"))


# -- an unreachable or hung host must not stall or silently pass the run --------


def test_ssh_calls_are_bounded_in_connect_and_in_total(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["timeout"] = argv, kw.get("timeout")
        return type("P", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    monkeypatch.setattr(w.subprocess, "run", fake_run)

    assert w.Runner().run("ut3", "hostname") == (0, "ok")

    assert "ConnectTimeout=%d" % w.SSH_CONNECT_TIMEOUT_S in seen["argv"]
    assert any(a.startswith("ServerAliveInterval=") for a in seen["argv"])
    assert seen["timeout"] == w.SSH_COMMAND_TIMEOUT_S


def test_a_hung_ssh_is_reported_as_a_failure_not_raised(monkeypatch):
    def hang(argv, **kw):
        raise w.subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(w.subprocess, "run", hang)

    rc, out = w.Runner().run("ut3", "hostname")

    assert rc != 0 and "timed out" in out


def test_up_skips_a_hung_host_and_still_serves_the_rest(conf, monkeypatch):
    cfg = w.parse_config(_data())

    class Hung(RecordingRunner):
        def run(self, ssh, script, readonly=False):
            if ssh == "muscat-ut3":
                return 255, "ssh timed out after 10s"
            return super().run(ssh, script, readonly)

    runner = Hung({"ut5": "muscat-ut5"})
    rc = w.cmd_up(cfg, [cfg.hosts["ut3"], cfg.hosts["ut5"]], runner, no_db)

    assert rc == 1
    assert not (conf / "muscatdb/workers/ut3-photometry.env").exists()  # nothing written for it
    assert any("ut5-ttv_fit" in c for c in runner.mutating("ut5"))


def test_status_exits_nonzero_when_a_host_is_unreachable(conf, capsys):
    cfg = w.parse_config(_data())

    class Down(RecordingRunner):
        def run(self, ssh, script, readonly=False):
            if ssh == "muscat-ut3":
                return 255, "ssh: connect timed out"
            return super().run(ssh, script, readonly)

    rc = w.cmd_status(cfg, [cfg.hosts["ut3"], cfg.hosts["ut5"]], Down({"ut5": "muscat-ut5"}), FakeDb(muscatdb_dev=[]))

    out = capsys.readouterr().out
    assert rc == 1
    assert "UNREACHABLE" in out and "ut5" in out  # the reachable host is still reported


def test_a_stalled_job_query_is_an_error_not_a_hang(tmp_path, monkeypatch):
    env = tmp_path / "pg.env"
    env.write_text("MUSCAT_POSTGRES_DSN=postgresql://u:p@h:5432/muscatdb_dev\n")
    data = _data()
    data["defaults"]["env_file"] = str(env)
    query = w.make_db_query(w.parse_config(data))
    seen = {}

    def stall(argv, **kw):
        seen["timeout"] = kw.get("timeout")
        raise w.subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(w.subprocess, "run", stall)

    with pytest.raises(RuntimeError, match="timed out"):
        query()
    assert seen["timeout"] == w.DB_QUERY_TIMEOUT_S
