"""Provider usage pull against a real PostgreSQL database
(PS_TEST_DATABASE_URL). Vendor HTTP is mocked; checks the operator route and
that stored rows land on the target tenant only and are idempotent."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
from collector import provider_usage as pu
from collector.app import app
from db.models import AIAsset, AIUsageEvent, Tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

START = datetime(2026, 10, 1, tzinfo=UTC)
END = datetime(2026, 10, 3, tzinfo=UTC)
DAY1 = int(START.timestamp())
OPENAI_KEY = "sk-admin-openai-SECRET"


def _handler(request: httpx.Request) -> httpx.Response:
    assert request.url.host == "api.openai.com"
    if request.url.path.endswith("/embeddings"):
        return httpx.Response(200, json={"data": [], "has_more": False})
    return httpx.Response(
        200,
        json={
            "data": [
                {
                    "start_time": DAY1,
                    "end_time": DAY1 + 86400,
                    "results": [
                        {
                            "input_tokens": 100,
                            "output_tokens": 50,
                            "num_model_requests": 3,
                            "project_id": "proj_a",
                            "api_key_id": "key_shadow",
                            "model": "gpt-4o-mini",
                        }
                    ],
                }
            ],
            "has_more": False,
        },
    )


async def _no_sleep(_s: float) -> None:
    return None


async def test_pull_stores_rows_for_target_tenant_only_and_is_idempotent(
    seeded_db,
):
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        tenant = (await db.execute(select(Tenant))).scalars().one()
        other = Tenant(name="Other", domain="other.com", settings={})
        db.add(other)
        await db.commit()
        tenant_id, other_id = tenant.id, other.id

    settings = pu.ProviderUsageSettings(
        provider_admin_keys={str(tenant_id): {"openai": OPENAI_KEY}}
    )
    for _ in range(2):
        async with factory() as db:
            result = await pu.pull_provider_usage(
                db,
                tenant_id,
                start=START,
                end=END,
                settings=settings,
                transport=httpx.MockTransport(_handler),
                sleep=_no_sleep,
            )
        assert result["providers"]["openai"]["status"] == "ok"

    async with factory() as db:
        assets = (await db.execute(select(AIAsset))).scalars().all()
        assert [(a.tenant_id, a.discovery_source, a.confidence) for a in assets] == [
            (tenant_id, ["platform_signal"], "medium")
        ]
        events = (await db.execute(select(AIUsageEvent))).scalars().all()
        assert [(e.tenant_id, e.tokens_in, e.tokens_out) for e in events] == [
            (tenant_id, 100, 50)
        ]
        n_other = await db.scalar(
            select(func.count()).select_from(AIAsset).where(AIAsset.tenant_id == other_id)
        )
        assert n_other == 0


async def test_operator_route_requires_token_and_known_tenant(seeded_db, monkeypatch):
    monkeypatch.setenv("PS_ADMIN_TOKEN", "op-token")
    monkeypatch.delenv("PS_PROVIDER_ADMIN_KEYS", raising=False)
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        tenant_id = (await db.execute(select(Tenant.id))).scalar_one()

    url = "/api/v1/admin/provider-usage/pull"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # A tenant ingest key is not an operator credential.
        resp = await c.post(
            url,
            json={"tenant_id": str(tenant_id)},
            headers={"Authorization": "Bearer ps-test-key-12345"},
        )
        assert resp.status_code == 401

        op = {"Authorization": "Bearer op-token"}
        resp = await c.post(url, json={"tenant_id": str(uuid.uuid4())}, headers=op)
        assert resp.status_code == 404

        resp = await c.post(
            url, json={"tenant_id": str(tenant_id), "days": 2}, headers=op
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["tenant_id"] == str(tenant_id)
        assert body["providers"] == {
            "openai": {"status": "not_configured"},
            "anthropic": {"status": "not_configured"},
        }
