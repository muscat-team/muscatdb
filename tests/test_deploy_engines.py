"""Production pins must identify an owner's exact non-editable installation."""
import copy
import importlib.util
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
