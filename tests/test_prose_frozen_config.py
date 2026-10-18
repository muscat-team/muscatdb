"""Frozen configuration must win before the engine reads development files."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from muscat_db import _prose_launcher as launcher, photometry


def test_production_prefix_uses_adapter_for_photometry_and_postprocess(monkeypatch):
    monkeypatch.setenv("MUSCAT_PROSE_FROZEN_CONFIG", "1")
    monkeypatch.delenv("MUSCAT_PROSE_PYTHON", raising=False)
    monkeypatch.setattr(photometry, "_conda_env_python", lambda env: "/pinned/bin/python")
    for module in (photometry._MODULE, photometry._POSTPROCESS_MODULE):
        assert photometry._prose_prefix(module) == [
            "/pinned/bin/python", "-I", str(Path(launcher.__file__)), module,
        ]


def test_frozen_configuration_refuses_a_missing_engine(monkeypatch):
    monkeypatch.setenv("MUSCAT_PROSE_FROZEN_CONFIG", "1")
    monkeypatch.delenv("MUSCAT_PROSE_PYTHON", raising=False)
    monkeypatch.setattr(photometry, "_conda_env_python", lambda env: None)
    with pytest.raises(RuntimeError, match="frozen"):
        photometry._prose_prefix()


def test_checksum_rejects_modified_telescope_snapshot(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot.yaml"
    snapshot.write_bytes(b"original")
    monkeypatch.setattr(launcher, "SNAPSHOT", snapshot)
    monkeypatch.setattr(launcher, "SNAPSHOT_SHA256", hashlib.sha256(b"original").hexdigest())
    assert launcher.snapshot_bytes() == b"original"
    snapshot.write_bytes(b"modified")
    with pytest.raises(RuntimeError, match="checksum"):
        launcher.snapshot_bytes()


def test_checked_in_snapshot_matches_its_digest_and_deployment_pin():
    import tomllib
    config = Path(launcher.__file__).resolve().parents[2] / "deploy/engines.toml"
    pin = tomllib.loads(config.read_text())["engines"]["prose"]
    assert hashlib.sha256(launcher.snapshot_bytes()).hexdigest() == pin["frozen_config_sha256"]


def test_launcher_rejects_an_engine_from_another_repository(monkeypatch):
    monkeypatch.setattr(launcher.importlib.metadata, "distribution", lambda name:
        SimpleNamespace(read_text=lambda filename: json.dumps({
            "url": "https://github.com/another-owner/prose2",
            "vcs_info": {"commit_id": "6029ea8777121bd4ba4de9248735744a101ab425"},
        })))
    with pytest.raises(RuntimeError, match="matching pinned prose"):
        launcher.main(["--check"])


def test_engine_never_initializes_development_config_and_fork_workers_inherit_snapshot(tmp_path):
    fake = tmp_path / "prose"
    fake.mkdir()
    (fake / "__init__.py").write_text("from .config import ConfigManager\nCONFIG = ConfigManager()\n")
    (fake / "config.py").write_text(
        "class ConfigManager:\n"
        "    def __init__(self):\n"
        "        raise RuntimeError('development configuration accessed')\n"
    )
    setup = f"import sys; sys.path.insert(0, {str(tmp_path)!r})\n"
    baseline = subprocess.run([sys.executable, "-c", setup + "import prose"],
                              text=True, capture_output=True, timeout=10)
    assert baseline.returncode != 0 and "development configuration accessed" in baseline.stderr
    code = setup + f"""
import json, runpy, multiprocessing
adapter = runpy.run_path({launcher.__file__!r})
adapter['install_config'](dict(settings={{'color': 'blue'}}, telescopes={{'camera': {{'gain': 17}}}}))
import prose
assert prose.CONFIG.telescopes_dict['camera']['gain'] == 17
try:
    prose.CONFIG.save()
except RuntimeError:
    pass
else:
    raise AssertionError('frozen configuration could be overwritten')
def read_gain(_):
    import prose
    return prose.CONFIG.telescopes_dict['camera']['gain']
with multiprocessing.get_context('fork').Pool(1) as pool:
    print(json.dumps(pool.map(read_gain, [0])))
"""
    result = subprocess.run([sys.executable, "-c", code],
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [17]
