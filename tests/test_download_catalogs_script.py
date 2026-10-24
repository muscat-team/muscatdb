"""`scripts/download_catalogs.sh` run as a real script against a stub `curl`.

A failed or unusable download is judged by what is left in `data/` and by the
exit code the nightly sees, not by the script text: the archive has answered
with DNS failures and can answer with an error document, and neither may
replace a good catalog or pass silently.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOGS = (
    "TOIs.csv", "nexsci_pscomppars.csv", "nexsci_pubdates.csv",
    "jwst_targets.csv", "spectra_targets.csv",
)
PREVIOUS = "previous,catalog\nkept,row\n"
FRESH = "col_a,col_b\n1,2\n"

# Mimics the curl options the script relies on: `-o FILE` and `--fail`.
_CURL_STUB = f"""#!/usr/bin/env bash
echo "$@" >> "$CURL_ARGS_LOG"
out=""; fail=0
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    --fail|-f) fail=1; shift ;;
    *) shift ;;
  esac
done
case "${{CURL_MODE:-ok}}" in
  ok) printf '{FRESH.replace(chr(10), "\\n")}' > "$out"; echo "HTTP 200, 16 bytes" ;;
  dns) echo "curl: (6) Could not resolve host: exoplanetarchive.ipac.caltech.edu" >&2; exit 6 ;;
  http500)
    if [ "$fail" = 1 ]; then echo "curl: (22) The requested URL returned error: 500" >&2; exit 22; fi
    printf '<html>Internal Server Error</html>\\n' > "$out"; echo "HTTP 500, 35 bytes" ;;
  votable)
    printf '<?xml version="1.0"?>\\n<VOTABLE><INFO name="QUERY_STATUS" value="ERROR"/></VOTABLE>\\n' > "$out"
    echo "HTTP 200, 90 bytes" ;;
  empty) : > "$out"; echo "HTTP 200, 0 bytes" ;;
esac
"""


@pytest.fixture
def tree(tmp_path):
    """A scratch checkout with the real script, existing catalogs and a stub curl."""
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "scripts" / "download_catalogs.sh", root / "scripts")
    data = root / "data"
    data.mkdir()
    for name in CATALOGS:
        (data / name).write_text(PREVIOUS)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(_CURL_STUB)
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    scratch = tmp_path / "scratch"
    args_log = tmp_path / "curl_args.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "MUSCAT_TMPDIR": str(scratch),
        "CURL_ARGS_LOG": str(args_log),
    }
    return {"root": root, "data": data, "env": env, "scratch": scratch, "args_log": args_log}


def _run(tree, mode):
    return subprocess.run(
        ["bash", str(tree["root"] / "scripts" / "download_catalogs.sh")],
        env={**tree["env"], "CURL_MODE": mode}, capture_output=True, text=True,
    )


def test_successful_downloads_replace_every_catalog(tree):
    result = _run(tree, "ok")

    assert result.returncode == 0, result.stdout + result.stderr
    assert {name: (tree["data"] / name).read_text() for name in CATALOGS} == dict.fromkeys(CATALOGS, FRESH)


@pytest.mark.parametrize("mode", ["dns", "http500", "votable", "empty"])
def test_a_failed_or_unusable_download_keeps_every_catalog_and_exits_nonzero(tree, mode):
    result = _run(tree, mode)

    assert result.returncode != 0
    assert {name: (tree["data"] / name).read_text() for name in CATALOGS} == dict.fromkeys(CATALOGS, PREVIOUS)
    assert "5 of 5 catalog downloads failed" in result.stdout
    assert all(name in result.stdout for name in CATALOGS)


def test_one_failure_does_not_stop_the_other_downloads(tree):
    result = _run(tree, "dns")

    assert len(tree["args_log"].read_text().splitlines()) == len(CATALOGS)
    assert result.returncode != 0


@pytest.mark.parametrize("mode", ["ok", "dns", "votable"])
def test_no_temporary_download_is_left_behind(tree, mode):
    _run(tree, mode)

    assert list(tree["scratch"].iterdir()) == []


def test_http_errors_fail_and_dns_failures_are_retried(tree):
    _run(tree, "ok")

    for call in tree["args_log"].read_text().splitlines():
        args = call.split()
        assert "--fail" in args
        assert "--retry-all-errors" in args
        assert "--retry" in args and "--retry-delay" in args
