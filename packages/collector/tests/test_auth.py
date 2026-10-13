import pytest
from httpx import AsyncClient, ASGITransport
from collector.app import app


@pytest.mark.asyncio
async def test_missing_auth_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/registry/assets")
    assert resp.status_code == 401
    assert resp.json()["detail"]["title"] == "Unauthorized"


@pytest.mark.asyncio
async def test_valid_auth_passes(seeded_db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/registry/assets",
            headers={"Authorization": "Bearer ps-test-key-12345"}
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_wrong_and_unknown_key_return_same_401(seeded_db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        wrong = await client.get(
            "/api/v1/registry/assets",
            headers={"Authorization": "Bearer ps-test-key-12346"}
        )
        unknown = await client.get(
            "/api/v1/registry/assets",
            headers={"Authorization": "Bearer ps-no-such-key"}
        )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
