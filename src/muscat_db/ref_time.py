"""Reference-time helpers for ``--ref_select time``.

The photometry page lets the user pick the reference time with a slider and
shows, for every band, the frame that would be used (the one closest in time)
as a small preview. Everything here is pure data work -- no web or DB access --
so it can be unit-tested directly; ``web.py`` wires it to the frames table.

``frames.jd_start`` is stored as ``JD - 2450000`` (see ``scanner.py``); the
helpers here convert back to full JD, which is what ``--ref_time`` takes.
"""
from __future__ import annotations

import base64
import datetime
import math
import re
import struct
import zlib
from functools import lru_cache
from pathlib import Path

import numpy as np
from astropy.io import fits

JD_START_OFFSET = 2_450_000.0
_JD_UNIX_EPOCH = 2_440_587.5
SLIDER_STEP_S = 600  # 10-minute increments
_MIN_JD = 2_000_000.0
PREVIEW_MAX_PX = 192
_FRAME_SUFFIXES = (".fits", ".fits.fz", ".fit", ".fts")
# MuSCAT/MuSCAT2 frames are dark-subtracted and flat-fielded by prose2's
# calibration step into ``<results>/<inst>/<date>_calibrated/<FRAME>_calibrated.fits``;
# the preview shows those (raw frames are uncorrected and hard to read).
CALIBRATED_INSTRUMENTS = ("muscat", "muscat2")

# Raw filter name (as stored in the obslog) -> pipeline band name.
_BAND_BY_FILTER = {"g": "gp", "r": "rp", "i": "ip", "z_s": "zs", "z": "zs"}

_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}(?:\.\d+)?))?$")
_ISO_RE = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})[T ](\d{1,2}):(\d{2})(?::(\d{2}(?:\.\d+)?))?$"
)


def band_for_filter(raw_filter: str | None) -> str:
    raw = (raw_filter or "").strip()
    return _BAND_BY_FILTER.get(raw, raw)


def _full_jd(jd_start: float) -> float:
    return float(jd_start) + JD_START_OFFSET


def _timed(frames: list[dict]) -> list[dict]:
    return [f for f in frames if f.get("jd_start") not in (None, 0) and f["jd_start"] > 0]


def jd_to_iso(jd: float) -> str:
    """Full JD as ``YYYY-MM-DDTHH:MM:SS`` (UTC, rounded to the second) -- the
    form passed to ``--ref_time``."""
    ts = round((jd - _JD_UNIX_EPOCH) * 86400.0)
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def is_valid_ref_time(text: str) -> bool:
    """Mirror of prose2's ``--ref_time`` grammar (HH:MM[:SS], ISO UTC, JD)."""
    text = (text or "").strip()
    m = _CLOCK_RE.match(text)
    if m:
        return int(m[1]) <= 23 and int(m[2]) <= 59 and float(m[3] or 0) < 60
    m = _ISO_RE.match(text)
    if m:
        try:
            datetime.datetime(*(int(m[i]) for i in range(1, 6)))
        except ValueError:
            return False
        return float(m[6] or 0) < 60
    try:
        return float(text) >= _MIN_JD
    except ValueError:
        return False


def n_steps(jd_min: float, jd_max: float, step_s: int = SLIDER_STEP_S) -> int:
    return max(0, math.ceil((jd_max - jd_min) * 86400.0 / step_s - 1e-9))


def slider_time(jd_min: float, jd_max: float, k: int, step_s: int = SLIDER_STEP_S) -> float:
    """JD at slider position *k*: ``jd_min + k * step``, clamped to the end."""
    return min(jd_min + k * step_s / 86400.0, jd_max)


def build_range(frames_by_ccd: dict[int, list[dict]], target: str) -> dict | None:
    """Time range and per-band summary of *target*'s frames, or None."""
    bands: list[dict] = []
    for ccd in sorted(frames_by_ccd):
        frames = _timed([f for f in frames_by_ccd[ccd] if f.get("object") == target])
        if not frames:
            continue
        jds = [_full_jd(f["jd_start"]) for f in frames]
        bands.append({
            "ccd": ccd,
            "band": band_for_filter(frames[0].get("filter")),
            "n": len(frames),
            "jd_first": min(jds),
            "jd_last": max(jds),
            "exptime": frames[0].get("exptime"),
        })
    if not bands:
        return None
    jd_min = min(b["jd_first"] for b in bands)
    jd_max = max(b["jd_last"] for b in bands)
    return {
        "jd_min": jd_min,
        "jd_max": jd_max,
        "step_s": SLIDER_STEP_S,
        "n_steps": n_steps(jd_min, jd_max),
        "start_iso": jd_to_iso(jd_min),
        "end_iso": jd_to_iso(jd_max),
        "bands": bands,
    }


def closest_frame(frames: list[dict], jd: float) -> tuple[dict, float]:
    """Frame whose start time is closest to full-JD *jd*, and the offset (s)."""
    timed = _timed(frames)
    if not timed:
        raise ValueError("no frame has a start time")
    best = min(timed, key=lambda f: abs(_full_jd(f["jd_start"]) - jd))
    return best, abs(_full_jd(best["jd_start"]) - jd) * 86400.0


def find_frame_file(raw_dir: Path, filename: str) -> Path | None:
    """Locate a frame's FITS file; the obslog stores names without extension."""
    for suffix in ("", *_FRAME_SUFFIXES):
        candidate = raw_dir / f"{filename}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def find_calibrated_file(calibrated_dir: Path | None, filename: str) -> Path | None:
    """The ``<FRAME>_calibrated.fits`` for a frame, or None when the directory
    is not given or holds no such file."""
    if calibrated_dir is None:
        return None
    candidate = Path(calibrated_dir) / f"{filename}_calibrated.fits"
    return candidate if candidate.is_file() else None


def _image_hdu_data(path: Path) -> np.ndarray:
    with fits.open(path, memmap=False) as hdul:
        for hdu in hdul:
            data = hdu.data
            if data is not None and getattr(data, "ndim", 0) == 2:
                return np.asarray(data, dtype="float32")
    raise ValueError(f"no 2-D image data in {path.name}")


def _downsample(data: np.ndarray, max_px: int) -> np.ndarray:
    factor = max(1, math.ceil(max(data.shape) / max_px))
    h = (data.shape[0] // factor) * factor
    w = (data.shape[1] // factor) * factor
    if factor == 1 or h == 0 or w == 0:
        return data
    return data[:h, :w].reshape(h // factor, factor, w // factor, factor).mean(axis=(1, 3))


def _png_gray(pixels: np.ndarray) -> bytes:
    """Encode an 8-bit grayscale array as PNG (stdlib only; no PIL here)."""
    h, w = pixels.shape
    raw = b"".join(b"\x00" + pixels[i].tobytes() for i in range(h))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def _render_png_uncached(path: Path, max_px: int) -> bytes:
    small = _downsample(_image_hdu_data(path), max_px)
    finite = small[np.isfinite(small)]
    if finite.size == 0:
        raise ValueError(f"image {path.name} has no finite pixels")
    # Sky-referenced linear stretch: dark background, stars stand out. Robust
    # (median/MAD) so a few saturated stars or hot pixels don't set the scale.
    med = float(np.median(finite))
    sigma = 1.4826 * float(np.median(np.abs(finite - med)))
    if sigma <= 0:
        sigma = float(np.std(finite)) or 1.0
    lo, hi = med - 1.0 * sigma, med + 12.0 * sigma
    scaled = np.clip((np.nan_to_num(small, nan=lo) - lo) / (hi - lo), 0.0, 1.0)
    pixels = (np.sqrt(scaled) * 255.0).astype("uint8")
    return _png_gray(np.ascontiguousarray(pixels[::-1]))  # FITS origin is bottom-left


@lru_cache(maxsize=256)
def _render_png_cached(path: str, mtime_ns: int, max_px: int) -> bytes:
    return _render_png_uncached(Path(path), max_px)


def render_png(path: Path, max_px: int = PREVIEW_MAX_PX) -> bytes:
    """Stretched, downsampled grayscale PNG of a FITS image. Cached per
    (path, mtime, size): slider positions are discrete, so scrubbing back and
    forth re-uses previous renders."""
    path = Path(path)
    return _render_png_cached(str(path), path.stat().st_mtime_ns, max_px)


def preview(
    frames_by_ccd: dict[int, list[dict]],
    target: str,
    jd: float,
    raw_dir: Path,
    max_px: int = PREVIEW_MAX_PX,
    calibrated_dir: Path | None = None,
) -> dict:
    """Per band: the frame closest in time to *jd* and a preview image.

    With *calibrated_dir* (MuSCAT/MuSCAT2) the calibrated frame is shown when
    it exists; otherwise the raw frame is used and the band is marked
    ``source="raw"`` with a note, so the caller can tell the user that frame has
    not been calibrated yet (calibrated files only exist for frames a
    photometry run has processed).
    """
    expected = calibrated_dir is not None
    out_bands: list[dict] = []
    for ccd in sorted(frames_by_ccd):
        frames = [f for f in frames_by_ccd[ccd] if f.get("object") == target]
        try:
            frame, offset_s = closest_frame(frames, jd)
        except ValueError:
            continue
        entry = {
            "ccd": ccd,
            "band": band_for_filter(frame.get("filter")),
            "frame": frame["filename"],
            "jd": _full_jd(frame["jd_start"]),
            "offset_s": round(offset_s, 3),
            "exptime": frame.get("exptime"),
            "image": None,
            "source": None,
        }
        path = find_calibrated_file(calibrated_dir, frame["filename"])
        if path is not None:
            entry["source"] = "calibrated"
        else:
            path = find_frame_file(raw_dir, frame["filename"])
            if path is not None:
                entry["source"] = "raw"
                if expected:
                    entry["note"] = "not calibrated yet (raw frame; a photometry run calibrates it)"
        if path is None:
            entry["error"] = f"file not found: {frame['filename']}"
        else:
            try:
                png = render_png(path, max_px)
                entry["image"] = "data:image/png;base64," + base64.b64encode(png).decode()
            except Exception as e:  # noqa: BLE001 - one bad frame must not blank the others
                entry["error"] = f"could not render {frame['filename']}: {e}"
        out_bands.append(entry)
    return {
        "ref_time": jd_to_iso(jd),
        "jd": jd,
        "calibrated_expected": expected,
        "bands": out_bands,
    }
