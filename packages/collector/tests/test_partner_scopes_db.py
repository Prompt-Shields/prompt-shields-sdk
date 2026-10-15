"""Partner scopes and rate-limit tiers end to end on PostgreSQL (#106).

Goes through the full app: admin API issues and changes credentials, the
partner authenticates with OAuth or an API key, and routes enforce the
scope and the tier's limit.
"""

from __future__ import annotations

import pytest
from collector.app import app
from collector.partner_auth import hash_secret, issue_access_token
from collector.partner_scopes import RATE_LIMIT_TIERS
from db.models import PartnerAuditLog, PartnerCredential, Tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ADMIN = {"Authorization": "Bearer ps-test-key-12345"}  # seeded tenant key
READ_ROUTES = [
    "/partner/v1/assets",
    "/partner/v1/vendors",
    "/partner/v1/changes?since=2026-01-01T00:00:00",
    "/partner/v1/export",
]


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _token(c: AsyncClient, client_id: str, secret: str) -> dict:
    r = await c.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": secret,
        },
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_granular_scope_and_tier_lifecycle(seeded_db):
    async with _client() as c:
        r = await c.post(
            "/api/v1/admin/partner-credentials",
            headers=ADMIN,
            json={
                "partner_name": "Ardoq",
                "scopes": ["assets:read"],
                "rate_limit_tier": "basic",
            },
        )
        assert r.status_code == 201, r.text
        cred = r.json()
        assert cred["scopes"] == ["assets:read"]
        assert cred["rate_limit_tier"] == "basic"
        assert cred["rate_limit"] == RATE_LIMIT_TIERS["basic"]

        h = await _token(c, cred["client_id"], cred["client_secret"])
        key = {"Authorization": f"Bearer {cred['api_key']}"}

        ok = await c.get("/partner/v1/assets", headers=h)
        assert ok.status_code == 200, ok.text
        assert ok.headers["X-RateLimit-Limit"] == str(RATE_LIMIT_TIERS["basic"])
        assert (await c.get("/partner/v1/export", headers=h)).status_code == 403
        assert (await c.get("/partner/v1/export", headers=key)).status_code == 403

        # Narrow to export only: the existing token loses assets:read at once.
        r = await c.patch(
            f"/api/v1/admin/partner-credentials/{cred['id']}",
            headers=ADMIN,
            json={"scopes": ["export:read"], "rate_limit_tier": "premium"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["rate_limit"] == RATE_LIMIT_TIERS["premium"]

        assert (await c.get("/partner/v1/assets", headers=h)).status_code == 403
        # The old token said assets:read only, so it gets nothing new either.
        assert (await c.get("/partner/v1/export", headers=h)).status_code == 403
        h2 = await _token(c, cred["client_id"], cred["client_secret"])
        exp = await c.get("/partner/v1/export", headers=h2)
        assert exp.status_code == 200, exp.text
        assert exp.headers["X-RateLimit-Limit"] == str(RATE_LIMIT_TIERS["premium"])
        assert (await c.get("/partner/v1/assets", headers=key)).status_code == 403
        assert (await c.get("/partner/v1/export", headers=key)).status_code == 200

        # A custom limit replaces the tier.
        r = await c.patch(
            f"/api/v1/admin/partner-credentials/{cred['id']}",
            headers=ADMIN,
            json={"rate_limit": 42},
        )
        assert r.json()["rate_limit"] == 42 and r.json()["rate_limit_tier"] is None
        exp = await c.get("/partner/v1/export", headers=key)
        assert exp.headers["X-RateLimit-Limit"] == "42"

    async with async_sessionmaker(seeded_db, class_=AsyncSession)() as s:
        codes = (
            await s.execute(
                select(PartnerAuditLog.status_code).where(
                    PartnerAuditLog.partner_id == cred["id"]
                )
            )
        ).scalars().all()
    assert codes.count(403) == 5


@pytest.mark.asyncio
async def test_phase1_registry_read_credentials_keep_working(seeded_db):
    """Rows and JWTs from before #106 hold only `registry:read`."""
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        tenant = (await s.execute(select(Tenant))).scalars().first()
        legacy = PartnerCredential(
            tenant_id=tenant.id,
            partner_name="legacy",
            client_id="legacy-client",
            client_secret_hash=hash_secret("x"),
            api_key_hash=hash_secret("ps-pk-legacy-key"),
            scopes=["registry:read"],
            rate_limit=777,
        )
        s.add(legacy)
        await s.commit()
    token, _ = issue_access_token(tenant.id, legacy.id, "legacy-client", ["registry:read"])

    async with _client() as c:
        for auth in (f"Bearer {token}", "Bearer ps-pk-legacy-key"):
            for path in READ_ROUTES:
                r = await c.get(path, headers={"Authorization": auth})
                assert r.status_code == 200, (path, r.text)
                assert r.headers["X-RateLimit-Limit"] == "777"

        listed = (await c.get("/api/v1/admin/partner-credentials", headers=ADMIN)).json()
    (row,) = [d for d in listed["data"] if d["client_id"] == "legacy-client"]
    assert row["scopes"] == ["registry:read"]
    assert row["rate_limit"] == 777 and row["rate_limit_tier"] is None


@pytest.mark.asyncio
async def test_admin_validation_and_catalog(seeded_db):
    async with _client() as c:
        base = {"partner_name": "x"}
        for extra in (
            {"scopes": ["registry:write"]},
            {"scopes": ["*"]},
            {"rate_limit_tier": "platinum"},
            {"rate_limit_tier": "basic", "rate_limit": 10},
        ):
            r = await c.post(
                "/api/v1/admin/partner-credentials", headers=ADMIN, json=base | extra
            )
            assert r.status_code == 400, (extra, r.text)

        r = await c.post("/api/v1/admin/partner-credentials", headers=ADMIN, json=base)
        body = r.json()
        assert "webhooks:manage" not in body["scopes"]
        assert body["rate_limit_tier"] == "standard"

        cat = (await c.get("/api/v1/admin/partner-scopes", headers=ADMIN)).json()
        assert "webhooks:manage" in {s["name"] for s in cat["scopes"]}
        assert "webhooks:manage" not in cat["aliases"]["registry:read"]
        assert {t["name"] for t in cat["rate_limit_tiers"]} == set(RATE_LIMIT_TIERS)
        assert (await c.get("/api/v1/admin/partner-scopes")).status_code == 401
