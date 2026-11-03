from __future__ import annotations

import numpy as np
import pytest
from astropy.io import fits

from muscat_db import scanner

KEYS = ["EXPTIME", "OBJECT", "RA", "DEC"]


def _write_mef(path, primary_cards, ext_cards_list):
    """Write a multi-extension FITS file: ``primary_cards`` go in the primary
    header and each entry of ``ext_cards_list`` becomes one image extension
    holding a small, deliberately non-block-aligned int16 image so that the
    data-unit padding has to be computed, not assumed."""
    primary = fits.PrimaryHDU()
    for k, v in primary_cards.items():
        primary.header[k] = v
    hdus = [primary]
    for i, cards in enumerate(ext_cards_list):
        ext = fits.ImageHDU(np.arange(37 * (i + 2), dtype=np.int16).reshape(i + 2, 37))
        for k, v in cards.items():
            ext.header[k] = v
        hdus.append(ext)
    fits.HDUList(hdus).writeto(path, overwrite=True)
    return str(path)


def _forbid_astropy(monkeypatch):
    def _boom(filepath, keys):
        raise AssertionError(f"astropy fallback used for {filepath}")
    monkeypatch.setattr(scanner, "_read_fits_header_astropy", _boom)


def test_raw_reader_fills_missing_keys_from_extension(tmp_path, monkeypatch):
    path = _write_mef(
        tmp_path / "mef.fits",
        {"EXPTIME": 30.0},
        [{"OBJECT": "TOI-1234", "RA": "10:00:00.0", "DEC": "+20:00:00"}],
    )
    _forbid_astropy(monkeypatch)
    assert scanner._read_fits_header_keys(path, KEYS) == {
        "EXPTIME": "30.0", "OBJECT": "TOI-1234", "RA": "10:00:00.0", "DEC": "+20:00:00",
    }


def test_raw_reader_keeps_primary_precedence(tmp_path, monkeypatch):
    path = _write_mef(
        tmp_path / "mef.fits",
        {"EXPTIME": 30.0, "OBJECT": "primary"},
        [{"OBJECT": "extension", "RA": "1", "DEC": "2"}],
    )
    _forbid_astropy(monkeypatch)
    kv = scanner._read_fits_header_keys(path, KEYS)
    assert kv["OBJECT"] == "primary"
    assert kv["RA"] == "1" and kv["DEC"] == "2"


def test_raw_reader_walks_past_a_heap_extension(tmp_path, monkeypatch):
    # A variable-length array column puts data in the heap (PCOUNT > 0), so
    # the second extension is only found when PCOUNT enters the size formula.
    primary = fits.PrimaryHDU()
    primary.header["EXPTIME"] = 10
    col = fits.Column(name="v", format="PJ()", array=np.array([np.arange(5), np.arange(3000)], dtype=object))
    table = fits.BinTableHDU.from_columns([col])
    image = fits.ImageHDU(np.zeros((3, 5), dtype=np.float32))
    image.header["OBJECT"] = "deep"
    image.header["RA"] = 1.5
    image.header["DEC"] = -2.5
    path = str(tmp_path / "heap.fits")
    fits.HDUList([primary, table, image]).writeto(path)
    # astropy only sizes the heap when writing, so check the on-disk header.
    assert fits.getheader(path, 1)["PCOUNT"] > 0
    _forbid_astropy(monkeypatch)
    assert scanner._read_fits_header_keys(path, KEYS) == {
        "EXPTIME": "10", "OBJECT": "deep", "RA": "1.5", "DEC": "-2.5",
    }


def test_raw_reader_single_hdu_missing_key_stays_blank(tmp_path, monkeypatch):
    primary = fits.PrimaryHDU(np.zeros((4, 4), dtype=np.int16))
    primary.header["EXPTIME"] = 5
    primary.header["OBJECT"] = "single"
    path = str(tmp_path / "single.fits")
    primary.writeto(path)
    _forbid_astropy(monkeypatch)
    assert scanner._read_fits_header_keys(path, KEYS) == {
        "EXPTIME": "5", "OBJECT": "single", "RA": "", "DEC": "",
    }


def test_raw_reader_still_defers_keyless_primary_to_astropy(tmp_path):
    path = _write_mef(tmp_path / "mef.fits", {}, [{"OBJECT": "x", "RA": "1", "DEC": "2", "EXPTIME": 1}])
    assert scanner._read_fits_header_raw(path, KEYS) is None


def test_raw_reader_truncated_extension_header_keeps_primary_values(tmp_path, monkeypatch):
    path = _write_mef(tmp_path / "mef.fits", {"EXPTIME": 30.0}, [{"OBJECT": "lost", "RA": "1", "DEC": "2"}])
    # Cut the file inside the extension header, before its END card.
    with open(path, "rb") as f:
        data = f.read()
    cut = data.index(b"XTENSION=") + 160
    with open(path, "wb") as f:
        f.write(data[:cut])
    _forbid_astropy(monkeypatch)
    assert scanner._read_fits_header_keys(path, KEYS) == {
        "EXPTIME": "30.0", "OBJECT": "", "RA": "", "DEC": "",
    }


def test_raw_reader_corrupt_primary_without_end_returns_blanks(tmp_path):
    path = tmp_path / "corrupt.fits"
    card = "SIMPLE  =                    T".ljust(80) + "BITPIX  =                   16".ljust(80)
    path.write_bytes(card.encode("ascii") + b" " * 2880)
    assert scanner._read_fits_header_raw(str(path), KEYS) == {k: "" for k in KEYS}


def test_raw_reader_unterminated_primary_with_a_key_returns_what_it_parsed(tmp_path, monkeypatch):
    # No END card within the read window: keep the keys found so far and do
    # not attempt to locate extensions, since the primary data offset is unknown.
    path = tmp_path / "long.fits"
    card = "SIMPLE  =                    T".ljust(80) + "EXPTIME =                 30.0".ljust(80)
    path.write_bytes(card.encode("ascii") + b" " * (scanner._FITS_HEADER_MAX_BYTES + 2880))
    _forbid_astropy(monkeypatch)
    assert scanner._read_fits_header_keys(str(path), KEYS) == {
        "EXPTIME": "30.0", "OBJECT": "", "RA": "", "DEC": "",
    }


@pytest.mark.parametrize("cards, expected", [
    ({"BITPIX": "16", "NAXIS": "0"}, 0),
    ({"BITPIX": "16", "NAXIS": "2", "NAXIS1": "37", "NAXIS2": "2"}, 2880),
    ({"BITPIX": "-32", "NAXIS": "2", "NAXIS1": "1000", "NAXIS2": "1"}, 5760),
    ({"BITPIX": "8", "NAXIS": "2", "NAXIS1": "10", "NAXIS2": "1", "PCOUNT": "3000", "GCOUNT": "1"}, 5760),
    ({"BITPIX": "16", "NAXIS": "2", "NAXIS1": "37"}, None),
    ({"BITPIX": "x", "NAXIS": "0"}, None),
])
def test_hdu_data_bytes(cards, expected):
    assert scanner._hdu_data_bytes(cards) == expected
