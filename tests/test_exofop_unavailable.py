"""An unreachable ExoFOP must read as unavailable, never as "no entries reported"."""

import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from muscat_db import catalog, exofop
from muscat_db.web import app


@pytest.fixture(autouse=True)
def clear_cache():
    exofop._exofop_cache.clear()
    yield
    exofop._exofop_cache.clear()


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def _scripted(monkeypatch, *answers):
    """Make ``catalog._sync_get`` return or raise each answer in turn; track calls."""
    queue = list(answers)
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        answer = queue.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(catalog, "_sync_get", get)
    return calls


def test_time_series_timeout_raises_and_logs_once(monkeypatch, caplog):
    calls = _scripted(monkeypatch, httpx.ReadTimeout("The read operation timed out"))
    with caplog.at_level(logging.WARNING, logger="muscat_db.exofop"):
        with pytest.raises(exofop.ExofopUnavailable):
            exofop.fetch_time_series("6715", _now=1000)
        # A repeat within the failure window is served from the cache: no new
        # request to ExoFOP and no second log line.
        with pytest.raises(exofop.ExofopUnavailable):
            exofop.fetch_time_series("6715", _now=1010)
    assert len(calls) == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "TOI-6715" in warnings[0].getMessage()
    assert "timed out" in warnings[0].getMessage()


def test_time_series_failure_is_retried_after_30_seconds(monkeypatch):
    good = _Resp({"time_series": [{"tsid": "1"}]})
    calls = _scripted(monkeypatch, httpx.ConnectError("offline"), good)
    with pytest.raises(exofop.ExofopUnavailable):
        exofop.fetch_time_series("6715", _now=1000)
    with pytest.raises(exofop.ExofopUnavailable):
        exofop.fetch_time_series("6715", _now=1029)
    assert len(calls) == 1
    assert exofop.fetch_time_series("6715", _now=1030) == [{"tsid": "1"}]
    assert len(calls) == 2
    # The recovered answer keeps the full one-hour cache.
    assert exofop.fetch_time_series("6715", _now=1030 + 3599) == [{"tsid": "1"}]
    assert len(calls) == 2


def test_empty_time_series_is_cached_for_less_than_an_hour(monkeypatch):
    calls = _scripted(
        monkeypatch, _Resp({"time_series": []}), _Resp({"time_series": [{"tsid": "1"}]}),
    )
    assert exofop.fetch_time_series("6715", _now=0) == []
    assert exofop.fetch_time_series("6715", _now=exofop._EXOFOP_EMPTY_CACHE_TTL_S - 1) == []
    assert len(calls) == 1
    assert exofop.fetch_time_series("6715", _now=exofop._EXOFOP_EMPTY_CACHE_TTL_S) == [{"tsid": "1"}]
    assert len(calls) == 2
    assert exofop._EXOFOP_EMPTY_CACHE_TTL_S < exofop._EXOFOP_CACHE_TTL_S


def test_fetch_by_name_distinguishes_outage_from_unknown_name(monkeypatch):
    _scripted(monkeypatch, httpx.ReadTimeout("timed out"))
    with pytest.raises(exofop.ExofopUnavailable):
        exofop.fetch_by_name("HIP 67522", _now=0)
    # Callers that only want coordinates keep the None contract during an outage.
    assert exofop.fetch_target_by_name("HIP 67522", _now=1) is None

    exofop._exofop_cache.clear()
    _scripted(monkeypatch, _Resp({"basic_info": {"tic_id": None}}))
    assert exofop.fetch_by_name("NOTAREALSTAR123", _now=0) is None

    exofop._exofop_cache.clear()
    request = httpx.Request("GET", "https://exofop.ipac.caltech.edu/tess/target.php")
    not_found = httpx.HTTPStatusError(
        "404", request=request, response=httpx.Response(404, request=request),
    )
    _scripted(monkeypatch, not_found)
    assert exofop.fetch_by_name("NOTAREALSTAR123", _now=0) is None


@pytest.mark.parametrize("target", ["TOI-6715", "HIP 67522"])
def test_build_report_says_unavailable_instead_of_asking_for_a_tic(monkeypatch, target):
    _scripted(monkeypatch, httpx.ReadTimeout("timed out"))
    monkeypatch.setattr(
        exofop, "resolve_tic_via_simbad",
        lambda name: pytest.fail("SIMBAD must not be asked while ExoFOP is down"),
    )
    report = exofop.build_time_series_report(target, _now=0)
    assert report["ok"] is False
    assert report["unavailable"] is True
    assert not report.get("needs_tic")
    assert "ExoFOP" in report["error"]


def test_route_answers_503_when_exofop_is_unavailable(mock_db, monkeypatch):
    _scripted(monkeypatch, httpx.ReadTimeout("timed out"))
    client = TestClient(app)
    r = client.get("/api/lco/archive/exofop", params={"OBJECT": "TOI-6715"})
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    assert body["unavailable"] is True
    assert body["needs_tic"] is False
    assert "ExoFOP" in body["error"]
