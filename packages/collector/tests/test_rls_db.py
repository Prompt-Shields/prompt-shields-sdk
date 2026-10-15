"""Postgres row-level security (#83) against a real database
(PS_TEST_DATABASE_URL).

These go around the application's tenant_id predicates on purpose: raw SQL
with no WHERE clause must still only reach the current tenant's rows, and
nothing at all without a tenant context. The tables are created by the test
user, which therefore owns them, so this also checks that FORCE ROW LEVEL
SECURITY binds the owner.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from collector import rls
from collector.app import app
from collector.config import settings
from collector.partner_auth import hash_secret
from db import rls as rls_sql
from db.api_keys import api_key_lookup_prefix, hash_api_key
from db.models import (
    AIAsset,
    AIUsageEvent,
    AssetAttestation,
    Base,
    DataFlow,
    PartnerAuditLog,
    PartnerCredential,
    RetentionAuditLog,
    RiskMapping,
    TelemetryClientStats,
    Tenant,
    WebhookDelivery,
    WebhookSubscription,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

KEY_A = "ps-test-key-12345"  # the seeded_db tenant
KEY_B = "ps-test-key-tenant-b"
PARTNER_KEY_A = "ps-pk-ardoq-tenant-a-key"
PARTNER_KEY_B = "ps-pk-ardoq-tenant-b-key"
TOKEN = "op-secret-token"


def test_every_tenant_table_has_a_policy():
    expected = {Tenant.__tablename__} | {
        t.name for t in Base.metadata.tables.values() if "tenant_id" in t.c
    }
    assert set(rls_sql.TENANT_TABLES) == expected


async def _seed(factory, tenant_id, name: str, partner_key: str) -> None:
    now = datetime.now(UTC)
    async with factory() as s:
        a = AIAsset(
            tenant_id=tenant_id, vendor=f"vendor-{name}", model="m", discovery_source=["sdk"]
        )
        p = PartnerCredential(
            tenant_id=tenant_id,
            partner_name="ardoq",
            client_id=f"client-{name}",
            client_secret_hash=hash_secret("secret"),
            api_key_hash=hash_secret(partner_key),
            scopes=["registry:read"],
            is_active="true",
        )
        s.add_all([a, p])
        await s.flush()
        s.add_all(
            [
                AIUsageEvent(
                    tenant_id=tenant_id,
                    asset_id=a.id,
                    source="sdk",
                    timestamp=now - timedelta(days=400),
                ),
                AIUsageEvent(tenant_id=tenant_id, asset_id=a.id, source="sdk"),
                DataFlow(
                    tenant_id=tenant_id,
                    asset_id=a.id,
                    source_system="crm",
                    destination_system="openai",
                    direction="outbound",
                ),
                RiskMapping(
                    tenant_id=tenant_id, asset_id=a.id, risk_category="pii", risk_level="high"
                ),
                PartnerAuditLog(
                    tenant_id=tenant_id, endpoint="/x", method="GET", status_code=200
                ),
                RetentionAuditLog(tenant_id=tenant_id, action="purge", actor="test"),
                TelemetryClientStats(tenant_id=tenant_id, client_id=f"client-{name}"),
                AssetAttestation(
                    tenant_id=tenant_id,
                    asset_id=a.id,
                    field="owner",
                    state="attested",
                    attested_by=f"owner@{name}.example",
                    actor_type="tenant",
                ),
            ]
        )
        sub = WebhookSubscription(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            partner_id=p.id,
            url="https://hooks.example/seed",
            event_types=["asset.created"],
            secret_ciphertext="unused",
            is_active=False,
        )
        s.add(sub)
        await s.flush()
        s.add(
            WebhookDelivery(
                tenant_id=tenant_id,
                subscription_id=sub.id,
                event_id=uuid.uuid4(),
                event_type="asset.created",
                payload={},
                status="succeeded",
            )
        )
        await s.commit()


@pytest.fixture
async def rls_db(seeded_db):
    """Two tenants with a row in every tenant table, RLS enforced."""
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
        b = b.id
    await _seed(admin, a, "a", PARTNER_KEY_A)
    await _seed(admin, b, "b", PARTNER_KEY_B)
    async with seeded_db.begin() as conn:
        await rls.enable(conn)
    plain = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    yield {"engine": seeded_db, "plain": plain, "admin": admin, "a": a, "b": b}


async def _tenant_ids(session, table: str) -> list:
    col = rls_sql.TENANT_TABLES[table]
    return list((await session.execute(text(f"SELECT {col} FROM {table}"))).scalars())


async def test_status_reports_enforced(rls_db):
    async with rls_db["engine"].connect() as conn:
        st = await rls.status(conn)
    assert st.tables_enforced
    assert not st.missing_tables
    assert {t.table for t in st.tables} == set(rls_sql.TENANT_TABLES)


async def test_query_without_tenant_context_returns_nothing(rls_db):
    async with rls_db["plain"]() as s:
        for table in rls_sql.TENANT_TABLES:
            assert await _tenant_ids(s, table) == [], table


async def test_unfiltered_query_returns_only_own_tenant(rls_db):
    for me in ("a", "b"):
        async with rls_db["plain"]() as s:
            await rls.set_tenant(s, rls_db[me])
            for table in rls_sql.TENANT_TABLES:
                ids = await _tenant_ids(s, table)
                assert ids, table
                assert set(ids) == {rls_db[me]}, table


async def test_cross_tenant_writes_fail_at_the_database(rls_db):
    a, b = rls_db["a"], rls_db["b"]
    async with rls_db["plain"]() as s:
        await rls.set_tenant(s, a)
        s.add(AIAsset(tenant_id=b, vendor="evil", discovery_source=["sdk"]))
        with pytest.raises(DBAPIError, match="row-level security"):
            await s.flush()

    async with rls_db["plain"]() as s:
        await rls.set_tenant(s, a)
        # Moving an own row to another tenant is refused (WITH CHECK).
        with pytest.raises(DBAPIError, match="row-level security"):
            await s.execute(text("UPDATE ai_assets SET tenant_id = :b"), {"b": b})

    async with rls_db["plain"]() as s:
        await rls.set_tenant(s, a)
        hit = await s.execute(
            text("UPDATE ai_assets SET vendor = 'x' WHERE tenant_id = :b"), {"b": b}
        )
        assert hit.rowcount == 0
        hit = await s.execute(
            text("DELETE FROM ai_usage_events WHERE tenant_id = :b"), {"b": b}
        )
        assert hit.rowcount == 0
        await s.commit()

    async with rls_db["admin"]() as s:
        vendors = (
            await s.execute(select(AIAsset.vendor).where(AIAsset.tenant_id == b))
        ).scalars().all()
        events = (
            await s.execute(select(AIUsageEvent.id).where(AIUsageEvent.tenant_id == b))
        ).scalars().all()
    assert vendors == ["vendor-b"]
    assert len(events) == 2


async def test_context_survives_commit_and_never_leaks_through_the_pool(rls_db):
    url = rls_db["engine"].url.render_as_string(hide_password=False)
    engine = create_async_engine(url, pool_size=1, max_overflow=0)
    try:
        factory = async_sessionmaker(engine, class_=AsyncSession)
        async with factory() as s:
            await rls.set_tenant(s, rls_db["a"])
            assert set(await _tenant_ids(s, "ai_assets")) == {rls_db["a"]}
            await s.commit()  # ends the transaction that held the setting
            assert set(await _tenant_ids(s, "ai_assets")) == {rls_db["a"]}
            await s.rollback()
            assert set(await _tenant_ids(s, "ai_assets")) == {rls_db["a"]}
        # Same pooled connection, no context.
        async with factory() as s:
            assert await _tenant_ids(s, "ai_assets") == []
            setting = (
                await s.execute(
                    text(f"SELECT current_setting('{rls_sql.TENANT_SETTING}', true)")
                )
            ).scalar()
            assert not setting
    finally:
        await engine.dispose()


async def test_auth_lookup_bypass_ends_with_the_block(rls_db):
    async with rls_db["plain"]() as s:
        async with rls.auth_lookup(s):
            seen = set(await _tenant_ids(s, "tenants"))
            creds = (await s.execute(select(PartnerCredential))).scalars().all()
        assert seen == {rls_db["a"], rls_db["b"]}
        # Rows loaded under the bypass are not left in the identity map.
        assert all(c not in s for c in creds)
        assert await _tenant_ids(s, "tenants") == []

        await rls.set_tenant(s, rls_db["a"])
        async with rls.auth_lookup(s):
            pass
        # The previous context is restored.
        assert set(await _tenant_ids(s, "tenants")) == {rls_db["a"]}


async def test_bypass_requires_a_reason(rls_db):
    async with rls_db["plain"]() as s:
        with pytest.raises(ValueError):
            await rls.bypass(s, "")


async def test_tenant_and_partner_routes_work_under_rls(rls_db):
    a, b = rls_db["a"], rls_db["b"]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.post(
            "/ingest/events",
            headers={"Authorization": f"Bearer {KEY_A}"},
            json={"events": [{"vendor": "anthropic", "model": "claude"}]},
        )
        assert resp.status_code == 200, resp.text

        for key, expected in ((KEY_A, {"vendor-a", "anthropic"}), (KEY_B, {"vendor-b"})):
            resp = await c.get(
                "/api/v1/registry/assets", headers={"Authorization": f"Bearer {key}"}
            )
            assert resp.status_code == 200, resp.text
            assert {x["vendor"] for x in resp.json()["data"]} == expected

        resp = await c.get(
            "/partner/v1/assets", headers={"Authorization": f"Bearer {PARTNER_KEY_B}"}
        )
        assert resp.status_code == 200, resp.text
        assert {x["vendor"] for x in resp.json()["data"]} == {"vendor-b"}

        resp = await c.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "client-a",
                "client_secret": "secret",
            },
        )
        assert resp.status_code == 200, resp.text
        jwt = resp.json()["access_token"]
        resp = await c.get("/partner/v1/assets", headers={"Authorization": f"Bearer {jwt}"})
        assert resp.status_code == 200, resp.text
        assert {x["vendor"] for x in resp.json()["data"]} == {"vendor-a", "anthropic"}

        resp = await c.get(
            "/api/v1/registry/assets", headers={"Authorization": "Bearer wrong-key"}
        )
        assert resp.status_code == 401

    async with rls_db["admin"]() as s:
        audit_tenants = (await s.execute(select(PartnerAuditLog.tenant_id))).scalars().all()
        stats = (
            await s.execute(
                select(TelemetryClientStats.received_events).where(
                    TelemetryClientStats.tenant_id == a,
                    TelemetryClientStats.client_id == "unidentified",
                )
            )
        ).scalar_one()
    assert audit_tenants.count(a) > 1 and audit_tenants.count(b) > 1
    assert stats == 1


async def test_operator_routes_bypass_with_an_audit_log(rls_db, monkeypatch, caplog):
    monkeypatch.setattr(settings, "admin_token", TOKEN)
    a, b = rls_db["a"], rls_db["b"]
    op = {"Authorization": f"Bearer {TOKEN}"}
    caplog.set_level(logging.INFO, logger="collector.rls")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/api/v1/operator/retention/purge", headers=op, json={})
        assert resp.status_code == 200, resp.text
        deleted = {r["tenant_id"]: r["events_deleted"] for r in resp.json()["data"]}
        assert deleted == {str(a): 1, str(b): 1}

        resp = await c.delete(
            f"/api/v1/operator/tenants/{b}", params={"confirm": str(b)}, headers=op
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["rows_deleted"]["tenants"] == 1
        assert resp.json()["rows_deleted"]["asset_attestations"] == 1
        assert resp.json()["rows_deleted"]["webhook_deliveries"] == 1
        assert resp.json()["rows_deleted"]["webhook_subscriptions"] == 1

    assert any(
        "rls bypass granted reason=operator:POST /api/v1/operator/retention/purge"
        in r.getMessage()
        for r in caplog.records
    )
    async with rls_db["admin"]() as s:
        tenants = set((await s.execute(select(Tenant.id))).scalars())
        attested = set((await s.execute(select(AssetAttestation.tenant_id))).scalars())
    assert tenants == {a}
    assert attested == {a}


async def test_attestation_delete_stays_blocked_under_a_bypass(rls_db):
    """An RLS bypass is not an erasure: the append-only trigger still
    refuses a DELETE that doesn't name the tenant being erased."""
    async with rls_db["admin"]() as s:
        with pytest.raises(DBAPIError, match="append-only"):
            await s.execute(text("DELETE FROM asset_attestations"))


async def test_disable_turns_enforcement_off(rls_db):
    async with rls_db["engine"].begin() as conn:
        await rls.disable(conn)
    async with rls_db["engine"].connect() as conn:
        assert not (await rls.status(conn)).tables_enforced
    async with rls_db["plain"]() as s:
        assert set(await _tenant_ids(s, "ai_assets")) == {rls_db["a"], rls_db["b"]}


async def test_rls_required_refuses_an_unenforced_database(rls_db):
    async with rls_db["engine"].connect() as conn:
        await rls.check_required(conn)  # enforced: no error
    async with rls_db["engine"].begin() as conn:
        await rls.disable(conn)
    async with rls_db["engine"].connect() as conn:
        with pytest.raises(RuntimeError, match="PS_RLS_REQUIRED"):
            await rls.check_required(conn)


async def test_cloud_inventory_and_partner_admin_routes_work_under_rls(rls_db, monkeypatch):
    """The cloud inventory operator pull is scoped to its target tenant (no
    bypass); the partner credential PATCH (scopes, rate-limit tier) and the
    per-route scope check, whose 403 writes an audit row, run as the tenant."""
    a, b = rls_db["a"], rls_db["b"]
    monkeypatch.setenv("PS_ADMIN_TOKEN", TOKEN)
    monkeypatch.delenv("PS_CLOUD_INVENTORY", raising=False)
    async with rls_db["admin"]() as s:
        asset_b = (
            await s.execute(select(AIAsset.id).where(AIAsset.tenant_id == b))
        ).scalar_one()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/admin/cloud-inventory/pull",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"tenant_id": str(b)},
        )
        assert resp.status_code == 200, resp.text
        clouds = resp.json()["clouds"]
        assert clouds and all(v == {"status": "not_configured"} for v in clouds.values())

        monkeypatch.delenv("PS_LEANIX", raising=False)
        resp = await c.post(
            "/api/v1/admin/connectors/leanix/sync",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"tenant_id": str(b)},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "not_configured"
        resp = await c.get(
            "/api/v1/admin/connectors/leanix/status",
            headers={"Authorization": f"Bearer {TOKEN}"},
            params={"tenant_id": str(b)},
        )
        assert resp.status_code == 200, resp.text

        key_b = {"Authorization": f"Bearer {KEY_B}"}
        creds = (await c.get("/api/v1/admin/partner-credentials", headers=key_b)).json()
        assert [x["client_id"] for x in creds["data"]] == ["client-b"]
        url = f"/api/v1/admin/partner-credentials/{creds['data'][0]['id']}"
        resp = await c.patch(
            url, headers=key_b, json={"scopes": ["assets:read"], "rate_limit_tier": "basic"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["rate_limit_tier"] == "basic"

        resp = await c.patch(
            url, headers={"Authorization": f"Bearer {KEY_A}"}, json={"scopes": ["risks:read"]}
        )
        assert resp.status_code == 404

        partner_b = {"Authorization": f"Bearer {PARTNER_KEY_B}"}
        resp = await c.get(f"/partner/v1/assets/{asset_b}/risks", headers=partner_b)
        assert resp.status_code == 403

        attest = f"/api/v1/registry/assets/{asset_b}/attestations"
        body = {"field": "asset", "state": "attested", "attested_by": "x@b.example"}
        resp = await c.post(attest, headers=key_b, json=body)
        assert resp.status_code == 201, resp.text
        resp = await c.get(attest, headers=key_b)
        assert resp.status_code == 200, resp.text
        resp = await c.post(
            attest, headers={"Authorization": f"Bearer {KEY_A}"}, json=body
        )
        assert resp.status_code == 404

    async with rls_db["admin"]() as s:
        denied = (
            await s.execute(
                select(PartnerAuditLog.tenant_id).where(PartnerAuditLog.status_code == 403)
            )
        ).scalars().all()
    assert denied == [b] and a not in denied


async def test_vertex_inventory_pull_runs_as_the_target_tenant_under_rls(rls_db):
    """A Vertex AI pull scoped to tenant B (as the route and CLI do) writes
    and later updates B's asset; tenant A cannot see it."""
    from collector import cloud_inventory as ci
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    a, b = rls_db["a"], rls_db["b"]
    endpoint = "projects/42/locations/europe-west4/endpoints/7"

    def vertex(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok"})
        assert request.url.host == "europe-west4-aiplatform.googleapis.com"
        if request.url.path.endswith("/endpoints"):
            dm = {"id": "1", "displayName": "gemini-tuned-claims"}
            return httpx.Response(
                200, json={"endpoints": [{"name": endpoint, "deployedModels": [dm]}]}
            )
        return httpx.Response(200, json={})

    pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    gcp = ci.GcpInventoryCredentials(
        service_account={
            "client_email": "inv@acme-ml.iam.gserviceaccount.com",
            "private_key": pem,
        },
        project_ids=["acme-ml-prod"],
        regions=["europe-west4"],
    )
    settings = ci.CloudInventorySettings(
        cloud_inventory={str(b): ci.TenantCloudCredentials(gcp=gcp)}
    )

    async def _no_sleep(_s: float) -> None:
        return None

    for _ in range(2):
        async with rls_db["plain"]() as s:
            await rls.set_tenant(s, b)
            result = await ci.pull_cloud_inventory(
                s,
                b,
                clouds=["gcp"],
                settings=settings,
                transport=httpx.MockTransport(vertex),
                sleep=_no_sleep,
            )
        assert result["clouds"]["gcp"]["status"] == "ok", result

    calling = f"gcp:{endpoint}/deployedModels/1"
    async with rls_db["plain"]() as s:
        await rls.set_tenant(s, b)
        rows = (
            await s.execute(
                select(AIAsset.vendor, AIAsset.region).where(
                    AIAsset.calling_service == calling
                )
            )
        ).all()
        assert rows == [("google", "europe-west4")]
    async with rls_db["plain"]() as s:
        await rls.set_tenant(s, a)
        hidden = await s.execute(
            select(AIAsset.id).where(AIAsset.calling_service == calling)
        )
        assert hidden.first() is None


async def test_inventoried_region_reaches_registry_partner_api_and_ai_bom(rls_db):
    """Region is captured for sovereignty reporting (#94): an Azure, AWS
    and GCP pull for tenant B (under RLS) shows each asset's region in the
    tenant registry API, the Partner API (list, detail, JSON export) and
    the CycloneDX AI-BOM, and none of it to tenant A."""
    from collector import cloud_inventory as ci
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    b = rls_db["b"]
    sub = "aaaaaaaa-0000-0000-0000-000000000001"
    account = (
        f"/subscriptions/{sub}/resourceGroups/rg/providers/"
        "Microsoft.CognitiveServices/accounts/foundry-se"
    )
    pt = "arn:aws:bedrock:eu-central-1:123456789012:provisioned-model/pt-1"
    endpoint = "projects/42/locations/europe-west4/endpoints/7"

    def cloud(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host in ("login.microsoftonline.com", "oauth2.googleapis.com"):
            return httpx.Response(200, json={"access_token": "tok"})
        if host == "management.azure.com":
            if path.endswith("/accounts"):
                acct = {"id": account, "kind": "OpenAI", "location": "swedencentral"}
                return httpx.Response(200, json={"value": [acct]})
            dep = {
                "name": "gpt4o",
                "properties": {
                    "model": {"name": "gpt-4o"},
                    "provisioningState": "Succeeded",
                },
            }
            return httpx.Response(200, json={"value": [dep]})
        if host == "bedrock.eu-central-1.amazonaws.com":
            if path == "/provisioned-model-throughputs":
                summary = {
                    "provisionedModelArn": pt,
                    "foundationModelArn": "arn:aws:bedrock:eu-central-1::"
                    "foundation-model/anthropic.claude-3-haiku-20240307-v1:0",
                    "status": "InService",
                }
                return httpx.Response(
                    200, json={"provisionedModelSummaries": [summary]}
                )
            return httpx.Response(200, json={})
        assert host == "europe-west4-aiplatform.googleapis.com"
        if path.endswith("/endpoints"):
            dm = {"id": "1", "displayName": "gemma2-claims"}
            return httpx.Response(
                200, json={"endpoints": [{"name": endpoint, "deployedModels": [dm]}]}
            )
        return httpx.Response(200, json={})

    pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    settings = ci.CloudInventorySettings(
        cloud_inventory={
            str(b): ci.TenantCloudCredentials(
                azure=ci.AzureInventoryCredentials(
                    directory_id="contoso.onmicrosoft.com",
                    client_id="app",
                    client_secret="s",
                    subscription_ids=[sub],
                ),
                aws=ci.AwsInventoryCredentials(
                    access_key_id="AKIAEXAMPLEKEY12345",
                    secret_access_key="s",
                    regions=["eu-central-1"],
                ),
                gcp=ci.GcpInventoryCredentials(
                    service_account={
                        "client_email": "inv@acme-ml.iam.gserviceaccount.com",
                        "private_key": pem,
                    },
                    project_ids=["acme-ml-prod"],
                    regions=["europe-west4"],
                ),
            )
        }
    )

    async def _no_sleep(_s: float) -> None:
        return None

    async with rls_db["plain"]() as s:
        await rls.set_tenant(s, b)
        result = await ci.pull_cloud_inventory(
            s,
            b,
            settings=settings,
            transport=httpx.MockTransport(cloud),
            sleep=_no_sleep,
        )
    assert {k: v["status"] for k, v in result["clouds"].items()} == {
        "azure": "ok",
        "aws": "ok",
        "gcp": "ok",
    }, result

    expected = {
        "microsoft": "swedencentral",
        "amazon": "eu-central-1",
        "google": "europe-west4",
    }

    def regions(rows: list[dict]) -> dict:
        return {r["vendor"]: r["region"] for r in rows if r["vendor"] in expected}

    tenant_b = {"Authorization": f"Bearer {KEY_B}"}
    partner_b = {"Authorization": f"Bearer {PARTNER_KEY_B}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/v1/registry/assets", headers=tenant_b)
        assert resp.status_code == 200, resp.text
        assert regions(resp.json()["data"]) == expected

        resp = await c.get("/partner/v1/assets", headers=partner_b)
        assert resp.status_code == 200, resp.text
        rows = resp.json()["data"]
        assert regions(rows) == expected
        for row in rows:
            if row["vendor"] in expected:
                detail = await c.get(f"/partner/v1/assets/{row['id']}", headers=partner_b)
                assert detail.status_code == 200, detail.text
                assert detail.json()["region"] == expected[row["vendor"]]

        resp = await c.get("/partner/v1/export", headers=partner_b)
        assert resp.status_code == 200, resp.text
        assert regions(resp.json()["data"]) == expected

        resp = await c.get("/partner/v1/export?format=cyclonedx", headers=partner_b)
        assert resp.status_code == 200, resp.text
        bom_regions = {}
        for comp in resp.json()["components"]:
            props = {p["name"]: p["value"] for p in comp.get("properties", [])}
            if props.get("promptshields:vendor") in expected:
                bom_regions[props["promptshields:vendor"]] = props.get(
                    "promptshields:region"
                )
        assert bom_regions == expected

        # Tenant A sees none of B's inventoried assets.
        resp = await c.get(
            "/api/v1/registry/assets", headers={"Authorization": f"Bearer {KEY_A}"}
        )
        assert resp.status_code == 200, resp.text
        assert regions(resp.json()["data"]) == {}


async def test_webhook_worker_claims_across_tenants_and_delivers_as_each(
    rls_db, monkeypatch, caplog
):
    """Webhook events are queued under the tenant's context; the worker's
    claim is a logged bypass and each attempt runs as the row's tenant."""
    from collector import webhooks

    monkeypatch.setattr(settings, "webhook_secret_key", "s" * 40)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 0)

    async def public(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(webhooks, "_resolve_host", public)
    async with rls_db["admin"]() as s:
        partners = dict(
            (await s.execute(select(PartnerCredential.tenant_id, PartnerCredential.id))).all()
        )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        for key, tenant in ((KEY_A, rls_db["a"]), (KEY_B, rls_db["b"])):
            h = {"Authorization": f"Bearer {key}"}
            resp = await c.post(
                "/api/v1/admin/webhooks/subscriptions",
                headers=h,
                json={"partner_id": str(partners[tenant]), "url": "https://hooks.example/x"},
            )
            assert resp.status_code == 201, resp.text
            resp = await c.post(
                "/ingest/events", headers=h, json={"events": [{"vendor": f"v-{key}"}]}
            )
            assert resp.status_code == 200, resp.text

    sent = []

    def handler(request):
        sent.append(json.loads(request.content)["tenant_id"])
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    # Without the bypass the claim sees no tenant's rows.
    run = await webhooks.deliver_due(
        rls_db["plain"], transport=transport, claim_factory=rls_db["plain"]
    )
    assert run.claimed == 0

    caplog.set_level(logging.INFO, logger="collector.rls")
    run = await webhooks.deliver_due(rls_db["plain"], transport=transport)
    assert (run.claimed, run.succeeded) == (2, 2)
    assert sorted(sent) == sorted([str(rls_db["a"]), str(rls_db["b"])])
    assert any("reason=webhook-worker" in r.getMessage() for r in caplog.records)


async def test_webhook_attestation_events_and_rekey_under_rls(rls_db, monkeypatch, caplog):
    """asset.attested is queued under the attesting tenant's context, and
    the rekey job lists tenants through a logged bypass, then re-encrypts
    each tenant's secrets as that tenant."""
    from collector import webhooks

    monkeypatch.setattr(settings, "webhook_secret_key", "s" * 40)
    monkeypatch.setattr(settings, "webhook_secret_keys", "")
    monkeypatch.setattr(settings, "rate_limit_per_minute", 0)

    async def public(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(webhooks, "_resolve_host", public)
    async with rls_db["admin"]() as s:
        partners = dict(
            (await s.execute(select(PartnerCredential.tenant_id, PartnerCredential.id))).all()
        )
        assets = dict((await s.execute(select(AIAsset.tenant_id, AIAsset.id))).all())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        for key, tenant in ((KEY_A, rls_db["a"]), (KEY_B, rls_db["b"])):
            h = {"Authorization": f"Bearer {key}"}
            resp = await c.post(
                "/api/v1/admin/webhooks/subscriptions",
                headers=h,
                json={
                    "partner_id": str(partners[tenant]),
                    "url": "https://hooks.example/x",
                    "event_types": ["asset.attested"],
                },
            )
            assert resp.status_code == 201, resp.text
            resp = await c.post(
                f"/api/v1/registry/assets/{assets[tenant]}/attestations",
                headers=h,
                json={"field": "asset", "state": "attested", "attested_by": "r@x.example"},
            )
            assert resp.status_code == 201, resp.text

    async with rls_db["admin"]() as s:
        queued = (
            await s.execute(
                select(WebhookDelivery.tenant_id, WebhookDelivery.payload).where(
                    WebhookDelivery.event_type == "asset.attested"
                )
            )
        ).all()
    assert sorted(str(t) for t, _ in queued) == sorted([str(rls_db["a"]), str(rls_db["b"])])
    assert all(p["tenant_id"] == str(t) for t, p in queued)

    monkeypatch.setattr(settings, "webhook_secret_keys", "k2:" + "n" * 40)
    # Without the bypass the listing sees no tenant's subscriptions.
    run = await webhooks.rekey_all(rls_db["plain"], list_factory=rls_db["plain"])
    assert run.checked == 0
    caplog.set_level(logging.INFO, logger="collector.rls")
    run = await webhooks.rekey_all(rls_db["plain"])
    # Two real subscriptions rekeyed; the seeded placeholders are unreadable.
    assert (run.checked, run.rekeyed, run.unreadable) == (4, 2, 2)
    assert any("reason=webhook-rekey" in r.getMessage() for r in caplog.records)


async def test_webhook_patch_and_secret_sweep_under_rls(rls_db, monkeypatch, caplog):
    """PATCH runs as the caller's tenant (another tenant's subscription is
    a 404, and the audit row lands under the caller); the expired-secret
    sweep lists tenants through a logged bypass and clears each tenant's
    rows as that tenant."""
    from collector import webhooks

    monkeypatch.setattr(settings, "webhook_secret_key", "s" * 40)
    monkeypatch.setattr(settings, "webhook_secret_keys", "")
    monkeypatch.setattr(settings, "rate_limit_per_minute", 0)

    async def public(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(webhooks, "_resolve_host", public)
    async with rls_db["admin"]() as s:
        partners = dict(
            (await s.execute(select(PartnerCredential.tenant_id, PartnerCredential.id))).all()
        )
    subs = {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        for key, tenant in ((KEY_A, rls_db["a"]), (KEY_B, rls_db["b"])):
            h = {"Authorization": f"Bearer {key}"}
            resp = await c.post(
                "/api/v1/admin/webhooks/subscriptions",
                headers=h,
                json={"partner_id": str(partners[tenant]), "url": "https://hooks.example/x"},
            )
            assert resp.status_code == 201, resp.text
            subs[tenant] = resp.json()["id"]
            resp = await c.post(
                f"/api/v1/admin/webhooks/subscriptions/{subs[tenant]}/rotate-secret",
                headers=h,
                json={"grace_period_seconds": 60},
            )
            assert resp.status_code == 200, resp.text

        h_a = {"Authorization": f"Bearer {KEY_A}"}
        url = "/api/v1/admin/webhooks/subscriptions/{}"
        resp = await c.patch(url.format(subs[rls_db["b"]]), headers=h_a, json={"is_active": False})
        assert resp.status_code == 404
        resp = await c.patch(
            url.format(subs[rls_db["a"]]),
            headers=h_a,
            json={"is_active": False, "event_types": ["asset.retired"]},
        )
        assert resp.status_code == 200, resp.text

    async with rls_db["admin"]() as s:
        b_row = await s.get(WebhookSubscription, uuid.UUID(subs[rls_db["b"]]))
        audit = (
            await s.execute(
                select(PartnerAuditLog.tenant_id, PartnerAuditLog.status_code).where(
                    PartnerAuditLog.method == "PATCH"
                )
            )
        ).all()
    assert sorted((str(t), code) for t, code in audit) == [
        (str(rls_db["a"]), 200),
        (str(rls_db["a"]), 404),
    ]
    assert b_row.is_active is True

    later = datetime.now(UTC) + timedelta(minutes=5)
    # Without the bypass the listing sees no tenant's subscriptions.
    cleared = await webhooks.clear_expired_previous_secrets(
        rls_db["plain"], now=later, list_factory=rls_db["plain"]
    )
    assert cleared == 0
    caplog.set_level(logging.INFO, logger="collector.rls")
    assert await webhooks.clear_expired_previous_secrets(rls_db["plain"], now=later) == 2
    assert any("reason=webhook-secret-sweep" in r.getMessage() for r in caplog.records)
    async with rls_db["admin"]() as s:
        left = (
            await s.execute(
                select(WebhookSubscription.id).where(
                    WebhookSubscription.previous_secret_ciphertext.is_not(None)
                )
            )
        ).all()
    assert left == []


async def test_risk_suggestions_run_and_all_tenants_cli_under_rls(rls_db):
    """The tenant route and the --all-tenants job each write suggestions
    for the right tenant only."""
    from collector.risk_suggestions import _run_cli

    event = {
        "vendor": "openai",
        "model": "gpt-4o",
        "use_case_name": "resume-screening",
        "business_unit": "HR",
        "detected_pii_types": ["email"],
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        for key in (KEY_A, KEY_B):
            h = {"Authorization": f"Bearer {key}"}
            resp = await c.post("/ingest/events", headers=h, json={"events": [event]})
            assert resp.status_code == 200, resp.text
        resp = await c.post(
            "/api/v1/admin/risk-suggestions/run",
            headers={"Authorization": f"Bearer {KEY_A}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["created"] > 0

    async def suggested() -> dict:
        async with rls_db["admin"]() as s:
            rows = (
                await s.execute(
                    select(RiskMapping.tenant_id, RiskMapping.asset_id).where(
                        RiskMapping.rule_id.is_not(None)
                    )
                )
            ).all()
        return {t: {a for tt, a in rows if tt == t} for t, _ in rows}

    assert set(await suggested()) == {rls_db["a"]}
    await _run_cli(None, rls_db["engine"].url.render_as_string(hide_password=False))
    by_tenant = await suggested()
    assert set(by_tenant) == {rls_db["a"], rls_db["b"]}
    async with rls_db["admin"]() as s:
        for tenant, assets in by_tenant.items():
            owners = set(
                (await s.execute(select(AIAsset.tenant_id).where(AIAsset.id.in_(assets))))
                .scalars()
            )
            assert owners == {tenant}


async def test_servicenow_sync_runs_as_its_tenant_under_rls(rls_db, monkeypatch):
    """The ServiceNow operator routes are scoped to the target tenant (no
    bypass). A sync scoped the same way reads only that tenant's registry
    through the Partner API, and a partner key of another tenant is refused
    before ServiceNow is called."""
    from collector import app as app_module
    from collector import servicenow as sn

    b = rls_db["b"]
    monkeypatch.setenv("PS_ADMIN_TOKEN", TOKEN)
    monkeypatch.delenv("PS_SERVICENOW", raising=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        op = {"Authorization": f"Bearer {TOKEN}"}
        resp = await c.post(
            "/api/v1/admin/connectors/servicenow/sync",
            headers=op,
            json={"tenant_id": str(b)},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "not_configured"
        resp = await c.get(
            "/api/v1/admin/connectors/servicenow/status",
            headers=op,
            params={"tenant_id": str(b)},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["configured"] is False

    # The in-process Partner API keeps loaded rows after commit, as in production.
    monkeypatch.setattr(app_module, "SessionLocal", rls_db["plain"])
    payloads: list[dict] = []

    def servicenow(r: httpx.Request) -> httpx.Response:
        payload = json.loads(r.content)
        payloads.append(payload)
        items = [
            {"className": i["className"], "operation": "INSERT", "sysId": "a" * 32}
            for i in payload["items"]
        ]
        return httpx.Response(200, json={"result": {"items": items}})

    async def public(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    async def sync(partner_key: str) -> dict:
        conn = {
            "instance_host": "acme.service-now.com",
            "username": "ps",
            "password": "pw",
            "partner_api": {"base_url": "https://collector.test", "api_key": partner_key},
        }
        async with rls_db["plain"]() as s:
            await rls.set_tenant(s, b)
            return await sn.sync_servicenow(
                s,
                b,
                settings=sn.ServiceNowSettings(servicenow={str(b): conn}),
                partner_transport=ASGITransport(app=app),
                servicenow_transport=httpx.MockTransport(servicenow),
                resolver=public,
            )

    out = await sync(PARTNER_KEY_B)
    assert out["status"] == "ok", out
    vendors = {
        i["values"]["name"]
        for i in payloads[0]["items"]
        if i["className"] == "cmdb_ci_service"
    }
    assert vendors == {"vendor-b"}

    payloads.clear()
    out = await sync(PARTNER_KEY_A)
    assert out["status"] == "error"
    assert "does not belong to the configured tenant" in out["error"]
    assert payloads == []
    async with rls_db["admin"]() as s:
        state = (await s.get(Tenant, b)).settings["connectors"]["servicenow"]
    assert state["last_status"] == "error" and state["cursor"]
