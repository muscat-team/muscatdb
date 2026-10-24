"""Probe real dist-info metadata without importing an engine's code."""
import json
import subprocess
import sys

import pytest

from muscat_db.engine_identity import is_mutable_version, version_probe


def _probe(tmp_path, direct, engine_dir=None):
    """Run the probe against a fake dist-info; the module lives in ``engine_dir``."""
    dist = tmp_path / "fake_engine-1.2.3.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: fake-engine\nVersion: 1.2.3\n")
    (dist / "direct_url.json").write_text(json.dumps(direct))
    engine_dir = engine_dir or tmp_path
    # If the probe imports the package, it must fail this test.
    (engine_dir / "fake_engine.py").write_text("raise RuntimeError('must not import engine')\n")
    paths = [str(tmp_path), str(engine_dir)]
    code = f"import sys; sys.path[:0] = {paths!r}\n" + version_probe(("absent-engine", "fake-engine"))
    result = subprocess.run([sys.executable, "-I", "-c", code],
                            check=True, capture_output=True, text=True, timeout=10)
    return result.stdout.strip()


@pytest.mark.parametrize("direct,expected", [
    ({"url": "https://github.com/owner/engine", "vcs_info": {
        "vcs": "git", "commit_id": "a" * 40}}, "1.2.3+git." + "a" * 40),
    # An editable install whose source directory is not a git checkout has
    # no commit to report.
    ({"url": "file:///nonexistent/engine", "dir_info": {"editable": True}}, "1.2.3"),
    ({}, "1.2.3"),
])
def test_version_uses_installed_commit_not_just_release_number(tmp_path, direct, expected):
    assert _probe(tmp_path, direct) == expected


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def test_editable_install_reports_checkout_commit_and_dirty_state(tmp_path):
    """A dev checkout's version names its HEAD, so runs from two commits differ."""
    repo = tmp_path / "engine src"  # a space exercises file:// URL decoding
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "mod.py").write_text("x = 1\n")
    _git(repo, "add", "mod.py")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    sha = _git(repo, "rev-parse", "HEAD")
    direct = {"url": repo.as_uri(), "dir_info": {"editable": True}}

    assert _probe(tmp_path / "a", direct) == f"1.2.3+git.{sha}.editable"

    (repo / "untracked.txt").write_text("ignored\n")
    assert _probe(tmp_path / "b", direct) == f"1.2.3+git.{sha}.editable"

    (repo / "mod.py").write_text("x = 2\n")
    assert _probe(tmp_path / "c", direct) == f"1.2.3+git.{sha}.editable.dirty"


def test_editable_install_with_stale_source_url_uses_import_location(tmp_path):
    """The env was installed through a mount path that no longer exists."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
         "--allow-empty", "-m", "init")
    sha = _git(repo, "rev-parse", "HEAD")
    direct = {"url": "file:///gone/mount/checkout", "dir_info": {"editable": True}}

    version = _probe(tmp_path / "site", direct, engine_dir=repo)

    assert version == f"1.2.3+git.{sha}.editable"


@pytest.mark.parametrize("version,mutable", [
    ("1.2.3+git." + "a" * 40, False),
    ("1.2.3", False),
    ("unknown", False),
    ("1.2.3+git." + "a" * 40 + ".editable", True),
    ("1.2.3+git." + "a" * 40 + ".editable.dirty", True),
])
def test_is_mutable_version(version, mutable):
    assert is_mutable_version(version) is mutable
