"""The archive page's "saved locally" flag reads directory listings.

Canary 2026-11-10: on ut2, after a lookup of a frame that did not exist yet,
the NFS client kept answering "not found" for about 30 s once a fleet worker on
another host had written it (default negative lookup caching), while a
directory listing showed the file at once. A per-frame ``Path.exists`` check
therefore reported frames a worker had just downloaded as missing.
"""

import os
from pathlib import Path

from muscat_db import lco

NIGHT = "261109"


def _row(n: int) -> dict:
    return {"filename": f"ogg2m001-ep02-20261109-{n:04d}-e91.fits.fz", "OBJECT": "TOI-1",
            "SITEID": "ogg", "TELID": "2m0a", "INSTRUME": "ep02",
            "DATE_OBS": "2026-11-10T07:00:00Z", "RA": 10.0, "DEC": 20.0}


def _setup(monkeypatch, tmp_path):
    night = tmp_path / "MuSCAT3" / NIGHT
    night.mkdir(parents=True)
    monkeypatch.setattr(lco, "frame_dest", lambda inst, obsdate, name: night / name)
    monkeypatch.setattr(lco, "_local_lco_datasets", lambda *a, **k: [])
    return night


def test_frames_a_worker_just_wrote_count_as_saved(monkeypatch, tmp_path):
    night = _setup(monkeypatch, tmp_path)
    (night / _row(1)["filename"]).write_bytes(b"frame")
    real_exists = Path.exists
    # The stale negative lookup: the file is there, but a lookup by name says no.
    monkeypatch.setattr(Path, "exists", lambda self: False if self.parent == night else real_exists(self))
    out, _ = lco._annotate_lco_archive_results("muscat3", [_row(1)])
    assert out[0]["saved_locally"] is True


def test_empty_and_missing_frames_are_not_saved_and_each_directory_is_listed_once(monkeypatch, tmp_path):
    night = _setup(monkeypatch, tmp_path)
    (night / _row(1)["filename"]).write_bytes(b"frame")
    (night / _row(2)["filename"]).write_bytes(b"")
    listed = []
    real_listdir = os.listdir
    monkeypatch.setattr(os, "listdir", lambda path: listed.append(Path(path)) or real_listdir(path))
    out, _ = lco._annotate_lco_archive_results("muscat3", [_row(1), _row(2), _row(3)])
    saved = {r["filename"]: r["saved_locally"] for r in out}
    assert saved == {_row(1)["filename"]: True, _row(2)["filename"]: False, _row(3)["filename"]: False}
    assert listed.count(night) == 1


def test_a_night_not_downloaded_yet_is_not_saved(monkeypatch, tmp_path):
    night = _setup(monkeypatch, tmp_path)
    night.rmdir()
    out, _ = lco._annotate_lco_archive_results("muscat3", [_row(1)])
    assert out[0]["saved_locally"] is False
