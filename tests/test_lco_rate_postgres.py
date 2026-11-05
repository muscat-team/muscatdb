"""Atomic admission shared by independent web/worker/profile processes."""

from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
import json
import threading

import pytest

from muscat_db import lco, lco_rate
from tests.test_job_store_postgres import _DSN, _postgres_reachable

pytestmark = pytest.mark.skipif(not _postgres_reachable(_DSN), reason="isolated PostgreSQL unavailable")


@pytest.fixture
def rate(monkeypatch):
    import psycopg

    monkeypatch.setenv("MUSCAT_LCO_RATE_DSN", _DSN)
    lco_rate._coordinator.cache_clear()
    with psycopg.connect(_DSN) as conn:
        conn.execute("DROP TABLE IF EXISTS lco_api_requests, lco_rate_state")
    now = [86399.0]
    monkeypatch.setattr(lco_rate.Coordinator, "_now", staticmethod(lambda conn: now[0]))
    yield lco_rate.Coordinator(_DSN), now
    lco_rate._coordinator.cache_clear()


def test_atomic_cross_host_requests(rate):
    coordinator, now = rate
    gate = threading.Barrier(12)
    def reserve(_):
        # Independent coordinators/connections, as on different hosts.
        other = lco_rate.Coordinator(_DSN)
        gate.wait(timeout=5)
        return other.reserve("archive")
    with ThreadPoolExecutor(12) as pool:
        results = list(pool.map(reserve, range(12)))
    assert results.count(0) == 1
    assert results.count(1) == 11
    now[0] += 1
    assert coordinator.reserve("archive") == 0


def test_dev_prod_and_restart_share_budget(rate, monkeypatch):
    coordinator, now = rate
    monkeypatch.setenv("MUSCAT_POSTGRES_DSN", "postgresql://profile/dev")
    assert lco_rate.coordinator().reserve("archive") == 0
    monkeypatch.setenv("MUSCAT_POSTGRES_DSN", "postgresql://profile/prod")
    lco_rate._coordinator.cache_clear()  # process restart: ledger survives
    assert lco_rate.coordinator().reserve("archive") == 1
    assert coordinator.reserve("archive") == 1


def test_rolling_daily_budget_does_not_reset_at_midnight(rate):
    coordinator, now = rate
    policy = dict(lco_rate._POLICY, api_daily=2)
    coordinator = lco_rate.Coordinator(_DSN, policy=policy)
    assert coordinator.reserve("archive") == 0
    now[0] = 86400
    assert coordinator.reserve("archive") == 0
    now[0] += 1
    assert coordinator.reserve("archive") == 86398
    now[0] = 86399 + lco_rate.DAY
    assert coordinator.reserve("archive") == 0
    now[0] += 1
    assert coordinator.reserve("archive") == 0
    now[0] += 1
    assert coordinator.reserve("archive") == 86398


def test_default_budget_leaves_headroom_below_both_lco_publications(rate):
    import psycopg

    coordinator, now = rate
    assert lco_rate._POLICY["api_daily"] == 4000  # archive guide: 5000; developer reference: 50000
    assert lco_rate._POLICY["anonymous_daily"] == 500  # below 1000 and 3500
    coordinator.reserve("archive")
    with psycopg.connect(_DSN) as conn:
        conn.execute("INSERT INTO lco_api_requests SELECT %s, false FROM generate_series(1, 3999)", (now[0],))
    now[0] += 1
    assert coordinator.reserve("archive") == 86399
    now[0] += 86399
    assert coordinator.reserve("archive") == 0


def test_anonymous_has_smaller_budget_and_storage_does_not_debit_api(rate):
    coordinator, now = rate
    coordinator = lco_rate.Coordinator(_DSN, policy=dict(lco_rate._POLICY, anonymous_daily=1, api_daily=2))
    assert coordinator.reserve("archive", anonymous=True) == 0
    now[0] += 1
    assert coordinator.reserve("archive", anonymous=True) == 86399
    assert coordinator.reserve("archive") == 0
    assert coordinator.reserve("storage") == 0
    assert coordinator.reserve("storage") == 0.25
    now[0] += 1
    assert coordinator.reserve("storage") == 0
    assert coordinator.reserve("archive") == 86398


def test_cooldown_is_shared_monotonic_and_separate_from_storage(rate):
    coordinator, now = rate
    other = lco_rate.Coordinator(_DSN)
    assert coordinator.cooldown("archive", "120") == 120
    now[0] += 1
    assert other.cooldown("archive", "1") == 119
    assert other.reserve("archive") == 119
    assert other.reserve("storage") == 0
    now[0] += 119
    assert other.reserve("archive") == 0
    assert coordinator.reserve("archive") == 1  # paced recovery, no burst


@pytest.mark.parametrize("header", [None, "garbage", "-3"])
def test_bad_retry_after_has_shared_fallback(rate, header):
    coordinator, now = rate
    assert coordinator.cooldown("storage", header) == 60
    assert lco_rate.Coordinator(_DSN).reserve("storage") == 60
    assert coordinator.reserve("archive") == 0


def test_http_date_uses_database_time_and_survives_midnight(rate):
    coordinator, now = rate
    header = formatdate(now[0] + 240, usegmt=True)
    assert coordinator.cooldown("archive", header) == 240
    now[0] += 1
    assert lco_rate.Coordinator(_DSN).reserve("archive") == 239


def test_daily_exhaustion_is_atomic(rate):
    coordinator, now = rate
    policy = dict(lco_rate._POLICY, api_daily=1, api_interval=0)
    with ThreadPoolExecutor(8) as pool:
        delays = list(pool.map(lambda _: lco_rate.Coordinator(_DSN, policy=policy).reserve("archive"), range(8)))
    assert delays.count(0) == 1
    assert delays.count(86400) == 7


def test_mismatched_policy_fails_closed_and_state_contains_no_identity(rate):
    import psycopg

    coordinator, now = rate
    coordinator.reserve("archive")
    with pytest.raises(lco.LcoError, match="policy mismatched"):
        lco_rate.Coordinator(_DSN, policy=dict(lco_rate._POLICY, api_daily=50000)).reserve("archive")
    with psycopg.connect(_DSN) as conn:
        rows = conn.execute("SELECT * FROM lco_rate_state").fetchall()
        events = conn.execute("SELECT * FROM lco_api_requests").fetchall()
    assert rows == [("archive", lco_rate._POLICY, now[0] + 1, 0)]
    assert events == [(now[0], False)]
    assert "postgres" not in json.dumps(rows)
