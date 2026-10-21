"""Catalog positions are Gaia DR2 at J2015.5: their epoch and proper motion
must reach the schedule page and the LCO target, or a high proper-motion star
is pointed at (and matched) arcseconds from where it is."""

from __future__ import annotations

from pathlib import Path

import pytest

from muscat_db import catalog, lco

# LHS 3844 as NExScI lists it (pscomppars and toi agree).
LHS3844 = {"ra": 340.4962038, "dec": -69.1721081, "pmra": 334.357, "pmdec": -726.974}


@pytest.fixture(autouse=True)
def _clean_cache():
    catalog._CATALOG_CACHE.clear()
    yield
    catalog._CATALOG_CACHE.clear()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    # _query_target_coordinates reads HERE/../../data/*.csv.
    here = tmp_path / "src" / "muscat_db"
    here.mkdir(parents=True)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(catalog, "HERE", here)
    monkeypatch.setattr(catalog.exp_calc, "resolve_target_coords", lambda n: None)
    return tmp_path / "data"


def _write_nasa(data_dir: Path, with_pm: bool = True) -> None:
    head = "pl_name,hostname,tic_id,ra_x,dec_x" + (",sy_pmra,sy_pmdec" if with_pm else "")
    row = f"LHS 3844 b,LHS 3844,TIC 410153553,{LHS3844['ra']},{LHS3844['dec']}"
    if with_pm:
        row += f",{LHS3844['pmra']},{LHS3844['pmdec']}"
    (data_dir / "nexsci_pscomppars.csv").write_text(head + "\n" + row + "\n")


def test_nasa_position_carries_gaia_epoch_and_proper_motion(data_dir):
    _write_nasa(data_dir)
    got = catalog.resolve_target_astrometry("LHS 3844")
    assert got == {"ra": LHS3844["ra"], "dec": LHS3844["dec"], "source": "nasa",
                   "epoch": 2015.5, "pmra": LHS3844["pmra"], "pmdec": LHS3844["pmdec"]}


def test_toi_position_carries_gaia_epoch_and_proper_motion(data_dir):
    (data_dir / "TOIs.csv").write_text(
        'TIC ID,TOI,ra_deg,dec_deg,PM RA (mas/yr),PM Dec (mas/yr)\n'
        f"410153553,136.01,{LHS3844['ra']},{LHS3844['dec']},{LHS3844['pmra']},{LHS3844['pmdec']}\n"
    )
    got = catalog.resolve_target_astrometry("TOI-136")
    assert (got["source"], got["epoch"], got["pmra"], got["pmdec"]) == (
        "toi", 2015.5, LHS3844["pmra"], LHS3844["pmdec"])


def test_csv_without_proper_motion_columns_keeps_the_epoch(data_dir):
    # A snapshot downloaded before the columns were added.
    _write_nasa(data_dir, with_pm=False)
    got = catalog.resolve_target_astrometry("LHS 3844")
    assert (got["epoch"], got["pmra"], got["pmdec"]) == (2015.5, None, None)


def test_simbad_fallback_is_j2000_without_proper_motion(data_dir, monkeypatch):
    monkeypatch.setattr(catalog.exp_calc, "resolve_target_coords", lambda n: (10.0, -20.0))
    got = catalog.resolve_target_astrometry("NOT-IN-CATALOG")
    assert got == {"ra": 10.0, "dec": -20.0, "source": "simbad", "epoch": 2000.0, "pmra": None, "pmdec": None}


def test_archive_resolver_still_returns_a_tuple(data_dir):
    _write_nasa(data_dir)
    assert catalog._resolve_archive_coords("LHS 3844") == (LHS3844["ra"], LHS3844["dec"], "nasa")


def test_download_queries_select_proper_motion():
    script = (Path(__file__).resolve().parent.parent / "scripts" / "download_catalogs.sh").read_text()
    script = script.replace(r'\"', '"')
    assert 'st_pmra AS "PM RA (mas/yr)", st_pmdec AS "PM Dec (mas/yr)"' in script
    assert "ra AS ra_x, dec AS dec_x, sy_pmra, sy_pmdec," in script


# --- LCO target ---------------------------------------------------------------

def _params(**extra) -> dict:
    return {
        "name": "pm", "proposal": "LCO2026A-001", "target_name": "LHS 3844",
        "ra": LHS3844["ra"], "dec": LHS3844["dec"], "kind": "sinistro",
        "exposure_time": 60, "exposure_count": 5, "filter": "rp",
        "windows": [{"start": "2026-07-01T00:00:00Z", "end": "2026-07-01T01:00:00Z"}],
        **extra,
    }


def _target(params: dict) -> dict:
    return lco.build_requestgroup("sinistro", params)["requests"][0]["configurations"][0]["target"]


def test_lco_target_carries_epoch_and_proper_motion():
    target = _target(_params(epoch=2015.5, pmra=LHS3844["pmra"], pmdec=LHS3844["pmdec"]))
    assert target["epoch"] == 2015.5
    assert target["proper_motion_ra"] == LHS3844["pmra"]
    assert target["proper_motion_dec"] == LHS3844["pmdec"]


def test_lco_target_without_astrometry_keeps_lco_defaults():
    target = _target(_params())
    assert not {"epoch", "proper_motion_ra", "proper_motion_dec"} & set(target)


def test_lco_target_epoch_alone_is_sent():
    target = _target(_params(epoch=2000.0))
    assert target["epoch"] == 2000.0
    assert "proper_motion_ra" not in target


@pytest.mark.parametrize("extra, message", [
    ({"pmra": 334.0, "pmdec": -727.0}, "needs the epoch"),
    ({"epoch": 15.5}, "outside"),
    ({"epoch": 2015.5, "pmra": 30000.0, "pmdec": 0.0}, "exceeds"),
    ({"epoch": "soon"}, "must be a number"),
    ({"epoch": float("nan")}, "must be a number"),
])
def test_lco_target_rejects_bad_astrometry(extra, message):
    with pytest.raises(lco.LcoError, match=message):
        _target(_params(**extra))
