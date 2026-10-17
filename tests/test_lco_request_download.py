"""Downloading a whole LCO request in one step (issue #256).

A MuSCAT3 night is a few thousand frames. Downloading it must not depend on the
browser posting every frame's archive URL back to the server, and selecting all
of a night's rows must still fit the payload cap.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from muscat_db import lco
from muscat_db.web import app

_NIGHT_FRAMES = 3496  # a real MuSCAT3 request whose select-all download hit the 2 MiB cap


def _frame(i: int) -> dict:
    """An archive search row with a presigned URL of realistic length."""
    name = f"ogg2m001-ep05-20260301-{i:04d}-e91"
    url = (
        f"https://archive-lco-global.s3.amazonaws.com/data/ogg/ep05/20260301/processed/{name}.fits.fz"
        f"?versionId={'v' * 32}&X-Amz-Algorithm=AWS4-HMAC-SHA256"
        f"&X-Amz-Credential={'A' * 20}%2F20260301%2Fus-west-2%2Fs3%2Faws4_request"
        "&X-Amz-Date=20260301T080000Z&X-Amz-Expires=172800&X-Amz-SignedHeaders=host"
        f"&X-Amz-Security-Token={'T' * 200}&X-Amz-Signature={'f' * 64}"
    )
    return {
        "id": 900_000_000 + i,
        "filename": f"{name}.fits.fz",
        "basename": name,
        "SITEID": "ogg",
        "TELID": "2m0a",
        "INSTRUME": "ep05",
        "DATE_OBS": "2026-03-01T08:12:34.567000Z",
        "DAY_OBS": "2026-02-28",
        "OBJECT": "TOI-1234",
        "PROPID": "KEY2026A-001",
        "reduction_level": 91,
        "url": url,
    }


def _minimal(frame: dict) -> dict:
    """What lco_archive.html's minimalFramePayload posts for a selected row."""
    keys = ("filename", "basename", "SITEID", "TELID", "INSTRUME", "DATE_OBS", "DAY_OBS", "OBJECT", "url")
    return {key: frame[key] for key in keys}


@pytest.fixture
def no_worker(monkeypatch):
    """Queue jobs without starting the download worker."""

    class _Executor:
        def submit(self, *args, **kwargs):
            return None

    monkeypatch.setattr(lco, "_ARCHIVE_DOWNLOAD_EXECUTOR", _Executor())
    monkeypatch.setattr(lco, "_ARCHIVE_DOWNLOAD_JOBS", {})


@pytest.fixture
def archive(monkeypatch):
    """A fake LCO archive serving one request's frames 1000 per page."""
    frames = [_frame(i) for i in range(_NIGHT_FRAMES)]
    calls: list[dict] = []

    def fake_request(url, user_name=None, token=None, **kwargs):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        calls.append(query)
        if query.get("request_id") != "4236675":
            return {"count": 0, "results": [], "next": None}
        offset = int(query.get("offset", 0))
        limit = int(query.get("limit", 100))
        page = frames[offset:offset + limit]
        nxt = None
        if offset + limit < len(frames):
            nxt = "https://archive-api.lco.global/frames/?" + urllib.parse.urlencode(
                {**query, "offset": offset + limit}
            )
        return {"count": len(frames), "results": page, "next": nxt}

    monkeypatch.setattr(lco, "_lco_api_request", fake_request)
    return {"frames": frames, "calls": calls}


def _post(path: str, body: dict):
    return TestClient(app).post(path, json=body)


def test_request_download_queues_the_whole_request_as_one_job(mock_db, no_worker, archive):
    r = _post("/api/lco/archive/request-download", {"request_id": "4236675"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["frames_total"] == _NIGHT_FRAMES
    (job,) = lco._ARCHIVE_DOWNLOAD_JOBS.values()
    assert {f["filename"] for f in job["frames"]} == {f["filename"] for f in archive["frames"]}
    assert job["auto_ingest"] is True
    # Four 1000-frame pages, reduced frames only unless asked otherwise.
    assert len(archive["calls"]) == 4
    assert all(c["reduction_level"] == "91" and c["limit"] == "1000" for c in archive["calls"])


def test_request_download_passes_the_requested_reduction_level(mock_db, no_worker, archive):
    r = _post("/api/lco/archive/request-download", {"request_id": "4236675", "reduction_level": "0"})

    assert r.status_code == 200, r.text
    assert archive["calls"][0]["reduction_level"] == "0"


@pytest.mark.parametrize("request_id", ["", "abc", "42 36", "4236675;1"])
def test_request_download_rejects_a_non_numeric_id(mock_db, no_worker, archive, request_id):
    r = _post("/api/lco/archive/request-download", {"request_id": request_id})

    assert r.status_code == 400
    assert r.json()["ok"] is False
    assert archive["calls"] == []
    assert lco._ARCHIVE_DOWNLOAD_JOBS == {}


def test_request_download_reports_a_request_with_no_frames(mock_db, no_worker, archive):
    r = _post("/api/lco/archive/request-download", {"request_id": "1"})

    assert r.status_code == 404
    assert r.json()["ok"] is False
    assert lco._ARCHIVE_DOWNLOAD_JOBS == {}


def test_request_download_refuses_a_truncated_search(mock_db, no_worker, monkeypatch):
    monkeypatch.setattr(
        lco,
        "archive_search_all",
        lambda filters, user_name=None, **kwargs: {
            "count": 12_000, "results": [_frame(i) for i in range(10)], "truncated": True,
        },
    )

    r = _post("/api/lco/archive/request-download", {"request_id": "4236675"})

    assert r.status_code == 413
    assert "10000" in r.json()["error"].replace(",", "")
    assert lco._ARCHIVE_DOWNLOAD_JOBS == {}


def test_archive_page_offers_a_request_download_button(mock_db):
    html = TestClient(app).get("/lco/archive").text

    assert 'id="btn-download-request"' in html
    assert "/api/lco/archive/request-download" in html


def test_selecting_every_frame_of_a_night_fits_the_payload_cap(mock_db, no_worker):
    picked = [_minimal(_frame(i)) for i in range(_NIGHT_FRAMES)]
    # Realistic size: this selection is what the old 2 MiB cap refused.
    assert len(json.dumps(picked, separators=(",", ":"))) > 2 * 1024 * 1024

    r = _post("/api/lco/archive/download", {"frames": picked, "background": True})

    assert r.status_code == 200, r.text
    assert r.json()["frames_total"] == _NIGHT_FRAMES


def test_an_oversized_selection_is_still_refused(mock_db, no_worker):
    one = _minimal(_frame(0))
    per_frame = len(json.dumps(one, separators=(",", ":"))) + 1
    picked = [one] * (lco._ARCHIVE_DOWNLOAD_MAX_PAYLOAD_BYTES // per_frame + 10)

    r = _post("/api/lco/archive/download", {"frames": picked, "background": True})

    assert r.status_code == 413
    assert lco._ARCHIVE_DOWNLOAD_JOBS == {}
