import pytest
from collector.app import app
from httpx import ASGITransport, AsyncClient

HEADERS = {"Authorization": "Bearer ps-test-key-12345"}


async def seed_assets(client):
    events = [
        {"vendor": "openai", "model": "gpt-4o", "use_case_name": "screening",
         "business_unit": "HR", "source": "sdk", "tokens_in": 100, "tokens_out": 200},
        {"vendor": "anthropic", "model": "claude-sonnet-4-20250514", "use_case_name": "contract-review",
         "business_unit": "Legal", "source": "gateway", "tokens_in": 500, "tokens_out": 1000},
    ]
    await client.post("/ingest/events", headers=HEADERS, json={"events": events})


@pytest.mark.asyncio
async def test_list_assets(seeded_db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await seed_assets(client)
        resp = await client.get("/api/v1/registry/assets", headers=HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 2
    assert body["meta"]["total"] == 2


@pytest.mark.asyncio
async def test_filter_by_business_unit(seeded_db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await seed_assets(client)
        resp = await client.get(
            "/api/v1/registry/assets?business_unit=HR", headers=HEADERS
        )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["business_unit"] == "HR"


@pytest.mark.asyncio
async def test_get_asset_by_id(seeded_db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await seed_assets(client)
        list_resp = await client.get("/api/v1/registry/assets", headers=HEADERS)
        asset_id = list_resp.json()["data"][0]["id"]

        resp = await client.get(f"/api/v1/registry/assets/{asset_id}", headers=HEADERS)

    assert resp.status_code == 200
    assert resp.json()["id"] == asset_id


@pytest.mark.asyncio
async def test_list_vendors(seeded_db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await seed_assets(client)
        resp = await client.get("/api/v1/registry/vendors", headers=HEADERS)

    assert resp.status_code == 200
    vendors = resp.json()["data"]
    assert set(vendors) == {"openai", "anthropic"}


@pytest.mark.asyncio
async def test_registry_reads_write_audit_rows(seeded_db):
    from db.models import PartnerAuditLog
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await seed_assets(client)
        list_resp = await client.get("/api/v1/registry/assets?business_unit=HR", headers=HEADERS)
        asset_id = list_resp.json()["data"][0]["id"]
        await client.get(f"/api/v1/registry/assets/{asset_id}", headers=HEADERS)

    async with AsyncSession(seeded_db) as session:
        rows = (
            await session.execute(select(PartnerAuditLog).order_by(PartnerAuditLog.timestamp))
        ).scalars().all()

    assert [r.endpoint for r in rows] == [
        "/api/v1/registry/assets",
        "/api/v1/registry/assets/{asset_id}",
    ]
    assert all(r.partner_id is None and r.api_key_prefix for r in rows)
    assert rows[0].query_params == {"business_unit": "HR"}
    assert rows[0].response_count == 1
    assert [str(i) for i in rows[1].asset_ids] == [asset_id]
