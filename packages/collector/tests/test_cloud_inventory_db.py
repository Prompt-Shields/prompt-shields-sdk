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

        # An unknown cloud is a client error, not silently dropped.
        resp = await c.post(
            url, json={"tenant_id": str(tenant_id), "clouds": ["azure", "gpc"]}, headers=op
        )
        assert resp.status_code == 400
        assert "gpc" in resp.json()["detail"]["detail"]

        resp = await c.post(url, json={"tenant_id": str(tenant_id)}, headers=op)
        assert resp.status_code == 200
        assert resp.json() == {
            "tenant_id": str(tenant_id),
            "clouds": {
                "azure": {"status": "not_configured"},
                "aws": {"status": "not_configured"},
                "gcp": {"status": "not_configured"},
            },
        }


BEDROCK_PT = "arn:aws:bedrock:eu-central-1:123456789012:provisioned-model/pt-1"


def _bedrock_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.host == "bedrock.eu-central-1.amazonaws.com"
    assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
    if request.url.path == "/provisioned-model-throughputs":
        return httpx.Response(
            200,
            json={
                "provisionedModelSummaries": [
                    {
                        "provisionedModelArn": BEDROCK_PT,
                        "foundationModelArn": "arn:aws:bedrock:eu-central-1::"
                        "foundation-model/anthropic.claude-3-haiku-20240307-v1:0",
                        "status": "InService",
                    }
                ]
            },
        )
    if request.url.path == "/listTagsForResource":
        return httpx.Response(
            200, json={"tags": [{"key": "owner", "value": "ai-lead@acme.com"}]}
        )
    return httpx.Response(200, json={})


async def test_aws_inventory_stores_bedrock_assets(seeded_db):
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        tenant_id = (await db.execute(select(Tenant.id))).scalar_one()

    settings = ci.CloudInventorySettings(
        cloud_inventory={
            str(tenant_id): ci.TenantCloudCredentials(
                aws=ci.AwsInventoryCredentials(
                    access_key_id="AKIAEXAMPLEKEY12345",
                    secret_access_key="s",
                    regions=["eu-central-1"],
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
                transport=httpx.MockTransport(_bedrock_handler),
                sleep=_no_sleep,
                now=datetime(2026, 10, 15, tzinfo=UTC),
            )
        assert result["clouds"]["aws"]["status"] == "ok"

    async with factory() as db:
        assets = (await db.execute(select(AIAsset))).scalars().all()
        assert [
            (a.tenant_id, a.vendor, a.model, a.region, a.owner_email, a.calling_service)
            for a in assets
        ] == [
            (
                tenant_id,
                "amazon",
                "anthropic.claude-3-haiku-20240307-v1:0",
                "eu-central-1",
                "ai-lead@acme.com",
                f"aws:{BEDROCK_PT}",
            )
        ]


VERTEX_EP = "projects/123456789012/locations/europe-west4/endpoints/900"


def _vertex_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "oauth2.googleapis.com":
        return httpx.Response(200, json={"access_token": "tok"})
    assert request.url.host == "europe-west4-aiplatform.googleapis.com"
    if request.url.path.endswith("/endpoints"):
        return httpx.Response(
            200,
            json={
                "endpoints": [
                    {
                        "name": VERTEX_EP,
                        "labels": {"env": "prod", "department": "claims"},
                        "deployedModels": [
                            {
                                "id": "1",
                                "model": "projects/123456789012/locations/"
                                "europe-west4/models/7",
                                "displayName": "gemma2-claims",
                            }
                        ],
                    }
                ]
            },
        )
    return httpx.Response(200, json={})


async def test_gcp_inventory_stores_vertex_assets(seeded_db):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        tenant_id = (await db.execute(select(Tenant.id))).scalar_one()

    settings = ci.CloudInventorySettings(
        cloud_inventory={
            str(tenant_id): ci.TenantCloudCredentials(
                gcp=ci.GcpInventoryCredentials(
                    service_account={
                        "type": "service_account",
                        "project_id": "acme-ml-prod",
                        "client_email": "inv@acme-ml-prod.iam.gserviceaccount.com",
                        "private_key": pem,
                    },
                    regions=["europe-west4"],
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
                transport=httpx.MockTransport(_vertex_handler),
                sleep=_no_sleep,
                now=datetime(2026, 10, 15, tzinfo=UTC),
            )
        assert result["clouds"]["gcp"]["status"] == "ok"

    async with factory() as db:
        assets = (await db.execute(select(AIAsset))).scalars().all()
        assert [
            (
                a.tenant_id,
                a.vendor,
                a.model,
                a.region,
                a.environment,
                a.business_unit,
                a.calling_service,
            )
            for a in assets
        ] == [
            (
                tenant_id,
                "google",
                "gemma2-claims",
                "europe-west4",
                "production",
                "claims",
                f"gcp:{VERTEX_EP}/deployedModels/1",
            )
        ]
