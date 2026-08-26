"""Per-engine conda environment name overrides (timer/harmonic).

``MUSCAT_PROSE_CONDA_ENV`` already let prose's conda env be renamed; timer and
harmonic hardcoded ``"timer"``/``"harmonic"`` everywhere, which blocks pointing
a staging instance's engines at separate environments from production (#79).
``MUSCAT_TIMER_CONDA_ENV`` and ``MUSCAT_HARMONIC_CONDA_ENV`` give them the same
override, via the same ``_conda_env_python`` resolution prose already uses.
"""

from __future__ import annotations

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
