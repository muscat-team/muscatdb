"""The CSV handed to timer must contain only columns timer should model.

``read_afphot`` maps BJD_TDB/Flux/Err to time/flux/error and promotes every
other column to a detrending covariate, so what muscat-db copies into the run
directory decides the design matrix.
"""

from __future__ import annotations

import pathlib

from muscat_db.transit_fit import (
    _TIMER_COVARIATE_COLUMNS,
    _copy_lightcurve_for_timer,
)

# The prose2 afphot header for MuSCAT/MuSCAT2 (ADU) frames.
_HEADER = (
    "BJD_TDB,Flux,Err,Bkg(ADU),Airmass,Dx(pix),Dy(pix),Peak(ADU),FWHM(pix),GJD_UTC"
)
_ROW = "2460000.1,1.0,0.001,120.5,1.21,0.3,-0.2,15000,4.1,2460000.0993"
_ROW2 = "2460000.2,1.001,0.001,118.0,1.19,0.1,-0.4,15400,4.4,2460000.1993"

# Real prose2 output (MuSCAT3 rp, V1298 Tau 2023-07-30, rows 1/39/79/119):
# BANZAI frames are in electrons, and prose2 >= PR #54 adds PeakRadius(pix),
# which is the same value on every row of a --aper_unit pix reduction.
_REAL_M3_CSV = """\
BJD_TDB,Flux,Err,Bkg(e-),Airmass,Dx(pix),Dy(pix),Peak(e-),PeakRadius(pix),FWHM(pix),GJD_UTC
2460157.0778767625,0.9983250065419238,0.0005168964122539613,213.90765380859375,1.7926462,-0.8600331168416666,-1.5094358130443104,16704.56640625,23.12488383043165,24.61479414529966,2460157.0796245257
2460157.0838328176,0.9966961406175014,0.0005070280661930534,183.4937942504883,1.7063655,-1.4875684694330604,-0.3637858923906645,19588.615234375,23.12488383043165,23.72426909029955,2460157.0855800463
2460157.0901132543,0.9983589032816255,0.0004902736953587457,125.7128890991211,1.6258369,-0.7755855819543122,-0.2600186667846033,24554.94921875,23.12488383043165,22.736342072940076,2460157.091859919
2460157.0964239477,1.0026637387431239,0.0004941712636580207,103.55465367635091,1.554069,1.3298050918311324,1.1383616317295369,19170.318359375,23.12488383043165,22.281830857259767,2460157.0981700467
"""


def _write(tmp_path: pathlib.Path, header: str, rows: list[str]) -> pathlib.Path:
    src = tmp_path / "in.csv"
    src.write_text("\n".join([header, *rows]) + "\n")
    return src


def _read_header(path: pathlib.Path) -> list[str]:
    return path.read_text().splitlines()[0].split(",")


def _copy(tmp_path: pathlib.Path, header: str, rows: list[str], **kw) -> list[str]:
    dst = tmp_path / "out.csv"
    _copy_lightcurve_for_timer(_write(tmp_path, header, rows), dst, **kw)
    return _read_header(dst)


def test_gjd_utc_is_dropped(tmp_path):
    """GJD_UTC is a second time axis, near-collinear with the trend block."""
    cols = _copy(tmp_path, _HEADER, [_ROW, _ROW2])
    assert "GJD_UTC" not in cols
    assert cols == [
        "BJD_TDB", "Flux", "Err",
        "Bkg(ADU)", "Airmass", "Dx(pix)", "Dy(pix)", "Peak(ADU)", "FWHM(pix)",
    ]


def test_real_prose2_csv_keeps_electron_covariates_and_drops_the_rest(tmp_path):
    """prose2 names Bkg/Peak by the frame's unit, so MuSCAT3/4, Sinistro and
    QHY600 CSVs say ``(e-)``. Those must stay covariates; PeakRadius(pix) and
    GJD_UTC must not reach timer."""
    src = tmp_path / "V1298Tau_muscat3_rp_230730.csv"
    src.write_text(_REAL_M3_CSV)
    dst = tmp_path / "out.csv"
    _copy_lightcurve_for_timer(src, dst)
    assert _read_header(dst) == [
        "BJD_TDB", "Flux", "Err",
        "Bkg(e-)", "Airmass", "Dx(pix)", "Dy(pix)", "Peak(e-)", "FWHM(pix)",
    ]


def test_every_unit_variant_of_bkg_and_peak_is_allowed():
    for name in ("Bkg(ADU)", "Bkg(e-)", "Peak(ADU)", "Peak(e-)"):
        assert name in _TIMER_COVARIATE_COLUMNS


def test_unknown_columns_are_dropped_not_passed_through(tmp_path):
    """Allow-list, not drop-list: a new prose2 column must not silently become
    a covariate."""
    cols = _copy(tmp_path, _HEADER + ",SomeNewColumn", [_ROW + ",7", _ROW2 + ",8"])
    assert "SomeNewColumn" not in cols


def test_constant_covariate_is_dropped(tmp_path):
    """timer standardizes each covariate by its std; a constant column becomes
    0/0 = NaN, or a copy of the mean when rounding leaves a tiny std."""
    cols = _copy(tmp_path, _HEADER, [_ROW, _ROW2.replace(",0.1,-0.4,", ",0.3,-0.4,")])
    assert "Dx(pix)" not in cols
    assert "Dy(pix)" in cols


def test_constant_upload_covariate_is_dropped(tmp_path):
    header = "BJD_TDB,Flux,Err,exptime"
    cols = _copy(
        tmp_path, header, ["1.0,1.0,0.1,60", "2.0,1.1,0.1,60", "3.0,0.9,0.1,60"],
        extra_covariates=("exptime",),
    )
    assert cols == ["BJD_TDB", "Flux", "Err"]


def test_exact_duplicate_covariate_is_dropped(tmp_path):
    """A covariate that is a linear copy of one already kept makes the design
    matrix singular; the first one listed stays."""
    header = "BJD_TDB,Flux,Err,FWHM(pix),fwhm_arcsec"
    rows = ["1.0,1.0,0.1,4.0,1.04", "2.0,1.1,0.1,5.0,1.30", "3.0,0.9,0.1,4.5,1.17"]
    cols = _copy(tmp_path, header, rows, extra_covariates=("fwhm_arcsec",))
    assert cols == ["BJD_TDB", "Flux", "Err", "FWHM(pix)"]


def test_correlated_but_not_duplicate_covariates_are_kept(tmp_path):
    header = "BJD_TDB,Flux,Err,FWHM(pix),fwhm_arcsec"
    rows = ["1.0,1.0,0.1,4.0,1.05", "2.0,1.1,0.1,5.0,1.30", "3.0,0.9,0.1,4.5,1.16"]
    cols = _copy(tmp_path, header, rows, extra_covariates=("fwhm_arcsec",))
    assert cols == ["BJD_TDB", "Flux", "Err", "FWHM(pix)", "fwhm_arcsec"]


def test_all_nan_covariate_is_dropped(tmp_path):
    """timer's bin_df ends in dropna(), so one all-NaN column empties the fit."""
    rows = [_ROW.replace(",1.21,", ",nan,"), _ROW2.replace(",1.19,", ",,")]
    cols = _copy(tmp_path, _HEADER, rows)
    assert "Airmass" not in cols
    assert "FWHM(pix)" in cols, "only the empty covariate should go"


def test_partly_populated_covariate_is_kept(tmp_path):
    rows = [_ROW.replace(",1.21,", ",,"), _ROW, _ROW2]
    assert "Airmass" in _copy(tmp_path, _HEADER, rows)


def test_reserved_columns_survive_even_when_empty(tmp_path):
    """Dropping Flux would be worse than letting timer report the problem."""
    cols = _copy(tmp_path, _HEADER, [_ROW.replace(",1.0,", ",,")])
    assert cols[:3] == ["BJD_TDB", "Flux", "Err"]


def test_unrecognized_format_is_copied_verbatim(tmp_path):
    """An unexpected header should fail inside timer, not silently here."""
    header = "time,flux,flux_err,airmass"
    dst = tmp_path / "out.csv"
    src = _write(tmp_path, header, ["1,2,3,4"])
    _copy_lightcurve_for_timer(src, dst)
    assert dst.read_text() == src.read_text()


def test_row_values_track_the_kept_columns(tmp_path):
    dst = tmp_path / "out.csv"
    _copy_lightcurve_for_timer(_write(tmp_path, _HEADER, [_ROW, _ROW2]), dst)
    header, row, _ = dst.read_text().splitlines()
    assert dict(zip(header.split(","), row.split(",")))["Airmass"] == "1.21"


# --- user-selected covariates (transit-fit form "covariates" option) ---------

import yaml  # noqa: E402

from muscat_db.transit_fit import (  # noqa: E402
    _TIMER_COVARIATE_CHOICES,
    _write_fit_inputs,
    selected_covariates,
    validate_fit_options,
)


def test_omitted_covariates_option_selects_every_choice():
    assert selected_covariates({}) == tuple(_TIMER_COVARIATE_CHOICES)


def test_covariates_option_is_parsed_in_canonical_order():
    assert selected_covariates({"covariates": " FWHM ,Airmass,FWHM"}) == ("Airmass", "FWHM")


def test_empty_covariates_option_selects_none():
    """Unticking every box fits with trend/GP/spline terms only."""
    assert selected_covariates({"covariates": ""}) == ()


def test_choices_cover_exactly_the_allowed_columns():
    cols = {c for group in _TIMER_COVARIATE_CHOICES.values() for c in group}
    assert cols == set(_TIMER_COVARIATE_COLUMNS)
    assert "PeakRadius(pix)" not in cols and "GJD_UTC" not in cols


def test_unknown_covariate_is_rejected_before_launch():
    """The form may only narrow the fixed set: a raw CSV column name such as
    PeakRadius(pix) or GJD_UTC can never be requested."""
    for bad in ("PeakRadius(pix)", "GJD_UTC", "Peak(e-)", "Airmass,Seeing"):
        err = validate_fit_options({"covariates": bad})
        assert err and "covariates" in err, bad


def test_known_covariates_pass_validation():
    assert validate_fit_options({"covariates": "Bkg,Peak,FWHM"}) is None
    assert validate_fit_options({"covariates": ""}) is None


def _fit_inputs(tmp_path, options):
    src = tmp_path / "V1298Tau_muscat3_rp_230730.csv"
    src.write_text(_REAL_M3_CSV)
    rdir = tmp_path / "run"
    rdir.mkdir()
    _write_fit_inputs(rdir, "muscat3", "230730", "V1298Tau", [src], options)
    meta = yaml.safe_load((rdir / "meta.yaml").read_text())
    return _read_header(rdir / src.name), meta


def test_selected_covariates_limit_the_timer_csv_and_are_recorded(tmp_path):
    cols, meta = _fit_inputs(tmp_path, {"covariates": "Peak,FWHM"})
    assert cols == ["BJD_TDB", "Flux", "Err", "Peak(e-)", "FWHM(pix)"]
    assert meta["covariates"] == ["Peak", "FWHM"]


def test_default_fit_inputs_keep_every_allowed_covariate(tmp_path):
    cols, meta = _fit_inputs(tmp_path, {})
    assert cols == [
        "BJD_TDB", "Flux", "Err",
        "Bkg(e-)", "Airmass", "Dx(pix)", "Dy(pix)", "Peak(e-)", "FWHM(pix)",
    ]
    assert meta["covariates"] == list(_TIMER_COVARIATE_CHOICES)


def test_form_offers_exactly_the_server_choices():
    """The transit-fit form's checkboxes and the server allow-list must agree,
    or a ticked box would be rejected (or a choice could never be made)."""
    import re

    html = (pathlib.Path(__file__).parents[1] / "src/muscat_db/templates/transit_fit.html").read_text()
    m = re.search(r'{% for cov in \[([^\]]*)\] %}', html)
    assert m, "covariate checkbox loop not found"
    assert tuple(re.findall(r'"([^"]+)"', m.group(1))) == tuple(_TIMER_COVARIATE_CHOICES)
