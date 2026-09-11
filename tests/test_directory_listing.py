"""Tests for ListingStaticFiles (muscat_db.directory_listing).

Some externally-synced trees (the MuSCAT2 quicklook dashboard) link to bare
directories with no index.html and rely on their original host's autoindex to
browse them. Starlette's own StaticFiles 404s in that case; ListingStaticFiles
adds a minimal fallback for it without changing any other StaticFiles behavior.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from muscat_db.directory_listing import ListingStaticFiles


def _make_client(tmp_path):
    app = FastAPI()
    app.mount("/files", ListingStaticFiles(directory=str(tmp_path), html=True), name="files")
    return TestClient(app)


def test_lists_files_and_subdirectories_when_no_index_html(tmp_path):
    (tmp_path / "b.txt").write_text("b")
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()

    resp = _make_client(tmp_path).get("/files/")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert '<a href="a.txt">a.txt</a>' in resp.text
    assert '<a href="b.txt">b.txt</a>' in resp.text
    assert '<a href="sub/">sub/</a>' in resp.text


def test_directory_without_trailing_slash_redirects_before_listing(tmp_path):
    (tmp_path / "sub").mkdir()

    resp = _make_client(tmp_path).get("/files/sub", follow_redirects=False)

    assert resp.status_code in (301, 307)
    assert resp.headers["location"].endswith("/files/sub/")


def test_index_html_still_wins_over_a_listing(tmp_path):
    (tmp_path / "index.html").write_text("<h1>real page</h1>")
    (tmp_path / "other.txt").write_text("x")

    resp = _make_client(tmp_path).get("/files/")

    assert resp.status_code == 200
    assert "real page" in resp.text
    assert "Index of" not in resp.text


def test_nested_directory_without_index_html_also_gets_a_listing(tmp_path):
    nested = tmp_path / "obslog" / "260101"
    nested.mkdir(parents=True)
    (nested / "obslog-muscat2-260101-ccd0.html").write_text("<p>log</p>")

    resp = _make_client(tmp_path).get("/files/obslog/260101/")

    assert resp.status_code == 200
    assert "Index of /obslog/260101/" in resp.text
    assert '<a href="obslog-muscat2-260101-ccd0.html">obslog-muscat2-260101-ccd0.html</a>' in resp.text
    # Parent-directory link, but not on the mount root.
    assert '<a href="../">../</a>' in resp.text


def test_mount_root_listing_has_no_parent_link(tmp_path):
    (tmp_path / "a.txt").write_text("a")

    resp = _make_client(tmp_path).get("/files/")

    assert "Index of /" in resp.text
    assert '<a href="../">../</a>' not in resp.text


def test_dotfiles_are_hidden_from_the_listing(tmp_path):
    (tmp_path / ".hidden").write_text("secret")
    (tmp_path / "visible.txt").write_text("x")

    resp = _make_client(tmp_path).get("/files/")

    assert "visible.txt" in resp.text
    assert ".hidden" not in resp.text


def test_a_real_file_request_is_unaffected(tmp_path):
    (tmp_path / "plot.png").write_bytes(b"fake-png-bytes")

    resp = _make_client(tmp_path).get("/files/plot.png")

    assert resp.status_code == 200
    assert resp.content == b"fake-png-bytes"


def test_nonexistent_path_still_404s(tmp_path):
    resp = _make_client(tmp_path).get("/files/does-not-exist")

    assert resp.status_code == 404
