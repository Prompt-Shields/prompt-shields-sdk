"""Retention purge and tenant erasure (#85).

Runs against an in-memory SQLite database (aiosqlite) with foreign keys
enforced, so it needs no Postgres. The Postgres-only column types are
compiled to SQLite equivalents for DDL; ``ai_assets`` and
``partner_credentials`` rows are inserted with raw SQL because SQLite cannot
bind a Python list to an ARRAY column.

The core property under test: nothing here ever deletes another tenant's rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from collector import retention
from collector.config import settings
from db.models import (
    AIAsset,
    AIUsageEvent,
    Base,
    DataFlow,
    McpToolUsage,
    PartnerAuditLog,
    PartnerCredential,
    RetentionAuditLog,
    RiskMapping,
    TelemetryClientStats,
    Tenant,
)
from httpx import ASGITransport, AsyncClient
from pgvector.sqlalchemy import Vector
from sqlalchemy import ARRAY, event, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(Vector, "sqlite")
def _vector_sqlite(type_, compiler, **kw):
    return "BLOB"


NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
TENANT_TABLES = (
    AIUsageEvent,
    DataFlow,
    RiskMapping,
    PartnerAuditLog,
    PartnerCredential,
    AIAsset,
    TelemetryClientStats,
    McpToolUsage,
)


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite://")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def make_tenant(db: AsyncSession, name: str, **tenant_settings) -> uuid.UUID:
    tenant = Tenant(name=name, settings=tenant_settings)
    db.add(tenant)
    await db.commit()
    return tenant.id


async def make_asset(db: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    asset_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO ai_assets (id, tenant_id, vendor, discovery_source, first_seen, last_seen)"
            " VALUES (:id, :tid, 'openai', '[\"sdk\"]', :ts, :ts)"
        ),
        {
            "id": asset_id.hex,
            "tid": tenant_id.hex,
            "ts": NOW.strftime("%Y-%m-%d %H:%M:%S.%f"),
        },
    )
    await db.commit()
    return asset_id


async def add_events(db, tenant_id, asset_id, *ages_days: int) -> None:
    for age in ages_days:
        db.add(
            AIUsageEvent(
                tenant_id=tenant_id,
                asset_id=asset_id,
                source="sdk",
                timestamp=NOW - timedelta(days=age),
            )
        )
    await db.commit()


async def seed_full_tenant(db, name: str, **tenant_settings) -> uuid.UUID:
    """A tenant with a row in every tenant-owned table."""
    tid = await make_tenant(db, name, **tenant_settings)
    asset_id = await make_asset(db, tid)
    await add_events(db, tid, asset_id, 1, 400)
    partner_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO partner_credentials"
            " (id, tenant_id, partner_name, client_id, client_secret_hash, scopes)"
            " VALUES (:id, :tid, 'ardoq', :cid, 'x', '[]')"
        ),
        {"id": partner_id.hex, "tid": tid.hex, "cid": f"ps-partner-{name}"},
    )
    db.add_all(
        [
            DataFlow(
                tenant_id=tid,
                asset_id=asset_id,
                source_system="crm",
                destination_system="openai",
                direction="outbound",
            ),
            RiskMapping(
                tenant_id=tid, asset_id=asset_id, risk_category="pii", risk_level="high"
            ),
            PartnerAuditLog(
                tenant_id=tid,
                partner_id=partner_id,
                endpoint="/partner/v1/assets",
                method="GET",
                status_code=200,
            ),
            TelemetryClientStats(tenant_id=tid, client_id="c1", received_events=1),
            TelemetryClientStats(tenant_id=tid, client_id="c2", received_events=2),
            McpToolUsage(
                tenant_id=tid,
                source="sdk",
                server_name="github",
                tool_name="create_issue",
                call_count=3,
            ),
        ]
    )
    await db.commit()
    return tid


async def add_mcp_usage(db, tenant_id, *ages_days: int) -> None:
    for i, age in enumerate(ages_days):
        db.add(
            McpToolUsage(
                tenant_id=tenant_id,
                source="sdk",
                server_name="github",
                tool_name=f"tool-{i}",
                call_count=1,
                first_seen=NOW - timedelta(days=age + 1),
                last_seen=NOW - timedelta(days=age),
            )
        )
    await db.commit()


async def count(db, model, tenant_id) -> int:
    return (
        await db.execute(
            select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
        )
    ).scalar_one()


async def snapshot(db, tenant_id) -> dict[str, int]:
    counts = {m.__tablename__: await count(db, m, tenant_id) for m in TENANT_TABLES}
    counts["tenants"] = (
        await db.execute(
            select(func.count()).select_from(Tenant).where(Tenant.id == tenant_id)
        )
    ).scalar_one()
    return counts


# ──────────────────────────────────────────────────────────────────────
# Retention period helpers
# ──────────────────────────────────────────────────────────────────────


class TestRetentionCutoff:
    def test_default(self, monkeypatch):
        monkeypatch.setattr(settings, "retention_days_default", 90)
        assert retention.retention_days_for({}) == 90
        assert retention.retention_days_for(None) == 90
        assert retention.retention_cutoff({}, NOW) == NOW - timedelta(days=90)

    def test_tenant_override(self):
        assert retention.retention_days_for({"retention_days": 30}) == 30
        assert retention.retention_days_for({"retention_days": "45"}) == 45
        tenant = Tenant(name="t", settings={"retention_days": 7})
        assert retention.retention_cutoff(tenant, NOW) == NOW - timedelta(days=7)

    @pytest.mark.parametrize("bad", [0, -5, True, 30.5, "abc", None, 10**9, [30]])
    def test_invalid_override_falls_back_to_default(self, bad, monkeypatch):
        monkeypatch.setattr(settings, "retention_days_default", 365)
        assert retention.retention_days_for({"retention_days": bad}) == 365

    def test_invalid_default_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "retention_days_default", 0)
        with pytest.raises(ValueError):
            retention.retention_days_for({})

    def test_naive_now_is_treated_as_utc(self):
        naive = NOW.replace(tzinfo=None)
        assert retention.retention_cutoff(
            {"retention_days": 1}, naive
        ) == NOW - timedelta(days=1)

    async def test_get_retention_cutoff(self, session_factory):
        async with session_factory() as db:
            tid = await make_tenant(db, "a", retention_days=10)
            assert await retention.get_retention_cutoff(
                db, tid, NOW
            ) == NOW - timedelta(days=10)
            assert await retention.get_retention_cutoff(db, uuid.uuid4(), NOW) is None


def test_erase_order_covers_every_tenant_table():
    """A new table with tenant_id must be added to ERASE_ORDER (or exempted)."""
    covered = {m.__tablename__ for m in retention.ERASE_ORDER} | retention.ERASE_EXEMPT
    with_tenant = {t.name for t in Base.metadata.sorted_tables if "tenant_id" in t.c}
    assert with_tenant - covered == set()


# ──────────────────────────────────────────────────────────────────────
# Purge
# ──────────────────────────────────────────────────────────────────────


class TestPurge:
    async def test_purges_per_tenant_cutoff_only(self, session_factory, monkeypatch):
        monkeypatch.setattr(settings, "retention_days_default", 365)
        async with session_factory() as db:
            a = await make_tenant(db, "a", retention_days=30)
            b = await make_tenant(db, "b")
            asset_a = await make_asset(db, a)
            asset_b = await make_asset(db, b)
            await add_events(db, a, asset_a, 1, 29, 31, 100, 400)
            await add_events(db, b, asset_b, 1, 31, 100, 364, 366, 400)

            results = await retention.purge_expired_events(db, now=NOW, batch_size=2)

            by_tenant = {r.tenant_id: r for r in results}
            assert by_tenant[str(a)].events_deleted == 3
            assert by_tenant[str(a)].retention_days == 30
            assert by_tenant[str(b)].events_deleted == 2
            assert await count(db, AIUsageEvent, a) == 2
            assert await count(db, AIUsageEvent, b) == 4
            # Assets are kept even when all their events are purged.
            assert await count(db, AIAsset, a) == 1

    async def test_idempotent_and_audited(self, session_factory):
        async with session_factory() as db:
            a = await make_tenant(db, "a", retention_days=30)
            asset = await make_asset(db, a)
            await add_events(db, a, asset, 40, 50)

            first = await retention.purge_tenant_events(db, a, now=NOW, actor="test")
            second = await retention.purge_tenant_events(db, a, now=NOW, actor="test")
            assert (first.events_deleted, second.events_deleted) == (2, 0)

            audits = (await db.execute(select(RetentionAuditLog))).scalars().all()
            assert len(audits) == 1  # a no-op run writes no audit row
            assert audits[0].tenant_id == a
            assert audits[0].action == "purge"
            assert audits[0].actor == "test"
            assert audits[0].details == {"ai_usage_events": 2}

    async def test_purges_mcp_usage_not_seen_within_retention(self, session_factory):
        async with session_factory() as db:
            a = await make_tenant(db, "a", retention_days=30)
            b = await make_tenant(db, "b", retention_days=30)
            await add_mcp_usage(db, a, 1, 29, 31, 400)
            await add_mcp_usage(db, b, 31, 400)

            dry = await retention.purge_tenant_events(db, a, now=NOW, dry_run=True)
            assert dry.mcp_tool_usage_deleted == 2
            assert await count(db, McpToolUsage, a) == 4

            result = await retention.purge_tenant_events(
                db, a, now=NOW, batch_size=1, actor="test"
            )
            assert (result.events_deleted, result.mcp_tool_usage_deleted) == (0, 2)
            assert await count(db, McpToolUsage, a) == 2
            assert await count(db, McpToolUsage, b) == 2  # other tenant untouched
            audit = (await db.execute(select(RetentionAuditLog))).scalar_one()
            assert audit.details == {"ai_usage_events": 0, "mcp_tool_usage": 2}

    async def test_single_tenant_purge_leaves_other_tenant_alone(self, session_factory):
        async with session_factory() as db:
            a = await make_tenant(db, "a", retention_days=1)
            b = await make_tenant(db, "b", retention_days=1)
            asset_a = await make_asset(db, a)
            asset_b = await make_asset(db, b)
            await add_events(db, a, asset_a, 10, 20)
            await add_events(db, b, asset_b, 10, 20, 30)

            await retention.purge_tenant_events(db, a, now=NOW)
            assert await count(db, AIUsageEvent, a) == 0
            assert await count(db, AIUsageEvent, b) == 3

    async def test_batches_until_done(self, session_factory):
        async with session_factory() as db:
            a = await make_tenant(db, "a", retention_days=1)
            asset = await make_asset(db, a)
            await add_events(db, a, asset, *range(2, 13))  # 11 old events

            result = await retention.purge_tenant_events(db, a, now=NOW, batch_size=3)
            assert result.events_deleted == 11
            assert await count(db, AIUsageEvent, a) == 0

    async def test_dry_run_deletes_nothing(self, session_factory):
        async with session_factory() as db:
            a = await make_tenant(db, "a", retention_days=1)
            asset = await make_asset(db, a)
            await add_events(db, a, asset, 5, 6)

            result = await retention.purge_tenant_events(db, a, now=NOW, dry_run=True)
            assert result.events_deleted == 2 and result.dry_run
            assert await count(db, AIUsageEvent, a) == 2
            assert (await db.execute(select(RetentionAuditLog))).first() is None

    async def test_unknown_tenant(self, session_factory):
        async with session_factory() as db:
            assert (
                await retention.purge_tenant_events(db, uuid.uuid4(), now=NOW) is None
            )

    async def test_rejects_bad_batch_size(self, session_factory):
        async with session_factory() as db:
            a = await make_tenant(db, "a")
            with pytest.raises(ValueError):
                await retention.purge_tenant_events(db, a, now=NOW, batch_size=0)


# ──────────────────────────────────────────────────────────────────────
# Erasure
# ──────────────────────────────────────────────────────────────────────


class TestErase:
    async def test_erases_only_that_tenant(self, session_factory):
        async with session_factory() as db:
            a = await seed_full_tenant(db, "a")
            b = await seed_full_tenant(db, "b")
            before_b = await snapshot(db, b)
            assert all(before_b.values())

            result = await retention.erase_tenant(db, a, batch_size=1, actor="test")

            assert result.tenant_existed is True
            assert result.rows_deleted["ai_usage_events"] == 2
            assert result.rows_deleted["mcp_tool_usage"] == 1
            assert result.rows_deleted["tenants"] == 1
            assert set((await snapshot(db, a)).values()) == {0}
            assert await snapshot(db, b) == before_b

    async def test_idempotent_and_audit_survives(self, session_factory):
        async with session_factory() as db:
            a = await seed_full_tenant(db, "a")
            await retention.erase_tenant(db, a, actor="test")
            again = await retention.erase_tenant(db, a, actor="test")

            assert again.tenant_existed is False
            assert set(again.rows_deleted.values()) == {0}
            audits = (
                (
                    await db.execute(
                        select(RetentionAuditLog).where(
                            RetentionAuditLog.tenant_id == a
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert [x.action for x in audits] == ["erase", "erase"]
            assert audits[0].details["ai_assets"] == 1

    async def test_set_retention(self, session_factory, monkeypatch):
        monkeypatch.setattr(settings, "retention_days_default", 365)
        async with session_factory() as db:
            a = await make_tenant(db, "a", api_key="keep-me")
            assert await retention.set_tenant_retention(db, a, 30) == 30
            tenant = await db.get(Tenant, a)
            await db.refresh(tenant)
            assert tenant.settings == {"api_key": "keep-me", "retention_days": 30}
            assert await retention.set_tenant_retention(db, a, None) == 365
            assert await retention.set_tenant_retention(db, uuid.uuid4(), 30) is None
            with pytest.raises(ValueError):
                await retention.set_tenant_retention(db, a, 0)


# ──────────────────────────────────────────────────────────────────────
# Operator endpoints
# ──────────────────────────────────────────────────────────────────────

TOKEN = "op-secret-token"
OP = {"Authorization": f"Bearer {TOKEN}"}


@pytest_asyncio.fixture
async def client(session_factory, monkeypatch):
    from collector import app as app_module

    monkeypatch.setattr(app_module, "SessionLocal", session_factory)
    monkeypatch.setattr(settings, "admin_token", TOKEN)
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestOperatorEndpoints:
    async def test_disabled_without_token(self, client, monkeypatch):
        monkeypatch.setattr(settings, "admin_token", "")
        resp = await client.post(
            "/api/v1/operator/retention/purge", headers=OP, json={}
        )
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "headers",
        [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}],
    )
    async def test_rejects_bad_token(self, client, headers):
        resp = await client.post(
            "/api/v1/operator/retention/purge", headers=headers, json={}
        )
        assert resp.status_code == 401

    async def test_tenant_api_key_is_not_operator(self, client, session_factory):
        async with session_factory() as db:
            a = await make_tenant(db, "a", api_key="ps-tenant-key")
        resp = await client.delete(
            f"/api/v1/operator/tenants/{a}",
            params={"confirm": str(a)},
            headers={"Authorization": "Bearer ps-tenant-key"},
        )
        assert resp.status_code == 401

    async def test_erase_requires_confirm(self, client, session_factory):
        async with session_factory() as db:
            a = await seed_full_tenant(db, "a")
        resp = await client.delete(f"/api/v1/operator/tenants/{a}", headers=OP)
        assert resp.status_code == 422
        resp = await client.delete(
            f"/api/v1/operator/tenants/{a}",
            params={"confirm": str(uuid.uuid4())},
            headers=OP,
        )
        assert resp.status_code == 400
        async with session_factory() as db:
            assert (await snapshot(db, a))["tenants"] == 1

    async def test_erase(self, client, session_factory):
        async with session_factory() as db:
            a = await seed_full_tenant(db, "a")
            b = await seed_full_tenant(db, "b")
            before_b = await snapshot(db, b)
        resp = await client.delete(
            f"/api/v1/operator/tenants/{a}", params={"confirm": str(a)}, headers=OP
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["tenant_existed"] is True
        assert body["rows_deleted"]["tenants"] == 1
        async with session_factory() as db:
            assert set((await snapshot(db, a)).values()) == {0}
            assert await snapshot(db, b) == before_b

    async def test_retention_and_purge(self, client, session_factory):
        async with session_factory() as db:
            a = await make_tenant(db, "a")
            b = await make_tenant(db, "b")
            asset_a = await make_asset(db, a)
            asset_b = await make_asset(db, b)
            now = datetime.now(UTC)
            for tid, asset in ((a, asset_a), (b, asset_b)):
                db.add(
                    AIUsageEvent(
                        tenant_id=tid,
                        asset_id=asset,
                        source="sdk",
                        timestamp=now - timedelta(days=20),
                    )
                )
            await db.commit()

        resp = await client.put(
            f"/api/v1/operator/tenants/{a}/retention",
            headers=OP,
            json={"retention_days": 0},
        )
        assert resp.status_code == 422
        resp = await client.put(
            f"/api/v1/operator/tenants/{a}/retention",
            headers=OP,
            json={"retention_days": 10},
        )
        assert resp.json() == {"tenant_id": str(a), "retention_days": 10}
        resp = await client.get(f"/api/v1/operator/tenants/{a}/retention", headers=OP)
        assert resp.json()["retention_days"] == 10

        resp = await client.post(
            "/api/v1/operator/retention/purge",
            headers=OP,
            json={"tenant_id": str(a), "dry_run": True},
        )
        assert resp.json()["data"][0]["events_deleted"] == 1

        resp = await client.post(
            "/api/v1/operator/retention/purge", headers=OP, json={}
        )
        assert resp.status_code == 200
        deleted = {r["tenant_id"]: r["events_deleted"] for r in resp.json()["data"]}
        assert deleted == {str(a): 1, str(b): 0}

        resp = await client.post(
            "/api/v1/operator/retention/purge",
            headers=OP,
            json={"tenant_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 404
