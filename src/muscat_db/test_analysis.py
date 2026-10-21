"""Measure closed-loop test observation frames and recommend science settings.

Only what BANZAI already produced is read: the SCI header and the per-source
``CAT`` table of each e91 product (one row per detected source, with ``peak``
and ``background`` in the image's units). No photometry runs here -- that
belongs in prose2 -- this compares catalog peak levels against the header
saturation level and scales them to a recommended exposure time.

Per (FOV, band) the recommendation carries two exposure times:

* ``exptime_target_s`` keeps the target's peak at ``SAFE_LEVEL_FRAC`` of the
  saturation level;
* ``exptime_with_comparison_s`` additionally keeps the brightest comparison
  star detected in the field below that level.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits

from muscat_db import test_observations

logger = logging.getLogger(__name__)

# Recommended exposures peak at this fraction of the saturation level.
SAFE_LEVEL_FRAC = 0.5
# A measured peak above this fraction of MAXLIN may be non-linear or clipped,
# so it only bounds the safe exposure from above instead of fixing it.
LINEAR_FRAC = 0.9
# The target is the brightest source this close to its coordinates. Request
# coordinates often lack proper motion (a real MuSCAT4 TOI frame put the target
# 5" off), so nearest-source matching within a few arcsec picks a neighbour.
TARGET_SEARCH_ARCSEC = 10.0
# Beyond this separation the identification is reported as a warning.
TARGET_MATCH_ARCSEC = 3.0
SOURCE_MATCH_ARCSEC = 2.0
# A comparison must be detected in this many frames of its FOV/band, which
# rejects cosmic rays that the per-frame catalog keeps as sources.
MIN_DETECTIONS = 2
# CAT-RA/CAT-DEC is the requested position, so it matches the configuration
# centre closely (header RA/DEC is the achieved pointing, tens of arcsec off).
# FOV candidates closer than test_observations.SAME_POINTING_ARCSEC are merged.
POINTING_TOL_ARCSEC = 2.0
EXPTIME_TOL_S = 0.01
DEFOCUS_TOL_MM = 0.01
# Defocus is only worth recommending when the in-focus safe exposure would
# spend more than a quarter of the time reading out.
MIN_DUTY_CYCLE = 0.75
# A comparison peaking below this fraction of saturation at the target's safe
# exposure is too faint to help.
MIN_COMP_FRAC = 0.05
# A neighbour contributing at least this fraction of the target's flux inside
# the photometric aperture is worth a warning (1% ~ 5 mag fainter).
CONTAM_FLUX_FRAC = 0.01
CONTAM_FWHM_MULT = 3.0
CONTAM_MIN_RADIUS_ARCSEC = 12.0
GAIA_FAINT_LIMIT = 21.0
# sep's OBJ_TRUNC flag: the source runs off the image edge.
SEP_OBJ_TRUNC = 2
DEFOCUS_STEP_MM = {"muscat": 0.5, "sinistro": 0.5, "qhy600": 0.025}
# Plan band key (muscat's simultaneous cameras) -> exposure-calculator band.
MUSCAT_BAND_CODES = {"g": "gp", "r": "rp", "i": "ip", "z": "zs"}

CoefLookup = Callable[[str, float], "tuple[float, float] | None"]
GaiaQuery = Callable[[float, float, float], object]


class FrameError(ValueError):
    pass


@dataclass(frozen=True)
class Frame:
    path: str
    request_id: int | None
    filter: str
    exptime: float
    reqtime: float
    defocus: float | None
    ra: float
    dec: float
    level: float
    pixscale: float
    sources: dict


def _header_float(header, *keys) -> float | None:
    for key in keys:
        try:
            value = float(header[key])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value):
            return value
    return None


def _pointing(header) -> tuple[float, float]:
    ra, dec = header.get("CAT-RA"), header.get("CAT-DEC")
    if ra is None or dec is None:
        ra, dec = header.get("RA"), header.get("DEC")
    if ra is None or dec is None:
        raise FrameError("header has no RA/DEC pointing")
    if isinstance(ra, str) and ":" in ra:
        coord = SkyCoord(ra, dec, unit=(u.hourangle, u.deg))
    else:
        coord = SkyCoord(float(ra), float(dec), unit=u.deg)
    return float(coord.ra.deg), float(coord.dec.deg)


def read_frame(path: Path | str) -> Frame:
    """Header metadata and source catalog of one BANZAI product."""
    try:
        with fits.open(path) as hdul:
            names = [h.name for h in hdul]
            if "SCI" not in names or "CAT" not in names:
                raise FrameError("no SCI/CAT extension")
            header = hdul["SCI"].header
            cat = hdul["CAT"].data
            columns = set(cat.columns.names)
            missing = {"ra", "dec", "peak", "background", "flux"} - columns
            if missing:
                raise FrameError(f"CAT lacks {', '.join(sorted(missing))}")
            sources = {name: np.asarray(cat[name], dtype=float) for name in ("ra", "dec", "peak", "background", "flux")}
            sources["fwhm"] = np.asarray(cat["fwhm"], dtype=float) if "fwhm" in columns else np.full(len(cat), np.nan)
            sources["flag"] = np.asarray(cat["flag"], dtype=int) if "flag" in columns else np.zeros(len(cat), int)
    except OSError as exc:
        raise FrameError(f"unreadable FITS: {exc}") from exc
    exptime = _header_float(header, "EXPTIME")
    if not exptime or exptime <= 0:
        raise FrameError("header has no positive EXPTIME")
    # MAXLIN (onset of non-linearity) precedes SATURATE: it is the safer
    # threshold and both are in the image's own units.
    level = _header_float(header, "MAXLIN", "SATURATE")
    if not level or level <= 0:
        raise FrameError("header has no MAXLIN/SATURATE level")
    wcs_error = header.get("WCSERR")
    if wcs_error not in (None, 0, "0"):
        # CAT ra/dec come from this solution; without it the target cannot be found.
        raise FrameError(f"BANZAI astrometric solution failed (WCSERR={wcs_error})")
    ra, dec = _pointing(header)
    request = header.get("REQNUM")
    try:
        request_id = int(request)
    except (TypeError, ValueError):
        request_id = None
    return Frame(
        path=str(path), request_id=request_id, filter=str(header.get("FILTER") or ""),
        exptime=exptime, reqtime=_header_float(header, "REQTIME") or exptime,
        defocus=_header_float(header, "FOCOBOFF", "FOCDMD"), ra=ra, dec=dec, level=level,
        pixscale=_header_float(header, "PIXSCALE") or float("nan"), sources=sources,
    )


def plan_band(kind: str, filter_name: str) -> str:
    """Plan exposure key for a frame filter: muscat's g/r/i/z camera, or the filter itself."""
    if kind in test_observations.SINGLE_FILTER_KINDS:
        return filter_name
    return filter_name[:1].lower()


def _offsets_arcsec(ra: np.ndarray, dec: np.ndarray, ra0: float, dec0: float) -> np.ndarray:
    dra = (np.asarray(ra) - ra0 + 180.0) % 360.0 - 180.0
    east = dra * math.cos(math.radians(dec0)) * 3600.0
    north = (np.asarray(dec) - dec0) * 3600.0
    return np.hypot(east, north)


def match_configuration(frame: Frame, plan: dict) -> int | None:
    """Index of the plan configuration that produced ``frame``, if unique."""
    band = plan_band(plan["kind"], frame.filter)
    matches = []
    for index, config in enumerate(plan["configurations"]):
        requested = config["exposure_times"].get(band)
        if requested is None or abs(requested - frame.reqtime) > EXPTIME_TOL_S:
            continue
        if frame.defocus is not None and abs(float(config["defocus_mm"]) - frame.defocus) > DEFOCUS_TOL_MM:
            continue
        offset = float(_offsets_arcsec(frame.ra, frame.dec, float(config["center_ra"]), float(config["center_dec"])))
        if offset > POINTING_TOL_ARCSEC:
            continue
        matches.append(index)
    return matches[0] if len(matches) == 1 else None


def _safe_exposure(total: float, exptime: float, level: float) -> tuple[float, bool]:
    """(safe exposure, is_upper_limit) from one measured peak+background level."""
    frac = total / level
    if frac < LINEAR_FRAC:
        return SAFE_LEVEL_FRAC * level * exptime / total, False
    # Clipped/non-linear: the true rate is at least this high.
    return SAFE_LEVEL_FRAC * exptime / frac, True


def _combine(estimates: list[tuple[float, bool]]) -> tuple[float, bool] | None:
    """Most conservative safe exposure; exact estimates win over upper limits."""
    exact = [t for t, bound in estimates if not bound]
    if exact:
        return min(exact), False
    bounds = [t for t, bound in estimates if bound]
    return (min(bounds), True) if bounds else None


def _floor_seconds(seconds: float) -> float:
    step = 0.1 if seconds >= 1.0 else 0.001
    return round(math.floor(seconds / step) * step, 6)


def _brightest_within(ra_arr, dec_arr, brightness, ra: float, dec: float) -> tuple[int, float] | None:
    """(index, separation) of the brightest entry within TARGET_SEARCH_ARCSEC."""
    if not len(ra_arr):
        return None
    sep = _offsets_arcsec(ra_arr, dec_arr, ra, dec)
    near = np.where((sep <= TARGET_SEARCH_ARCSEC) & np.isfinite(brightness))[0]
    if not len(near):
        return None
    index = int(near[np.argmax(brightness[near])])
    return index, float(sep[index])


def _target_index(frame: Frame, ra: float, dec: float) -> int | None:
    found = _brightest_within(frame.sources["ra"], frame.sources["dec"], frame.sources["flux"], ra, dec)
    return found[0] if found else None


def _group_stats(frames: list[Frame], ra: float, dec: float) -> dict:
    """Target and comparison safe exposures for the frames of one FOV and band."""
    target_estimates, target_fwhm, target_flux, target_sep = [], [], [], []
    for frame in frames:
        index = _target_index(frame, ra, dec)
        if index is None:
            continue
        target_sep.append(float(_offsets_arcsec(frame.sources["ra"][index], frame.sources["dec"][index], ra, dec)))
        total = frame.sources["peak"][index] + frame.sources["background"][index]
        if total > 0:
            target_estimates.append(_safe_exposure(total, frame.exptime, frame.level))
            target_flux.append(frame.sources["flux"][index] / frame.exptime)
        if np.isfinite(frame.sources["fwhm"][index]) and np.isfinite(frame.pixscale):
            target_fwhm.append(frame.sources["fwhm"][index] * frame.pixscale)
    target = _combine(target_estimates)
    stats: dict = {"n_frames": len(frames), "target_found": target is not None}
    if target is None:
        return stats
    t_target, target_bound = target
    stats["target"] = {
        "exptime_s": _floor_seconds(t_target), "upper_limit": target_bound,
        "fwhm_arcsec": round(float(np.median(target_fwhm)), 3) if target_fwhm else None,
        "flux_rate": float(np.median(target_flux)) if target_flux else None,
        "offset_arcsec": round(float(np.median(target_sep)), 2),
    }

    reference = max(frames, key=lambda f: len(f.sources["ra"]))
    ref_ra, ref_dec = reference.sources["ra"], reference.sources["dec"]
    ref_target = _target_index(reference, ra, dec)
    per_star: dict[int, list[tuple[float, bool]]] = {}
    for frame in frames:
        src = frame.sources
        usable = (src["flag"] & SEP_OBJ_TRUNC) == 0
        for i in np.where(usable)[0]:
            sep = _offsets_arcsec(ref_ra, ref_dec, src["ra"][i], src["dec"][i])
            j = int(np.argmin(sep))
            if sep[j] > SOURCE_MATCH_ARCSEC or j == ref_target:
                continue
            total = src["peak"][i] + src["background"][i]
            if total > 0:
                per_star.setdefault(j, []).append(_safe_exposure(total, frame.exptime, frame.level))
    min_detections = min(MIN_DETECTIONS, len(frames))
    comps = {j: _combine(est) for j, est in per_star.items() if len(est) >= min_detections}
    comps = {j: v for j, v in comps.items() if v is not None}

    brightest = None
    if comps:
        j, (t_comp, comp_bound) = min(comps.items(), key=lambda kv: kv[1][0])
        brightest = {
            "ra": round(float(ref_ra[j]), 6), "dec": round(float(ref_dec[j]), 6),
            "exptime_s": _floor_seconds(t_comp), "upper_limit": comp_bound,
            "sep_arcsec": round(float(_offsets_arcsec(ref_ra[j], ref_dec[j], ra, dec)), 1),
        }
    usable_comps = 0
    for t_comp, comp_bound in comps.values():
        # Peak fraction of this comparison at the target's safe exposure.
        frac_at_target = SAFE_LEVEL_FRAC * t_target / t_comp
        if not comp_bound and MIN_COMP_FRAC <= frac_at_target <= SAFE_LEVEL_FRAC:
            usable_comps += 1
    stats["brightest_comparison"] = brightest
    stats["n_comparisons"] = len(comps)
    stats["n_usable_comparisons"] = usable_comps
    stats["exptime_target_s"] = _floor_seconds(t_target)
    stats["exptime_with_comparison_s"] = _floor_seconds(min(t_target, brightest["exptime_s"]) if brightest else t_target)
    return stats


def _choose_fov(fov_stats: dict[int, dict[str, dict]]) -> int | None:
    """FOV with the most usable comparisons in every band; ties keep the optimized FOV."""
    eligible = {
        index: sum(s.get("n_usable_comparisons", 0) for s in bands.values())
        for index, bands in fov_stats.items()
        if bands and all(s.get("target_found") for s in bands.values())
    }
    if not eligible:
        return None
    return max(sorted(eligible), key=lambda index: eligible[index])


def default_coef_lookup(instrument: str) -> CoefLookup | None:
    """Peak coefficient vs focus from the exposure calculator, when it is calibrated.

    muscat3/muscat4 use the MuSCAT3 focus table (or their own DB calibration).
    Other instruments only have the MuSCAT3 table rescaled by aperture, whose mm
    scale does not transfer to different optics, so they need at least two
    calibrated focus points of their own.
    """
    from muscat_db import exposure

    coeffs = exposure.load_coeffs(instrument)
    needs_own_calibration = test_observations.kind_family(instrument) != "muscat"

    def lookup(band: str, focus_mm: float):
        if needs_own_calibration and sum(1 for (b, _f) in coeffs if b == band) < 2:
            return None
        return exposure.get_coeff(instrument, band, abs(focus_mm), coeffs)

    return lookup


def _calculator_band(kind: str, key: str) -> str | None:
    if kind in test_observations.SINGLE_FILTER_KINDS:
        from muscat_db.band_utils import _FILTER_BAND_ALIAS

        return _FILTER_BAND_ALIAS.get(key)
    return MUSCAT_BAND_CODES.get(key)


def recommend_defocus(kind: str, target_exptime: dict[str, float], tested_focus: float,
                      readout_s: float, coef_lookup: CoefLookup | None) -> dict:
    """Defocus only when the in-focus safe exposure would waste the duty cycle."""
    needed = readout_s * MIN_DUTY_CYCLE / (1.0 - MIN_DUTY_CYCLE)
    bright = {band: t for band, t in target_exptime.items() if t < needed}
    if not bright:
        return {"recommended": False, "defocus_mm": None,
                "reason": f"target is not bright: safe exposure ≥ {needed:.1f} s keeps the duty cycle ≥ {MIN_DUTY_CYCLE:.0%}"}
    from muscat_db import lco

    result = {"recommended": True, "defocus_mm": None, "min_exptime_s": round(needed, 1),
              "bright_bands": sorted(bright),
              "reason": f"target safe exposure below {needed:.1f} s ({MIN_DUTY_CYCLE:.0%} duty cycle with {readout_s:g} s readout)"}
    if coef_lookup is None:
        result["note"] = f"no focus calibration for {kind}; choose a defocus and re-test"
        return result
    limit = lco._DEFOCUS_LIMIT_MM[kind]
    step = DEFOCUS_STEP_MM[test_observations.kind_family(kind)]
    base = {}
    for band in bright:
        code = _calculator_band(kind, band)
        coef = coef_lookup(code, tested_focus) if code else None
        if coef is None:
            result["note"] = f"no focus calibration for band {band}; choose a defocus and re-test"
            return result
        base[band] = (code, coef[0])
    focus = abs(tested_focus)
    while focus < limit - 1e-9:
        focus = min(limit, round(focus + step, 6))
        predicted = {band: bright[band] * 10 ** (c0 - coef_lookup(code, focus)[0]) for band, (code, c0) in base.items()}
        if all(t >= needed for t in predicted.values()):
            result.update(defocus_mm=focus, predicted_exptime_s={b: _floor_seconds(t) for b, t in predicted.items()})
            return result
    result.update(defocus_mm=limit, note=f"even the ±{limit:g} mm limit keeps the exposure below {needed:.1f} s")
    return result


def _contamination(frames_by_band: dict[str, list[Frame]], ra: float, dec: float, radius: float,
                   gaia_query: GaiaQuery | None) -> tuple[list[dict], list[str]]:
    """Neighbours within ``radius`` of the identified target bright enough to add ≥CONTAM_FLUX_FRAC.

    Separations are measured from the target star itself (image or Gaia
    position), not from the requested coordinates, which can be arcsec off.
    """
    warnings: list[str] = []
    # One entry per neighbour across bands: (ra, dec, sep, worst ratio, bands).
    neighbours: list[list] = []
    for band, frames in sorted(frames_by_band.items()):
        reference = max(frames, key=lambda f: len(f.sources["ra"]))
        index = _target_index(reference, ra, dec)
        src = reference.sources
        if index is None or src["flux"][index] <= 0:
            continue
        sep = _offsets_arcsec(src["ra"], src["dec"], src["ra"][index], src["dec"][index])
        for j in np.where(sep <= radius)[0]:
            ratio = float(src["flux"][j] / src["flux"][index])
            if j == index or ratio < CONTAM_FLUX_FRAC:
                continue
            same = next((n for n in neighbours if _offsets_arcsec(n[0], n[1], src["ra"][j], src["dec"][j]) <= SOURCE_MATCH_ARCSEC), None)
            if same is None:
                neighbours.append([src["ra"][j], src["dec"][j], float(sep[j]), ratio, [band]])
            else:
                same[3] = max(same[3], ratio)
                same[4].append(band)
    found = [{"source": "image", "bands": bands, "sep_arcsec": round(sep_j, 1), "flux_ratio": round(ratio, 4)}
             for _ra, _dec, sep_j, ratio, bands in sorted(neighbours, key=lambda n: n[2])]
    if gaia_query is None:
        return found, warnings
    try:
        stars = gaia_query(ra, dec, radius + TARGET_SEARCH_ARCSEC)
    except Exception as exc:  # noqa: BLE001 - reported to the observer
        return found, [*warnings, f"Gaia contamination check unavailable: {exc}"]
    if stars is None or not len(stars):
        return found, warnings
    if getattr(stars, "error", None):
        return found, [*warnings, f"Gaia contamination check unavailable: {stars.error}"]
    gmag = np.asarray(stars.gmag, float)
    # Brightest = smallest G.
    match = _brightest_within(stars.ra, stars.dec, -gmag, ra, dec)
    if match is None:
        return found, [*warnings, "target not matched in Gaia; Gaia contamination check skipped"]
    t_idx = match[0]
    sep = _offsets_arcsec(stars.ra, stars.dec, stars.ra[t_idx], stars.dec[t_idx])
    for j in np.where(sep <= radius)[0]:
        if j == t_idx or not np.isfinite(gmag[j]):
            continue
        dmag = float(gmag[j] - gmag[t_idx])
        ratio = 10 ** (-0.4 * dmag)
        if ratio >= CONTAM_FLUX_FRAC:
            found.append({"source": "gaia", "sep_arcsec": round(float(sep[j]), 1),
                          "dmag": round(dmag, 2), "flux_ratio": round(ratio, 4)})
    return found, warnings


def default_gaia_query(ra: float, dec: float, radius: float):
    from muscat_db import fov

    return fov.cached_query_gaia_field(ra, dec, radius, min_mag=0.0, max_mag=GAIA_FAINT_LIMIT)


def analyze_record(record: dict, paths: list[Path], *, gaia_query: GaiaQuery | None = None,
                   coef_lookup: CoefLookup | None | str = "default") -> tuple[dict, dict]:
    """Result and recommendation for a finished test observation."""
    plan = record["plan"]
    kind = plan["kind"]
    target = plan.get("target_coord") or {}
    if target.get("ra") is None or target.get("dec") is None:
        raise test_observations.TestObservationError("plan has no target coordinates")
    ra, dec = float(target["ra"]), float(target["dec"])
    request_ids = {int(i) for i in record.get("request_ids") or []}
    if gaia_query is None:
        gaia_query = default_gaia_query
    if coef_lookup == "default":
        coef_lookup = default_coef_lookup(kind)

    skipped, unmatched = [], []
    groups: dict[tuple[int, str], list[Frame]] = {}
    seen_configs: set[int] = set()
    for path in paths:
        try:
            frame = read_frame(path)
        except FrameError as exc:
            skipped.append({"file": Path(path).name, "reason": str(exc)})
            continue
        if request_ids and frame.request_id not in request_ids:
            unmatched.append(Path(path).name)
            continue
        index = match_configuration(frame, plan)
        if index is None:
            unmatched.append(Path(path).name)
            continue
        seen_configs.add(index)
        fov_index = plan["configurations"][index]["fov_index"]
        groups.setdefault((fov_index, plan_band(kind, frame.filter)), []).append(frame)

    fov_stats: dict[int, dict[str, dict]] = {}
    for (fov_index, band), frames in sorted(groups.items()):
        fov_stats.setdefault(fov_index, {})[band] = _group_stats(frames, ra, dec)
    missing = [i for i in range(len(plan["configurations"])) if i not in seen_configs]
    result = {
        "version": test_observations.ANALYSIS_VERSION,
        "frames_analyzed": sum(len(f) for f in groups.values()),
        "skipped_frames": skipped, "unmatched_frames": unmatched,
        "missing_configurations": missing,
        "fovs": {str(i): bands for i, bands in fov_stats.items()},
    }

    best = _choose_fov(fov_stats)
    if best is None:
        return result, {"incomplete": True, "warnings": ["target was not measured in every band of any FOV"]}
    bands = fov_stats[best]
    fov = plan["fov_candidates"][best]
    tested_focus = float(plan["configurations"][0]["defocus_mm"])
    target_times = {b: s["exptime_target_s"] for b, s in bands.items()}
    defocus = recommend_defocus(kind, target_times, tested_focus, float(plan.get("readout_overhead_s") or 0.0), coef_lookup)

    fwhms = [s["target"]["fwhm_arcsec"] for s in bands.values() if s["target"].get("fwhm_arcsec")]
    fwhm_arcsec = max(fwhms) if fwhms else 0.0
    if defocus.get("defocus_mm") and coef_lookup is not None:
        pixscale = next((f.pixscale for f in next(iter(groups.values())) if np.isfinite(f.pixscale)), float("nan"))
        for band in bands:
            code = _calculator_band(kind, band)
            coef = coef_lookup(code, defocus["defocus_mm"]) if code else None
            if coef is not None and np.isfinite(pixscale):
                fwhm_arcsec = max(fwhm_arcsec, coef[1] * pixscale)
    radius = max(CONTAM_MIN_RADIUS_ARCSEC, CONTAM_FWHM_MULT * fwhm_arcsec)
    frames_by_band = {band: frames for (index, band), frames in groups.items() if index == best}
    contamination, warnings = _contamination(frames_by_band, ra, dec, radius, gaia_query)

    offset = max(s["target"]["offset_arcsec"] for s in bands.values())
    if offset > TARGET_MATCH_ARCSEC:
        warnings.append(f"target taken as the brightest source {offset:.1f}\" from its coordinates; "
                        "check the coordinates' proper motion/epoch")
    for band, stats in bands.items():
        if stats["target"]["upper_limit"]:
            warnings.append(f"{band}: target saturated in every test frame; its safe exposure is an upper limit")
        comp = stats.get("brightest_comparison")
        if comp and comp["upper_limit"]:
            warnings.append(f"{band}: brightest comparison saturated in every test frame; its limit is an upper bound")
    if contamination:
        warnings.append(f"{len(contamination)} source(s) within {radius:.0f}\" may contaminate the target aperture")
    recommendation = {
        "fov_index": best, "center_ra": fov["center_ra"], "center_dec": fov["center_dec"], "pa_deg": 0,
        # Candidate 1 is the target-centred FOV; a single candidate means the
        # optimized pointing already was target-centred.
        "target_centred": best == 1 or len(plan["fov_candidates"]) == 1,
        "exposure_times_target": target_times,
        "exposure_times_with_comparison": {b: s["exptime_with_comparison_s"] for b, s in bands.items()},
        "safe_level_frac": SAFE_LEVEL_FRAC, "tested_defocus_mm": tested_focus,
        "defocus": defocus, "contamination_radius_arcsec": round(radius, 1),
        "contamination": contamination, "warnings": warnings,
        "incomplete": bool(missing),
    }
    return result, recommendation
