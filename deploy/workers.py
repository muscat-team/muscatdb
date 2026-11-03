#!/usr/bin/env python3
"""Bring muscat-db job workers up and down on other hosts from one config file.

    .venv/bin/python deploy/workers.py [-c deploy/workers.toml] [--dry-run] up   [HOST ...]
    .venv/bin/python deploy/workers.py ...                                  down [--force] [HOST ...]
    .venv/bin/python deploy/workers.py ...                                  status [HOST ...]
    eval "$(.venv/bin/python deploy/workers.py web-env)"      # before starting the web app
    .venv/bin/python deploy/workers.py --profile prod ...     # a named [profiles.*] set

Run it from the control host (ut2): it needs Python >= 3.11 for ``tomllib``
(the workers' system python is older) and only sends shell commands over ssh.

How it works
------------
* One systemd *user* unit template, ``muscatdb-worker@.service``, instantiated
  per host and pipeline as ``muscatdb-worker@<host>-<pipeline>``.
* ``~/.config/systemd`` is on NFS and shared by every host, so a plain
  ``enable`` would enable the unit on all of them. Each instance therefore
  carries ``MUSCAT_WORKER_EXPECT_HOST`` (the host's real hostname, looked up at
  ``up`` time) and an ``ExecCondition`` that skips the unit anywhere else.
* The per-instance environment file holds no secrets. The Postgres password
  stays in ``pg.env``; ``deploy/worker-run.sh`` sources it at start and points
  the DSN at the configured host and database.
* Stopping a worker's unit kills its whole cgroup, including a fit it is
  running (``KillMode=control-group``), because a surviving fit would race the
  reclaim of its own row. ``down`` therefore refuses while jobs are running on
  the host unless ``--force``; a forced job is requeued by the orphan reclaim.
  ``up`` applies the same guard before it restarts a worker whose env changed
  or stops a stale unit: a busy pipeline is skipped with "restart pending" and
  its env file is left alone, so the next ``up`` retries it.
* The guard reads the profile and database back from the installed env files,
  not from the selected profile. ``down`` refuses when the fleet was installed
  under another profile (re-run with that ``--profile``), and ``status`` and
  ``up`` count jobs in the database the workers actually use.

Profiles
--------
``[profiles.<name>]`` overrides ``[defaults]`` keys that must change together
(database and repo, chiefly), so one fleet and one set of unit names serves dev
or production, never both. ``--profile`` (or ``MUSCAT_WORKERS_PROFILE``) picks
one; otherwise ``[defaults] profile`` applies. The workers run the checkout in
``repo``, so a production database needs the deploy checkout, not the dev tree.
Switch by stopping under the old profile, then starting under the new one::

    workers.py --profile dev  down     # refuses while jobs run
    workers.py --profile prod up       # env text changed: units restart

An unreachable or hung host is reported and skipped, never waited on: every ssh
call has a connect and a total timeout, and a stalled job query is an error.
``up`` and ``down`` exit 1 for it, and so does ``status``.

Needs on each host: ``loginctl enable-linger <user>`` so user units survive the
last logout (``status`` reports it; ``up`` only warns).
"""

from __future__ import annotations

import argparse
import functools
import os
import re
import shlex
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PIPELINES = ("photometry", "transit_fit", "ttv_fit")
UNIT_NAME = "muscatdb-worker@.service"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = Path(__file__).resolve().parent / "workers.toml"
_XDG = "export XDG_RUNTIME_DIR=/run/user/$(id -u); "
_DB_NAME = re.compile(r"[A-Za-z0-9_]+\Z")
# A host that drops packets would otherwise hold the run for the OS TCP timeout,
# and the hosts are visited one after another.
SSH_CONNECT_TIMEOUT_S = 10
SSH_COMMAND_TIMEOUT_S = 180  # covers a restart: TimeoutStopSec=60 plus the start
DB_QUERY_TIMEOUT_S = 30


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class HostSpec:
    key: str
    ssh: str
    pipelines: tuple[str, ...]
    max_slots: int
    job_threads: int
    notify: bool


@dataclass(frozen=True)
class Config:
    repo: Path
    env_file: str
    pg_host: str
    database: str
    control_plane: str
    queue_only_web: bool
    stagger_s: float
    max_full_jobs: int | None = None
    profile: str | None = None
    engine_envs: dict[str, str] = field(default_factory=dict)
    prose_frozen_config: bool = False
    hosts: dict[str, HostSpec] = field(default_factory=dict)


def _int(value, where: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{where} must be an integer >= {minimum}, got {value!r}")
    return value


# Keys a [profiles.<name>] table may override from [defaults] (plus queue_only,
# which otherwise lives under [web]).
_PROFILE_KEYS = frozenset({
    "repo", "env_file", "pg_host", "database", "control_plane", "max_slots",
    "job_threads", "notify", "stagger_s", "max_full_jobs", "queue_only", "engine_envs",
    "prose_frozen_config",
})


def _apply_profile(data: dict, profile: str | None) -> tuple[dict, dict, str | None]:
    """Return (defaults, web, profile name) with the selected profile merged in.

    A profile switches the things that must change together -- which database
    the workers claim from and which checkout they run -- so a half-switched
    fleet (production database, dev code) cannot be configured by hand.
    """
    d = dict(data.get("defaults") or {})
    web = dict(data.get("web") or {})
    profiles = data.get("profiles") or {}
    default = d.pop("profile", None)
    name = profile or default
    if name is None:
        return d, web, None
    if not profiles:
        raise ConfigError(f"profile {name!r} requested but the config has no [profiles.*] tables")
    if name not in profiles:
        raise ConfigError(f"unknown profile {name!r}; defined: {sorted(profiles)}")
    over = dict(profiles[name] or {})
    unknown = sorted(set(over) - _PROFILE_KEYS)
    if unknown:
        raise ConfigError(f"[profiles.{name}] unknown key(s) {unknown}; allowed: {sorted(_PROFILE_KEYS)}")
    if "queue_only" in over:
        web["queue_only"] = over.pop("queue_only")
    d.update(over)
    return d, web, name


def parse_config(data: dict, profile: str | None = None) -> Config:
    d, web, profile = _apply_profile(data, profile)
    frozen = d.get("prose_frozen_config", False)
    if not isinstance(frozen, bool):
        raise ConfigError("prose_frozen_config must be a boolean")
    engine_envs = d.get("engine_envs", {})
    if not isinstance(engine_envs, dict):
        raise ConfigError("engine_envs must map engine names to conda environment names")
    for engine, env_name in engine_envs.items():
        if (engine not in {"prose", "timer", "harmonic"}
                or not isinstance(env_name, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]+", env_name)):
            raise ConfigError(f"engine_envs contains an invalid engine or environment: {engine!r}")
    for need in ("pg_host", "database"):
        if not d.get(need):
            raise ConfigError(f"[defaults] {need} is required")
    hosts_raw = data.get("hosts") or {}
    if not hosts_raw:
        raise ConfigError("at least one [hosts.<name>] table is required")
    hosts: dict[str, HostSpec] = {}
    for key, h in hosts_raw.items():
        if "-" in key:
            raise ConfigError(f"host key {key!r} must not contain '-' (it separates host and pipeline)")
        pipes = tuple(h.get("pipelines") or ())
        if not pipes:
            raise ConfigError(f"[hosts.{key}] pipelines must list at least one pipeline")
        bad = [p for p in pipes if p not in PIPELINES]
        if bad:
            raise ConfigError(f"[hosts.{key}] unknown pipeline(s) {bad}; known: {list(PIPELINES)}")
        if len(set(pipes)) != len(pipes):
            raise ConfigError(f"[hosts.{key}] pipelines contains a duplicate")
        hosts[key] = HostSpec(
            key=key,
            ssh=str(h.get("ssh") or key),
            pipelines=pipes,
            max_slots=_int(h.get("max_slots", d.get("max_slots", 1)), f"[hosts.{key}] max_slots"),
            job_threads=_int(h.get("job_threads", d.get("job_threads", 8)), f"[hosts.{key}] job_threads", 1),
            notify=bool(h.get("notify", d.get("notify", True))),
        )
    return Config(
        repo=Path(d.get("repo") or REPO_ROOT).expanduser(),
        env_file=str(d.get("env_file") or "~/.config/muscatdb/pg.env"),
        pg_host=str(d["pg_host"]),
        database=str(d["database"]),
        control_plane=str(d.get("control_plane") or "postgres"),
        queue_only_web=bool(web.get("queue_only", False)),
        stagger_s=float(d.get("stagger_s", 3)),
        max_full_jobs=(
            _int(d["max_full_jobs"], "max_full_jobs", 1) if "max_full_jobs" in d else None
        ),
        profile=profile,
        engine_envs=dict(engine_envs),
        prose_frozen_config=frozen,
        hosts=hosts,
    )


def load_config(path: Path, profile: str | None = None) -> Config:
    with open(path, "rb") as f:
        return parse_config(tomllib.load(f), profile)


# ----------------------------------------------------------------- rendering


def instance(host: str, pipeline: str) -> str:
    return f"{host}-{pipeline}"


def unit_name(host: str, pipeline: str) -> str:
    return f"muscatdb-worker@{instance(host, pipeline)}.service"


def render_unit(repo: Path) -> str:
    return f"""\
# Managed by deploy/workers.py -- edit deploy/workers.toml, not this file.
[Unit]
Description=muscat-db job worker (%i)
After=network-online.target

[Service]
Type=simple
EnvironmentFile=%h/.config/muscatdb/workers/%i.env
# %h is shared over NFS: skip, silently, on any host this instance is not for.
ExecCondition=/bin/sh -c '[ "$$(hostname)" = "$$MUSCAT_WORKER_EXPECT_HOST" ]'
ExecStart={repo}/deploy/worker-run.sh %i
Restart=on-failure
RestartSec=10
# A fit that outlived its worker would race the reclaim of its own row.
KillMode=control-group
TimeoutStopSec=60

[Install]
WantedBy=default.target
"""


def render_env(cfg: Config, host: HostSpec, hostname: str) -> str:
    lines = {
        "MUSCAT_WORKER_EXPECT_HOST": hostname,
        "MUSCAT_CONTROL_PLANE": cfg.control_plane,
        "MUSCAT_WORKER_PG_HOST": cfg.pg_host,
        "MUSCAT_WORKER_PG_DATABASE": cfg.database,
        "MUSCAT_WORKER_PG_ENV": cfg.env_file,
        "MUSCAT_WORKER_MAX_SLOTS": str(host.max_slots),
        "MUSCAT_JOB_MAX_THREADS": str(host.job_threads),
        "MUSCAT_JOB_NOTIFY": "1" if host.notify else "0",
    }
    if cfg.max_full_jobs is not None:
        lines["MUSCAT_MAX_FULL_JOBS"] = str(cfg.max_full_jobs)
    if cfg.profile:
        # Also makes a profile switch change the env text, so `up` restarts the units.
        lines["MUSCAT_WORKER_PROFILE"] = cfg.profile
    for engine, name in sorted(cfg.engine_envs.items()):
        lines[f"MUSCAT_{engine.upper()}_CONDA_ENV"] = name
    lines["MUSCAT_PROSE_FROZEN_CONFIG"] = "1" if cfg.prose_frozen_config else "0"
    return "".join(f"{k}={v}\n" for k, v in lines.items())


def web_env(cfg: Config) -> str:
    out = [f"export MUSCAT_PROSE_FROZEN_CONFIG={int(cfg.prose_frozen_config)}\n"]
    for engine, name in sorted(cfg.engine_envs.items()):
        out.append(f"export MUSCAT_{engine.upper()}_CONDA_ENV={name}\n")
    if cfg.profile:
        out.append(f"# profile {cfg.profile}: database {cfg.database}, repo {cfg.repo}\n")
    if cfg.queue_only_web:
        out.append("# [web] queue_only = true: full runs are queued for the workers\n")
        out.append("export MUSCAT_WORKER_MAX_SLOTS=0\n")
    else:
        out.append("# [web] queue_only is false: the web app runs full jobs itself\n")
    if cfg.max_full_jobs is not None:
        out.append("# cluster-wide cap on concurrent full runs per pipeline\n")
        out.append(f"export MUSCAT_MAX_FULL_JOBS={cfg.max_full_jobs}\n")
    return "".join(out)


# -------------------------------------------------------------------- runners


class Runner:
    """Runs a shell snippet on a host (over ssh) and the local config writes."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    def run(self, ssh: str, script: str, readonly: bool = False) -> tuple[int, str]:
        """Run *script* on *ssh*. A dry run still executes read-only probes."""
        if self.dry_run and not readonly:
            print(f"[dry-run] ssh {ssh}: {script.strip()}")
            return 0, ""
        argv = [
            "ssh", "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT_S}",
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
            ssh, "bash", "-s",
        ]
        try:
            p = subprocess.run(
                argv, input=_XDG + script, capture_output=True, text=True, timeout=SSH_COMMAND_TIMEOUT_S
            )
        except subprocess.TimeoutExpired:
            return 255, f"ssh timed out after {SSH_COMMAND_TIMEOUT_S}s"
        return p.returncode, (p.stdout + p.stderr).strip()

    def write(self, path: Path, content: str) -> bool:
        """Write *path* if it differs; True when it changed."""
        old = path.read_text() if path.exists() else None
        if old == content:
            return False
        if self.dry_run:
            print(f"[dry-run] write {path}")
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return True

    def sleep(self, seconds: float) -> None:
        if not self.dry_run and seconds > 0:
            time.sleep(seconds)


def _conf_dir() -> Path:
    return Path(os.environ.get("MUSCAT_WORKERS_CONF_DIR", Path.home() / ".config"))


def _select(cfg: Config, names: list[str]) -> list[HostSpec]:
    if not names:
        return list(cfg.hosts.values())
    unknown = [n for n in names if n not in cfg.hosts]
    if unknown:
        raise ConfigError(f"unknown host(s) {unknown}; configured: {list(cfg.hosts)}")
    return [cfg.hosts[n] for n in names]


def _listed(rc_out: tuple[int, str]) -> list[str]:
    rc, out = rc_out
    return [line.split()[0] for line in out.splitlines() if line.startswith("muscatdb-worker@")] if rc == 0 else []


@dataclass(frozen=True)
class Installed:
    """What a worker's env file on disk says it was installed with."""

    profile: str | None
    database: str | None


def installed_state(host_key: str, pipeline: str) -> Installed | None:
    """Read the profile and database back from the env file ``up`` wrote.

    The unit runs from that file, so it -- not the selected profile -- says which
    database a running worker is using. None when no env file exists.
    """
    path = _conf_dir() / "muscatdb" / "workers" / f"{instance(host_key, pipeline)}.env"
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))
    return Installed(values.get("MUSCAT_WORKER_PROFILE"), values.get("MUSCAT_WORKER_PG_DATABASE"))


def _pipeline_of(unit: str) -> str:
    """``muscatdb-worker@ut3-ttv_fit.service`` -> ``ttv_fit``."""
    return unit.removeprefix("muscatdb-worker@").removesuffix(".service").split("-", 1)[-1]


def _database_of(cfg: Config, state: Installed | None) -> str:
    return state.database if state and state.database else cfg.database


def _jobs_on(hostname: str, databases: set[str], db_query) -> list[dict]:
    """``running`` job rows, in any of *databases*, owned by an instance on *hostname*."""
    rows: list[dict] = []
    for database in sorted(databases):
        rows += [r for r in db_query(database) if str(r.get("instance_id", "")).startswith(f"{hostname}:")]
    return rows


def _hold_reason(cfg: Config, host_key: str, hostname: str, pipeline: str, db_query) -> str | None:
    """Why stopping or restarting *pipeline*'s worker now is unsafe, else None.

    Checked against the database the worker was installed with. A row whose type
    is not a known pipeline counts for every pipeline, and a failed query counts
    as busy: this errs towards leaving a worker alone.
    """
    database = _database_of(cfg, installed_state(host_key, pipeline))
    try:
        rows = _jobs_on(hostname, {database}, db_query)
    except RuntimeError as exc:
        return f"cannot check for running jobs in {database} ({exc})"
    busy = sum(1 for r in rows if r.get("type") == pipeline or r.get("type") not in PIPELINES)
    return f"{busy} {pipeline} job(s) running on {hostname} (database {database})" if busy else None


# ------------------------------------------------------------------- commands


def cmd_up(cfg: Config, hosts: list[HostSpec], runner: Runner, db_query, force: bool = False) -> int:
    """Install and (re)start workers.

    A worker is restarted only when its env text changed, and a restart or a
    stale-unit stop kills the job it is running, so both are skipped for a busy
    pipeline unless *force*. A skipped pipeline's env file is left untouched so
    the next ``up`` still sees the difference. The job query runs only when such
    a change is actually pending (never on a first install).
    """
    conf = _conf_dir()
    runner.write(conf / "systemd" / "user" / UNIT_NAME, render_unit(cfg.repo))
    worker_run = cfg.repo / "deploy" / "worker-run.sh"
    rc_total = 0
    for host in hosts:
        rc, hostname = runner.run(host.ssh, "hostname", readonly=True)
        if rc != 0 or not hostname:
            print(f"{host.key}: cannot reach {host.ssh}: {hostname}", file=sys.stderr)
            rc_total = 1
            continue
        changed: set[str] = set()
        held: set[str] = set()
        for pipe in host.pipelines:
            env_path = conf / "muscatdb" / "workers" / f"{instance(host.key, pipe)}.env"
            text = render_env(cfg, host, hostname)
            if env_path.exists() and env_path.read_text() != text and not force:
                reason = _hold_reason(cfg, host.key, hostname, pipe, db_query)
                if reason:
                    print(
                        f"{host.key}: {pipe}: {reason}; restart pending, re-run up when idle or with --force",
                        file=sys.stderr,
                    )
                    held.add(pipe)
                    rc_total = 1
                    continue
            if runner.write(env_path, text):
                changed.add(pipe)
        probe = f"test -r {shlex.quote(str(worker_run))} && test -d ~/.config/muscatdb/workers"
        if runner.run(host.ssh, probe, readonly=True)[0] != 0:
            print(f"{host.key}: {worker_run} or the shared ~/.config is not visible there", file=sys.stderr)
            if not runner.dry_run:
                rc_total = 1
                continue
        rc, lingering = runner.run(host.ssh, 'loginctl show-user "$USER" -p Linger --value', readonly=True)
        if lingering.strip() != "yes":
            print(
                f"{host.key}: Linger=no -- workers stop when the last login session ends. "
                f"Run on {hostname}: loginctl enable-linger $USER",
                file=sys.stderr,
            )
        wanted = {unit_name(host.key, p) for p in host.pipelines}
        existing = _listed(runner.run(
            host.ssh, f"systemctl --user list-units 'muscatdb-worker@{host.key}-*' --plain --no-legend --all", readonly=True
        ))
        for stale in sorted(set(existing) - wanted):
            reason = None if force else _hold_reason(cfg, host.key, hostname, _pipeline_of(stale), db_query)
            if reason:
                print(f"{host.key}: {stale}: {reason}; not stopped, re-run up when idle or with --force", file=sys.stderr)
                rc_total = 1
                continue
            runner.run(host.ssh, f"systemctl --user disable --now {shlex.quote(stale)}")
            print(f"{host.key}: stopped {stale} (no longer in config)")
        runner.run(host.ssh, "systemctl --user daemon-reload")
        for i, pipe in enumerate(host.pipelines):
            if pipe in held:
                continue
            unit = unit_name(host.key, pipe)
            verb = "restart" if pipe in changed else "start"
            rc, out = runner.run(
                host.ssh, f"systemctl --user enable {shlex.quote(unit)} && systemctl --user {verb} {shlex.quote(unit)}"
            )
            print(f"{host.key}: {pipe}: {'ok' if rc == 0 else 'FAILED ' + out}")
            rc_total |= rc != 0
            if i < len(host.pipelines) - 1:
                # Workers share one schema check on first connect; starting them
                # one at a time avoids the concurrent-DDL deadlock.
                runner.sleep(cfg.stagger_s)
    return rc_total


def _down_refusal(cfg: Config, host: HostSpec, hostname: str, units: list[str], db_query) -> str | None:
    """Why ``down`` must not stop *units* on *host*, else None."""
    states = [installed_state(host.key, _pipeline_of(u)) for u in units]
    others = sorted({s.profile for s in states if s and s.profile and s.profile != cfg.profile})
    if others:
        by_profile: dict[str, list[str]] = {}
        for unit, state in zip(units, states):
            if state and state.profile:
                by_profile.setdefault(state.profile, []).append(_pipeline_of(unit))
        if len(by_profile) > 1:
            layout = "; ".join(f"{', '.join(pipes)} on {prof}" for prof, pipes in sorted(by_profile.items()))
            return (
                f"mixed profiles ({layout}), so no single --profile checks every worker's database. "
                f"An `up` that held a busy pipeline leaves this state. "
                f"Re-run with --force to stop anyway, or finish `up` once the held pipeline is idle."
            )
        return (
            f"installed under profile {', '.join(others)} but {cfg.profile or '(no profile)'} is selected, "
            f"so the wrong database would be checked for jobs. "
            f"Re-run with --profile {others[0]}, or --force to stop anyway."
        )
    databases = {_database_of(cfg, s) for s in states}
    try:
        busy = _jobs_on(hostname, databases, db_query)
    except RuntimeError as exc:
        return f"cannot check for running jobs ({exc}). Re-run with --force to stop anyway."
    if busy:
        return (
            f"{len(busy)} job(s) running on {hostname}; stopping kills them "
            f"(they are requeued by orphan reclaim). Re-run with --force."
        )
    return None


def cmd_down(cfg: Config, hosts: list[HostSpec], runner: Runner, db_query, force: bool) -> int:
    rc_total = 0
    for host in hosts:
        rc, hostname = runner.run(host.ssh, "hostname", readonly=True)
        if rc != 0:
            print(f"{host.key}: cannot reach {host.ssh}: {hostname}", file=sys.stderr)
            rc_total = 1
            continue
        existing = _listed(runner.run(
            host.ssh, f"systemctl --user list-units 'muscatdb-worker@{host.key}-*' --plain --no-legend --all", readonly=True
        ))
        units = sorted(set(existing) | {unit_name(host.key, p) for p in host.pipelines})
        # --force must work when the database is unreachable or renamed: skip every check
        refusal = None if force else _down_refusal(cfg, host, hostname, units, db_query)
        if refusal:
            print(f"{host.key}: {refusal}", file=sys.stderr)
            rc_total = 1
            continue
        for unit in units:
            rc, out = runner.run(host.ssh, f"systemctl --user disable --now {shlex.quote(unit)}")
            print(f"{host.key}: {unit}: {'stopped' if rc == 0 else out}")
    return rc_total


def cmd_status(cfg: Config, hosts: list[HostSpec], runner: Runner, db_query) -> int:
    rc_total = 0
    for host in hosts:
        rc, hostname = runner.run(host.ssh, "hostname", readonly=True)
        if rc != 0:
            print(f"{host.key}: UNREACHABLE ({hostname})")
            rc_total = 1  # a script must not read a partial picture as a healthy fleet
            continue
        _, linger = runner.run(host.ssh, 'loginctl show-user "$USER" -p Linger --value', readonly=True)
        print(f"{host.key} ({hostname})  linger={linger.strip() or '?'}")
        states = [installed_state(host.key, p) for p in host.pipelines]
        for pipe in host.pipelines:
            unit = unit_name(host.key, pipe)
            _, state = runner.run(host.ssh, f"systemctl --user is-active {shlex.quote(unit)}", readonly=True)
            print(f"  {pipe:<12} {state.strip() or '?'}")
        others = sorted({s.profile for s in states if s and s.profile and s.profile != cfg.profile})
        if others:
            print(f"  installed profile {', '.join(others)} but selected {cfg.profile or '(no profile)'}")
        databases = {_database_of(cfg, s) for s in states}
        print(f"  running jobs on this host: {len(_jobs_on(hostname, databases, db_query))}")
    return rc_total


def make_db_query(cfg: Config):
    """Return ``query(database=None)`` listing running job rows, via the repo venv's psycopg."""

    def query(database: str | None = None) -> list[dict]:
        database = database or cfg.database
        if not _DB_NAME.match(database):
            raise ConfigError(f"unusable database name {database!r}")
        env_path = Path(os.path.expanduser(cfg.env_file))
        dsn = ""
        for line in env_path.read_text().splitlines():
            line = line.strip().removeprefix("export ")
            if line.startswith("MUSCAT_POSTGRES_DSN="):
                dsn = line.split("=", 1)[1].strip("'\"")
        if not dsn:
            raise ConfigError(f"MUSCAT_POSTGRES_DSN not found in {env_path}")
        dsn = dsn.rsplit("/", 1)[0] + "/" + database
        code = (
            "import os,json,psycopg;"
            "c=psycopg.connect(os.environ['D'],connect_timeout=8);"
            "print(json.dumps([dict(instance_id=r[0],type=r[1]) for r in c.execute("
            "\"select instance_id,type from jobs where state='running'\")]))"
        )
        try:
            p = subprocess.run(
                [str(cfg.repo / ".venv" / "bin" / "python"), "-I", "-c", code],
                env={**os.environ, "D": dsn}, capture_output=True, text=True, timeout=DB_QUERY_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"job query timed out after {DB_QUERY_TIMEOUT_S}s") from None
        if p.returncode != 0:
            raise RuntimeError(f"job query failed: {p.stderr.strip().splitlines()[-1:]}")
        import json

        return json.loads(p.stdout)

    return query


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-c", "--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--dry-run", action="store_true", help="print what would change, touch nothing")
    ap.add_argument(
        "-p", "--profile",
        default=os.environ.get("MUSCAT_WORKERS_PROFILE"),
        help="[profiles.<name>] to apply (default: [defaults] profile, env MUSCAT_WORKERS_PROFILE)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").add_argument("hosts", nargs="*")
    u = sub.add_parser("up")
    u.add_argument("hosts", nargs="*")
    u.add_argument("--force", action="store_true", help="restart or stop workers even if they are running jobs")
    d = sub.add_parser("down")
    d.add_argument("hosts", nargs="*")
    d.add_argument("--force", action="store_true", help="stop even if jobs are running on the host")
    sub.add_parser("web-env")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config, args.profile)
        if cfg.profile and args.cmd != "web-env":
            print(f"profile {cfg.profile}: database {cfg.database}, repo {cfg.repo}", file=sys.stderr)
        if args.cmd == "web-env":
            sys.stdout.write(web_env(cfg))
            return 0
        runner = Runner(dry_run=args.dry_run)
        hosts = _select(cfg, args.hosts)
        query = functools.cache(make_db_query(cfg))  # one query per database per run
        if args.cmd == "up":
            return cmd_up(cfg, hosts, runner, query, args.force)
        if args.cmd == "down":
            return cmd_down(cfg, hosts, runner, query, args.force)
        return cmd_status(cfg, hosts, runner, query)
    except (ConfigError, OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
