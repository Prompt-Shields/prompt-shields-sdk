"""Routing, sovereignty and savings fields (PRD 0001 §7.12, #25): stored on
ai_usage_events instead of landing in ignored_fields, validated leniently
(a bad value costs that field, never the batch), and summarised by the
Partner API usage summary.

GATEWAY_EVENT mirrors gateway/src/middlewares/ps-telemetry.ts `psTelemetry`
for a routed call that fell back once.
"""

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest
from collector.app import app
from collector.ingest import ROUTE_FIELDS, IngestEvent
from db.models import AIUsageEvent
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

HEADERS = {"Authorization": "Bearer ps-test-key-12345"}

GATEWAY_EVENT = {
    "vendor": "openai",
    "model": "auto",
    "source": "gateway",
    "api_surface": "chat",
    "modality": "text",
    "tokens_in": 10,
    "tokens_out": 20,
    "cost": 0.0004,
    "requested_model": "auto",
    "served_model": "gpt-4o-mini",
    "route_mode": "gateway",
    "route_group": "cheap",
    "route_reason": "heuristic:simple+budget-clamp",
    "route_est_cost": 0.0005,
    "trace_id": "trace-1",
    "retry_count": 1,
    "attempts": [
        {"attempt": 1, "status": 429, "latency_ms": 5, "error_class": "rate_limited"},
        {"attempt": 2, "status": 200, "latency_ms": 9},
    ],
    "fallback_chain": ["rate_limited"],
}

# The §7.12 fields no client sends yet (P1 sensitivity-aware routing).
POLICY_FIELDS = {
    "sensitivity_level": "confidential",
    "sensitivity_source": "pii_escalated",
    "served_provider": "mistral-ai",
    "served_jurisdiction": "EEA",
    "policy_version": "nordic-sovereign@3",
    "counterfactual_cost": 0.0123,
    "baseline_id": "gpt-4o",
}


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
async def test_gateway_and_policy_fields_round_trip(seeded_db):
    resp, loss = await _ingest([{**GATEWAY_EVENT, **POLICY_FIELDS}])
    assert resp.status_code == 200, resp.text

    (ev,) = await _events(seeded_db)
    assert ev.api_surface == "chat"
    assert (ev.route_mode, ev.route_group, ev.route_reason) == (
        "gateway",
        "cheap",
        "heuristic:simple+budget-clamp",
    )
    assert ev.route_est_cost == Decimal("0.0005")
    assert ev.fallback_chain == ["rate_limited"]
    assert ev.sensitivity_level == "confidential"
    assert ev.sensitivity_source == "pii_escalated"
    assert ev.served_provider == "mistral-ai"
    assert ev.served_jurisdiction == "EEA"
    assert ev.policy_version == "nordic-sovereign@3"
    assert ev.counterfactual_cost == Decimal("0.0123")
    assert ev.baseline_id == "gpt-4o"

    # Only the per-attempt detail (#78) is still out of scope.
    (stats,) = loss["clients"]
    assert set(stats["ignored_fields"]) == {"trace_id", "retry_count", "attempts"}


@pytest.mark.asyncio
async def test_old_event_stores_nulls(seeded_db):
    resp, _ = await _ingest([{"vendor": "openai", "model": "gpt-4o"}])
    assert resp.status_code == 200, resp.text
    (ev,) = await _events(seeded_db)
    assert all(getattr(ev, f) is None for f in ROUTE_FIELDS)

    # An absent fallback_chain is SQL NULL, not JSON null.
    async with async_sessionmaker(seeded_db, class_=AsyncSession)() as s:
        n = await s.scalar(
            select(func.count()).where(AIUsageEvent.fallback_chain.is_(None))
        )
    assert n == 1


@pytest.mark.asyncio
async def test_malformed_fields_are_dropped_and_counted(seeded_db):
    bad = {
        "vendor": "openai",
        "model": "gpt-4o",
        "api_surface": "a" * 65,
        "route_mode": "learned",
        "route_group": 7,
        "route_reason": "r" * 257,
        "route_est_cost": -1,
        "sensitivity_level": ["secret"],
        "sensitivity_source": "guessed",
        "served_provider": " ",
        "served_jurisdiction": "j" * 65,
        "policy_version": "p" * 129,
        # An error message is not an error class: the whole chain is dropped.
        "fallback_chain": ["rate_limited", "429 Too Many Requests for key sk-..."],
        "counterfactual_cost": 1e6,
        "baseline_id": {"id": 1},
    }
    good = {"vendor": "openai", "model": "gpt-4o", "route_mode": " Local "}
    resp, loss = await _ingest([bad, good])
    assert resp.status_code == 200, resp.text

    (stats,) = loss["clients"]
    invalid = {k for k in stats["ignored_fields"] if k.endswith(":invalid")}
    assert invalid == {f"{f}:invalid" for f in ROUTE_FIELDS}

    events = await _events(seeded_db)
    dropped = next(e for e in events if e.route_mode is None)
    assert all(getattr(dropped, f) is None for f in ROUTE_FIELDS)
    assert next(e for e in events if e.route_mode).route_mode == "local"


@pytest.mark.asyncio
async def test_nul_in_a_route_field_costs_only_that_field(seeded_db):
    event = {
        **GATEWAY_EVENT,
        "route_reason": "heuristic\x00simple",
        "fallback_chain": ["rate\x00limited"],
    }
    resp, loss = await _ingest([event])
    assert resp.status_code == 200, resp.text
    assert resp.json()["rejected"] == 0

    (ev,) = await _events(seeded_db)
    assert ev.route_reason is None and ev.fallback_chain is None
    assert ev.route_group == "cheap"
    (stats,) = loss["clients"]
    assert stats["ignored_fields"]["route_reason:invalid"] == 1
    assert stats["ignored_fields"]["fallback_chain:invalid"] == 1


def test_fallback_chain_validation():
    def chain(value):
        return IngestEvent(vendor="v", fallback_chain=value).fallback_chain

    assert chain([]) == []
    assert chain(["server_error", "timeout"]) == ["server_error", "timeout"]
    assert chain(["rate_limited"] * 32) == ["rate_limited"] * 32
    for bad in (
        ["rate_limited"] * 33,
        ["Rate_Limited"],
        ["x" * 65],
        ["server error"],
        [""],
        [429],
        "rate_limited",
        {"0": "rate_limited"},
    ):
        assert chain(bad) is None, bad


def test_choice_fields():
    for value in ("gateway", "LOCAL", " none "):
        assert (
            IngestEvent(vendor="v", route_mode=value).route_mode
            == value.strip().lower()
        )
    for value in ("declared", "pii_escalated", "Default"):
        ev = IngestEvent(vendor="v", sensitivity_source=value)
        assert ev.sensitivity_source == value.lower()
    assert IngestEvent(vendor="v", route_mode="auto").route_mode is None
    assert IngestEvent(vendor="v", sensitivity_source="pii").sensitivity_source is None


@pytest.mark.asyncio
async def test_usage_summary_reports_savings_and_fallback_rate(seeded_db):
    events = [
        {**GATEWAY_EVENT, "cost": 0.004, "counterfactual_cost": 0.010},
        {**GATEWAY_EVENT, "cost": 0.002, "fallback_chain": []},
        # No cost reported, so no savings claimed.
        {
            **GATEWAY_EVENT,
            "cost": None,
            "fallback_chain": None,
            "counterfactual_cost": 0.5,
        },
        {**GATEWAY_EVENT, "fallback_chain": ["server_error", "timeout"]},
    ]
    resp, _ = await _ingest(events)
    assert resp.status_code == 200, resp.text

    from collector.partner_auth import hash_secret, issue_access_token
    from db.models import PartnerCredential, Tenant

    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
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

    async with _client() as c:
        r = await c.get(
            f"/partner/v1/assets/{asset_id}/usage-summary",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    # Only the first event reported both costs: 0.010 - 0.004.
    assert body["savings_usd"] == 0.006
    # Three events reported a chain; two of them were not empty.
    assert body["fallback_rate"] == round(2 / 3, 4)


# ──────────────────────────────────────────────────────────────────────
# Migration 023 (route fields)
# ──────────────────────────────────────────────────────────────────────


def _load_migration():
    pytest.importorskip("alembic")
    versions = Path(__file__).resolve().parents[2] / "db" / "alembic" / "versions"
    (path,) = versions.glob("*_route_fields.py")
    spec = importlib.util.spec_from_file_location("migration_route_fields", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_adds_the_ingested_fields():
    m = _load_migration()
    # The migration adds exactly the columns ingest stores.
    assert [name for name, _ in m.ROUTE_EVENT_COLUMNS] == list(ROUTE_FIELDS)
    assert set(ROUTE_FIELDS) <= set(AIUsageEvent.__table__.columns.keys())


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

    # seeded_db is at head (create_all), so the columns exist.
    async with seeded_db.begin() as conn:
        await conn.run_sync(run, m.downgrade)
        cols = await conn.run_sync(_columns, "ai_usage_events")
    assert not set(ROUTE_FIELDS) & set(cols)
    assert {"served_model", "api_key_fingerprint"} <= set(cols)

    async with seeded_db.begin() as conn:
        await conn.run_sync(run, m.upgrade)
        cols = await conn.run_sync(_columns, "ai_usage_events")
    assert all(cols[f]["nullable"] for f in ROUTE_FIELDS)

    resp, _ = await _ingest([{**GATEWAY_EVENT, **POLICY_FIELDS}])
    assert resp.status_code == 200, resp.text
