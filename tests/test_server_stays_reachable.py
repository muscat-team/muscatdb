"""Health and login checks must answer while every worker thread is busy.

FastAPI runs each plain ``def`` route on one shared pool of worker threads.
When slow requests held all of them (2026-11-07), ``/healthz`` and nginx's
``/_auth/cf`` login check queued behind them, nginx timed out, and every page
returned 500 even though the server itself was still running.
"""

from __future__ import annotations

import anyio
import anyio.to_thread
import httpx
import pytest

from muscat_db import cf_access, web

SECRET = "proxy-secret"


async def _get_with_pool_exhausted(path: str, headers: dict | None = None) -> httpx.Response:
    limiter = anyio.to_thread.current_default_thread_limiter()
    holders = [object() for _ in range(int(limiter.total_tokens))]
    for holder in holders:
        await limiter.acquire_on_behalf_of(holder)
    try:
        transport = httpx.ASGITransport(app=web.app, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            with anyio.fail_after(5):
                return await client.get(path, headers=headers or {})
    finally:
        for holder in holders:
            limiter.release_on_behalf_of(holder)


def test_healthz_answers_with_every_worker_thread_busy():
    response = anyio.run(_get_with_pool_exhausted, "/healthz")
    assert response.status_code == 200


@pytest.fixture
def access_configured(monkeypatch):
    monkeypatch.setenv("MUSCAT_PROXY_SECRET", SECRET)
    monkeypatch.setenv("MUSCAT_CF_TEAM_DOMAIN", "team.example.com")
    monkeypatch.setenv("MUSCAT_CF_AUD", "aud")
    monkeypatch.setattr(cf_access, "verified_email", lambda token: "a@example.com" if token == "jwt" else None)


def test_login_check_answers_with_every_worker_thread_busy(access_configured):
    response = anyio.run(_get_with_pool_exhausted, cf_access.AUTH_PATH,
                         {cf_access.PROXY_SECRET_HEADER: SECRET, cf_access.JWT_HEADER: "jwt"})
    assert response.status_code == 204
    assert response.headers[cf_access.EMAIL_HEADER] == "a@example.com"


def test_login_check_still_refuses_a_bad_token_off_the_shared_pool(access_configured):
    response = anyio.run(_get_with_pool_exhausted, cf_access.AUTH_PATH,
                         {cf_access.PROXY_SECRET_HEADER: SECRET, cf_access.JWT_HEADER: "forged"})
    assert response.status_code == 401
