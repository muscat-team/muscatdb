"""Uploading a light curve from a pipeline muscat-db does not run (issue #382).

Parser and join tests run on the small samples in ``tests/data/lc_upload``; the
refusal paths are the ones #382 settles (BJD_TDB only, exact join, all-NaN
covariate, repeated checksum); and a saved upload must reach the transit fit
with its mapping and checksums intact in the product record.
"""

from __future__ import annotations

import base64
import csv
import hashlib
from pathlib import Path

import pytest

from muscat_db import lc_upload, product_record
from muscat_db import photometry as phot
from muscat_db import transit_fit as fit
from muscat_db.instruments import EXTERNAL_INSTRUMENT
from muscat_db.lc_upload import Mapping, UploadError

DATA = Path(__file__).parent / "data" / "lc_upload"
PRIMARY = (DATA / "collab_lightcurve.csv").read_bytes()
ANCILLARY = (DATA / "collab_covariates.tsv").read_bytes()
DATE = "260103"
TARGET = "TOI-9999"

BASE_MAPPING = Mapping(
    time="time_bjd", flux="rel_flux_T1", err="rel_flux_err_T1", time_system="BJD_TDB",
    covariates=("AIRMASS",),
)
JOIN_MAPPING = Mapping(
    time="time_bjd", flux="rel_flux_T1", err="rel_flux_err_T1", time_system="BJD_TDB",
    covariates=("AIRMASS",), ancillary_covariates=("fwhm_arcsec",),
    join_primary="time_bjd", join_ancillary="bjd",
)


@pytest.fixture
def prose_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path / "prose"))
    return tmp_path / "prose"


def _save(**overrides):
    kwargs = dict(
        target=TARGET, date=DATE, band="r", facility="ASTEP", mapping=BASE_MAPPING,
        primary_name="collab_lightcurve.csv", primary_data=PRIMARY, author="carol",
    )
    kwargs.update(overrides)
    return lc_upload.save_upload(**kwargs)


def _read_csv(path):
    with open(path, newline="") as fh:
        return list(csv.reader(fh))


def _table(text, name="t.csv"):
    return lc_upload.parse_table(name, text.encode())


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_parses_the_commented_csv_sample():
    table = lc_upload.parse_table("collab_lightcurve.csv", PRIMARY)
    assert table.columns == ("time_bjd", "rel_flux_T1", "rel_flux_err_T1", "AIRMASS", "sky_ppm", "unused_text")
    assert len(table.rows) == 6


def test_parses_the_tab_separated_sample():
    table = lc_upload.parse_table("collab_covariates.tsv", ANCILLARY)
    assert table.columns == ("bjd", "fwhm_arcsec", "x_shift")
    assert table.rows[0] == ("2461044.025", "2.31", "0.12")


def test_parses_whitespace_columns_and_a_commented_header():
    table = _table("#BJD  flux  err\n2461044.0 1.0 0.001\n2461044.1 0.99 0.001\n")
    assert table.columns == ("BJD", "flux", "err")
    assert table.rows[1] == ("2461044.1", "0.99", "0.001")


def test_describe_lists_numeric_columns_for_the_form():
    info = lc_upload.describe(lc_upload.parse_table("p.csv", PRIMARY))
    assert info["n_rows"] == 6
    assert "unused_text" not in info["numeric_columns"]
    assert "sky_ppm" not in info["numeric_columns"]  # one 'nan' row
    assert info["numeric_columns"][:3] == ["time_bjd", "rel_flux_T1", "rel_flux_err_T1"]
    assert len(info["preview"]) == 5


@pytest.mark.parametrize("text,needle", [
    ("", "empty"),
    ("2461044.0,1.0,0.001\n", "add a header row"),
    ("a,b,a\n1,2,3\n", "repeated column"),
    ("a,,c\n1,2,3\n", "blank column"),
    ("a,b,c\n1,2\n", "line 2 has 2 cells"),
    ("a,b,c\n", "no data rows"),
])
def test_malformed_tables_are_refused(text, needle):
    with pytest.raises(UploadError, match=needle):
        _table(text)


def test_binary_and_non_utf8_files_are_refused():
    with pytest.raises(UploadError, match="binary"):
        lc_upload.parse_table("x.fits", b"SIMPLE  =\x00\x00")
    with pytest.raises(UploadError, match="not UTF-8"):
        lc_upload.parse_table("x.csv", "a,b\n\xe9,1\n".encode("latin-1"))


def test_oversized_upload_is_refused(monkeypatch):
    monkeypatch.setattr(lc_upload, "MAX_UPLOAD_BYTES", 10)
    with pytest.raises(UploadError, match="larger than"):
        lc_upload.parse_table("big.csv", PRIMARY)


# --------------------------------------------------------------------------
# Mapping and join
# --------------------------------------------------------------------------


def test_normalize_assigns_roles_by_the_chosen_columns():
    norm = lc_upload.normalize(lc_upload.parse_table("p.csv", PRIMARY), BASE_MAPPING)
    assert norm.columns == ("BJD_TDB", "Flux", "Err", "AIRMASS")
    assert norm.rows[0] == ("2461044.0", "1.0012", "0.0011", "1.21")
    assert norm.warnings == ()


def test_join_matches_by_exact_value_regardless_of_row_order_or_formatting():
    primary = lc_upload.parse_table("p.csv", PRIMARY)
    ancillary = lc_upload.parse_table("a.tsv", ANCILLARY)
    norm = lc_upload.normalize(primary, JOIN_MAPPING, ancillary)
    assert norm.columns == ("BJD_TDB", "Flux", "Err", "AIRMASS", "fwhm_arcsec")
    # The sample lists 2461044.025 first; it must land on the last primary row,
    # which spells the same time as 2461044.02500.
    assert [r[-1] for r in norm.rows] == ["2.1", "2.12", "2.2", "2.25", "2.28", "2.31"]


def test_join_refuses_unmatched_rows():
    ancillary = _table("bjd,fwhm\n2461044.0,2\n2461044.005,2\n", "a.csv")
    with pytest.raises(UploadError, match="do not join exactly: 4 row"):
        lc_upload.normalize(
            lc_upload.parse_table("p.csv", PRIMARY),
            Mapping(**{**JOIN_MAPPING.__dict__, "ancillary_covariates": ("fwhm",)}),
            ancillary,
        )


def test_join_refuses_a_duplicated_key():
    text = ANCILLARY.decode() + "2461044.000\t9.9\t0.0\n"
    with pytest.raises(UploadError, match="repeats 1 value"):
        lc_upload.normalize(lc_upload.parse_table("p.csv", PRIMARY), JOIN_MAPPING, _table(text, "a.tsv"))


def test_join_refuses_a_numeric_key_against_a_text_key():
    ancillary = _table("frame,fwhm\nf1,2\nf2,2\nf3,2\nf4,2\nf5,2\nf6,2\n", "a.csv")
    mapping = Mapping(**{**JOIN_MAPPING.__dict__, "join_ancillary": "frame", "ancillary_covariates": ("fwhm",)})
    with pytest.raises(UploadError, match="not comparable"):
        lc_upload.normalize(lc_upload.parse_table("p.csv", PRIMARY), mapping, ancillary)


def test_all_nan_covariate_is_refused():
    table = _table("t,f,e,c\n2461044.0,1,0.1,nan\n2461044.1,1,0.1,\n")
    mapping = Mapping(time="t", flux="f", err="e", time_system="BJD_TDB", covariates=("c",))
    with pytest.raises(UploadError, match="no numeric value in any row"):
        lc_upload.normalize(table, mapping)


def test_partly_missing_covariate_is_kept_with_a_warning():
    mapping = Mapping(**{**BASE_MAPPING.__dict__, "covariates": ("sky_ppm",)})
    norm = lc_upload.normalize(lc_upload.parse_table("p.csv", PRIMARY), mapping)
    assert norm.rows[3][-1] == "nan"
    assert norm.warnings == ("covariate 'sky_ppm' is missing in 1 row(s); timer drops those rows",)


def test_text_covariate_is_refused():
    mapping = Mapping(**{**BASE_MAPPING.__dict__, "covariates": ("unused_text",)})
    with pytest.raises(UploadError, match="not a number"):
        lc_upload.normalize(lc_upload.parse_table("p.csv", PRIMARY), mapping)


def test_offset_time_is_refused_as_ambiguous():
    table = _table("btjd,f,e\n3044.0,1,0.1\n3044.1,1,0.1\n")
    with pytest.raises(UploadError, match="not a full BJD_TDB"):
        lc_upload.normalize(table, Mapping(time="btjd", flux="f", err="e", time_system="BJD_TDB"))


def test_non_positive_error_is_refused():
    table = _table("t,f,e\n2461044.0,1,0.1\n2461044.1,1,0\n")
    with pytest.raises(UploadError, match="row 2 is not positive"):
        lc_upload.normalize(table, Mapping(time="t", flux="f", err="e", time_system="BJD_TDB"))


@pytest.mark.parametrize("mapping,needle", [
    (Mapping(time="time_bjd", flux="time_bjd", err="rel_flux_err_T1", time_system="BJD_TDB"), "three different"),
    (Mapping(**{**BASE_MAPPING.__dict__, "covariates": ("rel_flux_T1",)}), "already used"),
    (Mapping(**{**BASE_MAPPING.__dict__, "covariates": ("nope",)}), "no column 'nope'"),
    (Mapping(**{**BASE_MAPPING.__dict__, "ancillary_covariates": ("x",)}), "no second file"),
])
def test_inconsistent_mappings_are_refused(mapping, needle):
    with pytest.raises(UploadError, match=needle):
        lc_upload.normalize(lc_upload.parse_table("p.csv", PRIMARY), mapping)


def test_covariate_named_like_a_reserved_column_is_refused():
    table = _table("t,f,e,Flux\n2461044.0,1,0.1,3\n")
    with pytest.raises(UploadError, match="collide"):
        lc_upload.normalize(table, Mapping(time="t", flux="f", err="e", time_system="BJD_TDB", covariates=("Flux",)))


@pytest.mark.parametrize("system", ["JD_UTC", "HJD", "", "bjd_tdb"])
def test_only_bjd_tdb_is_accepted(system):
    payload = {"time": "t", "flux": "f", "err": "e", "time_system": system}
    with pytest.raises(UploadError):
        Mapping.from_payload(payload)


def test_mapping_from_payload_round_trips():
    payload = {
        "time": "time_bjd", "flux": "rel_flux_T1", "err": "rel_flux_err_T1", "time_system": "BJD_TDB",
        "covariates": ["AIRMASS"], "ancillary_covariates": ["fwhm_arcsec"],
        "join_primary": "time_bjd", "join_ancillary": "bjd",
    }
    assert Mapping.from_payload(payload) == JOIN_MAPPING


@pytest.mark.parametrize("payload", [None, [], {"time": 3}, {"time": "t", "flux": "f", "err": "e",
                                                               "time_system": "BJD_TDB", "covariates": "c"}])
def test_malformed_mapping_payloads_are_refused(payload):
    with pytest.raises(UploadError):
        Mapping.from_payload(payload)


# --------------------------------------------------------------------------
# Saving and the transit fit
# --------------------------------------------------------------------------


def test_save_writes_the_normalized_csv_and_product_record(prose_dir):
    result = _save(mapping=JOIN_MAPPING, ancillary_name="collab_covariates.tsv", ancillary_data=ANCILLARY)
    sha = hashlib.sha256(PRIMARY).hexdigest()
    assert result["run_id"] == f"upload-{sha[:8]}"
    assert result["inst"] == EXTERNAL_INSTRUMENT

    rdir = prose_dir / "external" / DATE / "_runs" / TARGET / result["run_id"]
    csv_path = rdir / result["file"]
    assert result["file"] == f"{TARGET}_external_r_{DATE}_{result['run_id']}.csv"
    assert _read_csv(csv_path)[0] == ["BJD_TDB", "Flux", "Err", "AIRMASS", "fwhm_arcsec"]

    record = product_record.read_record(rdir)
    assert record["level"] == "photometry"
    assert record["author"] == "carol"
    assert record["inputs"] == [{
        "kind": "upload",
        "facility": "ASTEP",
        "files": [
            {"role": "primary", "name": "collab_lightcurve.csv", "sha256": sha, "bytes": len(PRIMARY)},
            {"role": "ancillary", "name": "collab_covariates.tsv",
             "sha256": hashlib.sha256(ANCILLARY).hexdigest(), "bytes": len(ANCILLARY)},
        ],
    }]
    settings = record["settings"]
    assert settings["source"] == "upload"
    assert settings["band"] == "r"
    assert Mapping.from_payload(settings["mapping"]) == JOIN_MAPPING
    assert settings["covariates"] == ["AIRMASS", "fwhm_arcsec"]


def test_saved_upload_is_selectable_in_the_transit_fit(prose_dir):
    result = _save()
    csvs = fit.get_csv_lightcurves(EXTERNAL_INSTRUMENT, DATE, TARGET)
    assert [c.name for c in csvs] == [result["file"]]
    assert TARGET.replace("-", "") in {t.replace("-", "") for t in phot.discovered_targets(EXTERNAL_INSTRUMENT, DATE)}
    assert DATE in phot.output_dates(EXTERNAL_INSTRUMENT)
    # The band comes from the filename, as for prose output.
    assert fit.validate_no_duplicate_datasets(EXTERNAL_INSTRUMENT, DATE, csvs) is None
    assert EXTERNAL_INSTRUMENT in fit.FIT_INSTRUMENTS


def test_fit_inputs_keep_the_uploaded_covariates(prose_dir, tmp_path):
    """timer would otherwise lose every covariate not on prose's allow-list."""
    _save(mapping=JOIN_MAPPING, ancillary_name="collab_covariates.tsv", ancillary_data=ANCILLARY)
    csvs = fit.get_csv_lightcurves(EXTERNAL_INSTRUMENT, DATE, TARGET)
    out = tmp_path / "fit"
    out.mkdir()
    fit._write_fit_inputs(out, EXTERNAL_INSTRUMENT, DATE, TARGET, csvs, {})
    assert _read_csv(out / csvs[0].name)[0] == ["BJD_TDB", "Flux", "Err", "AIRMASS", "fwhm_arcsec"]


def test_prose_csv_still_drops_unlisted_columns(tmp_path):
    src = tmp_path / "TOI1_muscat3_r_260103.csv"
    src.write_text("BJD_TDB,Flux,Err,Airmass,fwhm_arcsec\n2461044.0,1,0.1,1.2,2.0\n")
    dst = tmp_path / "out.csv"
    fit._copy_lightcurve_for_timer(src, dst, lc_upload.upload_covariates(src))
    assert _read_csv(dst)[0] == ["BJD_TDB", "Flux", "Err", "Airmass"]


def test_transit_fit_record_points_at_the_upload(prose_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("MUSCAT_TIMER_DIR", str(tmp_path / "timer"))
    result = _save()
    csvs = fit.get_csv_lightcurves(EXTERNAL_INSTRUMENT, DATE, TARGET)
    rdir = fit.fit_output_dir(EXTERNAL_INSTRUMENT, DATE, TARGET, "default")
    rdir.mkdir(parents=True)
    fit._write_fit_product(rdir, EXTERNAL_INSTRUMENT, DATE, TARGET, csvs, {}, False, "dave")
    (entry,) = product_record.read_record(rdir)["inputs"]
    assert entry["kind"] == "product" and entry["level"] == "photometry"
    assert entry["instrument"] == EXTERNAL_INSTRUMENT and entry["run_id"] == result["run_id"]
    assert entry["pinned"]["inputs"][0]["kind"] == "upload"


def test_same_file_twice_is_refused_and_names_the_first(prose_dir):
    first = _save()
    with pytest.raises(UploadError, match=f"already uploaded.*{first['run_id']}"):
        _save(target="TOI-1", band="i")
    assert len(list((prose_dir / "external").glob("*/_runs/*/upload-*"))) == 1


@pytest.mark.parametrize("overrides,needle", [
    ({"date": "2601"}, "yymmdd"),
    ({"date": "261399"}, "not a calendar date"),
    ({"target": "../x"}, "target"),
    ({"target": ""}, "target"),
    ({"band": "V"}, "band must be one of"),
    ({"facility": " "}, "facility"),
    ({"facility": "x" * 81}, "facility"),
    ({"date": "260110"}, "days from night 260110"),
])
def test_save_refusals_write_nothing(prose_dir, overrides, needle):
    with pytest.raises(UploadError, match=needle):
        _save(**overrides)
    assert not (prose_dir / "external").exists() or not any((prose_dir / "external").rglob("*.csv"))


def test_a_failed_record_write_leaves_no_product(prose_dir, monkeypatch):
    monkeypatch.setattr(product_record, "write_record", lambda *a, **k: False)
    with pytest.raises(UploadError, match="could not write the product record"):
        _save()
    assert not list((prose_dir / "external").rglob("upload-*"))


def test_upload_covariates_ignores_a_photometry_run(tmp_path):
    product_record.write_record(tmp_path, product_record.build_record(
        "photometry", author="x", inputs=[], settings={"covariates": ["evil"]}))
    assert lc_upload.upload_covariates(tmp_path / "a.csv") == ()


# --------------------------------------------------------------------------
# Visibility and HTTP
# --------------------------------------------------------------------------


def test_upload_input_is_visible_to_a_scoped_viewer(env):  # noqa: F811
    from muscat_db.access import NightVisibility
    from muscat_db.database import ScopedDenied
    from muscat_db.product_visibility import ProductVisibility

    denied = ScopedDenied({"SECRET", ""}, allowed={"OPEN"})
    nights = NightVisibility(env, denied)
    # Fails closed for an LCO night with no summaries, but not for uploads.
    assert nights.hidden("muscat3", DATE, TARGET)
    assert not nights.hidden(EXTERNAL_INSTRUMENT, DATE, TARGET)
    assert not nights.unknown_night(EXTERNAL_INSTRUMENT, DATE)
    vis = ProductVisibility("eve", denied, nights)
    assert vis._input_visible({"kind": "upload", "files": []}, 0)
    assert not vis._input_visible({"kind": "bogus"}, 0)


def _b64(data):
    return base64.b64encode(data).decode()


@pytest.fixture
def http(env, client, prose_dir):  # noqa: F811 - env/client come from the import below
    return client


from tests.test_access_control_jobs import _headers, client, env  # noqa: E402,F401


def test_http_inspect_then_save_then_list_on_the_page(http):
    r = http.post("/api/lc-upload/inspect", headers=_headers("alice"),
                  json={"file": {"name": "p.csv", "content_b64": _b64(PRIMARY)}})
    assert r.status_code == 200 and r.json()["columns"][0] == "time_bjd"

    payload = {
        "target": TARGET, "date": DATE, "band": "r", "facility": "ASTEP",
        "primary": {"name": "p.csv", "content_b64": _b64(PRIMARY)},
        "ancillary": {"name": "a.tsv", "content_b64": _b64(ANCILLARY)},
        "mapping": {**JOIN_MAPPING.__dict__, "covariates": ["AIRMASS"], "ancillary_covariates": ["fwhm_arcsec"]},
    }
    r = http.post("/api/lc-upload/save", headers=_headers("alice"), json=payload)
    assert r.status_code == 200, r.text
    saved = r.json()
    rdir = phot.run_output_dir(EXTERNAL_INSTRUMENT, DATE, TARGET, saved["run_id"])
    assert product_record.read_record(rdir)["author"] == "alice"

    # alice is a restricted viewer in this fixture, and still sees the upload.
    page = http.get("/transit-fit", headers=_headers("alice"),
                    params={"inst": EXTERNAL_INSTRUMENT, "date": DATE, "target": TARGET})
    assert page.status_code == 200
    assert saved["file"] in page.text
    assert 'id="lc-upload"' in page.text

    again = http.post("/api/lc-upload/save", headers=_headers("alice"), json=payload)
    assert again.status_code == 400 and "already uploaded" in again.json()["error"]


@pytest.mark.parametrize("body,needle", [
    ({"file": {"name": "p.csv", "content_b64": "***"}}, "base64"),
    ({"file": "p.csv"}, "name, content_b64"),
    ({"file": {"name": "p.csv", "content_b64": _b64(b"a,b\n")}}, "no data rows"),
])
def test_http_inspect_refusals(http, body, needle):
    r = http.post("/api/lc-upload/inspect", headers=_headers("alice"), json=body)
    assert r.status_code == 400 and needle in r.json()["error"]


def test_http_save_refuses_jd_utc(http):
    payload = {
        "target": TARGET, "date": DATE, "band": "r", "facility": "ASTEP",
        "primary": {"name": "p.csv", "content_b64": _b64(PRIMARY)},
        "mapping": {**BASE_MAPPING.__dict__, "covariates": [], "time_system": "JD_UTC"},
    }
    r = http.post("/api/lc-upload/save", headers=_headers("alice"), json=payload)
    assert r.status_code == 400 and "BJD_TDB" in r.json()["error"]
    assert not phot.output_dates(EXTERNAL_INSTRUMENT)


@pytest.mark.parametrize("path", ["/api/lc-upload/inspect", "/api/lc-upload/save"])
def test_http_oversized_body_is_refused_before_parsing(http, monkeypatch, path):
    monkeypatch.setattr(lc_upload, "MAX_REQUEST_BYTES", 100)
    parsed = []
    monkeypatch.setattr(lc_upload, "parse_table", lambda *a, **k: parsed.append(a))
    body = '{"file": {"name": "p.csv", "content_b64": "' + "A" * 200 + '"}}'
    r = http.post(path, headers={**_headers("alice"), "Content-Type": "application/json"}, content=body)
    assert r.status_code == 413 and "larger than" in r.json()["error"]
    assert parsed == []


def test_http_non_json_body_is_refused(http):
    r = http.post("/api/lc-upload/inspect", headers=_headers("alice"), content=b"not json")
    assert r.status_code == 400 and "not JSON" in r.json()["error"]


def test_cell_cap_is_enforced(monkeypatch):
    monkeypatch.setattr(lc_upload, "MAX_CELLS", 10)
    with pytest.raises(UploadError, match="more than 10 cells"):
        lc_upload.parse_table("p.csv", PRIMARY)


def test_concurrent_save_of_the_same_file_is_refused_not_an_error(prose_dir, monkeypatch):
    """A double submit: both requests pass the checksum scan before either
    has written, so the directory claim must refuse the second."""
    monkeypatch.setattr(lc_upload, "find_checksum", lambda sha: None)
    first = _save()
    with pytest.raises(UploadError, match="already occupies"):
        _save()
    # The first upload is intact.
    rdir = phot.run_output_dir(EXTERNAL_INSTRUMENT, DATE, TARGET, first["run_id"])
    assert (rdir / first["file"]).is_file() and product_record.read_record(rdir)
