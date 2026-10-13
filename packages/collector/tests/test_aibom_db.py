"""CycloneDX AI-BOM export against a real PostgreSQL database (PS_TEST_DATABASE_URL).

Seeds two tenants, each with assets, data flows, risk mappings and a partner
credential, then calls GET /partner/v1/export?format=cyclonedx through the
full app (DB middleware, JWT auth, rate limit, audit).
"""

from __future__ import annotations

import json

import pytest
from collector.app import app
from collector.partner_auth import hash_secret, issue_access_token
from db.models import (
    AIAsset,
    DataFlow,
    PartnerAuditLog,
    PartnerCredential,
    RiskMapping,
    Tenant,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _seed(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        ta = Tenant(name="Tenant A", domain="a.example", settings={})
        tb = Tenant(name="Tenant B", domain="b.example", settings={})
        s.add_all([ta, tb])
        await s.flush()

        a1 = AIAsset(
            tenant_id=ta.id,
            vendor="openai",
            model="gpt-4o",
            use_case_name="A support summariser",
            owner_email="owner-a@a.example",
            discovery_source=["sdk", "gateway"],
        )
        a2 = AIAsset(
            tenant_id=ta.id, vendor="anthropic", model=None, discovery_source=["otel"]
        )
        b1 = AIAsset(
            tenant_id=tb.id,
            vendor="openai",
            model="gpt-4o",
            use_case_name="B secret project",
            discovery_source=["sdk"],
        )
        s.add_all([a1, a2, b1])
        await s.flush()

        s.add_all(
            [
                DataFlow(
                    tenant_id=ta.id,
                    asset_id=a1.id,
                    source_system="crm",
                    destination_system="openai",
                    direction="outbound",
                    data_classification="confidential",
                    detected_pii_types=["email"],
                ),
                DataFlow(
                    tenant_id=tb.id,
                    asset_id=b1.id,
                    source_system="b-payroll",
                    destination_system="openai",
                    direction="outbound",
                ),
                RiskMapping(
                    tenant_id=ta.id,
                    asset_id=a1.id,
                    risk_category="high_risk",
                    risk_level="high",
                    framework="eu_ai_act",
                ),
                RiskMapping(
                    tenant_id=tb.id,
                    asset_id=b1.id,
                    risk_category="b_only_category",
                    risk_level="low",
                ),
            ]
        )
        pa = PartnerCredential(
            tenant_id=ta.id,
            partner_name="pa",
            client_id="pa-client",
            client_secret_hash=hash_secret("x"),
            scopes=["registry:read"],
        )
        s.add(pa)
        await s.commit()

        token_a, _ = issue_access_token(ta.id, pa.id, "pa-client", ["registry:read"])
        return ta.id, tb.id, {a1.id, a2.id}, b1.id, token_a, pa.id


@pytest.mark.asyncio
async def test_cyclonedx_export_is_tenant_scoped_on_postgres(seeded_db):
    tenant_a, tenant_b, a_ids, b_id, token_a, partner_a = await _seed(seeded_db)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get(
            "/partner/v1/export",
            params={"format": "cyclonedx"},
            headers={"Authorization": f"Bearer {token_a}"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/vnd.cyclonedx+json")
    for h in ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"):
        assert h in resp.headers

    bom = resp.json()
    apps = [c for c in bom["components"] if c["type"] == "application"]
    assert {c["bom-ref"] for c in apps} == {f"promptshields:asset:{i}" for i in a_ids}
    (svc,) = [s for s in bom["services"] if s["name"] == "openai"]
    assert [d["name"] for d in svc["data"]] == ["crm -> openai"]

    text = json.dumps(bom)
    for leaked in (
        str(tenant_b),
        str(b_id),
        "B secret project",
        "b-payroll",
        "b_only_category",
    ):
        assert leaked not in text
    assert str(tenant_a) in text

    async with async_sessionmaker(seeded_db, class_=AsyncSession)() as s:
        audits = (
            await s.execute(
                select(PartnerAuditLog.endpoint, PartnerAuditLog.response_count).where(
                    PartnerAuditLog.partner_id == partner_a
                )
            )
        ).all()
    assert [tuple(a) for a in audits] == [("/partner/v1/export", 2)]

    pytest.importorskip("jsonschema")
    from .test_aibom import assert_valid

    assert_valid(bom)
