"""Required browser coverage must fail when its Python dependency is absent."""

import builtins
import runpy
from pathlib import Path

import pytest


@pytest.mark.parametrize("required", [False, True])
def test_missing_playwright_is_an_error_only_in_required_mode(monkeypatch, required):
    original_import = builtins.__import__

    def without_playwright(name, *args, **kwargs):
        if name == "playwright" or name.startswith("playwright."):
            raise ModuleNotFoundError("No module named 'playwright'", name="playwright")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_playwright)
    monkeypatch.setenv("MUSCAT_REQUIRE_BROWSER", "1" if required else "0")
    outcome = ModuleNotFoundError if required else pytest.skip.Exception
    with pytest.raises(outcome):
        runpy.run_path(str(Path(__file__).with_name("test_jobs_filter_chips_browser.py")))
