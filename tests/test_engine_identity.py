"""Probe real dist-info metadata without importing an engine's code."""
import json
import subprocess
import sys

import pytest

from muscat_db.engine_identity import version_probe


@pytest.mark.parametrize("direct,expected", [
    ({"url": "https://github.com/owner/engine", "vcs_info": {
        "vcs": "git", "commit_id": "a" * 40}}, "1.2.3+git." + "a" * 40),
    ({"url": "file:///dev/engine", "dir_info": {"editable": True}}, "1.2.3"),
    ({}, "1.2.3"),
])
def test_version_uses_installed_commit_not_just_release_number(tmp_path, direct, expected):
    dist = tmp_path / "fake_engine-1.2.3.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: fake-engine\nVersion: 1.2.3\n")
    (dist / "direct_url.json").write_text(json.dumps(direct))
    # If the probe imports the package, it must fail this test.
    (tmp_path / "fake_engine.py").write_text("raise RuntimeError('must not import engine')\n")
    code = f"import sys; sys.path.insert(0, {str(tmp_path)!r})\n" + version_probe(("absent-engine", "fake-engine"))
    result = subprocess.run([sys.executable, "-I", "-c", code],
                            check=True, capture_output=True, text=True, timeout=10)
    assert result.stdout.strip() == expected
