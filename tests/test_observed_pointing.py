"""Tests for muscat_db.database.get_observed_pointing.

Builds a minimal frames table directly (rather than going through the full
CSV-ingest/build_db pipeline) so these stay fast and focused on the
aggregation logic: pick the lowest CCD present that night, take the
median-by-declination (ra, dec) pair, and the median non-null PA.
"""
import tempfile
import os

import pytest

from muscat_db.database import _apply_schema, get_conn, get_observed_pointing


@pytest.fixture
def db_path():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    with get_conn(path) as conn:
        _apply_schema(conn)
    yield path
    os.unlink(path)


def _insert_frame(conn, **kw):
    defaults = dict(
        instrument="muscat3", obsdate="260101", ccd=0, filename="f.fits",
        object="WASP-12", jd_start=2460000.5, ut_start="00:00:00",
        exptime=10.0, read_mode="MUSCAT_FAST", filter="g",
        ra="06:36:38.00", declination="+29:40:20.0",
        airmass=1.1, focus=0.0, pa=None,
    )
    defaults.update(kw)
    conn.execute(
        """INSERT INTO frames
           (instrument, obsdate, ccd, filename, object, jd_start, ut_start,
            exptime, read_mode, filter, ra, declination, airmass, focus, pa)
           VALUES (:instrument, :obsdate, :ccd, :filename, :object, :jd_start,
                   :ut_start, :exptime, :read_mode, :filter, :ra, :declination,
                   :airmass, :focus, :pa)""",
        defaults,
    )


def test_returns_none_when_no_frames_match(db_path):
    assert get_observed_pointing(db_path, "muscat3", "260101", "WASP-12") is None


def test_picks_lowest_ccd_and_converts_to_degrees(db_path):
    with get_conn(db_path) as conn:
        _insert_frame(conn, ccd=0, ra="06:36:38.00", declination="+29:40:20.0")
        _insert_frame(conn, ccd=1, ra="06:00:00.00", declination="+00:00:00.0")
        conn.commit()

    result = get_observed_pointing(db_path, "muscat3", "260101", "WASP-12")
    assert result is not None
    assert result["ccd"] == 0
    assert result["ra_deg"] == pytest.approx((6 + 36 / 60 + 38 / 3600) * 15.0, abs=1e-6)
    assert result["dec_deg"] == pytest.approx(29 + 40 / 60 + 20 / 3600, abs=1e-6)
    assert result["n_frames"] == 1


def test_ignores_garbage_coordinates_via_pick_representative(db_path):
    with get_conn(db_path) as conn:
        _insert_frame(conn, ccd=0, ra="q", declination="OQ")
        _insert_frame(conn, ccd=0, ra="06:36:38.00", declination="+29:40:20.0")
        conn.commit()

    result = get_observed_pointing(db_path, "muscat3", "260101", "WASP-12")
    assert result is not None
    assert result["ra_deg"] == pytest.approx((6 + 36 / 60 + 38 / 3600) * 15.0, abs=1e-6)


def test_pa_is_median_of_non_null_values(db_path):
    with get_conn(db_path) as conn:
        _insert_frame(conn, ccd=0, pa=10.0)
        _insert_frame(conn, ccd=0, pa=20.0)
        _insert_frame(conn, ccd=0, pa=30.0)
        conn.commit()

    result = get_observed_pointing(db_path, "muscat3", "260101", "WASP-12")
    assert result["pa_deg"] == pytest.approx(20.0)


def test_pa_is_none_when_instrument_never_records_it(db_path):
    with get_conn(db_path) as conn:
        _insert_frame(conn, ccd=0, pa=None)
        conn.commit()

    result = get_observed_pointing(db_path, "muscat3", "260101", "WASP-12")
    assert result["pa_deg"] is None


def test_read_mode_reflects_the_used_ccd(db_path):
    with get_conn(db_path) as conn:
        _insert_frame(conn, instrument="sinistro", ccd=0, read_mode="full_frame")
        conn.commit()

    result = get_observed_pointing(db_path, "sinistro", "260101", "WASP-12")
    assert result["read_mode"] == "full_frame"


def test_scoped_to_instrument_obsdate_and_object(db_path):
    with get_conn(db_path) as conn:
        _insert_frame(conn, instrument="muscat3", obsdate="260101", object="WASP-12")
        _insert_frame(conn, instrument="muscat4", obsdate="260101", object="WASP-12")
        _insert_frame(conn, instrument="muscat3", obsdate="260102", object="WASP-12")
        _insert_frame(conn, instrument="muscat3", obsdate="260101", object="TOI-1234")
        conn.commit()

    assert get_observed_pointing(db_path, "muscat3", "260101", "WASP-12") is not None
    assert get_observed_pointing(db_path, "muscat4", "260101", "WASP-12") is not None
    assert get_observed_pointing(db_path, "muscat3", "260101", "TOI-1234") is not None
    assert get_observed_pointing(db_path, "muscat3", "260103", "WASP-12") is None
