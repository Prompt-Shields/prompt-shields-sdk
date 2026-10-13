"""Per-tenant rate limit on /ingest/events (#82).

No database: tenant resolution is stubbed from the bearer token and the
requests carry no events, so the handler never touches the session.
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from collector import ingest, partner_rate_limit
from collector.app import app
from collector.partner_rate_limit import FixedWindowRateLimiter, get_ingest_rate_limiter
from httpx import ASGITransport, AsyncClient

TENANTS = {"key-a": uuid.uuid4(), "key-b": uuid.uuid4()}


@pytest.fixture
def limiter(monkeypatch):
    async def fake_resolve_tenant(request):
        return TENANTS[request.headers["Authorization"].removeprefix("Bearer ")]

    monkeypatch.setattr(ingest, "resolve_tenant", fake_resolve_tenant)
    monkeypatch.setattr(ingest.settings, "rate_limit_per_minute", 3)
    # Pin the clock mid-window so a test never straddles a minute boundary.
    monkeypatch.setattr(
        partner_rate_limit, "time", SimpleNamespace(time=lambda: 1_700_000_030.0)
    )
    fresh = FixedWindowRateLimiter()
    app.dependency_overrides[get_ingest_rate_limiter] = lambda: fresh
    yield fresh
    app.dependency_overrides.pop(get_ingest_rate_limiter, None)


async def _post(client, key):
    return await client.post(
        "/ingest/events",
        headers={"Authorization": f"Bearer {key}"},
        json={"events": []},
    )


@pytest.mark.asyncio
async def test_ingest_returns_429_with_retry_after_when_exceeded(limiter):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        ok = [await _post(client, "key-a") for _ in range(3)]
        blocked = await _post(client, "key-a")

    assert [r.status_code for r in ok] == [200, 200, 200]
    assert ok[0].headers["X-RateLimit-Limit"] == "3"
    assert ok[-1].headers["X-RateLimit-Remaining"] == "0"
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == "10"
    assert blocked.json()["detail"]["status"] == 429


@pytest.mark.asyncio
async def test_ingest_limit_is_per_tenant(limiter):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        for _ in range(4):
            await _post(client, "key-a")
        other = await _post(client, "key-b")

    assert other.status_code == 200
    assert other.headers["X-RateLimit-Remaining"] == "2"


@pytest.mark.asyncio
async def test_ingest_limit_holds_under_concurrent_requests(limiter):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        responses = await asyncio.gather(*(_post(client, "key-a") for _ in range(20)))

    codes = [r.status_code for r in responses]
    assert codes.count(200) == 3
    assert codes.count(429) == 17


@pytest.mark.asyncio
async def test_ingest_limit_disabled_when_zero(limiter, monkeypatch):
    monkeypatch.setattr(ingest.settings, "rate_limit_per_minute", 0)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        responses = [await _post(client, "key-a") for _ in range(5)]

    assert all(r.status_code == 200 for r in responses)
    assert "X-RateLimit-Limit" not in responses[0].headers


def test_limiter_memory_bounded_with_many_low_volume_keys(monkeypatch):
    now = [1_700_000_030.0]
    monkeypatch.setattr(partner_rate_limit, "time", SimpleNamespace(time=lambda: now[0]))
    lim = FixedWindowRateLimiter()
    for minute in range(30):
        now[0] = 1_700_000_030.0 + 60 * minute
        for tenant in range(50):
            lim.check(tenant, 100)
    # Only the current and previous minute's buckets survive.
    assert len(lim._counts) <= 100
