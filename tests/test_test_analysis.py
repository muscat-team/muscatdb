"""Closed-loop test analysis on synthetic BANZAI products (SCI header + CAT table)."""

import math

import numpy as np
import pytest
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits

from muscat_db import fov, test_analysis as subject

TARGET = (150.0, 20.0)
MAXLIN = 100000.0
ARCSEC = 1.0 / 3600.0


def _plan(kind="muscat3", exposures=(10.0, 20.0), fovs=((150.01, 20.0), (150.0, 20.0)), defocus=0.0):
    configs = []
    for fov_index, (ra, dec) in enumerate(fovs):
        for t in exposures:
            times = {"rp": t} if kind in ("sinistro", "qhy600") else {"g": t, "r": t, "i": t, "z": t}
            configs.append({"fov_index": fov_index, "center_ra": ra, "center_dec": dec, "pa_deg": 0,
                            "exposure_times": times, "exposure_time": t, "defocus_mm": defocus, "repeats": 3})
    return {
        "kind": kind, "target_coord": {"ra": TARGET[0], "dec": TARGET[1]},
        "fov_candidates": [{"center_ra": ra, "center_dec": dec, "pa_deg": 0} for ra, dec in fovs],
        "configurations": configs, "readout_overhead_s": 6.0,
    }


def _write(path, *, filt, exptime, pointing, stars, defocus=0.0, reqnum=11, maxlin=MAXLIN, wcserr=0):
    """``stars`` is a list of (ra, dec, peak_rate e-/s, flux_rate)."""
    ra = np.array([s[0] for s in stars])
    dec = np.array([s[1] for s in stars])
    background = np.full(len(stars), 100.0)
    peak = np.array([min(s[2] * exptime + 100.0, maxlin * 1.02) - 100.0 for s in stars])
    flux = np.array([s[3] * exptime for s in stars])
    cat = fits.BinTableHDU.from_columns([
        fits.Column(name="ra", format="D", array=ra), fits.Column(name="dec", format="D", array=dec),
        fits.Column(name="peak", format="D", array=peak), fits.Column(name="background", format="D", array=background),
        fits.Column(name="flux", format="D", array=flux), fits.Column(name="fwhm", format="D", array=np.full(len(stars), 6.0)),
        fits.Column(name="flag", format="J", array=np.zeros(len(stars), int)),
    ], name="CAT")
    # Like real BANZAI headers: CAT-RA/CAT-DEC is the requested position
    # (sexagesimal), RA/DEC the achieved pointing tens of arcsec away.
    requested = SkyCoord(pointing[0], pointing[1], unit=u.deg)
    header = fits.Header({
        "EXPTIME": exptime, "REQTIME": exptime, "FILTER": filt, "MAXLIN": maxlin, "SATURATE": maxlin, "GAIN": 1.0,
        "PIXSCALE": 0.267, "FOCOBOFF": defocus, "REQNUM": str(reqnum), "WCSERR": wcserr,
        "CAT-RA": requested.ra.to_string(u.hourangle, sep=":", precision=4),
        "CAT-DEC": requested.dec.to_string(u.deg, sep=":", precision=3, alwayssign=True),
        "RA": float(pointing[0]) + 20 * ARCSEC, "DEC": float(pointing[1]) - 15 * ARCSEC,
    })
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(np.zeros((2, 2), "f4"), header=header, name="SCI"), cat]).writeto(path)
    return path


def _star(offset_arcsec, peak_rate, flux_rate=None):
    return (TARGET[0] + offset_arcsec * ARCSEC / math.cos(math.radians(TARGET[1])), TARGET[1], peak_rate,
            flux_rate if flux_rate is not None else peak_rate * 20)


def _frames(tmp_path, plan, stars_by_band, filters=("gp", "rp", "ip", "zs"), repeats=3):
    paths = []
    for config in plan["configurations"]:
        for f in filters:
            key = f if plan["kind"] in ("sinistro", "qhy600") else f[0]
            for n in range(repeats):
                name = f"{config['fov_index']}-{config['exposure_time']}-{f}-{n}.fits"
                paths.append(_write(tmp_path / name, filt=f, exptime=config["exposure_times"][key],
                                    pointing=(config["center_ra"], config["center_dec"]),
                                    stars=stars_by_band(config["fov_index"], f), defocus=config["defocus_mm"]))
    return paths


def _no_gaia(ra, dec, radius):
    return None


def _record(plan):
    return {"plan": plan, "request_ids": [11]}


def test_read_frame_takes_maxlin_and_requested_position(tmp_path):
    path = _write(tmp_path / "f.fits", filt="rp", exptime=10, pointing=(150.0, 20.0), stars=[_star(0, 1000)])
    frame = subject.read_frame(path)
    assert frame.level == MAXLIN
    assert frame.request_id == 11
    # CAT-RA/CAT-DEC, not the achieved RA/DEC pointing 25" away.
    assert frame.ra == pytest.approx(150.0, abs=1e-5)
    assert frame.dec == pytest.approx(20.0, abs=1e-5)


def test_frame_with_failed_astrometry_is_skipped(tmp_path):
    path = _write(tmp_path / "f.fits", filt="rp", exptime=10, pointing=(150.0, 20.0), stars=[_star(0, 1000)], wcserr=4)
    with pytest.raises(subject.FrameError, match="WCSERR"):
        subject.read_frame(path)


def test_target_is_the_brightest_source_near_its_coordinates(tmp_path):
    # Proper motion puts the target 5" from its coordinates; a faint source sits closer.
    plan = _plan(fovs=((150.0, 20.0),))
    paths = _frames(tmp_path, plan, lambda fov_index, f: [_star(1.5, 20), _star(5, 2000), _star(90, 600), _star(150, 300)], filters=("rp",))
    result, rec = subject.analyze_record(_record(plan), paths, gaia_query=_no_gaia, coef_lookup=None)
    target = result["fovs"]["0"]["r"]["target"]
    assert target["offset_arcsec"] == pytest.approx(5, abs=0.1)
    assert target["exptime_s"] < 30  # the bright star's limit, not the faint neighbour's
    assert any("brightest source" in w for w in rec["warnings"])


def test_frame_without_catalog_is_skipped(tmp_path):
    path = tmp_path / "nocat.fits"
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(np.zeros((2, 2)), name="SCI")]).writeto(path)
    with pytest.raises(subject.FrameError, match="CAT"):
        subject.read_frame(path)


def test_match_configuration_uses_band_time_and_pointing(tmp_path):
    plan = _plan()
    frame = subject.read_frame(_write(tmp_path / "f.fits", filt="ip", exptime=20.0, pointing=(150.0, 20.0), stars=[_star(0, 10)]))
    assert subject.match_configuration(frame, plan) == 3  # FOV 1, 20 s
    off = subject.read_frame(_write(tmp_path / "g.fits", filt="ip", exptime=15.0, pointing=(150.0, 20.0), stars=[_star(0, 10)]))
    assert subject.match_configuration(off, plan) is None


def test_target_safe_exposure_is_half_maxlin_over_peak_rate(tmp_path):
    plan = _plan(fovs=((150.0, 20.0),))
    # Target peak+background rate 2000 e-/s (peak 2000*t, background 100 per frame).
    paths = _frames(tmp_path, plan, lambda fov_index, f: [_star(0, 2000), _star(60, 100), _star(120, 80)], filters=("rp",))
    result, rec = subject.analyze_record(_record(plan), paths, gaia_query=_no_gaia, coef_lookup=None)
    stats = result["fovs"]["0"]["r"]
    # Most conservative exact frame: the 10 s one, whose fixed background
    # makes its (peak + background) rate the highest.
    expected = min(0.5 * MAXLIN * t / (2000 * t + 100) for t in (10.0, 20.0))
    assert stats["target"]["exptime_s"] == pytest.approx(math.floor(expected * 10) / 10)
    assert stats["target"]["upper_limit"] is False
    assert rec["exposure_times_target"]["r"] == stats["exptime_target_s"]


def test_saturated_target_gives_an_upper_limit(tmp_path):
    plan = _plan(fovs=((150.0, 20.0),))
    paths = _frames(tmp_path, plan, lambda fov_index, f: [_star(0, 20000), _star(60, 100), _star(120, 80)], filters=("rp",))
    result, rec = subject.analyze_record(_record(plan), paths, gaia_query=_no_gaia, coef_lookup=None)
    assert result["fovs"]["0"]["r"]["target"]["upper_limit"] is True
    assert any("upper limit" in w for w in rec["warnings"])


def test_brightest_comparison_caps_the_second_exposure(tmp_path):
    plan = _plan(fovs=((150.0, 20.0),))
    paths = _frames(tmp_path, plan, lambda fov_index, f: [_star(0, 1000), _star(90, 4000), _star(150, 300)], filters=("rp",))
    result, rec = subject.analyze_record(_record(plan), paths, gaia_query=_no_gaia, coef_lookup=None)
    stats = result["fovs"]["0"]["r"]
    assert stats["brightest_comparison"]["sep_arcsec"] == pytest.approx(90, abs=0.5)
    assert rec["exposure_times_with_comparison"]["r"] == stats["brightest_comparison"]["exptime_s"]
    assert rec["exposure_times_with_comparison"]["r"] < rec["exposure_times_target"]["r"]


def test_single_frame_cosmic_ray_is_not_a_comparison(tmp_path):
    plan = _plan(fovs=((150.0, 20.0),), exposures=(10.0,))
    paths = _frames(tmp_path, plan, lambda fov_index, f: [_star(0, 1000), _star(150, 300)], filters=("rp",))
    # A very bright "source" only in the first frame.
    paths[0].unlink()
    _write(paths[0], filt="rp", exptime=10.0, pointing=(150.0, 20.0),
           stars=[_star(0, 1000), _star(150, 300), _star(200, 50000)])
    result, _ = subject.analyze_record(_record(plan), paths, gaia_query=_no_gaia, coef_lookup=None)
    assert result["fovs"]["0"]["r"]["brightest_comparison"]["sep_arcsec"] == pytest.approx(150, abs=0.5)


def test_fov_with_more_usable_comparisons_wins(tmp_path):
    plan = _plan()

    def stars(fov_index, f):
        if fov_index == 0:  # optimized: several comparisons near the target's brightness
            return [_star(0, 1000), _star(60, 800), _star(90, 600), _star(120, 900)]
        return [_star(0, 1000), _star(60, 1)]

    paths = _frames(tmp_path, plan, stars, filters=("rp",))
    _, rec = subject.analyze_record(_record(plan), paths, gaia_query=_no_gaia, coef_lookup=None)
    assert rec["fov_index"] == 0
    assert rec["target_centred"] is False


def test_missing_configuration_marks_incomplete(tmp_path):
    plan = _plan(fovs=((150.0, 20.0),))
    paths = _frames(tmp_path, plan, lambda fov_index, f: [_star(0, 1000), _star(60, 500)], filters=("rp",))
    keep = [p for p in paths if "-20.0-" not in p.name]
    result, rec = subject.analyze_record(_record(plan), keep, gaia_query=_no_gaia, coef_lookup=None)
    assert result["missing_configurations"] == [1]
    assert rec["incomplete"] is True


def _muscat3_coef(band, focus):
    from muscat_db import exposure

    return exposure._muscat3_coef(band, abs(focus))


def test_defocus_not_recommended_for_a_faint_target():
    rec = subject.recommend_defocus("muscat3", {"r": 60.0}, 0.0, 6.0, _muscat3_coef)
    assert rec["recommended"] is False


def test_defocus_recommended_and_sized_for_a_bright_target():
    # 6 s readout -> 18 s needed for a 75% duty cycle.
    rec = subject.recommend_defocus("muscat3", {"r": 4.0}, 0.0, 6.0, _muscat3_coef)
    assert rec["recommended"] is True
    assert 0 < rec["defocus_mm"] <= 8
    assert rec["predicted_exptime_s"]["r"] >= 18.0
    smaller = round(rec["defocus_mm"] - 0.5, 6)
    if smaller > 0:
        assert 4.0 * 10 ** (_muscat3_coef("rp", 0)[0] - _muscat3_coef("rp", smaller)[0]) < 18.0


def test_defocus_without_calibration_says_so():
    rec = subject.recommend_defocus("qhy600", {"rp": 1.0}, 0.0, 4.0, None)
    assert rec["recommended"] is True
    assert rec["defocus_mm"] is None
    assert "calibration" in rec["note"]


def test_contamination_flags_image_and_gaia_neighbours(tmp_path):
    plan = _plan(fovs=((150.0, 20.0),))
    # Neighbour 5" away at 10% of the target flux.
    paths = _frames(tmp_path, plan, lambda fov_index, f: [_star(0, 1000, 20000), _star(5, 100, 2000), _star(90, 600)], filters=("rp",))

    def gaia(ra, dec, radius):
        return fov.StarField(
            ra=np.array([TARGET[0], _star(8, 0)[0]]), dec=np.array([TARGET[1], TARGET[1]]),
            gmag=np.array([10.0, 13.0]), bp_rp=np.array([0.8, 0.9]),
        )

    _, rec = subject.analyze_record(_record(plan), paths, gaia_query=gaia, coef_lookup=None)
    sources = {c["source"] for c in rec["contamination"]}
    assert sources == {"image", "gaia"}
    gaia_hit = next(c for c in rec["contamination"] if c["source"] == "gaia")
    assert gaia_hit["dmag"] == 3.0
    assert any("contaminate" in w for w in rec["warnings"])


def test_gaia_failure_is_a_warning_not_an_error(tmp_path):
    plan = _plan(fovs=((150.0, 20.0),))
    paths = _frames(tmp_path, plan, lambda fov_index, f: [_star(0, 1000), _star(90, 600)], filters=("rp",))

    def broken(ra, dec, radius):
        raise RuntimeError("vizier down")

    _, rec = subject.analyze_record(_record(plan), paths, gaia_query=broken, coef_lookup=None)
    assert any("vizier down" in w for w in rec["warnings"])
