"""Per-engine conda environment name overrides (timer/harmonic).

``MUSCAT_PROSE_CONDA_ENV`` already let prose's conda env be renamed; timer and
harmonic hardcoded ``"timer"``/``"harmonic"`` everywhere, which blocks pointing
a staging instance's engines at separate environments from production (#79).
``MUSCAT_TIMER_CONDA_ENV`` and ``MUSCAT_HARMONIC_CONDA_ENV`` give them the same
override, via the same ``_conda_env_python`` resolution prose already uses.
"""

from __future__ import annotations

import pytest

from muscat_db import photometry as phot
from muscat_db import transit_fit
from muscat_db import ttv_fit


def _make_conda_env(base, env_name):
    envpy = base / "envs" / env_name / "bin" / "python"
    envpy.parent.mkdir(parents=True)
    envpy.write_text("")
    envpy.chmod(0o755)
    return envpy


class TestCondaEnvGetters:
    def test_timer_conda_env_defaults_to_timer(self, monkeypatch):
        monkeypatch.delenv("MUSCAT_TIMER_CONDA_ENV", raising=False)
        assert phot.timer_conda_env() == "timer"

    def test_timer_conda_env_honors_override(self, monkeypatch):
        monkeypatch.setenv("MUSCAT_TIMER_CONDA_ENV", "timer-staging")
        assert phot.timer_conda_env() == "timer-staging"

    def test_harmonic_conda_env_defaults_to_harmonic(self, monkeypatch):
        monkeypatch.delenv("MUSCAT_HARMONIC_CONDA_ENV", raising=False)
        assert phot.harmonic_conda_env() == "harmonic"

    def test_harmonic_conda_env_honors_override(self, monkeypatch):
        monkeypatch.setenv("MUSCAT_HARMONIC_CONDA_ENV", "harmonic-staging")
        assert phot.harmonic_conda_env() == "harmonic-staging"


class TestTimerPrefixUsesOverride:
    def test_resolves_the_overridden_env_name_over_the_default(self, monkeypatch, tmp_path):
        # Both "timer" and "timer-staging" exist; the override must win.
        base = tmp_path / "miniconda3"
        _make_conda_env(base, "timer")
        staging_py = _make_conda_env(base, "timer-staging")
        monkeypatch.setenv("CONDA_EXE", str(base / "bin" / "conda"))
        monkeypatch.setenv("MUSCAT_TIMER_CONDA_ENV", "timer-staging")

        cmd = transit_fit._timer_prefix()

        assert cmd[0] == str(staging_py)

    def test_falls_back_to_the_default_env_name_without_override(self, monkeypatch, tmp_path):
        base = tmp_path / "miniconda3"
        default_py = _make_conda_env(base, "timer")
        monkeypatch.delenv("MUSCAT_TIMER_CONDA_ENV", raising=False)
        monkeypatch.setenv("CONDA_EXE", str(base / "bin" / "conda"))

        cmd = transit_fit._timer_prefix()

        assert cmd[0] == str(default_py)


class TestHarmonicPrefixUsesOverride:
    def test_resolves_the_overridden_env_name_over_the_default(self, monkeypatch, tmp_path):
        base = tmp_path / "miniconda3"
        _make_conda_env(base, "harmonic")
        staging_py = _make_conda_env(base, "harmonic-staging")
        monkeypatch.setenv("CONDA_EXE", str(base / "bin" / "conda"))
        monkeypatch.setenv("MUSCAT_HARMONIC_CONDA_ENV", "harmonic-staging")

        cmd = ttv_fit._harmonic_prefix()

        assert cmd[0] == str(staging_py)

    def test_falls_back_to_the_default_env_name_without_override(self, monkeypatch, tmp_path):
        base = tmp_path / "miniconda3"
        default_py = _make_conda_env(base, "harmonic")
        monkeypatch.delenv("MUSCAT_HARMONIC_CONDA_ENV", raising=False)
        monkeypatch.setenv("CONDA_EXE", str(base / "bin" / "conda"))

        cmd = ttv_fit._harmonic_prefix()

        assert cmd[0] == str(default_py)


_PREFIXES = {
    "prose": lambda: phot._prose_prefix(),
    "timer": lambda: transit_fit._timer_prefix(),
    "harmonic": lambda: ttv_fit._harmonic_prefix(),
}


class TestSelectedEnvNeverFallsBack:
    """A selected (pinned) env that is missing must not fall back (#101).

    The production profile selects each engine's pinned env through
    ``MUSCAT_<ENGINE>_CONDA_ENV``. Falling back to ``conda run``, a ``PATH``
    executable or the checkout would silently run unpinned engine code.
    """

    @pytest.mark.parametrize("engine", sorted(_PREFIXES))
    def test_missing_selected_env_raises(self, monkeypatch, tmp_path, engine):
        base = tmp_path / "miniconda3"
        _make_conda_env(base, engine)  # the dev env exists, the pinned one does not
        monkeypatch.setenv("CONDA_EXE", str(base / "bin" / "conda"))
        monkeypatch.setattr(phot.Path, "home", lambda: tmp_path / "nohome")
        monkeypatch.setenv(f"MUSCAT_{engine.upper()}_CONDA_ENV", f"{engine}-prod-0000000")
        monkeypatch.delenv("MUSCAT_PROSE_PYTHON", raising=False)
        monkeypatch.delenv("MUSCAT_PROSE_FROZEN_CONFIG", raising=False)

        with pytest.raises(RuntimeError, match=f"MUSCAT_{engine.upper()}_CONDA_ENV"):
            _PREFIXES[engine]()

    @pytest.mark.parametrize("engine", sorted(_PREFIXES))
    def test_missing_default_env_still_falls_back(self, monkeypatch, tmp_path, engine):
        """Development without the conda env keeps its existing fallbacks."""
        monkeypatch.setenv("CONDA_EXE", str(tmp_path / "none" / "bin" / "conda"))
        monkeypatch.setattr(phot.Path, "home", lambda: tmp_path / "nohome")
        monkeypatch.delenv(f"MUSCAT_{engine.upper()}_CONDA_ENV", raising=False)
        monkeypatch.delenv("MUSCAT_PROSE_PYTHON", raising=False)
        monkeypatch.delenv("MUSCAT_PROSE_FROZEN_CONFIG", raising=False)

        assert _PREFIXES[engine]()
