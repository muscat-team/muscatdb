"""Shared fixtures + resource-availability guards for the muscat_db test suite.

The fast suite is designed to "skip cleanly off-host": a few tests need
resources that only exist on a configured MuSCAT host — chiefly the large
NASA/TOI catalog CSVs under ``data/`` (git-ignored, so absent on CI and fresh
checkouts). The :func:`catalog` fixture lets those tests skip instead of failing
there, while still running wherever the catalogs are present.

It also resets ``muscat_db.web``'s module-level catalog caches between tests:
those caches are keyed by target name and persist for the process lifetime, so a
test that queries a catalog while the data is unavailable can otherwise poison a
later test with an empty cached result.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture
def mock_db(monkeypatch):
    """Set up a temporary database for testing web endpoints."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("MUSCAT_DB_PATH", path)

    # Initialize the database schema
    conn = sqlite3.connect(path)
    from muscat_db.database import SCHEMA
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    # Mock sync_jobs so it doesn't clean up our mock active jobs
    monkeypatch.setattr("muscat_db.photometry.sync_jobs", lambda: None)
    monkeypatch.setattr("muscat_db.transit_fit.sync_jobs", lambda: None)
    # Mock discover_orphan_fits so it doesn't load production files from disk
    monkeypatch.setattr("muscat_db.transit_fit._discover_orphan_fits", lambda existing: [])
    monkeypatch.setattr("muscat_db.lco.archive_download_jobs", lambda: [])

    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def catalog_available() -> bool:
    """True when the NASA + TOI catalog CSVs are present under ``data/``."""
    return (_DATA_DIR / "nexsci_pscomppars.csv").is_file() and (_DATA_DIR / "TOIs.csv").is_file()


@pytest.fixture
def catalog():
    """Skip a test unless the local NASA/TOI catalog CSVs are available.

    The CSVs are git-ignored (too large to track), so they are absent on CI and
    fresh clones; tests that read them skip there rather than fail.
    """
    if not catalog_available():
        pytest.skip("NASA/TOI catalog CSVs under data/ are unavailable (skips off-host)")


# Module-level caches in muscat_db.web that must be cleared between tests so a
# negative/empty result cached under one test's data configuration does not leak
# into another. LRUCache and plain dicts both expose .clear().
_WEB_CACHE_ATTRS = (
    "_CATALOG_CACHE",
    "_index_cache",
    "_toi_cache",
    "_toi_db_cache",
    "_nexsci_cache",
    "_harps_cache",
    "_boyle_cache",
)


@pytest.fixture(autouse=True, scope="session")
def _offline_iers():
    """Use astropy's bundled IERS-A table instead of downloading one.

    Time-scale conversions with a location (e.g. TDB to UTC in
    ``transit_obs.bjd_tdb_to_jd_utc``) need UT1, which astropy's ``IERS_Auto``
    fetches from datacenter.iers.org or the USNO mirror once the bundled table
    is over ``auto_max_age`` (30) days old. Both can be unreachable from CI
    runners, and the fetch then raises after long timeouts. Disabling the
    download and the age check keeps the suite offline and stops it rotting as
    the pinned ``astropy-iers-data`` ages; the predictive UT1 error this allows
    is far below what any test asserts.
    """
    from astropy.utils import iers

    with iers.conf.set_temp("auto_download", False), iers.conf.set_temp("auto_max_age", None):
        yield


@pytest.fixture(autouse=True)
def _isolate_proxy_auth_config(monkeypatch):
    """Keep tests independent of an installed production proxy secret."""
    monkeypatch.delenv("MUSCAT_REQUIRE_AUTH", raising=False)
    monkeypatch.delenv("MUSCAT_PROXY_SECRET", raising=False)
    monkeypatch.setenv(
        "MUSCAT_PROXY_SECRET_FILE",
        str(_DATA_DIR / ".missing-proxy-secret-for-tests"),
    )


@pytest.fixture(autouse=True)
def _restore_static_site_env():
    """Undo ``static_site._prepare_env`` after each test.

    ``build_site`` points the process at its database and switches it into
    snapshot mode through ``os.environ``, which a CLI build can leave set but a
    test must not: every later test would read the last build's database
    (issue #228: an anonymous viewer there can be denied rows another test
    expects to see). Snapshotted by hand because ``monkeypatch.delenv`` records
    nothing for a variable that is absent, so it would not undo a later set.
    """
    names = ("MUSCAT_DB_PATH", "MUSCAT_STATIC_SITE", "MUSCAT_LCO_MONITOR_ENABLED")
    saved = {name: os.environ.get(name) for name in names}
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture(autouse=True)
def _isolate_db_backup_dir(monkeypatch, tmp_path_factory):
    """Keep build_db()'s pre-rebuild snapshots out of the real ``~/temp``.

    Every build_db() over an existing file now snapshots it first; without
    this, each such test would write (and prune!) real backups on the host.
    """
    monkeypatch.setenv("MUSCAT_DB_BACKUP_DIR", str(tmp_path_factory.mktemp("db-backups")))


@pytest.fixture(autouse=True)
def _isolate_gaia_disk_cache(monkeypatch, tmp_path_factory):
    """Keep the persistent Gaia cone-search cache out of the real cache dir."""
    monkeypatch.setenv("MUSCAT_GAIA_CACHE_DIR", str(tmp_path_factory.mktemp("gaia-cache")))


@pytest.fixture(autouse=True)
def _browser_request_headers(monkeypatch):
    """Make unsafe TestClient calls match real same-origin browser requests.

    Browsers attach Origin to fetch/XHR mutations.  Individual CSRF tests can
    pass ``X-Test-No-Origin: 1`` to deliberately model a non-browser/malicious
    client without weakening the production middleware.
    """
    original = TestClient.request

    def request(self, method, url, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        omit_origin = headers.pop("X-Test-No-Origin", None)
        if (
            method.upper() not in {"GET", "HEAD", "OPTIONS", "TRACE"}
            and not omit_origin
            and not any(k.lower() in {"origin", "referer"} for k in headers)
        ):
            headers["Origin"] = "http://testserver"
        return original(self, method, url, headers=headers, **kwargs)

    monkeypatch.setattr(TestClient, "request", request)


@pytest.fixture(autouse=True)
def _offline_photometry_coord_resolution(monkeypatch):
    """Keep ``photometry.build_command`` off the network.

    It resolves every run's target (offline catalogs, then a live SIMBAD
    query), and nearly every photometry and page test builds a command for a
    made-up name. Resolving nothing reproduces the old command for those; a
    test about resolution patches ``phot._resolve_archive_coords`` itself.
    """
    monkeypatch.setattr("muscat_db.photometry._resolve_archive_coords", lambda name: None)


@pytest.fixture
def mock_target_coord_resolution(monkeypatch):
    """Stub the live SIMBAD/Sesame name-resolution fallback.

    ``exposure.resolve_target_coords`` calls astropy's ``SkyCoord.from_name``,
    a real network request, whenever a target isn't found in the local (git-
    ignored, often-absent) NASA/TOI catalog CSVs. ``catalog.py`` and ``web.py``
    both reach it via ``from muscat_db import exposure as exp_calc``, a module
    alias, so patching the attribute here covers both call sites. Coordinates
    below are an arbitrary stand-in near TOI-837's field, not a verified
    catalog value -- tests using this only need *some* resolvable coordinate.
    """
    monkeypatch.setattr(
        "muscat_db.exposure.resolve_target_coords",
        lambda name: (140.0, -64.0),
    )


@pytest.fixture(autouse=True)
def _reset_web_catalog_caches():
    """Clear muscat_db.web's process-level catalog caches before each test."""
    web = __import__("sys").modules.get("muscat_db.web")
    if web is not None:
        for attr in _WEB_CACHE_ATTRS:
            cache = getattr(web, attr, None)
            if cache is not None and hasattr(cache, "clear"):
                cache.clear()
    yield
