"""Retention purge and tenant erasure against a real PostgreSQL database
(PS_TEST_DATABASE_URL), through the operator routes. test_retention.py covers
the logic on SQLite; this checks the batched DELETE ... IN (SELECT ... LIMIT)
statements and foreign-key order on Postgres, and that another tenant's rows
are never touched."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from collector.app import app
from collector.config import settings
from db.models import (
    AIAsset,
    AIUsageEvent,
    DataFlow,
    PartnerAuditLog,
    PartnerCredential,
    RetentionAuditLog,
    RiskMapping,
    Tenant,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TOKEN = "op-secret-token"
OP = {"Authorization": f"Bearer {TOKEN}"}
TABLES = (AIUsageEvent, DataFlow, RiskMapping, PartnerAuditLog, PartnerCredential, AIAsset)


async def _seed_tenant(factory, name: str, **tenant_settings):
    now = datetime.now(UTC)
    async with factory() as s:
        t = Tenant(name=name, settings=tenant_settings)
        s.add(t)
        await s.flush()
        a = AIAsset(tenant_id=t.id, vendor="openai", model="gpt-4o", discovery_source=["sdk"])
        p = PartnerCredential(
            tenant_id=t.id,
            partner_name="ardoq",
            client_id=f"client-{name}",
            client_secret_hash="x",
            scopes=["registry:read"],
        )
        s.add_all([a, p])
        await s.flush()
        s.add_all(
            [
                AIUsageEvent(tenant_id=t.id, asset_id=a.id, source="sdk", timestamp=now - timedelta(days=age))
                for age in (1, 20, 21, 400)
            ]
            + [
                DataFlow(
                    tenant_id=t.id,
                    asset_id=a.id,
                    source_system="crm",
                    destination_system="openai",
                    direction="outbound",
                ),
                RiskMapping(tenant_id=t.id, asset_id=a.id, risk_category="pii", risk_level="high"),
                PartnerAuditLog(
                    tenant_id=t.id,
                    partner_id=p.id,
                    endpoint="/partner/v1/assets",
                    method="GET",
                    status_code=200,
                ),
            ]
        )
        await s.commit()
        return t.id


async def _snapshot(factory, tenant_id) -> dict[str, int]:
    async with factory() as s:
        counts = {
            m.__tablename__: await s.scalar(
                select(func.count()).select_from(m).where(m.tenant_id == tenant_id)
            )
            for m in TABLES
        }
        counts["tenants"] = await s.scalar(
            select(func.count()).select_from(Tenant).where(Tenant.id == tenant_id)
        )
        return counts


async def test_purge_and_erase_never_touch_other_tenants(seeded_db, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", TOKEN)
    monkeypatch.setattr(settings, "retention_batch_size", 1)  # force many batches
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    a = await _seed_tenant(factory, "a", retention_days=10)
    b = await _seed_tenant(factory, "b")  # default 365
    before_b = await _snapshot(factory, b)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/api/v1/operator/retention/purge", headers=OP, json={})
        assert resp.status_code == 200, resp.text
        deleted = {r["tenant_id"]: r["events_deleted"] for r in resp.json()["data"]}
        assert deleted[str(a)] == 3 and deleted[str(b)] == 1

        # Idempotent.
        resp = await c.post("/api/v1/operator/retention/purge", headers=OP, json={})
        assert all(r["events_deleted"] == 0 for r in resp.json()["data"])

        after_purge_b = await _snapshot(factory, b)
        assert after_purge_b["ai_usage_events"] == before_b["ai_usage_events"] - 1

        resp = await c.delete(
            f"/api/v1/operator/tenants/{a}", params={"confirm": str(a)}, headers=OP
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["rows_deleted"]["tenants"] == 1

    assert set((await _snapshot(factory, a)).values()) == {0}
    assert await _snapshot(factory, b) == after_purge_b
    async with factory() as s:
        actions = (
            await s.execute(
                select(RetentionAuditLog.action).where(RetentionAuditLog.tenant_id == a)
            )
        ).scalars().all()
    assert sorted(actions) == ["erase", "purge"]


async def test_operator_routes_404_without_token(seeded_db, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/operator/retention/purge",
            headers={"Authorization": "Bearer "},
            json={},
        )
    assert resp.status_code == 404
