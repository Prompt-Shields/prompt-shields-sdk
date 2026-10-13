"""Data flows derived from telemetry, against a real PostgreSQL database
(PS_TEST_DATABASE_URL): the ingest hook (/ingest/events and OTel), the batch
rebuild, idempotency and tenant isolation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from collector.app import app
from collector.data_flows import (
    MAX_DATA_SOURCES,
    _run_cli,
    build_flow_context,
    rebuild_flows,
)
from collector.partner_auth import hash_secret, issue_access_token
from db.api_keys import api_key_lookup_prefix, hash_api_key
from db.models import AIAsset, AIUsageEvent, PartnerCredential, Tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

KEY_A = "ps-test-key-12345"
KEY_B = "ps-other-key-67890"

SDK_EVENT = {
    "vendor": "openai",
    "model": "gpt-4o",
    "use_case_name": "resume-screening",
    "calling_service": "hr-screener",
    "data_classification": "internal",
    "data_sources": ["candidates_db", "ats_api"],
    "output_destination": "hiring_dashboard",
    "detected_pii_types": ["email", "national_id"],
    "region": "eu-west-1",
}

FLOW_SQL = (
    "select source_system, destination_system, direction, data_classification, "
    "detected_pii_types, event_count, first_seen, last_seen from data_flows "
    "where tenant_id = :t order by direction, source_system"
)


def _session(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)()


async def _tenant_id(engine, name="Test Corp"):
    async with _session(engine) as db:
        return (await db.execute(select(Tenant.id).where(Tenant.name == name))).scalar_one()


async def _add_tenant_b(engine):
    async with _session(engine) as db:
        t = Tenant(
            name="Other Corp",
            settings={},
            api_key_prefix=api_key_lookup_prefix(KEY_B),
            api_key_hash=hash_api_key(KEY_B),
        )
        db.add(t)
        await db.commit()
        return t.id


async def _flows(engine, tenant_id):
    async with engine.connect() as conn:
        rows = (await conn.execute(text(FLOW_SQL), {"t": tenant_id})).all()
    return [tuple(r) for r in rows]


async def _post(path, body, key=KEY_A, **kw):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.post(path, headers={"Authorization": f"Bearer {key}", **kw}, **body)


@pytest.mark.asyncio
async def test_ingest_produces_flows_visible_through_both_apis(seeded_db, monkeypatch):
    tenant = await _tenant_id(seeded_db)
    second = {**SDK_EVENT, "data_classification": "confidential",
              "data_sources": ["candidates_db"], "detected_pii_types": ["phone"]}
    resp = await _post("/ingest/events", {"json": {"events": [SDK_EVENT, second]}})
    assert resp.status_code == 200, resp.text

    flows = await _flows(seeded_db, tenant)
    dest = "openai/gpt-4o@eu-west-1"
    summary = [f[:6] for f in flows]
    assert summary == [
        ("ats_api", dest, "input", "internal", ["email", "national_id"], 1),
        # Highest classification and the union of PII categories are kept.
        ("candidates_db", dest, "input", "confidential",
         ["email", "national_id", "phone"], 2),
        (dest, "hiring_dashboard", "output", "confidential", None, 2),
    ]
    for f in flows:
        assert f[6] is not None and f[6] <= f[7]

    async with _session(seeded_db) as db:
        asset_id = (await db.execute(select(AIAsset.id))).scalar_one()
        ctx = (await db.execute(select(AIUsageEvent.flow_context))).scalars().all()
    assert {c["output_destination"] for c in ctx} == {"hiring_dashboard"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        internal = await c.get(f"/api/v1/registry/assets/{asset_id}/data-flows",
                               headers={"Authorization": f"Bearer {KEY_A}"})
    assert internal.status_code == 200
    got = sorted((f["source_system"], f["direction"]) for f in internal.json()["data"])
    assert got == [("ats_api", "input"), ("candidates_db", "input"), (dest, "output")]

    # Partner API reads the same table.
    async with _session(seeded_db) as db:
        cred = PartnerCredential(tenant_id=tenant, partner_name="p", client_id="p-client",
                                 client_secret_hash=hash_secret("x"),
                                 scopes=["registry:read"])
        db.add(cred)
        await db.commit()
        token, _ = issue_access_token(tenant, cred.id, "p-client", ["registry:read"])
    # The partner route commits its audit row before serialising, so it needs
    # the app's real expire_on_commit=False (the shared fixture uses the default).
    from collector import app as app_module

    monkeypatch.setattr(app_module, "SessionLocal", async_sessionmaker(
        seeded_db, class_=AsyncSession, expire_on_commit=False))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        partner = await c.get(f"/partner/v1/assets/{asset_id}/data-flows",
                              headers={"Authorization": f"Bearer {token}"})
    assert partner.status_code == 200, partner.text
    by_source = {f["source_system"]: f for f in partner.json()["data"]}
    assert set(by_source) == {"ats_api", "candidates_db", dest}
    assert "national_id" in by_source["candidates_db"]["detected_pii_types"]


@pytest.mark.asyncio
async def test_otel_spans_feed_flows(seeded_db):
    tenant = await _tenant_id(seeded_db)
    span = {
        "traceId": "5b8efff798038103d269b633813fc60c",
        "spanId": "eee19b7ec3c1b174",
        "name": "chat",
        "startTimeUnixNano": "1700000000000000000",
        "endTimeUnixNano": "1700000000450000000",
        "attributes": [
            {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
            {"key": "gen_ai.provider.name", "value": {"stringValue": "anthropic"}},
            {"key": "gen_ai.request.model", "value": {"stringValue": "claude-x"}},
        ],
    }
    payload = {"resourceSpans": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "support-bot"}}]},
        "scopeSpans": [{"spans": [span, span]}],
    }]}
    resp = await _post("/ingest/otel/v1/traces", {"content": json.dumps(payload)},
                       **{"Content-Type": "application/json"})
    assert resp.status_code == 200, resp.text

    flows = await _flows(seeded_db, tenant)
    assert [f[:6] for f in flows] == [
        ("support-bot", "anthropic/claude-x", "input", None, None, 2)
    ]


@pytest.mark.asyncio
async def test_rebuild_is_idempotent_and_matches_ingest(seeded_db):
    tenant = await _tenant_id(seeded_db)
    resp = await _post("/ingest/events", {"json": {"events": [SDK_EVENT, SDK_EVENT]}})
    assert resp.status_code == 200
    from_ingest = await _flows(seeded_db, tenant)

    for _ in range(2):
        async with _session(seeded_db) as db:
            assert await rebuild_flows(db, tenant) == 3
            await db.commit()
        assert await _flows(seeded_db, tenant) == from_ingest

    # From scratch, the batch job reproduces what ingest wrote.
    async with seeded_db.begin() as conn:
        await conn.execute(text("delete from data_flows"))
    async with _session(seeded_db) as db:
        await rebuild_flows(db, tenant)
        await db.commit()
    assert await _flows(seeded_db, tenant) == from_ingest


@pytest.mark.asyncio
async def test_rebuild_backfills_events_without_flow_context(seeded_db):
    """Events stored before flow_context existed derive a flow from the
    asset's calling service and classification."""
    tenant = await _tenant_id(seeded_db)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    async with _session(seeded_db) as db:
        asset = AIAsset(tenant_id=tenant, vendor="mistral", model="large",
                        calling_service="legal-app", data_classification="restricted",
                        discovery_source=["sdk"])
        db.add(asset)
        await db.flush()
        for i in range(3):
            db.add(AIUsageEvent(tenant_id=tenant, asset_id=asset.id, source="sdk",
                                timestamp=t0 + timedelta(days=i)))
        await db.commit()
        await rebuild_flows(db, tenant)
        await db.commit()

    assert await _flows(seeded_db, tenant) == [
        ("legal-app", "mistral/large", "input", "restricted", None, 3,
         t0, t0 + timedelta(days=2)),
    ]


@pytest.mark.asyncio
async def test_flows_are_tenant_scoped(seeded_db):
    tenant_a = await _tenant_id(seeded_db)
    tenant_b = await _add_tenant_b(seeded_db)

    assert (await _post("/ingest/events", {"json": {"events": [SDK_EVENT]}})).status_code == 200
    other = {**SDK_EVENT, "data_sources": ["b_warehouse"], "output_destination": None,
             "detected_pii_types": ["iban"]}
    assert (await _post("/ingest/events", {"json": {"events": [other]}},
                        key=KEY_B)).status_code == 200

    flows_a = await _flows(seeded_db, tenant_a)
    flows_b = await _flows(seeded_db, tenant_b)
    assert {f[0] for f in flows_a} == {"candidates_db", "ats_api", "openai/gpt-4o@eu-west-1"}
    assert [f[:6] for f in flows_b] == [
        ("b_warehouse", "openai/gpt-4o@eu-west-1", "input", "internal", ["iban"], 1)
    ]

    # Rebuilding A (via the CLI entry point) leaves B alone, and vice versa.
    url = seeded_db.url.render_as_string(hide_password=False)
    await _run_cli([tenant_a], url)
    assert await _flows(seeded_db, tenant_b) == flows_b
    assert await _flows(seeded_db, tenant_a) == flows_a
    await _run_cli(None, url)
    assert await _flows(seeded_db, tenant_b) == flows_b

    # Tenant A cannot read B's flows through the API.
    async with _session(seeded_db) as db:
        asset_b = (await db.execute(
            select(AIAsset.id).where(AIAsset.tenant_id == tenant_b))).scalar_one()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(f"/api/v1/registry/assets/{asset_b}/data-flows",
                           headers={"Authorization": f"Bearer {KEY_A}"})
    assert resp.json()["data"] == []


def test_flow_context_is_normalised_and_bounded():
    ctx = build_flow_context(
        data_sources=[" crm ", "", "crm", *[f"s{i}" for i in range(50)]],
        pii_types=["EMAIL", "national_id", "email"],
        output_destination="  ",
        data_classification="Confidential",
        calling_service="x" * 1000,
    )
    assert ctx["data_sources"][0] == "crm"
    assert len(ctx["data_sources"]) == MAX_DATA_SOURCES
    assert ctx["pii_types"] == ["email", "national_id"]
    assert "output_destination" not in ctx
    assert ctx["classification"] == "confidential"
    assert len(ctx["service"]) == 200
    assert build_flow_context() is None
    assert build_flow_context(data_classification="top-secret") is None
