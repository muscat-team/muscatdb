"""Guard the contract config.py states in its own docstring.

``config.ENV_VARS`` claims to list every environment variable the pipeline
consults. Nothing enforced that, so the registry silently fell behind the code
and stopped being the "one place to look when wiring up a new machine" it
advertises. This test fails when a new ``os.environ`` read is added without a
matching registry entry.
"""

from __future__ import annotations

import re
from pathlib import Path

from muscat_db.config import ENV_VARS, EnvVar, config_status, resolved_value

SRC = Path(__file__).resolve().parent.parent / "src" / "muscat_db"

# os.environ["X"], os.environ.get("X"), os.environ.get("X", default)
_ENV_READ = re.compile(r'os\.environ(?:\.get)?[\(\[]\s*["\']([A-Z][A-Z0-9_]*)["\']')

# Variables read only to *pass through* to a subprocess or set by the OS, which
# are not muscat-db configuration knobs and so are deliberately unregistered.
_NOT_CONFIGURATION = {
    "PATH",
    "PYTHONPATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "PWD",
}


def _env_reads() -> dict[str, set[str]]:
    """Every env var name read under src/muscat_db, mapped to the files reading it."""
    found: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "config.py":  # the registry itself
            continue
        for match in _ENV_READ.finditer(path.read_text(encoding="utf-8")):
            found.setdefault(match.group(1), set()).add(path.name)
    return found


def test_every_env_read_is_registered():
    registered = {var.name for var in ENV_VARS}
    reads = _env_reads()
    missing = {
        name: sorted(files)
        for name, files in reads.items()
        if name not in registered and name not in _NOT_CONFIGURATION
    }
    assert not missing, (
        "environment variables read in code but absent from config.ENV_VARS "
        f"(add them so the registry stays the single source of truth): {missing}"
    )


def test_registry_has_no_duplicate_names():
    names = [var.name for var in ENV_VARS]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert not duplicates, f"duplicate entries in config.ENV_VARS: {duplicates}"


def test_secrets_are_flagged_as_secret():
    """A credential must be marked secret so the status report never prints it.

    ``*_FILE`` names are paths *to* secret material, not the material itself, so
    the path is safe to display and is exempt.
    """
    for var in ENV_VARS:
        if var.name.endswith("_FILE"):
            continue
        if re.search(r"TOKEN|SECRET|_KEY$|PASSWORD", var.name):
            assert var.secret, f"{var.name} looks like a credential but secret=False"


def test_resolved_value_prefers_env_override(monkeypatch):
    var = EnvVar("MUSCAT_TEST_PATH", "/default/path", "test var")
    monkeypatch.setenv("MUSCAT_TEST_PATH", "/pinned/path")
    assert resolved_value(var) == "/pinned/path"


def test_resolved_value_falls_back_to_default_when_unset(monkeypatch):
    var = EnvVar("MUSCAT_TEST_PATH", "/default/path", "test var")
    monkeypatch.delenv("MUSCAT_TEST_PATH", raising=False)
    assert resolved_value(var) == "/default/path"


def test_config_status_shows_the_silent_default_a_shared_path_falls_back_to(monkeypatch):
    """A shared-input path (e.g. MUSCAT_OBSLOG_DIR) that falls back to its
    $HOME-derived default must be visible in the startup report -- this is the
    exact failure mode from issue #71, where two accounts silently resolved
    different obslog trees with no error and nothing in the output to say
    which one was chosen."""
    monkeypatch.delenv("MUSCAT_OBSLOG_DIR", raising=False)
    status = {name: (state, value) for name, state, value in config_status()}
    state, value = status["MUSCAT_OBSLOG_DIR"]
    assert state == "default"
    assert value is not None


def test_config_status_redacts_secret_values(monkeypatch):
    monkeypatch.setenv("LCO_API_TOKEN", "super-secret-token")
    status = {name: (state, value) for name, state, value in config_status()}
    state, value = status["LCO_API_TOKEN"]
    assert state == "set"
    assert value is None
