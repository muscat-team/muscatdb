#!/usr/bin/env python3
"""Bring muscat-db job workers up and down on other hosts from one config file.

    .venv/bin/python deploy/workers.py [-c deploy/workers.toml] [--dry-run] up   [HOST ...]
    .venv/bin/python deploy/workers.py ...                                  down [--force] [HOST ...]
    .venv/bin/python deploy/workers.py ...                                  status [HOST ...]
    eval "$(.venv/bin/python deploy/workers.py web-env)"      # before starting the web app

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

Needs on each host: ``loginctl enable-linger <user>`` so user units survive the
last logout (``status`` reports it; ``up`` only warns).
"""

from __future__ import annotations

import argparse
import os
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
    hosts: dict[str, HostSpec] = field(default_factory=dict)


def _int(value, where: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{where} must be an integer >= {minimum}, got {value!r}")
    return value


def parse_config(data: dict) -> Config:
    d = data.get("defaults") or {}
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
        queue_only_web=bool((data.get("web") or {}).get("queue_only", False)),
        stagger_s=float(d.get("stagger_s", 3)),
        max_full_jobs=(
            _int(d["max_full_jobs"], "[defaults] max_full_jobs", 1) if "max_full_jobs" in d else None
        ),
        hosts=hosts,
    )


def load_config(path: Path) -> Config:
    with open(path, "rb") as f:
        return parse_config(tomllib.load(f))


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
    return "".join(f"{k}={v}\n" for k, v in lines.items())


def web_env(cfg: Config) -> str:
    out = []
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
        p = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", ssh, "bash", "-s"],
            input=_XDG + script, capture_output=True, text=True,
        )
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


def running_jobs(host_name: str, db_query) -> int:
    """Number of ``running`` job rows whose instance_id belongs to *host_name*."""
    return sum(1 for r in db_query() if str(r.get("instance_id", "")).startswith(f"{host_name}:"))


# ------------------------------------------------------------------- commands


def cmd_up(cfg: Config, hosts: list[HostSpec], runner: Runner) -> int:
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
        for pipe in host.pipelines:
            env_path = conf / "muscatdb" / "workers" / f"{instance(host.key, pipe)}.env"
            if runner.write(env_path, render_env(cfg, host, hostname)):
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
            runner.run(host.ssh, f"systemctl --user disable --now {shlex.quote(stale)}")
            print(f"{host.key}: stopped {stale} (no longer in config)")
        runner.run(host.ssh, "systemctl --user daemon-reload")
        for i, pipe in enumerate(host.pipelines):
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


def cmd_down(cfg: Config, hosts: list[HostSpec], runner: Runner, db_query, force: bool) -> int:
    rc_total = 0
    for host in hosts:
        rc, hostname = runner.run(host.ssh, "hostname", readonly=True)
        if rc != 0:
            print(f"{host.key}: cannot reach {host.ssh}: {hostname}", file=sys.stderr)
            rc_total = 1
            continue
        busy = running_jobs(hostname, db_query)
        if busy and not force:
            print(
                f"{host.key}: {busy} job(s) running on {hostname}; stopping kills them "
                f"(they are requeued by orphan reclaim). Re-run with --force.",
                file=sys.stderr,
            )
            rc_total = 1
            continue
        existing = _listed(runner.run(
            host.ssh, f"systemctl --user list-units 'muscatdb-worker@{host.key}-*' --plain --no-legend --all", readonly=True
        ))
        units = sorted(set(existing) | {unit_name(host.key, p) for p in host.pipelines})
        for unit in units:
            rc, out = runner.run(host.ssh, f"systemctl --user disable --now {shlex.quote(unit)}")
            print(f"{host.key}: {unit}: {'stopped' if rc == 0 else out}")
    return rc_total


def cmd_status(cfg: Config, hosts: list[HostSpec], runner: Runner, db_query) -> int:
    rows = db_query()
    for host in hosts:
        rc, hostname = runner.run(host.ssh, "hostname", readonly=True)
        if rc != 0:
            print(f"{host.key}: UNREACHABLE ({hostname})")
            continue
        _, linger = runner.run(host.ssh, 'loginctl show-user "$USER" -p Linger --value', readonly=True)
        print(f"{host.key} ({hostname})  linger={linger.strip() or '?'}")
        for pipe in host.pipelines:
            unit = unit_name(host.key, pipe)
            _, state = runner.run(host.ssh, f"systemctl --user is-active {shlex.quote(unit)}", readonly=True)
            print(f"  {pipe:<12} {state.strip() or '?'}")
        mine = [r for r in rows if str(r.get("instance_id", "")).startswith(f"{hostname}:")]
        print(f"  running jobs on this host: {len(mine)}")
    return 0


def make_db_query(cfg: Config):
    """Return a callable listing running job rows, via the repo venv's psycopg."""

    def query() -> list[dict]:
        env_path = Path(os.path.expanduser(cfg.env_file))
        dsn = ""
        for line in env_path.read_text().splitlines():
            line = line.strip().removeprefix("export ")
            if line.startswith("MUSCAT_POSTGRES_DSN="):
                dsn = line.split("=", 1)[1].strip("'\"")
        if not dsn:
            raise ConfigError(f"MUSCAT_POSTGRES_DSN not found in {env_path}")
        dsn = dsn.rsplit("/", 1)[0] + "/" + cfg.database
        code = (
            "import os,json,psycopg;"
            "c=psycopg.connect(os.environ['D'],connect_timeout=8);"
            "print(json.dumps([dict(instance_id=r[0],type=r[1]) for r in c.execute("
            "\"select instance_id,type from jobs where state='running'\")]))"
        )
        p = subprocess.run(
            [str(cfg.repo / ".venv" / "bin" / "python"), "-I", "-c", code],
            env={**os.environ, "D": dsn}, capture_output=True, text=True,
        )
        if p.returncode != 0:
            raise RuntimeError(f"job query failed: {p.stderr.strip().splitlines()[-1:]}")
        import json

        return json.loads(p.stdout)

    return query


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-c", "--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--dry-run", action="store_true", help="print what would change, touch nothing")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("up", "status"):
        sub.add_parser(name).add_argument("hosts", nargs="*")
    d = sub.add_parser("down")
    d.add_argument("hosts", nargs="*")
    d.add_argument("--force", action="store_true", help="stop even if jobs are running on the host")
    sub.add_parser("web-env")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.cmd == "web-env":
            sys.stdout.write(web_env(cfg))
            return 0
        runner = Runner(dry_run=args.dry_run)
        hosts = _select(cfg, args.hosts)
        if args.cmd == "up":
            return cmd_up(cfg, hosts, runner)
        query = make_db_query(cfg)
        if args.cmd == "down":
            return cmd_down(cfg, hosts, runner, query, args.force)
        return cmd_status(cfg, hosts, runner, query)
    except (ConfigError, OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
