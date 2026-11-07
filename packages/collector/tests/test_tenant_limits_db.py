"""Per-tenant ingest rate limits (#345), against PostgreSQL with row-level
security enforced: an operator override in tenants.settings changes that
tenant's ingest limit only."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from collector import audit_trail, partner_rate_limit, rls, tenant_limits
from collector.app import app
from collector.config import settings
from collector.partner_rate_limit import (
    FixedWindowRateLimiter,
    get_feedback_rate_limiter,
    get_ingest_rate_limiter,
    get_kong_rate_limiter,
    get_mcp_rate_limiter,
    get_otel_traces_rate_limiter,
)
from db.api_keys import api_key_lookup_prefix, hash_api_key
from db.models import AIUsageEvent, Tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

KEY_A = "ps-test-key-12345"  # the seeded_db tenant
KEY_B = "ps-test-key-limits-b"
OP_TOKEN = "op-secret-token"
OP = {"Authorization": f"Bearer {OP_TOKEN}"}
GETTERS = (
    get_ingest_rate_limiter,
    get_kong_rate_limiter,
    get_mcp_rate_limiter,
    get_feedback_rate_limiter,
    get_otel_traces_rate_limiter,
)


def _returning(limiter):
    # A closure, not a default argument: FastAPI would read that as a
    # query parameter.
    return lambda: limiter


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", OP_TOKEN)
    monkeypatch.setattr(settings, "admin_require_scoped_tokens", False)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 3)
    monkeypatch.setattr(settings, "mcp_rate_limit_per_minute", 3)
    monkeypatch.setattr(settings, "otel_traces_rate_limit_per_minute", 2)
    monkeypatch.setattr(
        partner_rate_limit, "time", SimpleNamespace(time=lambda: 1_800_000_030.0)
    )
    audit_trail.auth_failure_limiter.reset()
    tenant_limits.invalidate()
    for getter in GETTERS:
        app.dependency_overrides[getter] = _returning(FixedWindowRateLimiter())
    yield
    for getter in GETTERS:
        app.dependency_overrides.pop(getter, None)
    tenant_limits.invalidate()


@pytest.fixture
async def tenants(seeded_db):
    admin = async_sessionmaker(
        seeded_db,
        class_=AsyncSession,
        expire_on_commit=False,
        info=rls.bypass_info("test-seed"),
    )
    async with admin() as s:
        a = (await s.execute(select(Tenant.id))).scalar_one()
        b = Tenant(
            name="Other Corp",
            settings={},
            api_key_prefix=api_key_lookup_prefix(KEY_B),
            api_key_hash=hash_api_key(KEY_B),
        )
        s.add(b)
        await s.commit()
    async with seeded_db.begin() as conn:
        await rls.enable(conn)
    return {"a": a, "b": b.id, "admin": admin}


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _ingest(c, key, path="/ingest/events"):
    return await c.post(
        path, headers={"Authorization": f"Bearer {key}"}, json={"events": []}
    )


async def _set(c, tenant_id, value):
    return await c.patch(
        f"/api/v1/operator/tenants/{tenant_id}",
        headers=OP,
        json={"settings": {tenant_limits.SETTINGS_KEY: value}},
    )


async def test_override_changes_only_that_tenants_ingest_limit(tenants):
    async with _client() as c:
        resp = await _set(c, tenants["a"], {"events": 5})
        assert resp.status_code == 200, resp.text
        assert resp.json()["settings"][tenant_limits.SETTINGS_KEY] == {"events": 5}

        a = [await _ingest(c, KEY_A) for _ in range(6)]
        b = [await _ingest(c, KEY_B) for _ in range(4)]
        # The override is per endpoint: /ingest/mcp keeps the env default.
        mcp = [await _ingest(c, KEY_A, "/ingest/mcp") for _ in range(4)]

    assert [r.status_code for r in a] == [200] * 5 + [429]
    assert a[0].headers["X-RateLimit-Limit"] == "5"
    assert [r.status_code for r in b] == [200] * 3 + [429]
    assert b[0].headers["X-RateLimit-Limit"] == "3"
    assert [r.status_code for r in mcp] == [200] * 3 + [429]
    assert mcp[0].headers["X-RateLimit-Limit"] == "3"


async def test_override_can_lower_and_remove_a_limit(tenants):
    async with _client() as c:
        assert (await _set(c, tenants["b"], {"events": 1})).status_code == 200
        first, second = await _ingest(c, KEY_B), await _ingest(c, KEY_B)
        assert first.status_code == 200 and second.status_code == 429
        assert second.headers["X-RateLimit-Limit"] == "1"

        # null removes the override; the env default (3) applies again.
        resp = await _set(c, tenants["b"], None)
        assert tenant_limits.SETTINGS_KEY not in resp.json()["settings"]
        third = await _ingest(c, KEY_B)
        assert third.status_code == 200
        assert third.headers["X-RateLimit-Limit"] == "3"


async def test_zero_turns_the_limit_off_for_the_tenant(tenants):
    async with _client() as c:
        assert (await _set(c, tenants["a"], {"events": 0})).status_code == 200
        a = [await _ingest(c, KEY_A) for _ in range(5)]
        b = [await _ingest(c, KEY_B) for _ in range(4)]
    assert all(r.status_code == 200 for r in a)
    assert "X-RateLimit-Limit" not in a[0].headers
    assert b[-1].status_code == 429


@pytest.mark.parametrize(
    "value",
    [
        "fast",
        {"events": -1},
        {"events": "10"},
        {"events": True},
        {"events": tenant_limits.MAX_LIMIT + 1},
        {"partner": 10},
    ],
)
async def test_invalid_overrides_are_refused(tenants, value):
    async with _client() as c:
        resp = await _set(c, tenants["a"], value)
        assert resp.status_code == 400, resp.text
        got = await c.get(f"/api/v1/operator/tenants/{tenants['a']}", headers=OP)
    assert tenant_limits.SETTINGS_KEY not in got.json()["settings"]


async def test_overrides_are_cached_and_dropped_on_change(tenants, monkeypatch):
    monkeypatch.setattr(settings, "tenant_limits_cache_ttl_seconds", 3600)
    calls = []
    real = tenant_limits.overrides_from

    def counting(value):
        calls.append(value)
        return real(value)

    monkeypatch.setattr(tenant_limits, "overrides_from", counting)
    async with _client() as c:
        await _ingest(c, KEY_A)
        await _ingest(c, KEY_A)
        assert len(calls) == 1
        await _set(c, tenants["a"], {"events": 10})
        resp = await _ingest(c, KEY_A)
    assert len(calls) == 2
    assert resp.headers["X-RateLimit-Limit"] == "10"


def _otlp_span(model: str) -> dict:
    attrs = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "openai",
        "gen_ai.request.model": model,
        "gen_ai.usage.input_tokens": 10,
        "gen_ai.usage.output_tokens": 20,
    }
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "otlp-limited"}}
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "x"},
                        "spans": [
                            {
                                "traceId": "5b8efff798038103d269b633813fc60c",
                                "spanId": "eee19b7ec3c1b174",
                                "name": "chat",
                                "kind": 3,
                                "startTimeUnixNano": "1700000000000000000",
                                "endTimeUnixNano": "1700000000450000000",
                                "attributes": [
                                    {"key": k, "value": {"intValue": str(v)}}
                                    if isinstance(v, int)
                                    else {"key": k, "value": {"stringValue": v}}
                                    for k, v in attrs.items()
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }


async def _traces(c, key, model="gpt-4o"):
    return await c.post(
        "/ingest/otel/v1/traces",
        headers={"Authorization": f"Bearer {key}"},
        json=_otlp_span(model),
    )


async def _usage_rows(tenants) -> dict:
    async with tenants["admin"]() as s:
        rows = await s.execute(
            select(AIUsageEvent.tenant_id, func.count())
            .where(AIUsageEvent.source == "otel")
            .group_by(AIUsageEvent.tenant_id)
        )
        return dict(rows.all())


async def test_otel_traces_have_their_own_tenant_limit(tenants, monkeypatch):
    """/ingest/otel/v1/traces is limited per tenant by
    PS_OTEL_TRACES_RATE_LIMIT_PER_MINUTE (429 with Retry-After past it; a
    blocked request stores nothing), apart from /ingest/events; the
    tenant override ingest_rate_limits.otel_traces applies; 0 turns it off."""
    async with _client() as c:
        assert (await _set(c, tenants["b"], {"otel_traces": 1})).status_code == 200
        a = [await _traces(c, KEY_A) for _ in range(3)]
        b = [await _traces(c, KEY_B) for _ in range(2)]
        events = await _ingest(c, KEY_A)

        assert [r.status_code for r in a] == [200, 200, 429], a[-1].text
        assert a[0].headers["X-RateLimit-Limit"] == "2"
        assert int(a[-1].headers["Retry-After"]) > 0
        assert "Rate limit exceeded" in a[-1].text
        assert [r.status_code for r in b] == [200, 429]
        assert b[0].headers["X-RateLimit-Limit"] == "1"
        # /ingest/events counts in its own bucket.
        assert events.status_code == 200
        assert events.headers["X-RateLimit-Limit"] == "3"
        assert await _usage_rows(tenants) == {tenants["a"]: 2, tenants["b"]: 1}

        monkeypatch.setattr(settings, "otel_traces_rate_limit_per_minute", 0)
        more = [await _traces(c, KEY_A) for _ in range(3)]
    assert [r.status_code for r in more] == [200] * 3
    assert "X-RateLimit-Limit" not in more[0].headers
