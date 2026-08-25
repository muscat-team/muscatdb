"""Compose MkDocs output with the UI snapshot under site/home.

Needs mkdocs (dev extra). Skip when it is not installed so `uv sync` without
--dev still runs the default suite.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mkdocs")

from muscat_db.static_site import build_site
from tests.test_static_site import _build_tiny_db

REPO_ROOT = Path(__file__).resolve().parents[1]
_TOUR_HREF = re.compile(r"""(?:href|src)=["'](/muscatdb/home/[^"']*)["']""")


def _resolve_tour_href(site: Path, href: str) -> Path:
    """Map a /muscatdb/home/... href to a file under site/home/."""
    rest = href.removeprefix("/muscatdb/home/").strip("/")
    if not rest:
        return site / "home" / "index.html"
    candidate = site / "home" / rest
    if candidate.is_file():
        return candidate
    return site / "home" / rest / "index.html"


def test_mkdocs_plus_snapshot_compose(tmp_path):
    site = tmp_path / "site"
    db = tmp_path / "mock.db"
    _build_tiny_db(str(db))

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mkdocs",
            "build",
            "--strict",
            "--site-dir",
            str(site),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout

    docs_index = (site / "index.html").read_text(encoding="utf-8")
    assert "md-header" in docs_index
    assert "snapshot-banner" not in docs_index
    assert "Pipeline Guide" not in docs_index

    build_site(
        site / "home",
        db_path=str(db),
        n_examples=1,
        include_figures=False,
        log=lambda _m: None,
    )

    home = (site / "home" / "index.html").read_text(encoding="utf-8")
    assert "<title>Home" in home
    assert 'id="world-topo-src"' in home
    assert "const STATIC_SITE = true;" in home
    assert "Connecting to LCO Weather API" not in home
    assert "Live weather is unavailable in this static snapshot." in home
    assert "snapshot-banner" in home

    assert not (site / "notes").exists()
    audit_hits = [p for p in site.rglob("*") if "security_audit" in p.name]
    assert audit_hits == []

    tour_hrefs: set[str] = set()
    for p in site.rglob("*.html"):
        rel = p.relative_to(site)
        if rel.parts and rel.parts[0] == "home":
            continue
        tour_hrefs.update(_TOUR_HREF.findall(p.read_text(encoding="utf-8")))

    assert tour_hrefs, "MkDocs pages must deep-link into /muscatdb/home/"
    for href in tour_hrefs:
        target = _resolve_tour_href(site, href)
        assert target.is_file(), f"{href} -> missing {target}"
