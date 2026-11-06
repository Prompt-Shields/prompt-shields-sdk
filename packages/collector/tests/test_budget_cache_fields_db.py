"""Spend budget (#28 gateway, #29 SDK) and gateway cache (#33) fields:
stored on ai_usage_events instead of landing in ignored_fields, validated
leniently (a bad value costs that field, never the batch), and summarised by
the Partner API usage summary.

BUDGET_EVENT mirrors the gateway's psTelemetry event for a call a downgrade
budget moved to a cheaper model (schemas/budget/README.md); CACHE_HIT mirrors
a semantic cache hit (withCacheHit: cost 0, cost_source cache).
"""

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest
from collector.app import app
from collector.ingest import BUDGET_CACHE_FIELDS, IngestEvent
from db.models import AIUsageEvent
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

HEADERS = {"Authorization": "Bearer ps-test-key-12345"}

BUDGET_EVENT = {
    "vendor": "openai",
    "model": "gpt-4o-mini",
    "source": "gateway",
    "cost": 0.0004,
    "route_mode": "gateway",
    "route_reason": "heuristic:simple+budget-downgrade",
    "budget_state": "exceeded",
    "budget_id": "mkt-monthly",
    "budget_action": "downgraded",
}

CACHE_HIT = {
    "vendor": "openai",
    "model": "gpt-4o-mini",
    "source": "gateway",
    "cost": 0,
    "cost_source": "cache",
    "cache_status": "SEMANTIC HIT",
    "cache_saved_cost": 0.0021,
}

# The cache-only event the gateway sends for every cache lookup
# (buildCacheEvent): no cost and no cost_source; est_tokens_saved is its
# estimate of the prompt tokens the lookup saved (0 on a miss).
CACHE_LOOKUP = {
    "vendor": "openai",
    "model": "gpt-4o-mini",
    "source": "gateway",
    "cache_status": "SEMANTIC HIT",
    "est_tokens_saved": 120,
}

# Migration 031 added these; 034 added est_tokens_saved.
MIGRATION_031_FIELDS = tuple(f for f in BUDGET_CACHE_FIELDS if f != "est_tokens_saved")


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _ingest(events):
    async with _client() as c:
        resp = await c.post("/ingest/events", headers=HEADERS, json={"events": events})
        loss = (await c.get("/telemetry/loss", headers=HEADERS)).json()
    return resp, loss


async def _events(engine):
    async with async_sessionmaker(engine, class_=AsyncSession)() as s:
        return (await s.execute(select(AIUsageEvent))).scalars().all()


@pytest.mark.asyncio
async def test_budget_and_cache_fields_round_trip(seeded_db):
    resp, loss = await _ingest([BUDGET_EVENT, CACHE_HIT, CACHE_LOOKUP])
    assert resp.status_code == 200, resp.text

    events = await _events(seeded_db)
    assert next(e for e in events if e.est_tokens_saved).est_tokens_saved == 120
    budget = next(e for e in events if e.budget_id)
    assert (budget.budget_state, budget.budget_id, budget.budget_action) == (
        "exceeded",
        "mkt-monthly",
        "downgraded",
    )
    assert budget.cache_status is None and budget.cache_saved_cost is None
    hit = next(e for e in events if e.cost_source == "cache")
    assert hit.est_tokens_saved is None
    assert hit.cache_status == "semantic hit"
    assert hit.cache_saved_cost == Decimal("0.0021")
    assert hit.cost == 0 and hit.cost_source == "cache"

    # Nothing these clients send is dropped any more.
    (stats,) = loss["clients"]
    assert not stats["ignored_fields"]


@pytest.mark.asyncio
async def test_old_event_stores_nulls(seeded_db):
    resp, _ = await _ingest([{"vendor": "openai", "model": "gpt-4o"}])
    assert resp.status_code == 200, resp.text
    (ev,) = await _events(seeded_db)
    assert all(getattr(ev, f) is None for f in BUDGET_CACHE_FIELDS)


@pytest.mark.asyncio
async def test_malformed_fields_are_dropped_and_counted(seeded_db):
    bad = {
        "vendor": "openai",
        "model": "gpt-4o",
        "budget_state": "over",
        "budget_id": "Mkt Monthly",
        "budget_action": "block",
        "cache_status": "STALE",
        "cache_saved_cost": -0.5,
        "est_tokens_saved": -3,
    }
    good = {"vendor": "openai", "model": "gpt-4o", "budget_state": " WARN "}
    resp, loss = await _ingest([bad, good])
    assert resp.status_code == 200, resp.text
    assert resp.json()["rejected"] == 0

    (stats,) = loss["clients"]
    invalid = {k for k in stats["ignored_fields"] if k.endswith(":invalid")}
    assert invalid == {f"{f}:invalid" for f in BUDGET_CACHE_FIELDS}

    events = await _events(seeded_db)
    dropped = next(e for e in events if e.budget_state is None)
    assert all(getattr(dropped, f) is None for f in BUDGET_CACHE_FIELDS)
    assert next(e for e in events if e.budget_state).budget_state == "warn"


@pytest.mark.asyncio
async def test_nul_in_a_budget_field_costs_only_that_field(seeded_db):
    resp, loss = await _ingest([{**BUDGET_EVENT, "budget_id": "mkt\x00monthly"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["rejected"] == 0

    (ev,) = await _events(seeded_db)
    assert ev.budget_id is None
    assert ev.budget_state == "exceeded" and ev.budget_action == "downgraded"
    (stats,) = loss["clients"]
    assert stats["ignored_fields"]["budget_id:invalid"] == 1


def test_field_validation():
    def ev(**kw):
        return IngestEvent(vendor="v", **kw)

    for state in ("ok", "warn", "unknown", "exceeded"):
        assert ev(budget_state=state.upper()).budget_state == state
    for action in ("none", "downgraded", "blocked"):
        assert ev(budget_action=action).budget_action == action
    for status in ("HIT", "MISS", "SEMANTIC HIT", "SEMANTIC MISS", "SEMANTIC BYPASS",
                   "REFRESH", "DISABLED"):
        assert ev(cache_status=status).cache_status == status.lower()
    for good in ("support-daily", "a", "team.ml_2026"):
        assert ev(budget_id=good).budget_id == good
    for bad in ("", "-lead", "Upper", "has space", "a" * 129, 7, ["x"]):
        assert ev(budget_id=bad).budget_id is None, bad
    assert ev(budget_state=True).budget_state is None
    assert ev(cache_status="hit ").cache_status == "hit"
    assert ev(cache_saved_cost=0).cache_saved_cost == 0
    for bad in (-1, 1e6, float("nan"), "0.1", True):
        assert ev(cache_saved_cost=bad).cache_saved_cost is None, bad
    assert ev(est_tokens_saved=0).est_tokens_saved == 0
    assert ev(est_tokens_saved=250.0).est_tokens_saved == 250
    for bad in (-1, 1.5, 2**31, "12", True, None, [3]):
        assert ev(est_tokens_saved=bad).est_tokens_saved is None, bad


async def _partner_token(engine):
    from collector.partner_auth import hash_secret, issue_access_token
    from db.models import PartnerCredential, Tenant

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        tenant = (await s.execute(select(Tenant))).scalar_one()
        asset_id = (await s.execute(select(AIUsageEvent.asset_id))).scalars().first()
        cred = PartnerCredential(
            tenant_id=tenant.id,
            partner_name="p",
            client_id="p-client",
            client_secret_hash=hash_secret("x"),
            scopes=["registry:read"],
        )
        s.add(cred)
        await s.commit()
        token, _ = issue_access_token(tenant.id, cred.id, "p-client", ["registry:read"])
    return token, asset_id


async def _summary(engine) -> dict:
    token, asset_id = await _partner_token(engine)
    async with _client() as c:
        r = await c.get(
            f"/partner/v1/assets/{asset_id}/usage-summary",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_usage_summary_reports_budget_and_cache_aggregates(seeded_db):
    events = [
        BUDGET_EVENT,
        {**BUDGET_EVENT, "budget_state": "warn", "budget_action": "none"},
        {**BUDGET_EVENT, "budget_action": "blocked"},
        # The gateway sends a cache-only event for every lookup and, on a
        # hit, the call's own event (cost_source cache) with the same status.
        CACHE_LOOKUP,
        CACHE_HIT,
        {**CACHE_LOOKUP, "cache_status": "HIT"},
        {**CACHE_HIT, "cache_status": "HIT", "cache_saved_cost": 0.0009},
        # A hit whose cached response had no priced usage: counted, saves 0.
        CACHE_LOOKUP,
        {**CACHE_HIT, "cache_saved_cost": None},
        # A miss: the call's own event carries no cache status.
        {**CACHE_LOOKUP, "cache_status": "MISS", "est_tokens_saved": 0},
        {"vendor": "openai", "model": "gpt-4o-mini", "source": "gateway", "cost": 0.003},
        {**CACHE_LOOKUP, "cache_status": "SEMANTIC BYPASS", "est_tokens_saved": 0},
        # Reports neither: left out of every rate.
        {"vendor": "openai", "model": "gpt-4o-mini", "source": "gateway"},
    ]
    resp, _ = await _ingest(events)
    assert resp.status_code == 200, resp.text

    body = await _summary(seeded_db)
    assert body["budget_downgraded_requests"] == 1
    assert body["budget_blocked_requests"] == 1
    # Three hits among five lookups; the calls' own hit events are not
    # counted a second time.
    assert body["cache_hit_rate"] == 0.6
    assert body["cache_saved_usd"] == 0.003
    # Summed over the lookup events (three hits at 120, two misses at 0).
    assert body["cache_tokens_saved"] == 360


@pytest.mark.asyncio
async def test_usage_summary_nulls_without_budget_or_cache_fields(seeded_db):
    resp, _ = await _ingest([{"vendor": "openai", "model": "gpt-4o", "cost": 0.01}])
    assert resp.status_code == 200, resp.text
    body = await _summary(seeded_db)
    assert body["budget_downgraded_requests"] is None
    assert body["budget_blocked_requests"] is None
    assert body["cache_hit_rate"] is None
    assert body["cache_saved_usd"] is None
    assert body["cache_tokens_saved"] is None


@pytest.mark.asyncio
async def test_usage_summary_tokens_saved_zero_when_only_misses(seeded_db):
    resp, _ = await _ingest([{**CACHE_LOOKUP, "cache_status": "MISS", "est_tokens_saved": 0}])
    assert resp.status_code == 200, resp.text
    body = await _summary(seeded_db)
    assert body["cache_tokens_saved"] == 0
    assert body["cache_saved_usd"] is None


# ──────────────────────────────────────────────────────────────────────
# Migration (budget and cache fields)
# ──────────────────────────────────────────────────────────────────────


def _load_migration():
    pytest.importorskip("alembic")
    versions = Path(__file__).resolve().parents[2] / "db" / "alembic" / "versions"
    (path,) = versions.glob("*_telemetry_budget_cache.py")
    spec = importlib.util.spec_from_file_location("migration_budget_cache", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_adds_the_ingested_fields():
    m = _load_migration()
    assert [name for name, _ in m.BUDGET_CACHE_EVENT_COLUMNS] == list(MIGRATION_031_FIELDS)
    assert set(BUDGET_CACHE_FIELDS) <= set(AIUsageEvent.__table__.columns.keys())


def _columns(sync_conn, table):
    return {c["name"]: c for c in inspect(sync_conn).get_columns(table)}


@pytest.mark.asyncio
async def test_migration_downgrade_and_upgrade(seeded_db):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    m = _load_migration()

    def run(sync_conn, step):
        ctx = MigrationContext.configure(sync_conn)
        with Operations.context(ctx):
            step()

    async with seeded_db.begin() as conn:
        await conn.run_sync(run, m.downgrade)
        cols = await conn.run_sync(_columns, "ai_usage_events")
    assert not set(MIGRATION_031_FIELDS) & set(cols)
    assert {"route_mode", "call_id"} <= set(cols)

    async with seeded_db.begin() as conn:
        await conn.run_sync(run, m.upgrade)
        cols = await conn.run_sync(_columns, "ai_usage_events")
    assert all(cols[f]["nullable"] for f in MIGRATION_031_FIELDS)

    resp, _ = await _ingest([BUDGET_EVENT, CACHE_HIT])
    assert resp.status_code == 200, resp.text


def _load_tokens_saved_migration():
    pytest.importorskip("alembic")
    versions = Path(__file__).resolve().parents[2] / "db" / "alembic" / "versions"
    (path,) = versions.glob("*_usage_event_est_tokens_saved.py")
    spec = importlib.util.spec_from_file_location("migration_est_tokens_saved", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_tokens_saved_migration_downgrade_and_upgrade(seeded_db):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    m = _load_tokens_saved_migration()

    def run(sync_conn, step):
        ctx = MigrationContext.configure(sync_conn)
        with Operations.context(ctx):
            step()

    async with seeded_db.begin() as conn:
        await conn.run_sync(run, m.downgrade)
        cols = await conn.run_sync(_columns, "ai_usage_events")
    assert "est_tokens_saved" not in cols
    assert set(MIGRATION_031_FIELDS) <= set(cols)

    async with seeded_db.begin() as conn:
        await conn.run_sync(run, m.upgrade)
        cols = await conn.run_sync(_columns, "ai_usage_events")
    assert cols["est_tokens_saved"]["nullable"]

    resp, _ = await _ingest([CACHE_LOOKUP])
    assert resp.status_code == 200, resp.text
    (ev,) = await _events(seeded_db)
    assert ev.est_tokens_saved == 120
