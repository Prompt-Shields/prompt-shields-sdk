"""Cloud AI inventory against a real PostgreSQL database
(PS_TEST_DATABASE_URL). Azure HTTP is mocked; checks the operator route and
that assets land on the target tenant only, with region and owner, and are
idempotent."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
from collector import cloud_inventory as ci
from collector.app import app
from db.models import AIAsset, Tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SUB = "aaaaaaaa-0000-0000-0000-000000000001"
ACCOUNT = (
    f"/subscriptions/{SUB}/resourceGroups/rg/providers/"
    "Microsoft.CognitiveServices/accounts/foundry-eu"
)


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "login.microsoftonline.com":
        return httpx.Response(200, json={"access_token": "tok"})
    assert request.url.host == "management.azure.com"
    if request.url.path.endswith("/accounts"):
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": ACCOUNT,
                        "kind": "AIServices",
                        "location": "germanywestcentral",
                        "tags": {"owner": "ai-lead@acme.com", "environment": "prod"},
                    }
                ]
            },
        )
    return httpx.Response(
        200,
        json={
            "value": [
                {
                    "name": "gpt4o",
                    "properties": {
                        "model": {"format": "OpenAI", "name": "gpt-4o"},
                        "provisioningState": "Succeeded",
                    },
                }
            ]
        },
    )


async def _no_sleep(_s: float) -> None:
    return None


async def test_inventory_stores_assets_for_target_tenant_only(seeded_db):
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        tenant = (await db.execute(select(Tenant))).scalars().one()
        other = Tenant(name="Other", domain="other.com", settings={})
        db.add(other)
        await db.commit()
        tenant_id, other_id = tenant.id, other.id

    settings = ci.CloudInventorySettings(
        cloud_inventory={
            str(tenant_id): ci.TenantCloudCredentials(
                azure=ci.AzureInventoryCredentials(
                    directory_id="contoso.onmicrosoft.com",
                    client_id="app",
                    client_secret="s",
                    subscription_ids=[SUB],
                )
            )
        }
    )
    for _ in range(2):
        async with factory() as db:
            result = await ci.pull_cloud_inventory(
                db,
                tenant_id,
                settings=settings,
                transport=httpx.MockTransport(_handler),
                sleep=_no_sleep,
                now=datetime(2026, 10, 15, tzinfo=UTC),
            )
        assert result["clouds"]["azure"]["status"] == "ok"

    async with factory() as db:
        assets = (await db.execute(select(AIAsset))).scalars().all()
        assert [
            (a.tenant_id, a.model, a.region, a.owner_email, a.environment, a.confidence)
            for a in assets
        ] == [
            (
                tenant_id,
                "gpt-4o",
                "germanywestcentral",
                "ai-lead@acme.com",
                "production",
                "medium",
            )
        ]
        assert assets[0].calling_service == f"azure:{ACCOUNT}/deployments/gpt4o"
        n_other = await db.scalar(
            select(func.count())
            .select_from(AIAsset)
            .where(AIAsset.tenant_id == other_id)
        )
        assert n_other == 0


async def test_operator_route_requires_token_and_known_tenant(seeded_db, monkeypatch):
    monkeypatch.setenv("PS_ADMIN_TOKEN", "op-token")
    monkeypatch.delenv("PS_CLOUD_INVENTORY", raising=False)
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        tenant_id = (await db.execute(select(Tenant.id))).scalar_one()

    url = "/api/v1/admin/cloud-inventory/pull"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            url,
            json={"tenant_id": str(tenant_id)},
            headers={"Authorization": "Bearer ps-test-key-12345"},
        )
        assert resp.status_code == 401

        op = {"Authorization": "Bearer op-token"}
        resp = await c.post(url, json={"tenant_id": str(uuid.uuid4())}, headers=op)
        assert resp.status_code == 404

        resp = await c.post(url, json={"tenant_id": str(tenant_id)}, headers=op)
        assert resp.status_code == 200
        assert resp.json() == {
            "tenant_id": str(tenant_id),
            "clouds": {"azure": {"status": "not_configured"}},
        }
