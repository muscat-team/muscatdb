"""scripts/ci_pytest.py: the CI split into non-browser and browser invocations (#365)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ci_pytest.py"


@pytest.fixture
def ci():
    spec = importlib.util.spec_from_file_location("ci_pytest", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_browser_paths_name_the_directory_and_every_browser_file(ci):
    paths = ci.browser_paths()
    assert paths[0] == "tests/browser"
    on_disk = sorted(str(p.relative_to(ci.REPO)) for p in ci.REPO.glob("tests/*_browser.py"))
    assert paths[1:] == on_disk and on_disk


def test_non_browser_run_ignores_exactly_the_browser_paths(ci):
    args = ci.non_browser_args()
    assert args[0] == "tests"
    assert args[1:] == [f"--ignore={p}" for p in ci.browser_paths()]


def test_run_executes_both_invocations_when_the_first_fails(ci, monkeypatch):
    calls = []
    monkeypatch.setattr(ci, "_pytest", lambda args: calls.append(args) or (1 if not calls[1:] else 0))
    assert ci.run(with_cov=False, fail_under=68) == 1
    assert len(calls) == 2
    assert "-n" in calls[0] and calls[0][calls[0].index("-n") + 1] == "auto"
    assert calls[1][: len(ci.browser_paths())] == ci.browser_paths()
    assert calls[1][calls[1].index("-n") + 1] == "2"
    assert calls[1][calls[1].index("--dist") + 1] == "loadfile"


def test_run_returns_the_browser_failure_when_only_it_fails(ci, monkeypatch):
    results = iter([0, 3])
    monkeypatch.setattr(ci, "_pytest", lambda args: next(results))
    assert ci.run(with_cov=False, fail_under=68) == 3


def test_coverage_gate_is_applied_once_to_the_combined_data(ci, monkeypatch):
    calls = []
    monkeypatch.setattr(ci, "_pytest", lambda args: calls.append(args) or 0)
    ci.run(with_cov=True, fail_under=70)
    first, second = calls
    assert "--cov=src/muscat_db" in first and "--cov=src/muscat_db" in second
    assert not any(a.startswith("--cov-fail-under") for a in first)
    assert "--cov-append" not in first
    assert "--cov-append" in second and "--cov-fail-under=70" in second


def _fake_collect(ci, monkeypatch, full, rest, browser, stray=frozenset()):
    def collect(args):
        if not args:
            return set(full)
        return set(rest) if args[0] == "tests" else set(browser)
    monkeypatch.setattr(ci, "_collect", collect)
    monkeypatch.setattr(ci, "_fixture_users", lambda args: set(stray) if args[0] == "tests" else set())


def test_split_check_passes_for_a_disjoint_complete_split(ci, monkeypatch, capsys):
    _fake_collect(ci, monkeypatch, {"a::t", "b::t"}, {"a::t"}, {"b::t"})
    assert ci.check_split() == 0
    assert "1 non-browser + 1 browser = 2 tests" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("rest", "browser", "message"),
    [
        ({"a::t"}, set(), "in neither selection"),
        ({"a::t", "b::t"}, {"b::t"}, "in both selections"),
        ({"a::t"}, {"b::t", "c::t"}, "only collected by a selection"),
    ],
)
def test_split_check_fails_when_the_selections_drift(ci, monkeypatch, capsys, rest, browser, message):
    _fake_collect(ci, monkeypatch, {"a::t", "b::t"}, rest, browser)
    assert ci.check_split() == 1
    assert message in capsys.readouterr().err


def test_split_check_holds_for_the_real_suite_paths(ci):
    # The real collection is checked in CI's ruff job; here only that the
    # selections name paths that exist, so a rename cannot leave one empty.
    assert all((ci.REPO / p).exists() for p in ci.browser_paths())


def test_split_check_fails_on_a_browser_test_outside_the_browser_paths(ci, monkeypatch, capsys):
    # e.g. tests/sub/test_x_browser.py: in the non-browser selection, so the
    # complete/disjoint check alone passes it.
    _fake_collect(ci, monkeypatch, {"a::t", "b::t"}, {"a::t"}, {"b::t"}, stray={"a::t"})
    assert ci.check_split() == 1
    assert "use the 'page' fixture outside the browser selection" in capsys.readouterr().err


def test_fixture_probe_finds_direct_and_indirect_users(ci, tmp_path):
    (tmp_path / "conftest.py").write_text(
        "import pytest\n"
        "@pytest.fixture\ndef page():\n    return object()\n"
        "@pytest.fixture\ndef logged_in(page):\n    return page\n"
    )
    (tmp_path / "test_x_browser.py").write_text(
        "def test_direct(page): pass\n"
        "def test_indirect(logged_in): pass\n"
        "def test_plain(): pass\n"
        "import pytest\n"
        "@pytest.mark.parametrize('page', ['/photometry'])\n"
        "def test_a_url_argument_named_page(page): pass\n"
    )
    users = ci._fixture_users([str(tmp_path)])
    assert {u.split("::")[-1] for u in users} == {"test_direct", "test_indirect"}
