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
OPEN_TARGET = "OPENTGT"  # observed under an open proposal in the access fixture
HIDDEN_TARGET = "HIDDENTGT"  # observed only under the restricted one

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
        "target": TARGET,
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


def test_upload_input_follows_the_target_for_a_scoped_viewer(env):  # noqa: F811
    from muscat_db import access
    from muscat_db.access import NightVisibility
    from muscat_db.product_visibility import ProductVisibility

    access.set_scoped(env, "eve", True)
    access.grant_target(env, "eve", OPEN_TARGET, "root")
    denied = access.denied_proposal_ids_for(env, "eve")
    assert denied
    nights = NightVisibility(env, denied)
    # An LCO night with no summaries fails closed; an upload night is not gated by nights.
    assert nights.hidden("muscat3", DATE, TARGET)
    assert not nights.hidden(EXTERNAL_INSTRUMENT, DATE, TARGET)
    assert not nights.unknown_night(EXTERNAL_INSTRUMENT, DATE)
    vis = ProductVisibility("eve", denied, nights)
    assert vis._input_visible({"kind": "upload", "target": OPEN_TARGET, "files": []}, 0)
    assert not vis._input_visible({"kind": "upload", "target": HIDDEN_TARGET, "files": []}, 0)
    assert not vis._input_visible({"kind": "upload", "target": TARGET, "files": []}, 0)  # never observed
    assert not vis._input_visible({"kind": "upload", "files": []}, 0)  # no target recorded
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
        "target": OPEN_TARGET, "date": DATE, "band": "r", "facility": "ASTEP",
        "primary": {"name": "p.csv", "content_b64": _b64(PRIMARY)},
        "ancillary": {"name": "a.tsv", "content_b64": _b64(ANCILLARY)},
        "mapping": {**JOIN_MAPPING.__dict__, "covariates": ["AIRMASS"], "ancillary_covariates": ["fwhm_arcsec"]},
    }
    r = http.post("/api/lc-upload/save", headers=_headers("alice"), json=payload)
    assert r.status_code == 200, r.text
    saved = r.json()
    rdir = phot.run_output_dir(EXTERNAL_INSTRUMENT, DATE, OPEN_TARGET, saved["run_id"])
    assert product_record.read_record(rdir)["author"] == "alice"

    # alice is a restricted viewer in this fixture, and still sees the upload.
    page = http.get("/transit-fit", headers=_headers("alice"),
                    params={"inst": EXTERNAL_INSTRUMENT, "date": DATE, "target": OPEN_TARGET})
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
        "target": OPEN_TARGET, "date": DATE, "band": "r", "facility": "ASTEP",
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


# --------------------------------------------------------------------------
# Review of #409: 400s, caps, visibility follows the target, delete
# --------------------------------------------------------------------------


def _payload(target=OPEN_TARGET, data=PRIMARY, **extra):
    return {
        "target": target, "date": DATE, "band": "r", "facility": "ASTEP",
        "primary": {"name": "p.csv", "content_b64": _b64(data)},
        "mapping": {**BASE_MAPPING.__dict__, "covariates": ["AIRMASS"]},
        **extra,
    }


def _upload(http, user="alice", **kw):
    return http.post("/api/lc-upload/save", headers=_headers(user), json=_payload(**kw))


_HUGE_FIELD = b'time_bjd;rel_flux_T1\n"' + b"x" * 200_000 + b'";1\n'


@pytest.mark.parametrize("path,body", [
    ("/api/lc-upload/save", _payload(target="a\x00b")),
    ("/api/lc-upload/save", _payload(target="x" * 300)),
    ("/api/lc-upload/save", _payload(data=_HUGE_FIELD)),
    ("/api/lc-upload/inspect", {"file": {"name": "p.csv", "content_b64": _b64(_HUGE_FIELD)}}),
])
def test_http_unexpected_input_is_a_400_not_a_500(http, path, body, prose_dir):
    r = http.post(path, headers=_headers("root"), json=body)
    assert r.status_code == 400, r.text
    assert r.json()["ok"] is False and r.json()["error"]
    assert not list(prose_dir.glob("external/*"))


def _table_of(n_rows, n_cols):
    head = ",".join(f"c{i}" for i in range(n_cols))
    return ("\n".join([head] + [",".join("1" for _ in range(n_cols))] * n_rows)).encode()


def test_row_cap_is_enforced(monkeypatch):
    monkeypatch.setattr(lc_upload, "MAX_ROWS", 3)
    assert len(lc_upload.parse_table("p.csv", _table_of(3, 2)).rows) == 3
    with pytest.raises(UploadError, match="more than 3 rows"):
        lc_upload.parse_table("p.csv", _table_of(4, 2))


def test_column_cap_is_enforced(monkeypatch):
    monkeypatch.setattr(lc_upload, "MAX_COLUMNS", 3)
    assert len(lc_upload.parse_table("p.csv", _table_of(2, 3)).columns) == 3
    with pytest.raises(UploadError, match="more than 3 columns"):
        lc_upload.parse_table("p.csv", _table_of(2, 4))


def test_viewer_cannot_upload_under_a_target_they_cannot_see(http, prose_dir, env):  # noqa: F811
    from muscat_db import access
    access.set_scoped(env, "eve", True)
    r = _upload(http, "alice", target=HIDDEN_TARGET)
    assert r.status_code == 403 and HIDDEN_TARGET in r.json()["error"]
    # A name nobody observed fails closed for a restricted viewer too, and says the same.
    r = _upload(http, "eve", target="NEVEROBSERVED")
    assert r.status_code == 403
    assert not list(prose_dir.glob("external/*"))
    assert _upload(http, "root", target=HIDDEN_TARGET).status_code == 200


def test_an_upload_is_visible_to_whoever_may_see_its_target(http):
    saved = _upload(http, "root", target=OPEN_TARGET).json()
    params = {"inst": EXTERNAL_INSTRUMENT, "date": DATE, "target": OPEN_TARGET}
    page = http.get("/transit-fit", headers=_headers("alice"), params=params)
    assert saved["file"] in page.text  # alice did not upload it, but may see the target


def test_an_upload_under_a_hidden_target_is_hidden_and_not_in_the_picker(http):
    saved = _upload(http, "root", target=HIDDEN_TARGET).json()
    params = {"inst": EXTERNAL_INSTRUMENT, "date": DATE}
    for user, listed in (("root", True), ("alice", False)):
        page = http.get("/transit-fit", headers=_headers(user), params=params)
        assert (HIDDEN_TARGET in page.text) is listed, user
        page = http.get("/transit-fit", headers=_headers(user), params={**params, "target": HIDDEN_TARGET})
        assert (saved["file"] in page.text) is listed, user


def test_duplicate_refusal_does_not_name_an_upload_the_submitter_cannot_see(http):
    saved = _upload(http, "root", target=HIDDEN_TARGET).json()
    r = _upload(http, "alice", target=OPEN_TARGET)
    assert r.status_code == 400 and "already uploaded" in r.json()["error"]
    assert saved["run_id"] not in r.json()["error"] and HIDDEN_TARGET not in r.json()["error"]


def test_duplicate_refusal_names_a_visible_upload(http):
    saved = _upload(http, "alice").json()
    r = _upload(http, "alice")
    assert saved["run_id"] in r.json()["error"]


def _delete(http, user, run_id, target=OPEN_TARGET):
    return http.post("/api/lc-upload/delete", headers=_headers(user),
                     json={"date": DATE, "target": target, "run_id": run_id})


def test_uploader_can_delete_a_mistaken_upload_and_upload_again(http, prose_dir):
    saved = _upload(http, "alice").json()
    rdir = phot.run_output_dir(EXTERNAL_INSTRUMENT, DATE, OPEN_TARGET, saved["run_id"])
    assert rdir.is_dir()
    assert _delete(http, "alice", saved["run_id"]).status_code == 200
    assert not rdir.exists()
    again = _upload(http, "alice", band="i")  # same file, corrected band
    assert again.status_code == 200, again.text


def test_delete_is_refused_to_someone_who_does_not_own_the_upload(http):
    saved = _upload(http, "alice").json()
    r = _delete(http, "bob", saved["run_id"])
    assert r.status_code == 403
    assert phot.run_output_dir(EXTERNAL_INSTRUMENT, DATE, OPEN_TARGET, saved["run_id"]).is_dir()
    assert _delete(http, "root", saved["run_id"]).status_code == 200


def test_delete_hidden_or_unknown_runs_404_and_only_uploads_are_deletable(http):
    saved = _upload(http, "root", target=HIDDEN_TARGET).json()
    assert _delete(http, "alice", saved["run_id"], HIDDEN_TARGET).status_code == 404
    assert _delete(http, "root", "not-an-upload").status_code == 400
    assert _delete(http, "root", "upload-0000000000000000").status_code == 404


@pytest.fixture(params=[False, True], ids=["implicit-groups", "explicit-groups"])
def upload_access(env, request):  # noqa: F811
    from muscat_db import access, access_explicit, access_groups
    from muscat_db.database import get_conn

    access.set_scoped(env, "eve", True)
    access.set_scoped(env, "mallory", True)
    access.grant_target(env, "eve", "TOI-9999", "root")
    with get_conn(env) as conn:
        # This compound grant must not turn into a target-only grant.
        access_groups.add_grant(conn, "user:mallory", target="TOI-9999", proposal_id="PRIVATE")
        if request.param:
            access_explicit.migrate(conn, extra_users=["alice", "bob", "eve", "mallory", "root"])
        conn.commit()
    return env


@pytest.mark.parametrize("user,allowed", [("alice", True), ("eve", True), ("mallory", False)])
def test_never_observed_target_upload_permissions(http, upload_access, user, allowed):
    # The review's contract: team membership or an unqualified target grant;
    # a spelling alias must use the same stable target key.
    response = _upload(http, user, target="TOI09999.01")
    assert response.status_code == (200 if allowed else 403), response.text


def test_never_observed_upload_visibility_and_pinned_fit_after_delete(http, upload_access):
    response = _upload(http, "root", target="TOI-9999")
    assert response.status_code == 200, response.text
    saved = response.json()
    params = {"inst": EXTERNAL_INSTRUMENT, "date": DATE, "target": "TOI-9999"}
    csvs = fit.get_csv_lightcurves(EXTERNAL_INSTRUMENT, DATE, "TOI-9999")
    rdir = fit.fit_output_dir(EXTERNAL_INSTRUMENT, DATE, "TOI-9999", "default")
    rdir.mkdir(parents=True)
    fit._write_fit_product(rdir, EXTERNAL_INSTRUMENT, DATE, "TOI-9999", csvs, {}, False, "root")
    (rdir / "out").mkdir()
    (rdir / "out" / "summary.csv").write_text("parameter,mean\nt0[0],2461044.0\n")
    for user, visible in [("alice", True), ("eve", True), ("mallory", False)]:
        page = http.get("/transit-fit", headers=_headers(user), params=params)
        assert (saved["file"] in page.text) is visible, user
        picker = http.get("/transit-fit", headers=_headers(user), params={"inst": EXTERNAL_INSTRUMENT, "date": DATE})
        assert ("TOI-9999" in picker.text) is visible, user
    assert _delete(http, "root", saved["run_id"], "TOI-9999").status_code == 200
    for user, visible in [("alice", True), ("eve", True), ("mallory", False)]:
        response = http.get("/api/transit-fit/outputs", headers=_headers(user), params={**params, "run": "default"})
        assert response.status_code == 200
        assert response.json()["outputs"]["has_any"] is visible, user


@pytest.mark.parametrize("field,value", [
    ("target", "x" * 300), ("run_id", "upload-" + "x" * 300),
    ("target", "星" * 100), ("run_id", "upload-" + "星" * 84),
])
def test_delete_oversized_components_returns_400_without_removing_upload(http, field, value):
    saved = _upload(http, "alice").json()
    payload = {"date": DATE, "target": OPEN_TARGET, "run_id": saved["run_id"], field: value}
    response = http.post("/api/lc-upload/delete", headers=_headers("root"), json=payload)
    assert response.status_code == 400, response.text
    assert phot.run_output_dir(EXTERNAL_INSTRUMENT, DATE, OPEN_TARGET, saved["run_id"]).is_dir()


def test_save_rejects_oversized_utf8_csv_name_before_creating_directories(http, prose_dir):
    response = _upload(http, "root", target="星" * 84)
    assert response.status_code == 400, response.text
    assert "bytes" in response.json()["error"]
    assert not list(prose_dir.glob("external/*"))


def test_save_accepts_utf8_filename_at_portable_boundary(http):
    # 213 bytes of target + the 42-byte CSV/temp suffix reaches NAME_MAX=255.
    response = _upload(http, "root", target="星" * 71)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("restriction", [
    {"instrument": "muscat3"}, {"date_from": "260101"}, {"date_from": "260101", "date_to": "260104"},
])
def test_never_observed_compound_target_grant_does_not_bypass_constraints(http, upload_access, restriction):
    from muscat_db import access_groups
    from muscat_db.database import get_conn
    with get_conn(upload_access) as conn:
        access_groups.add_grant(conn, "user:mallory", target="NEWSTAR", **restriction)
        conn.commit()
    assert _upload(http, "mallory", target="NEWSTAR").status_code == 403


def test_observed_restricted_alias_does_not_use_unobserved_team_rule(http, upload_access):
    from muscat_db.database import get_conn
    from tests.test_access_control import _RESTRICTED
    with get_conn(upload_access) as conn:
        conn.execute(
            "INSERT INTO summaries (instrument, obsdate, ccd, object, proposal_id) VALUES (?, ?, 0, ?, ?)",
            ("muscat3", DATE, "TOI-12345", _RESTRICTED),
        )
        conn.commit()
    assert _upload(http, "alice", target="TOI012345.01").status_code == 403
