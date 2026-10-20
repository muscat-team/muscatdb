#!/usr/bin/env python3
"""Audit, provision and verify non-editable science engines (#101).

Run with ``uv run python deploy/engines.py audit|install|verify [ENGINE ...]``.
Install clones the working dependency environment, replaces only the engine
with its owner's pinned commit, and records conda/pip dependencies next to it.
It never overwrites an existing environment or changes a running service.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

OWNERS = {
    "prose": "https://github.com/jpdeleon/prose2",
    "timer": "https://github.com/john-livingston/timer",
    "harmonic": "https://github.com/john-livingston/harmonic",
}
DEFAULT_CONFIG = Path(__file__).with_name("engines.toml")
FROZEN_CONFIG = Path(__file__).resolve().parents[1] / "src/muscat_db/_prose_telescopes.yaml"
PROSE_LAUNCHER = FROZEN_CONFIG.with_name("_prose_launcher.py")
# Metadata and find_spec avoid importing engines (imports may create config
# files, download reference data, or initialize numerical runtimes).
PROBE = """
import importlib.metadata as m, importlib.util, json, sys
d = m.distribution(sys.argv[1])
s = importlib.util.find_spec(sys.argv[2])
print(json.dumps(dict(version=d.version, python=sys.version,
    prefix=sys.prefix, executable=sys.executable,
    module_file=s.origin if s else None,
    dependencies={name: m.version(name) for name in json.loads(sys.argv[3])},
    direct_url=json.loads(d.read_text('direct_url.json') or '{}'))))
"""


def load_pins(path: Path) -> dict:
    pins = tomllib.loads(path.read_text())["engines"]
    for name, pin in pins.items():
        if name not in OWNERS or pin["repository"].removesuffix(".git") != OWNERS[name]:
            raise ValueError(f"{name}: repository must be the engine owner's repository")
        if not re.fullmatch(r"[0-9a-f]{40}", pin["commit"]):
            raise ValueError(f"{name}: commit must be a full SHA")
        for key in ("environment", "source_environment"):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", pin[key]):
                raise ValueError(f"{name}: invalid {key}")
        if pin["environment"] == pin["source_environment"]:
            raise ValueError(f"{name}: production and development environments must differ")
    return pins


def run(argv: list[str], *, env: dict | None = None) -> str:
    return subprocess.run(argv, check=True, capture_output=True, text=True,
                          timeout=1800, env=env).stdout


def probe(prefix: Path, name: str, module: str, dependencies: dict | None = None) -> dict:
    # -I removes cwd, PYTHONPATH and user site-packages from the audit.
    return json.loads(run([str(prefix / "bin/python"), "-I", "-c", PROBE, name, module,
                           json.dumps(list(dependencies or {}))]))


def validate_install(name: str, pin: dict, info: dict, prefix: Path) -> None:
    direct = info["direct_url"]
    vcs = direct.get("vcs_info", {})
    if direct.get("dir_info", {}).get("editable"):
        raise ValueError(f"{name}: editable install is not allowed in production")
    if direct.get("url", "").removesuffix(".git") != pin["repository"].removesuffix(".git"):
        raise ValueError(f"{name}: installed repository does not match the pin")
    if vcs.get("vcs") != "git" or vcs.get("commit_id") != pin["commit"]:
        raise ValueError(f"{name}: installed commit does not match the pin")
    if Path(info["prefix"]).resolve() != prefix.resolve():
        raise ValueError(f"{name}: interpreter belongs to another environment")
    module_file = info.get("module_file")
    if not module_file or not Path(module_file).resolve().is_relative_to(prefix.resolve()):
        raise ValueError(f"{name}: imported module is outside the production environment")
    for package, version in pin.get("dependency_overrides", {}).items():
        if info.get("dependencies", {}).get(package) != version:
            raise ValueError(f"{name}: {package} does not match pinned version {version}")


def check_frozen_config(prefix: Path, pin: dict, info: dict) -> None:
    output = run([str(prefix / "bin/python"), "-I", str(PROSE_LAUNCHER), "--check"])
    config = json.loads(output.splitlines()[-1])
    if config["prose_config_sha256"] != pin["frozen_config_sha256"]:
        raise ValueError("prose: frozen configuration does not match the deployment pin")
    info.update(config)


def write_record(name: str, pin: dict, info: dict, conda: Path, dest: Path) -> None:
    record = {"engine": name, "pin": pin, "installed": info,
              "conda_explicit": run([str(conda), "list", "--prefix", str(dest), "--explicit"]),
              "pip_freeze": run([str(dest / "bin/python"), "-I", "-m", "pip", "freeze", "--all"])}
    (dest / "muscat-engine.json").write_text(json.dumps(record, indent=2) + "\n")


def capture_config(python: Path, pin: dict, destination: Path) -> None:
    """Capture resolved aliases, preserving YAML tuple types and shared entries."""
    if destination.exists():
        raise ValueError(f"{destination} exists; review a new snapshot before replacing it")
    code = """
import base64, hashlib, json, subprocess, yaml, prose
from pathlib import Path
from importlib.metadata import version, distribution
direct = json.loads(distribution('prose').read_text('direct_url.json') or '{}')
if direct.get('dir_info', {}).get('editable'):
    source = str(Path(prose.__file__).parent)
    head = subprocess.check_output(['git', '-C', source, 'rev-parse', 'HEAD'], text=True).strip()
    if head != COMMIT or subprocess.run(['git', '-C', source, 'diff', '--quiet', 'HEAD', '--', '.']).returncode:
        raise RuntimeError('capture requires clean prose code at the approved commit')
elif direct.get('vcs_info', {}).get('commit_id') != COMMIT:
    raise RuntimeError('capture requires the approved prose commit')
record = dict(schema=1, settings=prose.CONFIG.config,
    telescopes=prose.CONFIG.telescopes_dict,
    files={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
           for p in sorted(prose.CONFIG.folder_path.glob('*.telescope'))})
record['engine_version'] = version('prose')
record['engine_commit'] = COMMIT
record['repository'] = REPOSITORY
payload = yaml.dump(record, sort_keys=True).encode()
print('MUSCAT_CONFIG:' + base64.b64encode(payload).decode())
""".replace("COMMIT", repr(pin["commit"])).replace("REPOSITORY", repr(pin["repository"]))
    output = run([str(python), "-I", "-c", code])
    encoded = next(line.removeprefix("MUSCAT_CONFIG:") for line in output.splitlines()
                   if line.startswith("MUSCAT_CONFIG:"))
    data = base64.b64decode(encoded, validate=True)
    destination.write_bytes(data)
    print(f"{destination}: sha256 {hashlib.sha256(data).hexdigest()}")


def install(name: str, pin: dict, conda: Path, root: Path, scratch: Path) -> None:
    dest = root / pin["environment"]
    if dest.exists():
        raise ValueError(f"{dest} already exists; verify it or choose a new environment name")
    source = root / pin["source_environment"]
    # Check the source before making any new environment.
    probe(source, name, pin["module"])
    env = {**os.environ, "TMPDIR": str(scratch), "PYTHONNOUSERSITE": "1"}
    print(f"{name}: cloning dependencies into {dest}", flush=True)
    run([str(conda), "create", "--yes", "--prefix", str(dest),
         "--clone", str(source)], env=env)
    python = str(dest / "bin/python")
    run([python, "-I", "-m", "pip", "uninstall", "--yes", name], env=env)
    run([python, "-I", "-m", "pip", "install", "--no-deps",
         f"git+{pin['repository']}@{pin['commit']}"], env=env)
    overrides = pin.get("dependency_overrides", {})
    if overrides:
        run([python, "-I", "-m", "pip", "install", "--no-deps",
             *(f"{package}=={version}" for package, version in overrides.items())], env=env)
    info = probe(dest, name, pin["module"], overrides)
    validate_install(name, pin, info, dest)
    if name == "prose":
        check_frozen_config(dest, pin, info)
    run([python, "-I", "-m", "pip", "check"], env=env)
    write_record(name, pin, info, conda, dest)
    print(f"{name}: verified {pin['commit']}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("audit", "install", "verify", "capture-config"))
    parser.add_argument("engines", nargs="*")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--conda", type=Path, default=Path.home() / "miniconda3/bin/conda")
    parser.add_argument("--scratch", type=Path, default=Path.home() / "temp")
    parser.add_argument("--record", action="store_true",
                        help="after successful verify, save the environment dependency record")
    parser.add_argument("--snapshot", type=Path, default=FROZEN_CONFIG)
    args = parser.parse_args(argv)
    try:
        pins = load_pins(args.config)
        selected = args.engines or list(pins)
        unknown = set(selected) - set(pins)
        if unknown:
            raise ValueError(f"engines without approved pins: {sorted(unknown)}")
        root = args.conda.parent.parent / "envs"
        if args.command == "capture-config":
            capture_config(root / pins["prose"]["source_environment"] / "bin/python",
                           pins["prose"], args.snapshot)
            return 0
        for name in selected:
            pin = pins[name]
            if args.command == "install":
                args.scratch.mkdir(parents=True, exist_ok=True)
                install(name, pin, args.conda, root, args.scratch)
            else:
                env_name = pin["source_environment"] if args.command == "audit" else pin["environment"]
                prefix = root / env_name
                info = probe(prefix, name, pin["module"], pin.get("dependency_overrides"))
                if args.command == "verify":
                    validate_install(name, pin, info, prefix)
                    if name == "prose":
                        check_frozen_config(prefix, pin, info)
                    run([str(prefix / "bin/python"), "-I", "-m", "pip", "check"])
                    if args.record:
                        write_record(name, pin, info, args.conda, prefix)
                print(json.dumps({"engine": name, "environment": env_name, **info}, indent=2))
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print(f"engine {args.command} failed: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError):
            print(exc.stderr or exc.stdout, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
