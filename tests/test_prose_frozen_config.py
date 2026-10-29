"""Frozen configuration must win before the engine reads development files.

prose2 loads an explicit schema-1 file read-only from PROSE_CONFIG_FILE
(jpdeleon/prose2#16), so production launches the pinned engine directly with
that variable set instead of through an import adapter (#101).
"""
import hashlib
import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from muscat_db import photometry, prose_config

_PINNED = "3.4.0+git." + "a" * 40


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setenv("MUSCAT_PROSE_FROZEN_CONFIG", "1")
    monkeypatch.delenv("MUSCAT_PROSE_PYTHON", raising=False)
    monkeypatch.setattr(photometry, "_conda_env_python", lambda env: "/pinned/bin/python")
    monkeypatch.setattr(photometry, "prose_version", lambda: _PINNED)


def test_production_prefix_runs_the_pinned_engine_isolated(frozen):
    for module in (photometry._MODULE, photometry._POSTPROCESS_MODULE):
        assert photometry._prose_prefix(module) == ["/pinned/bin/python", "-I", "-m", module]


def test_frozen_job_env_selects_the_packaged_snapshot(frozen):
    env = photometry._job_env()
    assert env["PROSE_CONFIG_FILE"] == str(prose_config.SNAPSHOT)
    assert env["PROSE_CONFIG_SHA256"] == prose_config.SNAPSHOT_SHA256
    assert Path(env["PROSE_CONFIG_FILE"]).is_file()


def test_frozen_run_log_records_the_snapshot_digest(frozen):
    import io
    log = io.StringIO()
    photometry._write_log_header(log, ["photometry", "--inst", "x"])
    assert log.getvalue() == (
        "$ photometry --inst x\n\n"
        f"MuSCAT prose telescope snapshot sha256: {prose_config.SNAPSHOT_SHA256}\n"
    )


def test_development_job_env_leaves_prose_configuration_alone(monkeypatch):
    monkeypatch.setenv("MUSCAT_PROSE_FROZEN_CONFIG", "0")
    monkeypatch.delenv("PROSE_CONFIG_FILE", raising=False)
    assert "PROSE_CONFIG_FILE" not in photometry._job_env()


def test_frozen_configuration_refuses_a_missing_engine(frozen, monkeypatch):
    monkeypatch.setattr(photometry, "_conda_env_python", lambda env: None)
    with pytest.raises(RuntimeError, match="frozen"):
        photometry._prose_prefix()


def test_frozen_configuration_refuses_an_explicit_interpreter(frozen, monkeypatch):
    """MUSCAT_PROSE_PYTHON would bypass the selected pinned environment."""
    monkeypatch.setenv("MUSCAT_PROSE_PYTHON", "/somewhere/python")
    with pytest.raises(RuntimeError, match="MUSCAT_PROSE_PYTHON"):
        photometry._prose_prefix()


@pytest.mark.parametrize("version", [
    "3.4.0", "unknown", "3.4.0+git." + "a" * 40 + ".editable",
])
def test_frozen_configuration_requires_a_pinned_engine_install(frozen, monkeypatch, version):
    monkeypatch.setattr(photometry, "prose_version", lambda: version)
    with pytest.raises(RuntimeError, match="pinned"):
        photometry._prose_prefix()


def test_checksum_rejects_modified_telescope_snapshot(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot.yaml"
    snapshot.write_bytes(b"original")
    monkeypatch.setattr(prose_config, "SNAPSHOT", snapshot)
    monkeypatch.setattr(prose_config, "SNAPSHOT_SHA256", hashlib.sha256(b"original").hexdigest())
    assert prose_config.snapshot_bytes() == b"original"
    snapshot.write_bytes(b"modified")
    with pytest.raises(RuntimeError, match="checksum"):
        prose_config.snapshot_bytes()
    with pytest.raises(RuntimeError, match="checksum"):
        prose_config.frozen_env()


def test_checked_in_snapshot_matches_its_digest_and_deployment_pin():
    root = Path(prose_config.__file__).resolve().parents[2]
    pin = tomllib.loads((root / "deploy/engines.toml").read_text())["engines"]["prose"]
    data = prose_config.snapshot_bytes()
    assert hashlib.sha256(data).hexdigest() == pin["frozen_config_sha256"]
    snapshot = yaml.load(data, Loader=yaml.FullLoader)
    assert snapshot["engine_commit"] == pin["commit"]
    assert snapshot["repository"] == pin["repository"]
    workers = tomllib.loads((root / "deploy/workers.toml").read_text())
    assert workers["profiles"]["prod"]["engine_envs"]["prose"] == pin["environment"]
    record = json.loads((root / "deploy/conda/production/prose.json").read_text())
    assert record["pin"] == pin
    assert record["installed"]["version"] == snapshot["engine_version"]
    assert record["installed"]["direct_url"]["vcs_info"]["commit_id"] == pin["commit"]
    assert record["installed"]["prose_config_sha256"] == pin["frozen_config_sha256"]
    assert record["installed"]["telescope_aliases"] == len(snapshot["telescopes"])


def test_frozen_snapshot_keeps_muscat_camera_aliases():
    snapshot = yaml.load(prose_config.snapshot_bytes(), Loader=yaml.FullLoader)
    telescopes = snapshot["telescopes"]
    for camera in range(2, 10):
        instrument = "MuSCAT3" if camera <= 5 else "MuSCAT4"
        assert instrument in telescopes[f"ep{camera:02d}"]["name"]


@pytest.mark.slow
def test_real_engine_loads_the_snapshot_read_only_without_touching_home(tmp_path):
    """The dev prose env (a prose2 checkout with #16) honours the frozen file."""
    python = photometry._conda_env_python("prose")
    if python is None:
        pytest.skip("prose conda env unavailable")
    home = tmp_path / "home"
    home.mkdir()
    code = ("import json, prose; c = prose.CONFIG; print(json.dumps("
            "[c.read_only, c.config_sha256, len(c.telescopes_dict)]))")
    env = {**os.environ, "HOME": str(home), **prose_config.frozen_env()}
    out = subprocess.run([python, "-I", "-c", code], env=env, capture_output=True,
                         text=True, timeout=300, check=True).stdout.strip().splitlines()[-1]
    snapshot = yaml.load(prose_config.snapshot_bytes(), Loader=yaml.FullLoader)
    assert json.loads(out) == [True, prose_config.SNAPSHOT_SHA256, len(snapshot["telescopes"])]
    assert not (home / ".prose").exists()
