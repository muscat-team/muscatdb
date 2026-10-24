"""A TTV fit needs a planet pair: harmonic models TTVs per adjacent pair, so a
single planet letter has no TTV model, with or without a non-transiting outer
planet (which needs a letter of its own)."""

import pytest

from muscat_db import ttv_fit as ttv

_CSV = "planet,epoch,tc,tc_unc\nb,0,2459000.0,0.0005\nb,1,2459004.0,0.0005\n"


def _options(letters: str, **extra) -> dict:
    return {"csv_content": _CSV, "ini_content": "[INIT]\na_bc = 0.001\n",
            "planet_letters": letters, **extra}


@pytest.mark.parametrize("non_transiting_outer", [False, True])
def test_a_single_planet_letter_is_refused_with_the_reason(non_transiting_outer):
    error = ttv.validate_ttv_options(_options("b", non_transiting_outer=non_transiting_outer))

    assert error is not None
    assert "at least two planet letters" in error
    assert "Non-transiting outer planet" in error


@pytest.mark.parametrize("non_transiting_outer", [False, True])
def test_two_planet_letters_are_accepted(non_transiting_outer):
    assert ttv.validate_ttv_options(_options("bc", non_transiting_outer=non_transiting_outer)) is None
