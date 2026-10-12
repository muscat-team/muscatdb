"""Reference-time slider backend: range, closest-frame lookup, preview PNGs."""
from __future__ import annotations

import base64
import sqlite3
import struct
import zlib

import numpy as np
import pytest
from astropy.io import fits

from muscat_db import ref_time as rt

# frames.jd_start is stored as "JD - 2450000"
T0 = 9707.6  # 2022-05-08 02:24 UT
INST, DATE, TARGET = "muscat2", "220507", "TOI05205.01"


def _frame(ccd, name, offset_s, filt="g", exptime=30.0, obj=TARGET):
    return {
        "ccd": ccd, "filename": name, "object": obj, "filter": filt,
        "exptime": exptime, "jd_start": T0 + offset_s / 86400.0,
    }


def _frames_by_ccd():
    return {
        0: [_frame(0, f"MCT20_22050700{i:02d}", 30.0 * i, "g") for i in range(10)],
        2: [_frame(2, f"MCT22_22050712{i:02d}", 15.0 * i, "i", 15.0) for i in range(20)],
    }


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_band_for_filter_maps_raw_filters_and_passes_unknowns_through():
    assert rt.band_for_filter("g") == "gp"
    assert rt.band_for_filter("z_s") == "zs"
    assert rt.band_for_filter("gp") == "gp"
    assert rt.band_for_filter("weird") == "weird"


def test_build_range_spans_all_bands_in_full_jd_with_10_minute_steps():
    r = rt.build_range(_frames_by_ccd(), TARGET)

    assert r["jd_min"] == pytest.approx(T0 + 2_450_000.0)
    assert r["jd_max"] == pytest.approx(T0 + 2_450_000.0 + 285.0 / 86400.0)
    assert r["step_s"] == 600
    assert r["n_steps"] == 1  # 285 s -> one step reaches the end
    assert [b["band"] for b in r["bands"]] == ["gp", "ip"]
    assert r["bands"][1]["n"] == 20 and r["bands"][1]["exptime"] == 15.0


def test_build_range_ignores_other_objects_and_returns_none_without_target():
    frames = {0: [_frame(0, "a", 0.0, obj="OTHER")]}
    assert rt.build_range(frames, TARGET) is None


def test_slider_time_steps_by_10_minutes_and_clamps_to_end():
    jd_min, jd_max = 2459707.6, 2459707.6 + 25 * 60 / 86400.0
    assert rt.n_steps(jd_min, jd_max) == 3
    assert rt.slider_time(jd_min, jd_max, 0) == pytest.approx(jd_min)
    assert rt.slider_time(jd_min, jd_max, 1) == pytest.approx(jd_min + 600 / 86400.0)
    assert rt.slider_time(jd_min, jd_max, 3) == pytest.approx(jd_max)  # clamped


def test_closest_frame_returns_nearest_and_offset_seconds():
    frames = _frames_by_ccd()[2]
    jd = T0 + 2_450_000.0 + 70.0 / 86400.0  # nearest 15 s frame is #5 (75 s)

    frame, offset = rt.closest_frame(frames, jd)

    assert frame["filename"] == "MCT22_2205071205"
    assert offset == pytest.approx(5.0, abs=1e-3)


def test_closest_frame_skips_frames_without_time():
    frames = [{"filename": "bad", "jd_start": None}, _frame(0, "good", 10.0)]
    frame, _ = rt.closest_frame(frames, T0 + 2_450_000.0)
    assert frame["filename"] == "good"


def test_jd_to_iso_is_second_resolution_utc():
    assert rt.jd_to_iso(2459707.5 + (2 * 3600 + 24 * 60 + 8) / 86400.0) == "2022-05-08T02:24:08"


@pytest.mark.parametrize("ok", ["02:24", "02:24:08", "2022-05-08T02:24:08",
                                "2022-05-08 02:24:08", "2459707.6001"])
def test_is_valid_ref_time_accepts_clock_iso_and_jd(ok):
    assert rt.is_valid_ref_time(ok)


@pytest.mark.parametrize("bad", ["", "abc", "25:00", "12", "02:61", "2022-13-45T00:00:00"])
def test_is_valid_ref_time_rejects_garbage(bad):
    assert not rt.is_valid_ref_time(bad)


# ── image rendering ──────────────────────────────────────────────────────────

def _write_fits(path, shape=(300, 400), ext=0):
    data = np.random.default_rng(0).normal(100.0, 5.0, shape).astype("float32")
    data[150:155, 200:205] += 5000.0  # a bright "star"
    if ext == 0:
        fits.PrimaryHDU(data).writeto(path)
    else:
        fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(data)]).writeto(path)
    return path


def _png_size(png: bytes):
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", png[16:24])
    return w, h


def test_render_png_downsamples_to_max_px_and_is_valid_png(tmp_path):
    png = rt.render_png(_write_fits(tmp_path / "a.fits"), max_px=100)
    w, h = _png_size(png)
    assert max(w, h) <= 100 and w > h  # aspect ratio kept (400x300)
    # IDAT decompresses to h rows of (1 filter byte + w pixels)
    idat = png[png.index(b"IDAT") + 4:]
    length = struct.unpack(">I", png[png.index(b"IDAT") - 4:png.index(b"IDAT")])[0]
    assert len(zlib.decompress(idat[:length])) == h * (w + 1)


def test_render_png_reads_image_extension_for_compressed_files(tmp_path):
    png = rt.render_png(_write_fits(tmp_path / "b.fits", ext=1), max_px=64)
    assert _png_size(png)[0] <= 64


def test_render_png_raises_for_missing_image_data(tmp_path):
    p = tmp_path / "empty.fits"
    fits.PrimaryHDU().writeto(p)
    with pytest.raises(ValueError, match="image"):
        rt.render_png(p)


def test_find_frame_file_tries_known_suffixes(tmp_path):
    (tmp_path / "A.fits.fz").write_bytes(b"")
    (tmp_path / "B.fits").write_bytes(b"")
    assert rt.find_frame_file(tmp_path, "A") == tmp_path / "A.fits.fz"
    assert rt.find_frame_file(tmp_path, "B") == tmp_path / "B.fits"
    assert rt.find_frame_file(tmp_path, "C") is None


def test_preview_returns_per_band_frame_offset_and_image(tmp_path):
    frames = _frames_by_ccd()
    for fr in (f for fl in frames.values() for f in fl):
        _write_fits(tmp_path / f"{fr['filename']}.fits", shape=(40, 50))
    jd = T0 + 2_450_000.0 + 60.0 / 86400.0

    out = rt.preview(frames, TARGET, jd, tmp_path, max_px=32)

    assert out["ref_time"] == rt.jd_to_iso(jd)
    by_band = {b["band"]: b for b in out["bands"]}
    assert by_band["gp"]["frame"] == "MCT20_2205070002"   # 60 s / 30 s cadence
    assert by_band["ip"]["frame"] == "MCT22_2205071204"   # 60 s / 15 s cadence
    assert by_band["gp"]["offset_s"] == pytest.approx(0.0, abs=1e-3)
    assert by_band["ip"]["exptime"] == 15.0
    for b in by_band.values():
        assert b["image"].startswith("data:image/png;base64,")
        base64.b64decode(b["image"].split(",", 1)[1])


def test_preview_reports_missing_files_per_band_without_failing(tmp_path):
    frames = {0: [_frame(0, "MCT20_nope", 0.0)]}
    out = rt.preview(frames, TARGET, T0 + 2_450_000.0, tmp_path)
    assert out["bands"][0]["image"] is None
    assert "not found" in out["bands"][0]["error"]


# ── routes ───────────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "muscat.db"))
    monkeypatch.setenv("MUSCAT_DATA_DIR", str(tmp_path / "data"))
    from muscat_db.web import app

    with TestClient(app) as c:
        yield c


def _seed(tmp_path, client):
    from muscat_db import photometry as phot

    raw = phot.raw_data_dir(INST, DATE)
    raw.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(tmp_path / "muscat.db")
    for ccd, fl in _frames_by_ccd().items():
        for fr in fl:
            _write_fits(raw / f"{fr['filename']}.fits", shape=(40, 50))
            conn.execute(
                "INSERT INTO frames (instrument,obsdate,ccd,filename,object,jd_start,"
                "filter,exptime) VALUES (?,?,?,?,?,?,?,?)",
                (INST, DATE, ccd, fr["filename"], TARGET, fr["jd_start"],
                 fr["filter"], fr["exptime"]),
            )
    conn.commit()
    conn.close()


def test_range_route_returns_bands_and_steps(client, tmp_path):
    _seed(tmp_path, client)
    r = client.get(f"/api/photometry/ref-time/{INST}/{DATE}/{TARGET}")
    assert r.status_code == 200
    body = r.json()
    assert body["step_s"] == 600 and body["n_steps"] == 1
    assert [b["band"] for b in body["bands"]] == ["gp", "ip"]
    assert body["start_iso"].startswith("2022-05-08T02:24")


def test_range_route_404_without_frames(client):
    r = client.get(f"/api/photometry/ref-time/{INST}/{DATE}/NOPE")
    assert r.status_code == 404


def test_preview_route_returns_one_image_per_band(client, tmp_path):
    _seed(tmp_path, client)
    jd = T0 + 2_450_000.0 + 60.0 / 86400.0
    r = client.get(
        f"/api/photometry/ref-time/{INST}/{DATE}/{TARGET}/preview", params={"jd": jd}
    )
    assert r.status_code == 200
    bands = {b["band"]: b for b in r.json()["bands"]}
    assert bands["gp"]["image"].startswith("data:image/png;base64,")
    assert bands["ip"]["frame"] == "MCT22_2205071204"


def test_preview_route_rejects_non_finite_jd(client, tmp_path):
    _seed(tmp_path, client)
    r = client.get(
        f"/api/photometry/ref-time/{INST}/{DATE}/{TARGET}/preview", params={"jd": "nan"}
    )
    assert r.status_code == 422
