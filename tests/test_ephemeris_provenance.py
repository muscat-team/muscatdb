"""Ephemeris provenance (#383): every catalog entry says which catalog it came
from, NASA entries carry a paper per parameter, and target-info reports the
seed the schedule page's linear fit anchors on."""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from muscat_db import catalog, web

LEONARDI = (
    "<a refstr=LEONARDI_ET_AL__2024 "
    "href=https://ui.adsabs.harvard.edu/abs/2024A&A...686A..84L/abstract "
    "target=ref>Leonardi et al. 2024</a>"
)
IVSHINA = (
    '<a refstr=IVSHINA__AMP__WINN_2022 '
    'href="https://ui.adsabs.harvard.edu/abs/2022ApJS..259...62I/abstract" '
    "target=ref>Ivshina &amp; Winn 2022</a>"
)
LEONARDI_REF = {
    "label": "Leonardi et al. 2024",
    "url": "https://ui.adsabs.harvard.edu/abs/2024A&A...686A..84L/abstract",
}
IVSHINA_REF = {
    "label": "Ivshina & Winn 2022",
    "url": "https://ui.adsabs.harvard.edu/abs/2022ApJS..259...62I/abstract",
}


@pytest.fixture(autouse=True)
def _clean_cache():
    catalog._CATALOG_CACHE.clear()
    yield
    catalog._CATALOG_CACHE.clear()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    here = tmp_path / "src" / "muscat_db"
    here.mkdir(parents=True)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(catalog, "HERE", here)

    def no_network(*args, **kwargs):
        raise AssertionError("catalog lookup went online")

    monkeypatch.setattr(catalog, "_sync_get", no_network)
    return tmp_path / "data"


def _write_csv(path: Path, rows: list[dict]) -> None:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    path.write_text(buf.getvalue())


WASP12 = {
    "pl_name": "WASP-12 b", "hostname": "WASP-12", "tic_id": "TIC 86396382",
    "pl_tranmid": "2457010.512173", "pl_tranmiderr1": "0.00007", "pl_tranmiderr2": "-0.00007",
    "pl_orbper": "1.09141890", "pl_orbpererr1": "0.0000003", "pl_orbpererr2": "-0.0000003",
    "pl_trandur": "3.0", "pl_trandurerr1": "0.01", "pl_trandurerr2": "-0.01",
}


# --- parse_nasa_reflink ------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (LEONARDI, LEONARDI_REF),
    (IVSHINA, IVSHINA_REF),
    ("<a refstr=X href='https://exofop.ipac.caltech.edu/tess/view_toi.php' target=ref>ExoFOP-TESS TOI</a>",
     {"label": "ExoFOP-TESS TOI", "url": "https://exofop.ipac.caltech.edu/tess/view_toi.php"}),
    ("<a href=/docs/composite_calc.html target=_blank>Calculated Value</a>",
     {"label": "Calculated Value", "url": "https://exoplanetarchive.ipac.caltech.edu/docs/composite_calc.html"}),
    ("Smith et al. 2020", {"label": "Smith et al. 2020", "url": None}),
])
def test_reflink_parser_returns_label_and_absolute_url(value, expected):
    assert catalog.parse_nasa_reflink(value) == expected


@pytest.mark.parametrize("value", [None, "", "   ", 3.0])
def test_reflink_parser_returns_none_for_an_empty_cell(value):
    assert catalog.parse_nasa_reflink(value) is None


def test_reflink_parser_drops_a_non_http_href():
    got = catalog.parse_nasa_reflink("<a href=javascript:alert(1)>Evil 2024</a>")
    assert got == {"label": "Evil 2024", "url": None}


# --- catalog entries ---------------------------------------------------------

def test_nasa_entry_carries_a_paper_per_parameter(data_dir):
    _write_csv(data_dir / "nexsci_pscomppars.csv", [{
        **WASP12,
        "pl_tranmid_reflink": LEONARDI,
        "pl_orbper_reflink": IVSHINA,
        "pl_trandur_reflink": LEONARDI,
    }])
    entry = catalog._query_target_planets_nasa("WASP-12")["b"]
    assert entry["source"] == "NASA"
    assert entry["refs"] == {"t0": LEONARDI_REF, "period": IVSHINA_REF, "duration": LEONARDI_REF}
    assert (entry["t0"], entry["period"], entry["duration"]) == (2457010.512173, 1.0914189, 3.0)
    assert entry["t0_unc"] == pytest.approx(0.00007)


def test_nasa_csv_without_reflink_columns_has_no_refs(data_dir):
    # A catalog downloaded before the reflink columns existed.
    _write_csv(data_dir / "nexsci_pscomppars.csv", [WASP12])
    entry = catalog._query_target_planets_nasa("WASP-12")["b"]
    assert entry["source"] == "NASA"
    assert "refs" not in entry


def test_online_nasa_query_asks_for_and_parses_reflinks(data_dir, monkeypatch):
    seen = {}

    class Resp:
        def json(self):
            return [{**WASP12, "pl_tranmid_reflink": LEONARDI, "pl_orbper_reflink": LEONARDI,
                     "pl_trandur_reflink": ""}]

    def fake_get(url, **kwargs):
        seen["url"] = url
        return Resp()

    monkeypatch.setattr(catalog, "_sync_get", fake_get)
    entry = catalog._query_target_planets_nasa("WASP-12")["b"]
    for column in ("pl_tranmid_reflink", "pl_orbper_reflink", "pl_trandur_reflink"):
        assert column in seen["url"]
    assert entry["refs"] == {"t0": LEONARDI_REF, "period": LEONARDI_REF}


def test_toi_and_local_csv_entries_name_their_source(data_dir):
    (data_dir / "TOIs.csv").write_text(
        "TIC ID,TOI,Epoch (BJD),Period (days),Duration (hours)\n"
        "1000,1000.01,2461314.86,2.17,2.0\n"
    )
    (data_dir / "muscatdb_targets_old.csv").write_text("name;t0;period\nOLDSTAR-1;2450000.1;3.5\n")
    assert catalog._query_target_planets_toi("TOI-1000")["b"]["source"] == "TOI"
    assert catalog._query_target_planets_catalog("TOI-1000")["b"]["source"] == "TOI"
    local = catalog._query_target_planets_catalog("OLDSTAR-1")["b"]
    assert local == {"t0": 2450000.1, "period": 3.5, "source": "local CSV"}


# --- linear-fit seed ---------------------------------------------------------

def test_linear_fit_seed_follows_the_page_order_and_keeps_refs():
    nasa = {"t0": 1.0, "period": 2.0, "source": "NASA", "refs": {"t0": LEONARDI_REF}}
    toi = {"t0": 3.0, "period": 4.0, "source": "TOI"}
    assert web._linear_fit_seed({}, nasa, toi) == {
        "source": "NASA", "t0": 1.0, "period": 2.0, "refs": {"t0": LEONARDI_REF}}
    assert web._linear_fit_seed({"t0": 5.0}, None, toi)["source"] == "TOI"
    assert web._linear_fit_seed({}, {}, {}) is None


def test_target_info_reports_the_linear_fit_seed(mock_db, monkeypatch):
    from fastapi.testclient import TestClient

    nasa = {"b": {"t0": 1.0, "period": 2.0, "source": "NASA", "refs": {"t0": LEONARDI_REF}}}
    monkeypatch.setattr(web, "_query_target_planets_nasa", lambda t: dict(nasa))
    monkeypatch.setattr(web, "_query_target_planets_toi", lambda t: {})
    monkeypatch.setattr(web, "_query_target_planets_catalog", lambda t: dict(nasa))
    monkeypatch.setattr(web, "_target_coordinates", lambda t: None)
    monkeypatch.setattr(web, "_user_ephem_sheet_cfg", lambda *a, **k: None)
    monkeypatch.setattr(web.ttv, "list_ttv_runs", lambda t: [])
    client = TestClient(web.app)
    body = client.get("/api/ephemeris/target-info", params={"target": "NOSUCH-TARGET-383"}).json()
    assert body["ok"]
    assert body["linear_fit_seed"]["b"] == {
        "source": "NASA", "t0": 1.0, "period": 2.0, "refs": {"t0": LEONARDI_REF}}


def test_online_nasa_query_ignores_hosts_that_only_share_a_prefix(data_dir, monkeypatch):
    # The archive query matches hostname LIKE 'HIP 6752%', which also returns
    # HIP 67522. A mistyped name must not borrow that planet's ephemeris.
    hip67522 = {**WASP12, "hostname": "HIP 67522", "pl_name": "HIP 67522 b"}

    class Resp:
        def json(self):
            return [hip67522]

    monkeypatch.setattr(catalog, "_sync_get", lambda url, **kwargs: Resp())
    assert catalog._query_target_planets_nasa("HIP 6752") == {}
    assert catalog._query_target_planets_nasa("HIP 67522")["b"]["period"] == pytest.approx(1.0914189)
