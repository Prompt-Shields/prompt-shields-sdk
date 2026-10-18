import pytest
from collector.app import app
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_ingest_single_event(seeded_db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/ingest/events",
            headers={"Authorization": "Bearer ps-test-key-12345"},
            json={"events": [{
                "vendor": "openai",
                "model": "gpt-4o",
                "use_case_name": "interview-screening",
                "business_unit": "HR",
                "owner_email": "jane@test.com",
                "environment": "production",
                "source": "sdk",
                "tokens_in": 150,
                "tokens_out": 300,
                "latency_ms": 450,
            }]}
        )
    assert resp.status_code == 200
    assert resp.json()["ingested"] == 1
    assert resp.json()["assets_created"] == 1


@pytest.mark.asyncio
async def test_ingest_requires_auth():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/ingest/events", json={"events": []})
    assert resp.status_code == 401
