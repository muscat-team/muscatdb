"""Run the CI test suite as two invocations: non-browser tests, then browser tests.

Browser tests wait on page state that depends on requests to the live server,
so under ``-n auto`` they compete with the CPU-heavy unit tests and flake
(#365). This keeps the required check names unchanged and splits the run
inside each job:

1. everything except the browser tests, at ``-n auto``;
2. the browser tests (``tests/browser/`` and ``tests/*_browser.py``) at
   ``-n 2 --dist loadfile``, so one worker owns a file's module fixtures.

Both invocations always run, so a failure in the first still reports the
second. With ``--cov`` the second appends to the first's coverage data and
the ``--cov-fail-under`` gate is applied once, to the combined figure.

``check-split`` collects the full suite and both selections and fails unless
the selections are disjoint and together cover the full collection, so a
test cannot silently drop out of CI when the split paths drift. It also fails
when a test outside the browser selection requests the Playwright ``page``
fixture (say ``tests/sub/test_x_browser.py``), since that test would otherwise
run in the parallel non-browser invocation.

Usage:
    uv run python scripts/ci_pytest.py run [--cov] [--fail-under N]
    uv run python scripts/ci_pytest.py check-split
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BROWSER_DIR = "tests/browser"
BROWSER_GLOB = "tests/*_browser.py"
BROWSER_WORKERS = "2"
COVERAGE_FLOOR = 68  # the ratchet from ci.yml; target 80%
COMMON = ["--tb=short", "-q"]
BROWSER_FIXTURE = "page"

# Collects in-process with a plugin that names every test requesting the
# browser fixture, directly or through another fixture. A test parametrized on
# an argument of that name (``@parametrize("page", ["/photometry"])``) lists it
# in fixturenames too, but is not a browser test.
_FIXTURE_PROBE = """
import sys, pytest
class Probe:
    def pytest_collection_modifyitems(self, items):
        name = sys.argv[1]
        for item in items:
            params = getattr(getattr(item, "callspec", None), "params", {})
            if name in getattr(item, "fixturenames", ()) and name not in params:
                print("USES " + item.nodeid)
sys.exit(pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider", *sys.argv[2:]], plugins=[Probe()]))
"""


def browser_paths() -> list[str]:
    files = sorted(str(p.relative_to(REPO)) for p in REPO.glob(BROWSER_GLOB))
    return [BROWSER_DIR, *files]


def non_browser_args() -> list[str]:
    return ["tests", *(f"--ignore={p}" for p in browser_paths())]


def _pytest(args: list[str]) -> int:
    cmd = [sys.executable, "-m", "pytest", *args]
    print("+ " + " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=REPO)


def run(with_cov: bool, fail_under: int) -> int:
    cov_first: list[str] = []
    cov_second: list[str] = []
    if with_cov:
        cov_first = ["--cov=src/muscat_db", "--cov-report="]
        cov_second = [
            "--cov=src/muscat_db", "--cov-append",
            "--cov-report=xml", "--cov-report=term-missing",
            f"--cov-fail-under={fail_under}",
        ]
    first = _pytest([*non_browser_args(), "-n", "auto", *COMMON, *cov_first])
    second = _pytest([
        *browser_paths(), "-n", BROWSER_WORKERS, "--dist", "loadfile", *COMMON, *cov_second,
    ])
    print(f"non-browser exit {first}, browser exit {second}", flush=True)
    return first or second


def _collect(args: list[str]) -> set[str]:
    cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *args]
    out = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    if out.returncode not in (0, 5):  # 5: nothing collected
        sys.stderr.write(out.stdout + out.stderr)
        raise SystemExit(f"collection failed: {' '.join(args) or '(full suite)'}")
    return {line for line in out.stdout.splitlines() if "::" in line}


def _fixture_users(args: list[str]) -> set[str]:
    """Node ids in the selection *args* that request the browser fixture."""
    cmd = [sys.executable, "-c", _FIXTURE_PROBE, BROWSER_FIXTURE, *args]
    out = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    if out.returncode not in (0, 5):
        sys.stderr.write(out.stdout + out.stderr)
        raise SystemExit(f"collection failed: {' '.join(args)}")
    return {line[len("USES "):] for line in out.stdout.splitlines() if line.startswith("USES ")}


def check_split() -> int:
    full = _collect([])
    rest = _collect(non_browser_args())
    browser = _collect(browser_paths())
    problems = []
    if stray := _fixture_users(non_browser_args()):
        problems.append(
            f"{len(stray)} tests use the {BROWSER_FIXTURE!r} fixture outside the browser selection "
            f"(move them under {BROWSER_DIR}/ or name them {BROWSER_GLOB}), e.g. {sorted(stray)[:3]}"
        )
    if overlap := rest & browser:
        problems.append(f"{len(overlap)} tests in both selections, e.g. {sorted(overlap)[:3]}")
    if missing := full - rest - browser:
        problems.append(f"{len(missing)} tests in neither selection, e.g. {sorted(missing)[:3]}")
    if extra := (rest | browser) - full:
        problems.append(f"{len(extra)} tests only collected by a selection, e.g. {sorted(extra)[:3]}")
    if not browser:
        problems.append("the browser selection collected nothing")
    for p in problems:
        print(f"split check: {p}", file=sys.stderr)
    if not problems:
        print(f"split check: {len(rest)} non-browser + {len(browser)} browser = {len(full)} tests")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="run both selections")
    run_p.add_argument("--cov", action="store_true", help="collect coverage and gate on the combined figure")
    run_p.add_argument("--fail-under", type=int, default=COVERAGE_FLOOR)
    sub.add_parser("check-split", help="verify the selections are disjoint and complete")
    args = parser.parse_args(argv)
    if args.command == "run":
        return run(args.cov, args.fail_under)
    return check_split()


if __name__ == "__main__":
    raise SystemExit(main())
