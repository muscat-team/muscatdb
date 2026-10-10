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
        "defaults": {"pg_host": "10.0.0.1", "database": "muscatdb_test", "repo": "/srv/muscat"},
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
    assert "MUSCAT_WORKER_PG_DATABASE=muscatdb_test\n" in text
    assert "POSTGRES_DSN" not in text and "password" not in text.lower()


def test_web_env_follows_queue_only_switch():
    on = w.parse_config(_data())
    off = w.parse_config(_data(web={"queue_only": False}))
    assert "export MUSCAT_WORKER_MAX_SLOTS=0" in w.web_env(on)
    assert "export" not in w.web_env(off)


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


def test_up_installs_unit_and_env_then_starts_each_pipeline_staggered(conf):
    cfg = w.parse_config(_data())
    runner = RecordingRunner({"muscat-ut3": "muscat-ut3"})

    rc = w.cmd_up(cfg, [cfg.hosts["ut3"]], runner)

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
    w.cmd_up(cfg, [cfg.hosts["ut5"]], RecordingRunner({"ut5": "muscat-ut5"}))

    second = RecordingRunner({"ut5": "muscat-ut5"})
    w.cmd_up(cfg, [cfg.hosts["ut5"]], second)
    assert any(" start muscatdb-worker@ut5-ttv_fit.service" in c for c in second.mutating("ut5"))
    assert not any(" restart " in c for c in second.mutating("ut5"))

    changed = w.parse_config(_data(hosts={"ut5": {"pipelines": ["ttv_fit"], "max_slots": 3}}))
    third = RecordingRunner({"ut5": "muscat-ut5"})
    w.cmd_up(changed, [changed.hosts["ut5"]], third)
    assert any(" restart muscatdb-worker@ut5-ttv_fit.service" in c for c in third.mutating("ut5"))


def test_up_stops_instances_no_longer_in_the_config(conf):
    cfg = w.parse_config(_data())
    runner = RecordingRunner(
        {"muscat-ut3": "muscat-ut3"},
        existing={"muscat-ut3": ["muscatdb-worker@ut3-ttv_fit.service", "muscatdb-worker@ut3-photometry.service"]},
    )

    w.cmd_up(cfg, [cfg.hosts["ut3"]], runner)

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
    rc = w.cmd_up(cfg, [cfg.hosts["ut3"], cfg.hosts["ut5"]], runner)

    assert rc == 1
    assert any("ut5-ttv_fit" in c for c in runner.mutating("ut5"))
    assert runner.mutating("muscat-ut3") == []


def test_down_refuses_while_jobs_run_on_the_host(conf, capsys):
    cfg = w.parse_config(_data())
    runner = RecordingRunner({"muscat-ut3": "muscat-ut3"})
    rows = lambda: [{"instance_id": "muscat-ut3:123:abcd", "type": "transit_fit"}]  # noqa: E731

    rc = w.cmd_down(cfg, [cfg.hosts["ut3"]], runner, rows, force=False)

    assert rc == 1
    assert runner.mutating("muscat-ut3") == []
    assert "--force" in capsys.readouterr().err


def test_down_ignores_jobs_on_other_hosts_and_stops_every_instance(conf):
    cfg = w.parse_config(_data())
    runner = RecordingRunner({"muscat-ut3": "muscat-ut3"})
    rows = lambda: [{"instance_id": "muscat-ut5:9:ffff", "type": "ttv_fit"}]  # noqa: E731

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
    rows = lambda: [{"instance_id": "muscat-ut3:123:abcd", "type": "transit_fit"}]  # noqa: E731

    assert w.cmd_down(cfg, [cfg.hosts["ut3"]], runner, rows, force=True) == 0
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
