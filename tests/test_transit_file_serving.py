"""Transit-fit output files are served from an allow-list of media types, and a
failed zip does not echo server paths (issue #273, items 2 and 6).

An .html or .svg written into a run directory by an engine used to be served
with a guessed type and rendered on the app's own origin. Known extensions keep
their type; anything else downloads as an attachment.
"""
# ruff: noqa: F811

from __future__ import annotations

import logging
import zipfile

import pytest
from fastapi import HTTPException

from muscat_db import transit_fit as fit
from muscat_db import web
# The ``env`` and ``client`` fixtures build the open night and its fit run.
from tests.test_access_control_jobs import INST, RUN, client, env  # noqa: F401

DATE = "260103"
TARGET = "OPENTGT"


def _url(name):
    return f"/api/transit-fit/file/{INST}/{DATE}/{TARGET}/run/{RUN}/{name}"


def _put(name, data=b"x"):
    rdir = fit.fit_output_dir(INST, DATE, TARGET, RUN)
    (rdir / "out").mkdir(exist_ok=True)
    (rdir / "out" / name).write_bytes(data)


def _get(client, name):
    return client.get(_url(name), headers={"X-Forwarded-User": "root"})


@pytest.mark.parametrize("name", ["page.html", "image.svg", "report.xhtml", "data.xml", "x.js", "x.pdf", "x.pkl"])
def test_unlisted_types_download_as_attachments(client, name):
    _put(name, b"<script>alert(1)</script>")
    r = _get(client, name)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("name,ctype", [
    ("fit.png", "image/png"),
    ("summary.csv", "text/csv"),
    ("timer-fit.log", "text/plain"),
    ("notes.txt", "text/plain"),
    ("fit.yaml", "text/plain"),
    ("sys.yaml", "text/plain"),
    ("samples.npz", "application/octet-stream"),
])
def test_listed_types_keep_their_type_and_open_inline(client, name, ctype):
    _put(name)
    r = _get(client, name)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(ctype)
    assert "attachment" not in r.headers.get("content-disposition", "")
    assert r.headers["x-content-type-options"] == "nosniff"


def test_extension_match_ignores_case(client):
    _put("PLOT.PNG")
    assert _get(client, "PLOT.PNG").headers["content-type"].startswith("image/png")
    _put("Evil.HTML")
    assert _get(client, "Evil.HTML").headers["content-disposition"].startswith("attachment")


def test_failed_zip_returns_a_generic_error_and_logs_the_path(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("MUSCAT_TMPDIR", str(tmp_path / "tmp"))
    src = tmp_path / "a.csv"
    src.write_text("x")

    def boom(*a, **k):
        raise OSError("/raid_ut2/secret/place: disk on fire")

    monkeypatch.setattr(zipfile, "ZipFile", boom)
    with caplog.at_level(logging.ERROR, logger=web.logger.name):
        with pytest.raises(HTTPException) as exc:
            web._create_zip_response([(src, "a.csv")], "out.zip")

    assert exc.value.status_code == 500
    assert "secret" not in str(exc.value.detail) and "/" not in str(exc.value.detail)
    assert "disk on fire" in caplog.text
