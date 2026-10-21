"""Archive-calibrated exposure model (muscat_db.exposure_model) and its use by /exposure."""

import math

import numpy as np
import pytest
from astropy.io import fits
from fastapi.testclient import TestClient

from muscat_db import exposure, exposure_model as subject
from muscat_db.exposure import EXTINCTION


@pytest.fixture
def exposure_db(tmp_path, monkeypatch):
    path = tmp_path / "exposure.db"
    monkeypatch.setenv("MUSCAT_DB_PATH", str(path))
    return str(path)


def _write_frame(path, *, zp=24.0, airmass=1.2, fwhm=1.5, pixscale=0.267, maxlin=100000.0, defocus=0.0,
                 band="rp", mode="MUSCAT_FAST", date="2026-09-01T00:00:00", peak_fraction=0.04, n_stars=30):
    flux = np.linspace(2e5, 4e5, n_stars)
    background = np.full(n_stars, 50.0)
    cat = fits.BinTableHDU.from_columns([
        fits.Column(name="peak", format="D", array=flux * peak_fraction),
        fits.Column(name="flux", format="D", array=flux),
        fits.Column(name="background", format="D", array=background),
        fits.Column(name="flag", format="J", array=np.zeros(n_stars, int)),
    ], name="CAT")
    header = fits.Header({"L1ZP": zp, "AIRMASS": airmass, "L1FWHM": fwhm, "PIXSCALE": pixscale, "MAXLIN": maxlin,
                          "FOCOBOFF": defocus, "FILTER": band, "CONFMODE": mode, "DATE-OBS": date})
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(np.zeros((2, 2), "f4"), header=header, name="SCI"), cat]).writeto(path)
    return path


def _sample(**overrides):
    values = dict(filename="f.fits", band="rp", mode="", date="2026-09-01", zp=24.0, airmass=1.0, fwhm_arcsec=1.5,
                  pixscale=0.25, defocus_mm=0.0, level=100000.0, peak_fraction=0.9 / 36)
    values.update(overrides)
    return subject.FrameSample(**values)


# --- measurement ---------------------------------------------------------------------

def test_measure_frame_reads_header_and_peak_fraction(tmp_path):
    sample = subject.measure_frame(_write_frame(tmp_path / "f.fits", defocus=-2.0))
    assert sample.zp == 24.0 and sample.level == 100000.0
    assert sample.defocus_mm == 2.0  # sign of the secondary offset does not matter
    assert sample.peak_fraction == pytest.approx(0.04)


def test_measure_frame_skips_frames_without_zeropoint(tmp_path):
    path = _write_frame(tmp_path / "f.fits")
    with fits.open(path, mode="update") as hdul:
        del hdul["SCI"].header["L1ZP"]
    assert subject.measure_frame(path) is None


def test_measure_frame_ignores_stars_near_saturation(tmp_path):
    # Faintest star peaks at 8000 e-, above 70% of a 10000 e- MAXLIN: none usable.
    path = _write_frame(tmp_path / "f.fits", maxlin=10000.0, peak_fraction=0.04)
    assert subject.measure_frame(path) is None


# --- fit -------------------------------------------------------------------------------

def test_fit_recovers_zenith_zeropoint_peak_constant_and_defocus_slope():
    k = 0.1
    samples = []
    for i in range(20):
        seeing = 1.2 + 0.05 * i
        samples.append(_sample(zp=24.0 - k * 0.5, airmass=1.5, fwhm_arcsec=seeing, peak_fraction=0.9 / (seeing / 0.25) ** 2))
    for i, d in enumerate([2.0, 3.0, 4.0, 5.0, 6.0, 2.0, 3.0, 4.0]):
        fwhm = math.hypot(1.6, 0.8 * d)
        samples.append(_sample(defocus_mm=d, fwhm_arcsec=fwhm, peak_fraction=1.2 / (fwhm / 0.25) ** 2))
    model = subject.fit_band(samples, k)
    assert model.zp_median == pytest.approx(24.0)  # corrected to zenith
    assert model.a_focus == pytest.approx(0.9)
    assert model.a_defocus == pytest.approx(1.2, rel=0.05)
    assert model.defocus_c == pytest.approx(0.8, rel=0.1)
    # The safe image is never wider than the typical one.
    for d in (1.0, 3.0, 6.0):
        assert math.hypot(model.seeing_good, model.defocus_c_safe * d) <= math.hypot(model.seeing_median, model.defocus_c * d)
    assert model.seeing_good < model.seeing_median


def test_fit_uses_lowest_recent_saturation_level():
    samples = [_sample(level=47400.0, date="2026-09-01") for _ in range(6)]
    samples += [_sample(level=42000.0, date="2026-08-01") for _ in range(3)]
    samples += [_sample(level=30000.0, date="2024-01-01")]  # older than a year
    assert subject.fit_band(samples, 0.1).level == 42000.0


def test_fit_needs_enough_frames():
    assert subject.fit_band([_sample()] * (subject.MIN_FRAMES - 1), 0.1) is None


def test_fit_instrument_keeps_sinistro_readout_modes_apart():
    samples = [_sample(mode="central_2k_2x2", pixscale=0.778, level=240000.0) for _ in range(10)]
    samples += [_sample(mode="full_frame", pixscale=0.389, level=120000.0) for _ in range(10)]
    samples += [_sample(mode="lco2_500kHz_DRH") for _ in range(10)]  # engineering mode: ignored
    models = {(m.band, m.mode): m for m in subject.fit_instrument("sinistro", samples, EXTINCTION)}
    assert set(models) == {("rp", "central_2k_2x2"), ("rp", "full_frame")}
    assert models[("rp", "central_2k_2x2")].level == 240000.0


# --- prediction ------------------------------------------------------------------------

def _model(**overrides):
    values = dict(band="rp", mode="", zp_safe=25.0, zp_median=24.8, a_focus=1.0, a_defocus=1.2, defocus_c=1.0,
                  defocus_c_safe=0.8, seeing_good=1.2, seeing_median=1.6, pixscale=0.25, level=100000.0, n_frames=50)
    values.update(overrides)
    return subject.BandModel(**values)


def test_peak_rate_follows_zeropoint_and_psf():
    model = _model()
    # mag 15 at zenith: 10**(0.4*10) e-/s total; 1.2" FWHM = 4.8 px.
    assert subject.peak_rate(model, 15.0, 1.0, 0.1) == pytest.approx(1e4 * 1.0 / 4.8 ** 2)
    assert subject.peak_rate(model, 15.0, 2.0, 0.1) == pytest.approx(1e4 * 10 ** -0.04 / 4.8 ** 2)


def test_defocus_lowers_peak_using_the_safe_slope():
    model = _model()
    fwhm_pix = math.hypot(1.2, 0.8 * 3.0) / 0.25
    assert subject.peak_rate(model, 15.0, 1.0, 0.1, defocus_mm=3.0) == pytest.approx(1e4 * 1.2 / fwhm_pix ** 2)


def test_uncalibrated_defocus_does_not_lower_the_peak():
    model = _model(defocus_c=None, defocus_c_safe=None, a_defocus=None)
    assert subject.peak_rate(model, 15.0, 1.0, 0.1, defocus_mm=0.5) == subject.peak_rate(model, 15.0, 1.0, 0.1)


def test_worse_seeing_lowers_the_peak():
    model = _model()
    assert subject.peak_rate(model, 15.0, 1.0, 0.1, seeing=2.4) == pytest.approx(subject.peak_rate(model, 15.0, 1.0, 0.1) / 4)


def test_lookup_borrows_same_pixel_mode_with_the_lower_level():
    models = {("gp", "central30x30"): _model(band="gp", mode="central30x30", pixscale=0.74, level=42000.0),
              ("rp", "full_frame"): _model(band="rp", mode="full_frame", pixscale=0.74, level=46200.0)}
    model = subject.lookup(models, "qhy600", "gp", "full_frame", {}, lambda b: 0.0)
    assert model.mode == "full_frame" and model.level == 42000.0


def test_lookup_derives_narrowband_from_parent():
    models = {("rp", ""): _model()}
    model = subject.lookup(models, "muscat3", "r_narrow", None, {"r_narrow": "rp"}, lambda b: -1.0)
    assert model.band == "r_narrow" and model.zp_safe == 24.0


def test_lookup_returns_none_for_unknown_band():
    assert subject.lookup({("rp", ""): _model()}, "muscat3", "U", None, {}, lambda b: 0.0) is None


# --- /exposure integration: regressions found against real BANZAI data --------------------

def _measured_peak(zp, fwhm_arcsec, pixscale, mag=12.0, a=0.95):
    return 10 ** (-0.4 * (mag - zp)) * a / (fwhm_arcsec / pixscale) ** 2


def test_qhy600_peak_matches_archive_measurement(exposure_db):
    # Archive medians (2026-10): rp L1ZP 21.02, seeing ~2.5-3", 0.74"/px. The
    # old MuSCAT3-scaled model predicted 10-20x too low.
    measured = _measured_peak(21.02, 2.9, 0.74)
    predicted = exposure.calc_peak("qhy600", "rp", 12.0, 0.0, 1.0, 1.0)["peak_electrons"]
    assert 1.0 <= predicted / measured <= 3.0


def test_sinistro_peak_matches_archive_measurement(exposure_db):
    # Full frame rp: L1ZP 23.66, seeing ~2.2", 0.389"/px. Old model: 2.5-4x too low.
    measured = _measured_peak(23.66, 2.2, 0.389)
    predicted = exposure.calc_peak("sinistro", "rp", 12.0, 0.0, 1.0, 1.0, confmode="full_frame")["peak_electrons"]
    assert 1.0 <= predicted / measured <= 3.0


def test_sinistro_saturation_depends_on_readout_mode(exposure_db):
    # Header MAXLIN: ~113-131k e- unbinned, ~217-256k e- in 2x2 binning; the old
    # model used 246400 for both.
    assert 100000 < exposure.full_well_e("sinistro", "rp", "full_frame") < 135000
    assert 200000 < exposure.full_well_e("sinistro", "rp", "central_2k_2x2") < 260000


def test_muscat4_zs_saturation_comes_from_headers(exposure_db):
    # The old table said 115200 e- for MuSCAT4 zs; headers never showed that.
    assert exposure.full_well_e("muscat4", "zs") != 115200


def test_qhy600_saturation_is_the_lowest_unit(exposure_db):
    assert exposure.full_well_e("qhy600", "rp", "central30x30") == 42000.0


def test_muscat4_is_not_overpredicted(exposure_db):
    # MuSCAT4 gp in focus: L1ZP ~24.6, seeing ~2", 0.267"/px. Old model: 5x too high.
    measured = _measured_peak(24.6, 2.0, 0.267)
    predicted = exposure.calc_peak("muscat4", "gp", 12.0, 0.0, 1.0, 1.0)["peak_electrons"]
    assert 1.0 <= predicted / measured <= 3.0


def test_exptime_round_trips_through_peak(exposure_db):
    exp = exposure.calc_exptime("muscat3", "rp", 11.0, 0.0, 50000.0, 1.3, seeing=1.8)
    peak = exposure.calc_peak("muscat3", "rp", 11.0, 0.0, exp["exptime"], 1.3, seeing=1.8)
    assert peak["peak_electrons"] == pytest.approx(50000.0, rel=0.01)
    assert exp["seeing_arcsec"] == 1.8 and exp["model_source"] == "default"


def test_calc_all_bands_targets_fraction_of_mode_level(exposure_db):
    result = exposure.calc_all_bands("sinistro", {"rp": 12.0}, 0.0, confmode="central_2k_2x2", sat_frac=0.5)
    row = result["results"][0]
    assert row["peak_electrons"] == pytest.approx(0.5 * row["full_well"], rel=0.01)


# --- calibration ---------------------------------------------------------------------------

def test_calibrate_fits_and_stores_from_archive_frames(exposure_db, tmp_path):
    root = tmp_path / "QHY600CMOS"
    for night in range(4):
        nd = root / f"2609{night + 10:02d}"
        nd.mkdir(parents=True)
        for i in range(4):
            _write_frame(nd / f"coj0m416-sq36-202609{night + 10:02d}-{i:04d}-e91.fits", zp=21.0 + 0.01 * i,
                         band="rp", mode="central30x30", pixscale=0.74, fwhm=2.5, maxlin=46000.0,
                         peak_fraction=1.0 / (2.5 / 0.74) ** 2)
        _write_frame(nd / f"ogg2m001-ep02-202609{night + 10:02d}-0001-e91.fits")  # mis-filed MuSCAT frame
    result = subject.calibrate("qhy600", str(root), EXTINCTION, exposure._conn)
    assert result["ok"] and result["bands_calibrated"] == 1 and result["usable_frames"] == 16
    status = exposure.calibration_status("qhy600")
    assert status["source"] == "archive" and status["bands"][0]["mode"] == "central30x30"
    assert exposure.full_well_e("qhy600", "rp", "central30x30") == 46000.0


def test_calibrate_without_usable_frames_keeps_defaults(exposure_db, tmp_path):
    (tmp_path / "SBIG" / "200101").mkdir(parents=True)
    result = subject.calibrate("sbig", str(tmp_path / "SBIG"), EXTINCTION, exposure._conn)
    assert not result["ok"] and "defaults" in result["error"]
    assert exposure.calibration_status("sbig")["source"] == "default"


# --- web ------------------------------------------------------------------------------------

def test_calculate_rejects_bad_seeing(exposure_db):
    from muscat_db.web import app

    r = TestClient(app).post("/api/exposure/calculate", json={"instrument": "muscat3", "mags": {"rp": 12}, "seeing": -1})
    assert r.status_code == 400 and "Seeing" in r.json()["error"]


def test_calculate_uses_seeing(exposure_db):
    from muscat_db.web import app

    client = TestClient(app)
    good = client.post("/api/exposure/calculate", json={"instrument": "muscat3", "mags": {"rp": 12}, "seeing": 1.0}).json()
    poor = client.post("/api/exposure/calculate", json={"instrument": "muscat3", "mags": {"rp": 12}, "seeing": 2.0}).json()
    assert poor["recommended_exptime"] == pytest.approx(4 * good["recommended_exptime"], rel=0.05)


def test_coeffs_endpoint_reports_model_for_archive_instrument(exposure_db):
    from muscat_db.web import app

    rows = TestClient(app).get("/api/exposure/coeffs/sinistro?confmode=full_frame").json()["coeffs"]
    focus = sorted({r["focus_mm"] for r in rows if r["band"] == "rp"})
    assert focus[0] == 0.0 and focus[-1] == 5.0  # LCO's Sinistro defocus limit
    coefs = [r["coef"] for r in sorted((r for r in rows if r["band"] == "rp"), key=lambda r: r["focus_mm"])]
    assert coefs == sorted(coefs, reverse=True)  # defocus only ever lowers the peak
