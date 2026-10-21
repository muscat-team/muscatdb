"""Archive-calibrated peak-count model for BANZAI-reduced LCO instruments.

Every BANZAI e91 product carries what a saturation model needs, measured on
that frame:

* ``L1ZP``  -- photometric zeropoint, mag for 1 e-/s, at the frame's airmass;
* ``L1FWHM`` -- image FWHM (arcsec), seeing plus any defocus;
* ``MAXLIN`` -- onset of non-linearity (e-), which depends on camera and
  readout mode (Sinistro's 2x2-binned mode saturates ~2x higher);
* the ``CAT`` table, whose ``peak``/``flux`` ratio of bright unsaturated stars
  is the fraction of a star's light landing in its brightest pixel.

The model is

    peak e-/s = 10**(-0.4 * (mag + k * (X - 1) - ZP)) * A / FWHM_pix**2
    FWHM**2   = seeing**2 + (c * defocus_mm)**2

with ZP, A, c, the good-seeing FWHM and the saturation level fitted per
instrument, band and readout mode from archive frames.
"""

from __future__ import annotations

import json
import math
import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from astropy.io import fits

# Bright stars used for the peak fraction must stay well inside the linear range
# and well above the sky noise.
FPK_MAX_LEVEL_FRAC = 0.7
FPK_MIN_PEAK_SIGMA = 20.0
FPK_STARS = 20
FPK_MIN_STARS = 5

# Filename patterns selecting each instrument's own frames. Directories alone
# are not enough: /data/Sinistro holds MuSCAT (ep) frames and /data/MuSCAT4
# holds Spectral (fs01) frames.
FRAME_PATTERNS = {
    "muscat3": re.compile(r"^ogg2m001-ep\d\d-"),
    "muscat4": re.compile(r"^coj2m002-ep\d\d-"),
    "sinistro": re.compile(r"^[a-z]{3}1m\d{3}-fa\d\d-"),
    "qhy600": re.compile(r"^[a-z]{3}0m4\d{2}-sq\d\d-"),
    "sbig": re.compile(r"^[a-z]{3}0m4\d{2}-kb\d\d-"),
}


@dataclass(frozen=True)
class FrameSample:
    filename: str
    band: str
    mode: str
    date: str
    zp: float
    airmass: float
    fwhm_arcsec: float
    pixscale: float
    defocus_mm: float
    level: float
    peak_fraction: float


def _float(header, key) -> float | None:
    try:
        value = float(header[key])
    except (KeyError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def measure_frame(path: Path | str) -> FrameSample | None:
    """Model inputs measured on one BANZAI product, or None when it lacks them."""
    try:
        with fits.open(path) as hdul:
            if "SCI" not in hdul or "CAT" not in hdul:
                return None
            header = hdul["SCI"].header
            cat = hdul["CAT"].data
            if cat is None or not {"peak", "flux", "background"} <= set(cat.columns.names):
                return None
            peak = np.asarray(cat["peak"], float)
            flux = np.asarray(cat["flux"], float)
            background = np.asarray(cat["background"], float)
            flag = np.asarray(cat["flag"], int) if "flag" in cat.columns.names else np.zeros(len(cat), int)
    except (OSError, ValueError):
        return None
    zp, airmass = _float(header, "L1ZP"), _float(header, "AIRMASS")
    fwhm, pixscale, level = _float(header, "L1FWHM"), _float(header, "PIXSCALE"), _float(header, "MAXLIN")
    if None in (zp, airmass, fwhm, pixscale, level) or not (10 < zp < 35) or not (1 <= airmass < 3):
        return None
    if fwhm <= 0 or pixscale <= 0 or level <= 0:
        return None
    good = ((flux > 0) & (flag == 0) & (peak + background < FPK_MAX_LEVEL_FRAC * level)
            & (peak > FPK_MIN_PEAK_SIGMA * np.sqrt(np.maximum(background, 1.0))))
    if good.sum() < FPK_MIN_STARS:
        return None
    brightest = np.argsort(-flux[good])[:FPK_STARS]
    fraction = float(np.median((peak[good] / flux[good])[brightest]))
    defocus = _float(header, "FOCOBOFF")
    return FrameSample(
        filename=Path(path).name, band=str(header.get("FILTER") or ""),
        mode=str(header.get("CONFMODE") or ""), date=str(header.get("DATE-OBS") or "")[:10],
        zp=zp, airmass=airmass, fwhm_arcsec=fwhm, pixscale=pixscale,
        defocus_mm=abs(defocus) if defocus is not None else 0.0, level=level, peak_fraction=fraction,
    )


# --- fit -----------------------------------------------------------------------

# A frame is in focus below this defocus.
IN_FOCUS_MM = 0.3
# Frames this far out of focus fix the FWHM-vs-defocus slope.
DEFOCUS_FIT_MIN_MM = 0.9
MIN_FRAMES = 8
MIN_DEFOCUS_FRAMES = 5
MIN_FOCUS_FRAMES = 3
# Zeropoint used for saturation: good-transparency nights, not the median.
SAFE_ZP_PERCENTILE = 84.0
# Default seeing when the observer gives none: good seeing gives the highest peak.
GOOD_SEEING_PERCENTILE = 16.0
# Defocused image size varies frame to frame; a narrow donut gives the highest
# peak, so saturation uses this percentile of the FWHM-vs-defocus slope.
SAFE_DEFOCUS_PERCENTILE = 16.0
# The saturation level is the lowest MAXLIN seen recently: cameras within one
# instrument class differ (QHY600 units span 42000-47400 e-).
LEVEL_RECENT_DAYS = 365
# Readout modes the model distinguishes. MuSCAT's fast/slow readout changes
# neither the pixel scale nor the saturation level, so its modes are pooled.
MODEL_MODES = {"sinistro": ("central_2k_2x2", "full_frame"), "qhy600": ("central30x30", "full_frame")}


@dataclass(frozen=True)
class BandModel:
    band: str
    mode: str
    zp_safe: float          # zenith zeropoint, mag for 1 e-/s
    zp_median: float
    a_focus: float          # peak fraction x FWHM_pix**2, in focus
    a_defocus: float | None
    defocus_c: float | None  # arcsec of FWHM per mm of defocus (median)
    defocus_c_safe: float | None  # SAFE_DEFOCUS_PERCENTILE slope, used for saturation
    seeing_good: float      # arcsec
    seeing_median: float
    pixscale: float         # arcsec/pixel in this mode
    level: float            # saturation (MAXLIN), e-
    n_frames: int
    source: str = "archive"

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def mode_key(instrument: str, mode: str) -> str | None:
    """Model mode for a frame's CONFMODE; None for modes the model does not cover."""
    modes = MODEL_MODES.get(instrument)
    if modes is None:
        return ""
    return mode if mode in modes else None


def _recent_level(samples: list[FrameSample]) -> float:
    dates = sorted(s.date for s in samples if s.date)
    if not dates:
        return min(s.level for s in samples)
    newest = np.datetime64(dates[-1])
    recent = [s.level for s in samples if s.date and (newest - np.datetime64(s.date)) <= np.timedelta64(LEVEL_RECENT_DAYS, "D")]
    return float(min(recent or [s.level for s in samples]))


def fit_band(samples: list[FrameSample], extinction: float) -> BandModel | None:
    """Model for one instrument band/mode from its archive frames."""
    if len(samples) < MIN_FRAMES:
        return None
    zp = np.array([s.zp + extinction * (s.airmass - 1.0) for s in samples])
    fwhm = np.array([s.fwhm_arcsec for s in samples])
    pix = np.array([s.pixscale for s in samples])
    defocus = np.array([s.defocus_mm for s in samples])
    a = np.array([s.peak_fraction for s in samples]) * (fwhm / pix) ** 2
    focused = defocus < IN_FOCUS_MM
    seeing = fwhm[focused] if focused.sum() >= MIN_FOCUS_FRAMES else fwhm
    a_focus = float(np.median(a[focused])) if focused.sum() >= MIN_FOCUS_FRAMES else float(np.median(a))

    far = defocus >= DEFOCUS_FIT_MIN_MM
    a_defocus = c = c_safe = None
    if far.sum() >= MIN_DEFOCUS_FRAMES and focused.sum() >= MIN_FOCUS_FRAMES:
        # Each slope is measured against the seeing the prediction pairs it
        # with: the median for the typical image, good seeing for the safe one.
        # Then SAFE_DEFOCUS_PERCENTILE of defocused frames are narrower than
        # the safe curve FWHM**2 = seeing_good**2 + (c_safe * d)**2.
        s_good = float(np.percentile(fwhm[focused], GOOD_SEEING_PERCENTILE))
        s_median = float(np.median(fwhm[focused]))
        c = float(np.median(np.sqrt(np.maximum(fwhm[far] ** 2 - s_median ** 2, 0.0)) / defocus[far]))
        c_safe = float(np.percentile(np.sqrt(np.maximum(fwhm[far] ** 2 - s_good ** 2, 0.0)) / defocus[far],
                                     SAFE_DEFOCUS_PERCENTILE))
        # Measured against the smaller good seeing, the slope absorbs part of
        # the seeing; capped at the median slope, the safe image is never wider
        # than the typical one at any defocus.
        c_safe = min(c_safe, c)
        a_defocus = float(np.median(a[defocus >= IN_FOCUS_MM]))
    return BandModel(
        band=samples[0].band, mode="", zp_safe=float(np.percentile(zp, SAFE_ZP_PERCENTILE)),
        zp_median=float(np.median(zp)), a_focus=a_focus, a_defocus=a_defocus, defocus_c=c, defocus_c_safe=c_safe,
        seeing_good=float(np.percentile(seeing, GOOD_SEEING_PERCENTILE)), seeing_median=float(np.median(seeing)),
        pixscale=float(np.median(pix)), level=_recent_level(samples), n_frames=len(samples),
    )


def fit_instrument(instrument: str, samples: list[FrameSample], extinction: dict[str, float]) -> list[BandModel]:
    groups: dict[tuple[str, str], list[FrameSample]] = {}
    for sample in samples:
        mode = mode_key(instrument, sample.mode)
        if mode is None or sample.band not in extinction:
            continue
        groups.setdefault((sample.band, mode), []).append(sample)
    models = []
    for (band, mode), group in sorted(groups.items()):
        model = fit_band(group, extinction[band])
        if model is not None:
            models.append(BandModel(**{**model.as_dict(), "band": band, "mode": mode}))
    return models


# --- prediction --------------------------------------------------------------------

def fwhm_arcsec(model: BandModel, defocus_mm: float, seeing: float | None = None, *, safe: bool = False) -> float:
    """Image FWHM for this seeing (default: good seeing) and defocus.

    ``safe`` uses the narrow-donut slope, for saturation estimates.
    """
    s = seeing if seeing and seeing > 0 else model.seeing_good
    # Without a measured defocus slope, assume defocus does not widen the image:
    # that over-predicts the peak, the safe side for saturation.
    c = (model.defocus_c_safe if safe else model.defocus_c) or 0.0
    return math.hypot(s, c * abs(defocus_mm))


def peak_fraction(model: BandModel, defocus_mm: float, seeing: float | None = None) -> float:
    """Fraction of a star's flux in its brightest pixel, on the saturation-safe side."""
    fwhm_pix = fwhm_arcsec(model, defocus_mm, seeing, safe=True) / model.pixscale
    defocused = abs(defocus_mm) >= IN_FOCUS_MM and model.defocus_c and model.a_defocus
    a = model.a_defocus if defocused else model.a_focus
    return min(1.0, a / fwhm_pix ** 2)


def peak_rate(model: BandModel, mag: float, airmass: float, extinction: float,
              defocus_mm: float = 0.0, seeing: float | None = None) -> float:
    """Peak-pixel electrons per second (sky excluded) for a star of ``mag``."""
    flux = 10 ** (-0.4 * (mag + extinction * (airmass - 1.0) - model.zp_safe))
    return flux * peak_fraction(model, defocus_mm, seeing)


# --- storage and lookup ------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS exposure_models (
    instrument  TEXT NOT NULL,
    band        TEXT NOT NULL,
    mode        TEXT NOT NULL DEFAULT '',
    params_json TEXT NOT NULL,
    n_frames    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (instrument, band, mode)
);
"""

# Two readout modes of one camera share a fit when their pixels are the same
# size (QHY600's central30x30 is a crop of full_frame).
SAME_PIXSCALE_TOL = 0.02


def _default_models(instrument: str) -> dict[tuple[str, str], BandModel]:
    from muscat_db.exposure_model_defaults import DEFAULT_MODELS, DEFAULT_SOURCE, FIELDS

    source = DEFAULT_SOURCE.get(instrument, "default")
    models = {}
    for row in DEFAULT_MODELS.get(instrument, []):
        model = BandModel(**dict(zip(FIELDS, row)), source=source)
        models[(model.band, model.mode)] = model
    return models


MODEL_INSTRUMENTS = frozenset({"muscat3", "muscat4", "sinistro", "qhy600", "sbig"})


def save_models(conn, instrument: str, models: list[BandModel]) -> None:
    """Replace an instrument's stored fit."""
    conn.executescript(SCHEMA)
    conn.execute("DELETE FROM exposure_models WHERE instrument = ?", (instrument,))
    conn.executemany(
        "INSERT INTO exposure_models (instrument, band, mode, params_json, n_frames) VALUES (?, ?, ?, ?, ?)",
        [(instrument, m.band, m.mode, json.dumps({**m.as_dict(), "source": "archive"}, sort_keys=True), m.n_frames)
         for m in models],
    )
    conn.commit()


def stored_models(conn, instrument: str) -> dict[tuple[str, str], tuple[BandModel, str]]:
    conn.executescript(SCHEMA)
    rows = conn.execute(
        "SELECT band, mode, params_json, updated_at FROM exposure_models WHERE instrument = ?", (instrument,)
    ).fetchall()
    return {(r[0], r[1]): (BandModel(**json.loads(r[2])), r[3]) for r in rows}


def models_for(conn, instrument: str) -> dict[tuple[str, str], BandModel]:
    """Stored fit where one exists, built-in defaults elsewhere."""
    merged = _default_models(instrument)
    merged.update({key: model for key, (model, _updated) in stored_models(conn, instrument).items()})
    return merged


def default_mode(instrument: str) -> str:
    modes = MODEL_MODES.get(instrument)
    return modes[0] if modes else ""


def lookup(models: dict[tuple[str, str], BandModel], instrument: str, band: str, mode: str | None,
           narrow_parent: dict[str, str], narrow_offset_mag: Callable[[str], float]) -> BandModel | None:
    """Model for a band and readout mode.

    A narrowband filter without its own fit uses its broadband parent, fainter
    by the bandwidth ratio. A readout mode without its own fit borrows the
    other mode's when the pixels are the same size, with the lower saturation
    level of the two.
    """
    mode = mode if mode in MODEL_MODES.get(instrument, ("",)) else default_mode(instrument)
    model = models.get((band, mode))
    if model is None:
        same_band = [m for (b, _), m in models.items() if b == band]
        pixscale = next((m.pixscale for (_, md), m in models.items() if md == mode), None)
        for other in same_band:
            if pixscale and abs(other.pixscale - pixscale) <= SAME_PIXSCALE_TOL * pixscale:
                level = min(m.level for (_, md), m in models.items() if md == mode)
                model = BandModel(**{**other.as_dict(), "mode": mode, "level": min(level, other.level)})
                break
    if model is None and band in narrow_parent:
        parent = lookup(models, instrument, narrow_parent[band], mode, narrow_parent, narrow_offset_mag)
        if parent is not None:
            offset = narrow_offset_mag(band)
            model = BandModel(**{**parent.as_dict(), "band": band, "zp_safe": parent.zp_safe + offset,
                                 "zp_median": parent.zp_median + offset})
    return model


# --- calibration from the archive -------------------------------------------------

CALIBRATION_MAX_FRAMES = 800
CALIBRATION_FRAMES_PER_NIGHT = 4
# Fixed so a re-run over an unchanged archive reproduces the same fit.
CALIBRATION_SEED = 20261021


def archive_frames(instrument: str, root: str, max_frames: int = CALIBRATION_MAX_FRAMES):
    """Frame paths spread over randomly chosen nights of the instrument's archive."""
    pattern = FRAME_PATTERNS[instrument]
    nights = sorted(Path(root).glob("[0-9]" * 6))
    random.Random(CALIBRATION_SEED).shuffle(nights)
    picked = 0
    rng = random.Random(CALIBRATION_SEED)
    for night in nights:
        files = sorted(p for p in night.glob("*-e91.fits*") if pattern.match(p.name))
        for path in rng.sample(files, min(CALIBRATION_FRAMES_PER_NIGHT, len(files))):
            yield path
            picked += 1
            if picked >= max_frames:
                return


def calibrate(instrument: str, root: str, extinction: dict[str, float], conn_factory,
              cancel_event=None, progress=None, max_frames: int = CALIBRATION_MAX_FRAMES) -> dict:
    """Fit an instrument's model from its archive frames and store it."""
    if instrument not in FRAME_PATTERNS:
        return {"ok": False, "instrument": instrument, "error": f"no archive model for {instrument}"}
    samples, scanned = [], 0
    for path in archive_frames(instrument, root, max_frames):
        if cancel_event is not None and cancel_event.is_set():
            return {"ok": False, "cancelled": True, "instrument": instrument}
        scanned += 1
        sample = measure_frame(path)
        if sample is not None:
            samples.append(sample)
        if progress is not None and scanned % 20 == 0:
            progress("measuring", scanned, max_frames)
    models = fit_instrument(instrument, samples, extinction)
    if not models:
        return {"ok": False, "instrument": instrument, "total_frames": scanned, "usable_frames": len(samples),
                "error": "no band had enough frames with L1ZP, L1FWHM and MAXLIN; built-in defaults stay in use"}
    conn = conn_factory()
    try:
        save_models(conn, instrument, models)
    finally:
        conn.close()
    return {"ok": True, "instrument": instrument, "bands_calibrated": len(models),
            "total_frames": scanned, "usable_frames": len(samples), "source": "archive"}
