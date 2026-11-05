"""Savings and sovereign-share reports (#26) against a real PostgreSQL
database (PS_TEST_DATABASE_URL), through the full app: DB middleware, JWT
auth, scopes, rate limit, audit and, in one test, row-level security.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from collector import rls
from collector.app import app
from collector.partner_auth import hash_secret, issue_access_token
from collector.usage_reports import EEA_COUNTRIES, SOVEREIGN_JURISDICTIONS
from db.models import AIAsset, AIUsageEvent, PartnerAuditLog, PartnerCredential, Tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

D = Decimal
PARTNER_KEY_A = "ps-pk-usage-reports-a"


async def _seed(engine, admin_info=None):
    """Tenant A (two assets) and tenant B. Returns ids and tokens."""
    # Fixed to midday so every event in "today" falls in the same UTC day.
    now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    kw = {"info": admin_info} if admin_info else {}
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, **kw)
    async with factory() as s:
        ta = Tenant(name="Tenant A", domain="a.example", settings={})
        tb = Tenant(name="Tenant B", domain="b.example", settings={})
        s.add_all([ta, tb])
        await s.flush()
        support = AIAsset(
            tenant_id=ta.id, vendor="openai", model="gpt-4o", discovery_source=["sdk"],
            use_case_name="support", business_unit="ops",
        )
        legal = AIAsset(
            tenant_id=ta.id, vendor="mistral", model="large", discovery_source=["sdk"],
            use_case_name="contracts", business_unit=None,
        )
        other = AIAsset(
            tenant_id=tb.id, vendor="openai", model="gpt-4o", discovery_source=["sdk"],
            use_case_name="support", business_unit="ops",
        )
        s.add_all([support, legal, other])
        await s.flush()

        def ev(tenant, asset, age, **fields):
            return AIUsageEvent(
                tenant_id=tenant.id, asset_id=asset.id, timestamp=now - age,
                source="gateway", **fields,
            )

        s.add_all([
            # Routed, both costs: saved 0.030 - 0.010 = 0.020. Sovereign.
            ev(ta, support, timedelta(0), cost=D("0.010"), counterfactual_cost=D("0.030"),
               served_jurisdiction="EEA", sensitivity_level="confidential"),
            # Routed, both costs: saved 0.008. Served in the US.
            ev(ta, support, timedelta(0), cost=D("0.002"), counterfactual_cost=D("0.010"),
               served_jurisdiction="US", sensitivity_level="public"),
            # Cost but no counterfactual: in totals, not in savings.
            ev(ta, legal, timedelta(days=1), cost=D("0.100"),
               served_jurisdiction="SE", sensitivity_level="confidential"),
            # Counterfactual but no cost: not in savings either.
            ev(ta, legal, timedelta(days=1), counterfactual_cost=D("5.000"),
               served_jurisdiction="FR"),
            # Nothing reported.
            ev(ta, legal, timedelta(days=2)),
            # Outside 7d: routing cost more than the baseline.
            ev(ta, support, timedelta(days=10), cost=D("0.500"),
               counterfactual_cost=D("0.400"), served_jurisdiction="US",
               sensitivity_level="public"),
            # Tenant B: must never appear in tenant A's numbers.
            ev(tb, other, timedelta(0), cost=D("9.000"), counterfactual_cost=D("99.000"),
               served_jurisdiction="EEA", sensitivity_level="restricted"),
        ])

        pa = PartnerCredential(
            tenant_id=ta.id, partner_name="pa", client_id="pa-client",
            client_secret_hash=hash_secret("x"), api_key_hash=hash_secret(PARTNER_KEY_A),
            scopes=["registry:read"], is_active="true",
        )
        narrow = PartnerCredential(
            tenant_id=ta.id, partner_name="narrow", client_id="narrow-client",
            client_secret_hash=hash_secret("x"), scopes=["assets:read"],
        )
        s.add_all([pa, narrow])
        await s.commit()
        token_a, _ = issue_access_token(ta.id, pa.id, "pa-client", ["registry:read"])
        token_narrow, _ = issue_access_token(ta.id, narrow.id, "narrow-client", ["assets:read"])
        return {
            "tenant_a": ta.id, "support": support.id, "legal": legal.id, "partner_a": pa.id,
            "token_a": token_a, "token_narrow": token_narrow, "now": now,
        }


async def _get(path, token):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get(path, headers={"Authorization": f"Bearer {token}"})


@pytest.mark.asyncio
async def test_savings_totals_and_null_counterfactual_handling(seeded_db):
    seed = await _seed(seeded_db)
    r7 = await _get("/partner/v1/usage/savings?period=7d", seed["token_a"])
    r30 = await _get("/partner/v1/usage/savings", seed["token_a"])
    assert r7.status_code == 200, r7.text
    assert r7.json() == {
        "period": "7d",
        "group_by": "none",
        "total": {
            "total_requests": 5,
            "estimated_cost_usd": 0.112,
            "requests_with_counterfactual": 2,
            "counterfactual_coverage": 0.4,
            "compared_cost_usd": 0.012,
            "counterfactual_cost_usd": 0.04,
            "savings_usd": 0.028,
            "savings_rate": 0.7,
        },
        "data": [],
    }
    t30 = r30.json()["total"]
    assert (t30["total_requests"], t30["requests_with_counterfactual"]) == (6, 3)
    # 0.028 saved inside 7d, then a call that cost 0.1 more than its baseline.
    assert t30["savings_usd"] == pytest.approx(-0.072)


@pytest.mark.asyncio
async def test_savings_group_by(seeded_db):
    seed = await _seed(seeded_db)

    async def data(group_by):
        r = await _get(f"/partner/v1/usage/savings?period=30d&group_by={group_by}", seed["token_a"])
        assert r.status_code == 200, r.text
        assert r.json()["group_by"] == group_by
        return {row["key"]: row for row in r.json()["data"]}

    by_uc = await data("use_case")
    assert set(by_uc) == {"support", "contracts"}  # tenant B's "support" is not merged in
    assert by_uc["support"]["total_requests"] == 3
    assert by_uc["support"]["savings_usd"] == pytest.approx(-0.072)
    assert by_uc["contracts"]["savings_usd"] is None
    assert by_uc["contracts"]["requests_with_counterfactual"] == 0
    assert by_uc["contracts"]["counterfactual_coverage"] == 0.0
    assert by_uc["contracts"]["estimated_cost_usd"] == 0.1

    by_bu = await data("business_unit")
    assert set(by_bu) == {"ops", None}
    assert by_bu[None]["total_requests"] == 3

    by_asset = await data("asset")
    assert set(by_asset) == {str(seed["support"]), str(seed["legal"])}

    by_day = await data("day")
    today = seed["now"].date().isoformat()
    assert by_day[today]["savings_usd"] == 0.028
    assert sum(r["total_requests"] for r in by_day.values()) == 6

    by_month = await data("month")
    assert all(k.endswith("-01") for k in by_month)


@pytest.mark.asyncio
async def test_sovereign_share_by_sensitivity_level(seeded_db):
    seed = await _seed(seeded_db)
    r = await _get("/partner/v1/usage/sovereign-share?period=7d", seed["token_a"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["period"] == "7d"
    assert body["sovereign_jurisdictions"] == sorted(SOVEREIGN_JURISDICTIONS)
    # Four events report a jurisdiction (EEA, US, SE, FR); three are sovereign.
    # Spend with a jurisdiction: 0.010 + 0.002 + 0.100; sovereign 0.110.
    assert body["total"] == {
        "total_requests": 5,
        "requests_with_jurisdiction": 4,
        "sovereign_requests": 3,
        "request_share": 0.75,
        "cost_with_jurisdiction_usd": 0.112,
        "sovereign_cost_usd": 0.11,
        "spend_share": 0.9821,
    }
    levels = {row["sensitivity_level"]: row for row in body["by_sensitivity_level"]}
    assert set(levels) == {"confidential", "public", None}
    assert levels["confidential"]["request_share"] == 1.0
    assert levels["confidential"]["spend_share"] == 1.0
    assert levels["public"]["request_share"] == 0.0
    assert levels["public"]["sovereign_cost_usd"] == 0.0
    assert levels["public"]["spend_share"] == 0.0
    # FR with no cost, plus one event that reported nothing.
    assert levels[None]["requests_with_jurisdiction"] == 1
    assert levels[None]["spend_share"] is None
    assert "restricted" not in levels  # tenant B only


@pytest.mark.asyncio
async def test_usage_summary_reports_sovereign_share(seeded_db):
    seed = await _seed(seeded_db)
    r = await _get(f"/partner/v1/assets/{seed['legal']}/usage-summary?period=7d", seed["token_a"])
    assert r.status_code == 200, r.text
    assert r.json()["sovereign_share"] == 1.0  # SE and FR; the third reported none
    r = await _get(f"/partner/v1/assets/{seed['support']}/usage-summary", seed["token_a"])
    assert r.json()["sovereign_share"] == round(1 / 3, 4)


@pytest.mark.asyncio
async def test_reports_need_usage_read_and_are_audited(seeded_db):
    seed = await _seed(seeded_db)
    for path in ("/partner/v1/usage/savings", "/partner/v1/usage/sovereign-share"):
        assert (await _get(path, seed["token_narrow"])).status_code == 403
        assert (await _get(path, seed["token_a"])).status_code == 200
    assert (await _get("/partner/v1/usage/savings?group_by=model", seed["token_a"])).status_code == 422

    async with async_sessionmaker(seeded_db, class_=AsyncSession)() as s:
        endpoints = (
            await s.execute(
                select(PartnerAuditLog.endpoint).where(
                    PartnerAuditLog.partner_id == seed["partner_a"],
                    PartnerAuditLog.status_code == 200,
                )
            )
        ).scalars().all()
    assert sorted(endpoints) == [
        "/partner/v1/usage/savings",
        "/partner/v1/usage/sovereign-share",
    ]


@pytest.mark.asyncio
async def test_reports_are_clamped_to_retention(seeded_db):
    seed = await _seed(seeded_db)
    async with async_sessionmaker(seeded_db, class_=AsyncSession)() as s:
        tenant = await s.get(Tenant, seed["tenant_a"])
        tenant.settings = {"retention_days": 5}
        await s.commit()
    r = await _get("/partner/v1/usage/savings?period=90d", seed["token_a"])
    assert r.json()["total"]["total_requests"] == 5  # the 10-day-old event is out


@pytest.mark.asyncio
async def test_reports_work_under_rls(seeded_db):
    seed = await _seed(seeded_db, admin_info=rls.bypass_info("test-seed"))
    async with seeded_db.begin() as conn:
        await rls.enable(conn)
    savings = await _get("/partner/v1/usage/savings?period=7d&group_by=use_case", PARTNER_KEY_A)
    share = await _get("/partner/v1/usage/sovereign-share?period=7d", PARTNER_KEY_A)
    assert savings.status_code == 200, savings.text
    assert savings.json()["total"]["savings_usd"] == 0.028
    assert {r["key"] for r in savings.json()["data"]} == {"support", "contracts"}
    assert share.status_code == 200, share.text
    assert share.json()["total"]["request_share"] == 0.75


def test_eea_set_matches_the_sdk_route_policy():
    sdk = Path(__file__).resolve().parents[2] / "sdk"
    sys.path.insert(0, str(sdk))
    try:
        route_policy = pytest.importorskip("prompt_shields.route_policy")
    finally:
        sys.path.remove(str(sdk))
    assert route_policy.EEA_COUNTRIES == EEA_COUNTRIES
