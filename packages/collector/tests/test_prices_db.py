"""Negotiated prices and monthly cost reconciliation (#348) against
PostgreSQL (PS_TEST_DATABASE_URL).

Covers: an override from the tenant's active policy applies at ingest only
to calls on or after ``valid_from``; only tenant-scope policies may carry
``prices``; re-pricing stored events; the reconciliation flags a seeded 10%
gap and ignores a 2% gap; reruns replace; the Partner API and its scopes.
Row-level security is covered in test_rls_db.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from collector import cost_reconciliation, prices, rls
from collector.admin_auth import ADMIN_SCOPES
from collector.app import app
from collector.config import settings
from collector.partner_auth import hash_secret, issue_access_token
from db.models import (
    AIAsset,
    AIUsageEvent,
    CostReconciliation,
    PartnerCredential,
    Policy,
    PolicyVersion,
    Team,
    Tenant,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.scoped_admin_default

KEY = "ps-test-key-12345"
OP_TOKEN = "op-secret-token"
MONTH = date(2026, 10, 1)
NOW = datetime(2026, 11, 2, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", OP_TOKEN)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 0)


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _factory(engine):
    return async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, info=rls.bypass_info("test")
    )


async def _tenant(factory) -> uuid.UUID:
    async with factory() as s:
        return (await s.execute(select(Tenant.id))).scalar_one()


async def _set_prices(factory, tenant_id, section: dict, scope="tenant", team_id=None) -> None:
    async with factory() as s:
        p = Policy(
            tenant_id=tenant_id,
            name=f"p-{uuid.uuid4().hex[:6]}",
            scope=scope,
            team_id=team_id,
            created_by="test",
            active_version=1,
        )
        s.add(p)
        await s.flush()
        doc = {"schema": "ps-policy.v1", "sections": {"prices": {"version": 1, **section}}}
        s.add(
            PolicyVersion(
                tenant_id=tenant_id,
                policy_id=p.id,
                version=1,
                document=doc,
                sha256="0" * 64,
                created_by="test",
                created_at=NOW,
            )
        )
        await s.commit()


# ──────────────────────────────────────────────────────────────────────
# Ingest


async def test_override_applies_at_ingest_only_on_or_after_valid_from(seeded_db):
    factory = _factory(seeded_db)
    tid = await _tenant(factory)
    today = datetime.now(UTC).date()
    await _set_prices(
        factory,
        tid,
        {
            "overrides": [
                {
                    "provider": "openai",
                    "model": "gpt-4o",
                    "valid_from": (today - timedelta(days=1)).isoformat(),
                    "input_per_1k": 0.001,
                    "output_per_1k": 0.004,
                },
                # Not yet in force.
                {
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "valid_from": (today + timedelta(days=2)).isoformat(),
                    "multiplier": 0.5,
                },
            ]
        },
    )
    events = [
        {"vendor": "openai", "model": "gpt-4o", "tokens_in": 1000, "tokens_out": 1000,
         "cost": 0.0125, "cost_source": "pricing_table", "session_id": "neg"},
        {"vendor": "openai", "model": "gpt-4o-mini", "tokens_in": 1000, "tokens_out": 1000,
         "cost": 0.00075, "cost_source": "pricing_table", "session_id": "future"},
        {"vendor": "openai", "model": "gpt-4o", "tokens_in": 1000, "tokens_out": 1000,
         "cost": 0.02, "cost_source": "provider_reported", "session_id": "billed"},
    ]
    async with _client() as c:
        r = await c.post("/ingest/events", headers={"Authorization": f"Bearer {KEY}"},
                         json={"events": events})
    assert r.status_code == 200, r.text
    async with factory() as s:
        rows = {
            e.session_id: (e.cost, e.cost_source)
            for e in (await s.execute(select(AIUsageEvent))).scalars()
        }
    assert rows["neg"] == (Decimal("0.005000"), "negotiated")
    assert rows["future"] == (Decimal("0.000750"), "pricing_table")
    assert rows["billed"] == (Decimal("0.020000"), "provider_reported")


async def test_only_tenant_policies_take_prices_and_team_prices_are_ignored(seeded_db):
    factory = _factory(seeded_db)
    tid = await _tenant(factory)
    async with factory() as s:
        team = Team(tenant_id=tid, name="eng")
        s.add(team)
        await s.commit()
    section = {"version": 1, "overrides": []}
    async with _client() as c:
        boot = await c.post(
            f"/api/v1/operator/tenants/{tid}/admin-tokens",
            headers={"Authorization": f"Bearer {OP_TOKEN}"},
            json={"name": "boot", "scopes": list(ADMIN_SCOPES)},
        )
        assert boot.status_code == 201, boot.text
        h = {"Authorization": f"Bearer {boot.json()['token']}"}
        doc = {"schema": "ps-policy.v1", "sections": {"prices": section}}
        bad = await c.post("/api/v1/admin/policies", headers=h, json={
            "name": "team", "scope": "team", "team_id": str(team.id), "document": doc})
        assert bad.status_code == 422
        assert "only a tenant-scope" in str(bad.json())
        ok = await c.post("/api/v1/admin/policies", headers=h, json={
            "name": "tenant", "scope": "tenant", "document": doc, "activate": True})
        assert ok.status_code == 201, ok.text
        # A team policy cannot get a prices section through a new version either.
        team_p = await c.post("/api/v1/admin/policies", headers=h, json={
            "name": "team", "scope": "team", "team_id": str(team.id)})
        assert team_p.status_code == 201, team_p.text
        bad = await c.post(f"/api/v1/admin/policies/{team_p.json()['id']}/versions",
                           headers=h, json={"document": doc})
        assert bad.status_code == 422


async def test_reprice_stored_events_and_rebuild(seeded_db):
    factory = _factory(seeded_db)
    tid = await _tenant(factory)
    async with factory() as s:
        asset = AIAsset(tenant_id=tid, vendor="anthropic", model="claude-x",
                        discovery_source=["sdk"])
        s.add(asset)
        await s.flush()
        s.add_all([
            AIUsageEvent(tenant_id=tid, asset_id=asset.id, source="sdk", session_id="old",
                         timestamp=datetime(2026, 9, 30, tzinfo=UTC), cost=Decimal(1),
                         cost_source="pricing_table"),
            AIUsageEvent(tenant_id=tid, asset_id=asset.id, source="sdk", session_id="in",
                         timestamp=datetime(2026, 10, 3, tzinfo=UTC), cost=Decimal(1),
                         cost_source="pricing_table"),
            AIUsageEvent(tenant_id=tid, asset_id=asset.id, source="sdk", session_id="done",
                         timestamp=datetime(2026, 10, 3, tzinfo=UTC), cost=Decimal("0.5"),
                         cost_source="negotiated"),
        ])
        await s.commit()
    await _set_prices(factory, tid, {"overrides": [
        {"provider": "anthropic", "model": "*", "valid_from": "2026-10-01", "multiplier": 0.8}]})
    result = await prices.reprice_and_rebuild(
        factory, tid, datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC))
    assert result["events_repriced"] == 1 and result["rollups"] is not None
    async with factory() as s:
        rows = {e.session_id: e.cost for e in (await s.execute(select(AIUsageEvent))).scalars()}
    # Before valid_from: kept; in the window: discounted; already negotiated: kept.
    assert rows == {"old": Decimal("1.000000"), "in": Decimal("0.800000"),
                    "done": Decimal("0.500000")}


# ──────────────────────────────────────────────────────────────────────
# Reconciliation


async def _seed_usage(factory, tid):
    """October: gpt-4o reported 10% above the provider, gpt-4o-mini 2%,
    an unpriced model 50% in tokens, Anthropic with no provider pull."""
    async with factory() as s:
        def asset(vendor, model, source):
            a = AIAsset(tenant_id=tid, vendor=vendor, model=model, discovery_source=[source])
            s.add(a)
            return a

        sdk_4o = asset("openai", "gpt-4o", "sdk")
        sdk_mini = asset("openai", "gpt-4o-mini", "sdk")
        sdk_odd = asset("openai", "odd-model", "sdk")
        pull_4o = asset("openai", "gpt-4o-2024-08-06", "platform_signal")
        pull_mini = asset("openai", "gpt-4o-mini", "platform_signal")
        pull_odd = asset("openai", "odd-model", "platform_signal")
        claude = asset("anthropic", "claude-sonnet-4", "sdk")
        await s.flush()

        def ev(a, day, tin, tout, source="sdk", cost=None, served=None):
            return AIUsageEvent(tenant_id=tid, asset_id=a.id, source=source,
                                timestamp=datetime(2026, 10, day, 12, tzinfo=UTC),
                                tokens_in=tin, tokens_out=tout, served_model=served,
                                cost=None if cost is None else Decimal(str(cost)))

        s.add_all([
            # gpt-4o: 1.1M/110k reported vs 1M/100k pulled -> +10%.
            ev(sdk_4o, 3, 550_000, 55_000, cost=2.0, served="gpt-4o-2024-08-06"),
            ev(sdk_4o, 4, 550_000, 55_000, cost=2.0),
            ev(pull_4o, 3, 500_000, 50_000, source="platform_signal"),
            ev(pull_4o, 4, 500_000, 50_000, source="platform_signal"),
            # gpt-4o-mini: 10.2M/1.02M vs 10M/1M -> +2%.
            ev(sdk_mini, 5, 10_200_000, 1_020_000, cost=2.1),
            ev(pull_mini, 5, 10_000_000, 1_000_000, source="platform_signal"),
            # odd-model (no list price): 300k vs 200k tokens -> +50%, tokens basis.
            ev(sdk_odd, 6, 200_000, 100_000),
            ev(pull_odd, 6, 150_000, 50_000, source="platform_signal"),
            ev(claude, 7, 1_000_000, 1_000_000, cost=18.0),
            # Outside the month.
            AIUsageEvent(tenant_id=tid, asset_id=pull_4o.id, source="platform_signal",
                         timestamp=datetime(2026, 11, 1, 0, 30, tzinfo=UTC),
                         tokens_in=9_000_000, tokens_out=0),
        ])
        await s.commit()


async def _rows(factory, tid) -> dict[tuple[str, str], CostReconciliation]:
    async with factory() as s:
        rows = (await s.execute(select(CostReconciliation).where(
            CostReconciliation.tenant_id == tid))).scalars().all()
    return {(r.provider, r.model): r for r in rows}


async def test_reconciliation_flags_a_10_percent_gap_and_ignores_2_percent(seeded_db):
    factory = _factory(seeded_db)
    tid = await _tenant(factory)
    await _seed_usage(factory, tid)
    async with factory() as s:
        run = await cost_reconciliation.reconcile_month(s, tid, MONTH, now=NOW)
    assert run.providers == {"openai": "ok", "anthropic": "no_provider_data"}
    rows = await _rows(factory, tid)
    assert set(rows) == {("openai", "gpt-4o"), ("openai", "gpt-4o-mini"), ("openai", "odd-model")}
    big = rows[("openai", "gpt-4o")]
    assert big.basis == "price" and big.flagged
    assert big.variance_pct == pytest.approx(10.0)
    # List price 0.0025 / 0.01 per 1k: 1M in + 100k out = $3.50.
    assert float(big.provider_value) == pytest.approx(3.5)
    assert (big.estimated_tokens_in, big.provider_tokens_in) == (1_100_000, 1_000_000)
    assert float(big.estimated_cost) == 4.0
    small = rows[("openai", "gpt-4o-mini")]
    assert small.variance_pct == pytest.approx(2.0) and not small.flagged
    odd = rows[("openai", "odd-model")]
    assert odd.basis == "tokens" and odd.variance_pct == pytest.approx(50.0) and odd.flagged
    assert run.flagged == 2


async def test_negotiated_rates_value_both_sides_and_reruns_replace(seeded_db):
    factory = _factory(seeded_db)
    tid = await _tenant(factory)
    await _seed_usage(factory, tid)
    await _set_prices(factory, tid, {"overrides": [
        {"provider": "openai", "model": "odd-model", "valid_from": "2026-10-01",
         "input_per_1k": 0.001, "output_per_1k": 0.001}]})
    async with factory() as s:
        await s.execute(
            Tenant.__table__.update().where(Tenant.id == tid).values(
                settings={"cost_reconciliation": {"threshold_pct": 60, "min_usd": 0}}))
        await s.commit()
    async with factory() as s:
        await cost_reconciliation.reconcile_month(s, tid, MONTH, now=NOW)
    async with factory() as s:
        run = await cost_reconciliation.reconcile_month(s, tid, MONTH, now=NOW)
    rows = await _rows(factory, tid)
    assert len(rows) == 3 and run.flagged == 0
    odd = rows[("openai", "odd-model")]
    assert odd.basis == "price" and float(odd.provider_value) == pytest.approx(0.2)
    assert odd.threshold_pct == 60


async def test_future_month_is_refused_and_disabled_tenants_skip(seeded_db):
    factory = _factory(seeded_db)
    tid = await _tenant(factory)
    async with factory() as s:
        with pytest.raises(ValueError):
            await cost_reconciliation.reconcile_month(s, tid, date(2026, 12, 1), now=NOW)
        run = await cost_reconciliation.reconcile_month(
            s, tid, MONTH, now=NOW, cfg=cost_reconciliation.ReconciliationConfig(enabled=False))
    assert run.skipped == "disabled"


async def test_partner_api_lists_and_runs_with_scopes(seeded_db):
    factory = _factory(seeded_db)
    tid = await _tenant(factory)
    await _seed_usage(factory, tid)

    async with factory() as s:
        def partner(name, scopes):
            p = PartnerCredential(tenant_id=tid, partner_name=name, client_id=f"{name}-c",
                                  client_secret_hash=hash_secret("x"), scopes=scopes,
                                  is_active="true")
            s.add(p)
            return p

        reader = partner("reader", ["usage:read"])
        runner = partner("runner", ["usage:read", "usage:reconcile"])
        await s.commit()

    def h(p):
        token, _ = issue_access_token(tid, p.id, p.client_id, p.scopes)
        return {"Authorization": f"Bearer {token}"}

    async with _client() as c:
        denied = await c.post("/partner/v1/cost-reconciliations/run", headers=h(reader),
                              json={"month": "2026-10"})
        assert denied.status_code == 403
        bad = await c.post("/partner/v1/cost-reconciliations/run", headers=h(runner),
                           json={"month": "2026-13"})
        assert bad.status_code == 422
        ran = await c.post("/partner/v1/cost-reconciliations/run", headers=h(runner),
                           json={"month": "2026-10"})
        assert ran.status_code == 200, ran.text
        assert ran.json()["flagged"] == 2 and len(ran.json()["data"]) == 3
        listed = await c.get("/partner/v1/cost-reconciliations?month=2026-10&flagged=true",
                             headers=h(reader))
        assert listed.status_code == 200, listed.text
        assert sorted(x["model"] for x in listed.json()["data"]) == ["gpt-4o", "odd-model"]
