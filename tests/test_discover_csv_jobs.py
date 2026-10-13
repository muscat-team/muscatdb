"""_discover_csv_jobs must not ingest a CSV filed under a different date (#197).

`obslog/muscat3/231111/` once held `obslog-muscat3-231112-ccd{0..3}.csv`, and
`obslog/sinistro/260624/` held `obslog-sinistro-260625-ccd0.csv`. Both were
ingested under the folder's date, so every frame was in the db twice.
"""
from __future__ import annotations

import logging

import pytest

from muscat_db import database


@pytest.fixture
def obslog(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "OBSLOG_BASE", str(tmp_path))
    return tmp_path


def _touch(root, inst, folder, name):
    d = root / inst / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("filename\n")


def test_csv_named_for_its_folder_date_is_discovered(obslog):
    _touch(obslog, "muscat3", "231111", "obslog-muscat3-231111-ccd0.csv")
    _touch(obslog, "muscat3", "231111", "obslog-muscat3-231111-ccd3.csv")

    jobs = database._discover_csv_jobs("muscat3", "231111")

    assert [(i, d, c) for i, d, _, c in jobs] == [
        ("muscat3", "231111", 0),
        ("muscat3", "231111", 3),
    ]


def test_csv_for_another_date_is_skipped_not_ingested_under_the_folder(obslog, caplog):
    _touch(obslog, "muscat3", "231111", "obslog-muscat3-231111-ccd0.csv")
    _touch(obslog, "muscat3", "231111", "obslog-muscat3-231112-ccd0.csv")

    with caplog.at_level(logging.WARNING, logger=database.logger.name):
        jobs = database._discover_csv_jobs("muscat3", "231111")

    assert [p.rsplit("/", 1)[1] for _, _, p, _ in jobs] == ["obslog-muscat3-231111-ccd0.csv"]
    assert "obslog-muscat3-231112-ccd0.csv" in caplog.text
    assert "231111" in caplog.text


def test_misplaced_csv_alone_yields_no_job(obslog):
    """The sinistro 260624 shape: the only CSV in the folder is for the next day."""
    _touch(obslog, "sinistro", "260624", "obslog-sinistro-260625-ccd0.csv")

    assert database._discover_csv_jobs("sinistro", "260624") == []


def test_name_without_a_date_segment_keeps_the_old_behaviour(obslog):
    """Only a parseable, different date is rejected; anything else is as before."""
    _touch(obslog, "muscat3", "231111", "obslog-legacy-ccd2.csv")

    jobs = database._discover_csv_jobs("muscat3", "231111")

    assert [(d, c) for _, d, _, c in jobs] == [("231111", 2)]
