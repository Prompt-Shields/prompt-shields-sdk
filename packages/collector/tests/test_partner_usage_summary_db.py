"""Usage-summary against a real PostgreSQL database (PS_TEST_DATABASE_URL).

Seeds two tenants, each with an asset, a partner credential and usage
events, then calls the endpoint through the full app (DB middleware, JWT
auth, rate limit, audit) and checks the aggregates Postgres returns.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from collector.app import app
from collector.partner_auth import hash_secret, issue_access_token
from db.models import AIAsset, AIUsageEvent, PartnerAuditLog, PartnerCredential, Tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _seed(engine):
    """Two tenants. Returns (asset_a, asset_b, token_a, token_b, partner_a)."""
    now = datetime.now(UTC)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        ta = Tenant(name="Tenant A", domain="a.example", settings={})
        tb = Tenant(name="Tenant B", domain="b.example", settings={})
        s.add_all([ta, tb])
        await s.flush()

        asset_a = AIAsset(tenant_id=ta.id, vendor="openai", model="gpt-4o", discovery_source=["sdk"])
        asset_b = AIAsset(tenant_id=tb.id, vendor="openai", model="gpt-4o", discovery_source=["sdk"])
        s.add_all([asset_a, asset_b])
        await s.flush()

        def ev(tenant, asset, age, **kw):
            return AIUsageEvent(
                tenant_id=tenant.id, asset_id=asset.id, timestamp=now - age, source="sdk", **kw
            )

        s.add_all([
            # Tenant A, inside 7d
            ev(ta, asset_a, timedelta(hours=1), tokens_in=100, tokens_out=200,
               cost=Decimal("0.010000"), latency_ms=100, session_id="s1"),
            ev(ta, asset_a, timedelta(days=2), tokens_in=50, tokens_out=25,
               cost=Decimal("0.005000"), latency_ms=300, session_id="s1"),
            # Tenant A, nulls are ignored by SUM/AVG; NULL session not counted
            ev(ta, asset_a, timedelta(days=3)),
            # Tenant A, 7d..30d
            ev(ta, asset_a, timedelta(days=10), tokens_in=1000, tokens_out=1000,
               cost=Decimal("1.000000"), latency_ms=500, session_id="s2"),
            # Tenant A, 30d..90d
            ev(ta, asset_a, timedelta(days=60), tokens_in=7, tokens_out=7, session_id="s3"),
            # Tenant A, older than 90d
            ev(ta, asset_a, timedelta(days=91), tokens_in=99999, tokens_out=99999),
            # Tenant B — must never show up in tenant A's numbers
            ev(tb, asset_b, timedelta(hours=1), tokens_in=5000, tokens_out=5000,
               cost=Decimal("9.000000"), latency_ms=9000, session_id="sb"),
        ])

        pa = PartnerCredential(tenant_id=ta.id, partner_name="pa", client_id="pa-client",
                               client_secret_hash=hash_secret("x"), scopes=["registry:read"])
        pb = PartnerCredential(tenant_id=tb.id, partner_name="pb", client_id="pb-client",
                               client_secret_hash=hash_secret("x"), scopes=["registry:read"])
        s.add_all([pa, pb])
        await s.commit()

        token_a, _ = issue_access_token(ta.id, pa.id, "pa-client", ["registry:read"])
        token_b, _ = issue_access_token(tb.id, pb.id, "pb-client", ["registry:read"])
        return asset_a.id, asset_b.id, token_a, token_b, pa.id


@pytest.mark.asyncio
async def test_usage_summary_totals_and_tenant_isolation_on_postgres(seeded_db):
    asset_a, asset_b, token_a, token_b, partner_a = await _seed(seeded_db)
    ha = {"Authorization": f"Bearer {token_a}"}
    hb = {"Authorization": f"Bearer {token_b}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r7 = await c.get(f"/partner/v1/assets/{asset_a}/usage-summary?period=7d", headers=ha)
        r30 = await c.get(f"/partner/v1/assets/{asset_a}/usage-summary", headers=ha)
        r90 = await c.get(f"/partner/v1/assets/{asset_a}/usage-summary?period=90d", headers=ha)
        cross = await c.get(f"/partner/v1/assets/{asset_b}/usage-summary", headers=ha)
        missing = await c.get(f"/partner/v1/assets/{uuid.uuid4()}/usage-summary", headers=ha)
        rb = await c.get(f"/partner/v1/assets/{asset_b}/usage-summary?period=7d", headers=hb)
        cross_b = await c.get(f"/partner/v1/assets/{asset_a}/usage-summary", headers=hb)

    assert r7.status_code == 200, r7.text
    assert r7.json() == {
        "total_requests": 3,
        "total_tokens_in": 150,
        "total_tokens_out": 225,
        "estimated_cost_usd": 0.015,
        "unique_sessions": 1,
        "avg_latency_ms": 200,
        "period": "7d",
    }
    assert r30.json() == {
        "total_requests": 4,
        "total_tokens_in": 1150,
        "total_tokens_out": 1225,
        "estimated_cost_usd": 1.015,
        "unique_sessions": 2,
        "avg_latency_ms": 300,
        "period": "30d",
    }
    b90 = r90.json()
    assert (b90["total_requests"], b90["total_tokens_in"], b90["unique_sessions"]) == (5, 1157, 3)

    # Another tenant's asset is indistinguishable from a missing one.
    assert cross.status_code == 404 and missing.status_code == 404
    assert cross.json() == missing.json()
    assert cross_b.status_code == 404

    assert rb.json()["total_requests"] == 1
    assert rb.json()["total_tokens_in"] == 5000

    async with async_sessionmaker(seeded_db, class_=AsyncSession)() as s:
        audits = (await s.execute(
            select(PartnerAuditLog.status_code).where(PartnerAuditLog.partner_id == partner_a)
        )).scalars().all()
    assert sorted(audits) == [200, 200, 200, 404, 404]
