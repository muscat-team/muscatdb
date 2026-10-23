"""Production pins must identify an owner's exact non-editable installation."""
import copy
import importlib.util
import json
import tomllib
from pathlib import Path

import pytest

_path = Path(__file__).resolve().parents[1] / "deploy" / "engines.py"
_spec = importlib.util.spec_from_file_location("deploy_engines", _path)
engines = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(engines)


def test_accepts_pinned_install_inside_selected_environment(tmp_path):
    pin = engines.load_pins(engines.DEFAULT_CONFIG)["timer"]
    info = {
        "prefix": str(tmp_path), "module_file": str(tmp_path / "lib/timer/__init__.py"),
        "direct_url": {"url": pin["repository"],
                       "vcs_info": {"vcs": "git", "commit_id": pin["commit"]}},
    }
    engines.validate_install("timer", pin, info, tmp_path)


@pytest.mark.parametrize("defect,match", [
    ("editable", "editable"), ("fork", "repository"), ("commit", "commit"),
    ("shadow", "outside"), ("interpreter", "interpreter"),
])
def test_rejects_installations_that_could_run_unreviewed_code(tmp_path, defect, match):
    pin = engines.load_pins(engines.DEFAULT_CONFIG)["timer"]
    info = {
        "prefix": str(tmp_path), "module_file": str(tmp_path / "lib/timer/__init__.py"),
        "direct_url": {"url": pin["repository"],
                       "vcs_info": {"vcs": "git", "commit_id": pin["commit"]}},
    }
    if defect == "editable":
        info["direct_url"]["dir_info"] = {"editable": True}
    elif defect == "fork":
        info["direct_url"]["url"] = "https://github.com/somebody/timer"
    elif defect == "commit":
        info["direct_url"]["vcs_info"]["commit_id"] = "0" * 40
    elif defect == "shadow":
        info["module_file"] = str(tmp_path.parent / "checkout/timer/__init__.py")
    else:
        info["prefix"] = str(tmp_path.parent / "another-env")
    with pytest.raises(ValueError, match=match):
        engines.validate_install("timer", pin, info, tmp_path)


@pytest.mark.parametrize("key,value", [
    ("repository", "https://github.com/jpdeleon/timer"),
    ("commit", "master"), ("environment", "timer"),
])
def test_rejects_unapproved_pin_config(tmp_path, key, value):
    pin = copy.deepcopy(engines.load_pins(engines.DEFAULT_CONFIG)["timer"])
    pin[key] = value
    config = tmp_path / "pins.toml"
    config.write_text("[engines.timer]\n" + "".join(f'{k} = "{v}"\n' for k, v in pin.items()))
    with pytest.raises(ValueError):
        engines.load_pins(config)


def _write_pin(tmp_path, name, pin):
    config = tmp_path / "pins.toml"
    config.write_text(f"[engines.{name}]\n" + "".join(f'{k} = "{v}"\n' for k, v in pin.items()))
    return config


def test_harmonic_is_pinned_to_a_declared_fork_of_its_owner():
    pin = engines.load_pins(engines.DEFAULT_CONFIG)["harmonic"]
    assert pin["repository"] == "https://github.com/jpdeleon/harmonic"
    assert pin["fork_of"] == engines.OWNERS["harmonic"]
    assert pin["fork_reason"].strip()


@pytest.mark.parametrize("change", [
    {"fork_reason": ""},                                     # undeclared reason
    {"fork_of": "https://github.com/somebody/harmonic"},     # not the owner
    {"fork_of": None},                                       # fork without fork_of
])
def test_rejects_a_fork_pin_that_is_not_declared(tmp_path, change):
    pin = copy.deepcopy(engines.load_pins(engines.DEFAULT_CONFIG)["harmonic"])
    for key, value in change.items():
        if value is None:
            pin.pop(key)
        else:
            pin[key] = value
    with pytest.raises(ValueError, match="fork"):
        engines.load_pins(_write_pin(tmp_path, "harmonic", pin))


def test_validate_install_requires_the_fork_repository_for_a_fork_pin(tmp_path):
    pin = engines.load_pins(engines.DEFAULT_CONFIG)["harmonic"]
    info = {
        "prefix": str(tmp_path), "module_file": str(tmp_path / "lib/harmonic/__init__.py"),
        "direct_url": {"url": pin["fork_of"],
                       "vcs_info": {"vcs": "git", "commit_id": pin["commit"]}},
    }
    with pytest.raises(ValueError, match="repository"):
        engines.validate_install("harmonic", pin, info, tmp_path)
    info["direct_url"]["url"] = pin["repository"]
    engines.validate_install("harmonic", pin, info, tmp_path)


@pytest.mark.parametrize("loaded,match", [
    ({"read_only": False}, "read-only"),
    ({"prose_config_sha256": "0" * 64}, "does not match"),
])
def test_frozen_config_check_requires_the_engine_to_load_the_snapshot_read_only(
        tmp_path, monkeypatch, loaded, match):
    pin = engines.load_pins(engines.DEFAULT_CONFIG)["prose"]
    report = {"read_only": True, "prose_config_sha256": pin["frozen_config_sha256"],
              "telescope_aliases": 94, **loaded}
    seen = {}

    def fake_run(argv, *, env=None):
        seen["env"] = env
        return "noise from the engine\n" + json.dumps(report) + "\n"

    monkeypatch.setattr(engines, "run", fake_run)
    with pytest.raises(ValueError, match=match):
        engines.check_frozen_config(tmp_path, pin, {})
    assert seen["env"]["PROSE_CONFIG_FILE"] == str(engines.FROZEN_CONFIG)
    assert seen["env"]["PROSE_CONFIG_SHA256"] == pin["frozen_config_sha256"]


def test_frozen_config_check_records_what_the_engine_loaded(tmp_path, monkeypatch):
    pin = engines.load_pins(engines.DEFAULT_CONFIG)["prose"]
    report = {"read_only": True, "prose_config_sha256": pin["frozen_config_sha256"],
              "telescope_aliases": 94}
    monkeypatch.setattr(engines, "run", lambda argv, *, env=None: json.dumps(report))
    info = {}
    engines.check_frozen_config(tmp_path, pin, info)
    assert info["telescope_aliases"] == 94


def test_install_never_replaces_an_existing_environment(tmp_path):
    pin = engines.load_pins(engines.DEFAULT_CONFIG)["timer"]
    dest = tmp_path / pin["environment"]
    dest.mkdir()
    sentinel = dest / "running-job"
    sentinel.write_text("keep")
    with pytest.raises(ValueError, match="already exists"):
        engines.install("timer", pin, Path("/unused/conda"), tmp_path, tmp_path)
    assert sentinel.read_text() == "keep"


def test_production_workers_use_the_environments_in_the_approved_pins():
    config = tomllib.loads(_path.with_name("workers.toml").read_text())
    selected = config["profiles"]["prod"]["engine_envs"]
    pins = engines.load_pins(engines.DEFAULT_CONFIG)
    assert selected == {name: pin["environment"] for name, pin in pins.items()
                        if pin.get("rollout", True)}


def test_dependency_override_must_match_the_actual_installed_version(tmp_path):
    pin = engines.load_pins(engines.DEFAULT_CONFIG)["prose"]
    info = {
        "prefix": str(tmp_path), "module_file": str(tmp_path / "lib/prose/__init__.py"),
        "direct_url": {"url": pin["repository"],
                       "vcs_info": {"vcs": "git", "commit_id": pin["commit"]}},
        "dependencies": {"matplotlib": "3.9.3"},
    }
    with pytest.raises(ValueError, match="matplotlib"):
        engines.validate_install("prose", pin, info, tmp_path)
    info["dependencies"]["matplotlib"] = "3.8.4"
    engines.validate_install("prose", pin, info, tmp_path)
