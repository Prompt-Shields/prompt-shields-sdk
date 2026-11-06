"""Routing learner auto-mode fields (#34): stored on ai_usage_events
instead of landing in ignored_fields, validated leniently (a bad value costs
that field, never the batch), and summarised by the Partner API usage
summary.

LEARNER_EVENT mirrors the SDK router's event for a call the learner
explored (prompt_shields.router: route_learner_mode, route_propensity,
route_learner_version, route_learner_frozen).
"""

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest
from collector.app import app
from collector.ingest import LEARNER_FIELDS, IngestEvent
from db.models import AIUsageEvent
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

HEADERS = {"Authorization": "Bearer ps-test-key-12345"}

LEARNER_EVENT = {
    "vendor": "mistral",
    "model": "auto",
    "requested_model": "auto",
    "served_model": "mistral-small-eu",
    "source": "sdk",
    "route_mode": "local",
    "route_reason": "heuristic:simple+learner-explore",
    "use_case_name": "support",
    "route_learner_mode": "explore",
    "route_propensity": 0.0125,
    "route_learner_version": "lv1-0123456789abcdef",
    "route_learner_frozen": False,
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
async def test_learner_fields_round_trip(seeded_db):
    frozen = {**LEARNER_EVENT, "route_learner_mode": "EXPLOIT", "route_propensity": 1.0,
              "route_learner_frozen": True}
    resp, loss = await _ingest([LEARNER_EVENT, frozen])
    assert resp.status_code == 200, resp.text

    events = await _events(seeded_db)
    explored = next(e for e in events if e.route_learner_mode == "explore")
    assert explored.route_propensity == Decimal("0.0125")
    assert explored.route_learner_version == "lv1-0123456789abcdef"
    assert explored.route_learner_frozen is False
    exploit = next(e for e in events if e.route_learner_mode == "exploit")
    assert exploit.route_propensity == 1 and exploit.route_learner_frozen is True

    # Nothing the router sends is dropped any more.
    (stats,) = loss["clients"]
    assert not stats["ignored_fields"]


@pytest.mark.asyncio
async def test_old_event_stores_nulls(seeded_db):
    resp, _ = await _ingest([{"vendor": "openai", "model": "gpt-4o"}])
    assert resp.status_code == 200, resp.text
    (ev,) = await _events(seeded_db)
    assert all(getattr(ev, f) is None for f in LEARNER_FIELDS)


@pytest.mark.asyncio
async def test_malformed_fields_are_dropped_and_counted(seeded_db):
    bad = {
        "vendor": "openai",
        "model": "gpt-4o",
        "route_learner_mode": "random",
        "route_propensity": 1.5,
        "route_learner_version": "v1",
        "route_learner_frozen": "false",
    }
    good = {"vendor": "openai", "model": "gpt-4o", "route_learner_mode": " Policy "}
    resp, loss = await _ingest([bad, good])
    assert resp.status_code == 200, resp.text
    assert resp.json()["rejected"] == 0

    (stats,) = loss["clients"]
    invalid = {k for k in stats["ignored_fields"] if k.endswith(":invalid")}
    assert invalid == {f"{f}:invalid" for f in LEARNER_FIELDS}

    events = await _events(seeded_db)
    assert len(events) == 2
    dropped = next(e for e in events if e.route_learner_mode is None)
    assert all(getattr(dropped, f) is None for f in LEARNER_FIELDS)
    assert next(e for e in events if e.route_learner_mode).route_learner_mode == "policy"


@pytest.mark.asyncio
async def test_nul_in_a_learner_field_costs_only_that_field(seeded_db):
    resp, loss = await _ingest([{**LEARNER_EVENT, "route_learner_version": "lv1-\x00"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["rejected"] == 0

    (ev,) = await _events(seeded_db)
    assert ev.route_learner_version is None
    assert ev.route_learner_mode == "explore" and ev.route_propensity == Decimal("0.0125")
    (stats,) = loss["clients"]
    assert stats["ignored_fields"]["route_learner_version:invalid"] == 1


def test_field_validation():
    def ev(**kw):
        return IngestEvent(vendor="v", **kw)

    for mode in ("policy", "exploit", "explore"):
        assert ev(route_learner_mode=mode.upper()).route_learner_mode == mode
    for bad in ("", "auto", 1, True, ["explore"]):
        assert ev(route_learner_mode=bad).route_learner_mode is None, bad
    assert ev(route_learner_version="lv1-0123456789abcdef").route_learner_version
    for bad in ("lv1-0123456789ABCDEF", "lv1-0123", "lv2-0123456789abcdef",
                "lv1-0123456789abcdef0", 7):
        assert ev(route_learner_version=bad).route_learner_version is None, bad
    for good in (1, 1.0, 0.5, 0.000001):
        assert ev(route_propensity=good).route_propensity == good
    for bad in (0, 0.0000001, -0.1, 1.000001, 2, float("nan"), "0.5", True, 10**400):
        assert ev(route_propensity=bad).route_propensity is None, bad
    assert ev(route_learner_frozen=True).route_learner_frozen is True
    assert ev(route_learner_frozen=False).route_learner_frozen is False
    for bad in ("true", 1, 0, None):
        assert ev(route_learner_frozen=bad).route_learner_frozen is None, bad


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
async def test_usage_summary_reports_learner_shares(seeded_db):
    plain = {"vendor": "mistral", "model": "auto", "served_model": "mistral-small-eu",
             "source": "sdk", "use_case_name": "support"}
    events = [
        LEARNER_EVENT,
        {**LEARNER_EVENT, "route_learner_mode": "exploit", "route_propensity": 0.95},
        {**LEARNER_EVENT, "route_learner_mode": "policy", "route_propensity": 0.95},
        {**LEARNER_EVENT, "route_learner_mode": "exploit", "route_propensity": 0.95},
        # Not decided by the learner (not enabled, or a pinned model).
        plain, plain, plain, plain,
    ]
    resp, _ = await _ingest(events)
    assert resp.status_code == 200, resp.text

    body = await _summary(seeded_db)
    assert body["total_requests"] == 8
    assert body["learner_requests"] == 4
    assert body["learner_routed_share"] == 0.5
    assert body["learner_explore_share"] == 0.25


@pytest.mark.asyncio
async def test_usage_summary_without_learner_events(seeded_db):
    resp, _ = await _ingest([{"vendor": "openai", "model": "gpt-4o", "cost": 0.01}])
    assert resp.status_code == 200, resp.text
    body = await _summary(seeded_db)
    assert body["learner_requests"] == 0
    assert body["learner_routed_share"] == 0.0
    assert body["learner_explore_share"] is None


# ──────────────────────────────────────────────────────────────────────
# Migration (learner fields)
# ──────────────────────────────────────────────────────────────────────


def _load_migration():
    pytest.importorskip("alembic")
    versions = Path(__file__).resolve().parents[2] / "db" / "alembic" / "versions"
    (path,) = versions.glob("*_learner_route_telemetry.py")
    spec = importlib.util.spec_from_file_location("migration_learner_route", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_adds_the_ingested_fields():
    m = _load_migration()
    assert [name for name, _ in m.LEARNER_EVENT_COLUMNS] == list(LEARNER_FIELDS)
    assert set(LEARNER_FIELDS) <= set(AIUsageEvent.__table__.columns.keys())


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
    assert not set(LEARNER_FIELDS) & set(cols)
    assert {"route_mode", "budget_state"} <= set(cols)

    async with seeded_db.begin() as conn:
        await conn.run_sync(run, m.upgrade)
        cols = await conn.run_sync(_columns, "ai_usage_events")
    assert all(cols[f]["nullable"] for f in LEARNER_FIELDS)

    resp, _ = await _ingest([LEARNER_EVENT])
    assert resp.status_code == 200, resp.text
