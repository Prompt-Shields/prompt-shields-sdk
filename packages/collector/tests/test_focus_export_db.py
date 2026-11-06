"""FOCUS cost export (#31) against a real PostgreSQL database
(PS_TEST_DATABASE_URL), through the full app: DB middleware, JWT auth, the
`usage:export` scope, rate limit headers, audit, retention, streaming and,
in one test, row-level security."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from collector import focus_export, rls
from collector.app import app
from collector.partner_auth import hash_secret, issue_access_token
from db.models import AIAsset, AIUsageEvent, PartnerAuditLog, PartnerCredential, Tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

D = Decimal
EXPORT_KEY = "ps-pk-focus-export-a"


async def _seed(engine, admin_info=None):
    """Tenant A (two assets) and tenant B, events on two days."""
    day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=2
    )
    kw = {"info": admin_info} if admin_info else {}
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, **kw)
    async with factory() as s:
        ta = Tenant(name="Tenant A", domain="a.example", settings={})
        tb = Tenant(name="Tenant B", domain="b.example", settings={})
        s.add_all([ta, tb])
        await s.flush()
        support = AIAsset(
            tenant_id=ta.id, vendor="openai", model="gpt-4o", discovery_source=["sdk"],
            use_case_name="support", business_unit="ops", environment="prod",
            region="swedencentral",
        )
        evil = AIAsset(
            tenant_id=ta.id, vendor="mistral", model="large", discovery_source=["sdk"],
            use_case_name="=HYPERLINK(\"http://evil\")", business_unit=None,
        )
        other = AIAsset(
            tenant_id=tb.id, vendor="openai", model="gpt-4o", discovery_source=["sdk"],
            use_case_name="support", business_unit="ops",
        )
        s.add_all([support, evil, other])
        await s.flush()

        def ev(tenant, asset, at, **fields):
            return AIUsageEvent(
                tenant_id=tenant.id, asset_id=asset.id, timestamp=at, source="gateway",
                **fields,
            )

        s.add_all([
            # Day 1, support, served by azure: two calls in one row.
            ev(ta, support, day + timedelta(hours=1), cost=D("0.010"), tokens_in=100,
               tokens_out=10, served_model="gpt-4o-2024-08-06", served_provider="azure"),
            ev(ta, support, day + timedelta(hours=5), cost=D("0.020"), tokens_in=200,
               tokens_out=20, served_model="gpt-4o-2024-08-06", served_provider="azure"),
            # Day 1, support, different model: its own row.
            ev(ta, support, day + timedelta(hours=2), cost=D("0.001"), tokens_in=50,
               served_model="gpt-4o-mini"),
            # Day 1, no cost: left out.
            ev(ta, support, day + timedelta(hours=3), tokens_in=999),
            # Day 2, evil asset, no tokens.
            ev(ta, evil, day + timedelta(days=1, hours=4), cost=D("0.500")),
            # Ten days old.
            ev(ta, support, day - timedelta(days=8), cost=D("1.000"), tokens_in=1),
            # Tenant B: never in tenant A's export.
            ev(tb, other, day + timedelta(hours=1), cost=D("9.000"), tokens_in=9),
        ])

        exporter = PartnerCredential(
            tenant_id=ta.id, partner_name="finops", client_id="finops-client",
            client_secret_hash=hash_secret("x"), api_key_hash=hash_secret(EXPORT_KEY),
            scopes=["usage:export"], is_active="true",
        )
        legacy = PartnerCredential(
            tenant_id=ta.id, partner_name="legacy", client_id="legacy-client",
            client_secret_hash=hash_secret("x"), scopes=["registry:read"],
        )
        s.add_all([exporter, legacy])
        await s.commit()
        token, _ = issue_access_token(ta.id, exporter.id, "finops-client", ["usage:export"])
        token_legacy, _ = issue_access_token(ta.id, legacy.id, "legacy-client", ["registry:read"])
        return {
            "tenant_a": ta.id, "support": support.id, "evil": evil.id,
            "exporter": exporter.id, "token": token, "token_legacy": token_legacy, "day": day,
        }


async def _get(path, token, **params):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get(path, params=params, headers={"Authorization": f"Bearer {token}"})


def _csv(resp) -> list[dict]:
    reader = csv.reader(io.StringIO(resp.text))
    header = next(reader)
    assert header == list(focus_export.COLUMNS)
    return [dict(zip(header, line, strict=True)) for line in reader]


PATH = "/partner/v1/usage/focus-export"


@pytest.mark.asyncio
async def test_csv_export_rows_per_day_asset_model_provider(seeded_db):
    seed = await _seed(seeded_db)
    r = await _get(PATH, seed["token"], start=(seed["day"] - timedelta(days=1)).isoformat())
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["x-focus-version"] == "1.4"
    assert r.headers["content-disposition"].startswith('attachment; filename="focus-cost-')
    assert "x-ratelimit-limit" in r.headers
    assert r.headers["cache-control"] == "no-store"

    rows = _csv(r)
    keyed = {(row["ResourceId"], row["SkuId"]): row for row in rows}
    assert len(rows) == 3  # no-cost, old and tenant B events are not exported

    azure = keyed[(str(seed["support"]), "gpt-4o-2024-08-06")]
    assert azure["BilledCost"] == azure["EffectiveCost"] == "0.030000"
    assert azure["ListCost"] == azure["ContractedCost"] == "0.030000"
    assert azure["x_RequestCount"] == "2"
    assert azure["PricingQuantity"] == azure["ConsumedQuantity"] == "330"
    assert azure["PricingUnit"] == "Tokens"
    assert azure["ServiceProviderName"] == azure["HostProviderName"] == "azure"
    assert azure["InvoiceIssuerName"] == "azure"
    assert azure["ServiceName"] == "openai"
    assert azure["ChargePeriodStart"] == f"{seed['day']:%Y-%m-%dT%H:%M:%SZ}"
    assert azure["ChargePeriodEnd"] == f"{seed['day'] + timedelta(days=1):%Y-%m-%dT%H:%M:%SZ}"
    assert azure["BillingPeriodStart"] == f"{seed['day']:%Y-%m-01T00:00:00Z}"
    assert azure["BillingAccountId"] == str(seed["tenant_a"])
    assert azure["BillingAccountName"] == "Tenant A"
    assert azure["RegionId"] == "swedencentral"
    assert json.loads(azure["Tags"]) == {
        "ps:business_unit": "ops", "ps:environment": "prod", "ps:use_case": "support",
    }
    assert azure["x_BusinessUnit"] == "ops" and azure["x_UseCaseName"] == "support"

    # served_provider missing: the asset's vendor is the provider.
    mini = keyed[(str(seed["support"]), "gpt-4o-mini")]
    assert mini["ServiceProviderName"] == "openai"
    assert mini["PricingQuantity"] == "50"

    # A formula in a registry field is neutralised; no tokens -> null quantity.
    evil = keyed[(str(seed["evil"]), "large")]
    assert evil["ResourceName"] == "'=HYPERLINK(\"http://evil\")"
    assert evil["x_UseCaseName"].startswith("'=")
    assert evil["PricingQuantity"] == "" and evil["PricingUnit"] == ""
    assert evil["BilledCost"] == "0.500000"


@pytest.mark.asyncio
async def test_jsonl_hour_granularity_and_merge_safe(seeded_db):
    seed = await _seed(seeded_db)
    r = await _get(
        PATH, seed["token"], format="jsonl", granularity="hour", cost_mode="merge_safe",
        start=seed["day"].isoformat(), end=(seed["day"] + timedelta(days=1)).isoformat(),
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/x-ndjson")
    rows = [json.loads(line) for line in r.text.splitlines()]
    # Hours 1, 2 and 5 on day 1; the no-cost call at hour 3 is left out.
    assert [row["ChargePeriodStart"][11:13] for row in rows] == ["01", "02", "05"]
    assert all(row["BilledCost"] == 0 and row["EffectiveCost"] == 0 for row in rows)
    assert sum(row["x_EstimatedCost"] for row in rows) == pytest.approx(0.031)
    assert rows[0]["ChargeClass"] is None
    assert rows[0]["Tags"]["ps:use_case"] == "support"


@pytest.mark.asyncio
async def test_window_validation_and_scope(seeded_db):
    seed = await _seed(seeded_db)
    # registry:read (every Phase 1 read scope) does not include usage:export.
    assert (await _get(PATH, seed["token_legacy"])).status_code == 403
    bad = await _get(PATH, seed["token"], start="2026-10-02", end="2026-10-01")
    assert bad.status_code == 400
    too_long = await _get(PATH, seed["token"], start="2024-01-01", end="2026-01-01")
    assert too_long.status_code == 400
    assert (await _get(PATH, seed["token"], format="parquet")).status_code == 422
    assert (await _get(PATH, seed["token"], granularity="month")).status_code == 422

    async with async_sessionmaker(seeded_db, class_=AsyncSession)() as s:
        audited = (
            await s.execute(
                select(PartnerAuditLog.endpoint, PartnerAuditLog.status_code).where(
                    PartnerAuditLog.tenant_id == seed["tenant_a"]
                )
            )
        ).all()
    assert (PATH, 403) in audited


@pytest.mark.asyncio
async def test_export_is_clamped_to_retention_and_audited(seeded_db):
    seed = await _seed(seeded_db)
    full = _csv(await _get(PATH, seed["token"], start=(seed["day"] - timedelta(days=30)).isoformat()))
    assert len(full) == 4  # includes the ten-day-old call

    async with async_sessionmaker(seeded_db, class_=AsyncSession)() as s:
        tenant = await s.get(Tenant, seed["tenant_a"])
        tenant.settings = {"retention_days": 5}
        await s.commit()
    r = await _get(PATH, seed["token"], start=(seed["day"] - timedelta(days=30)).isoformat())
    assert len(_csv(r)) == 3
    start = datetime.fromisoformat(r.headers["x-export-window-start"])
    assert start > seed["day"] - timedelta(days=6)

    async with async_sessionmaker(seeded_db, class_=AsyncSession)() as s:
        endpoints = (
            await s.execute(
                select(PartnerAuditLog.endpoint).where(
                    PartnerAuditLog.partner_id == seed["exporter"],
                    PartnerAuditLog.status_code == 200,
                )
            )
        ).scalars().all()
    assert endpoints == [PATH, PATH]


@pytest.mark.asyncio
async def test_export_streams_many_rows_in_chunks(seeded_db, monkeypatch):
    seed = await _seed(seeded_db)
    monkeypatch.setattr(focus_export, "STREAM_BATCH", 2)
    r = await _get(
        PATH, seed["token"], format="jsonl", granularity="hour",
        start=(seed["day"] - timedelta(days=1)).isoformat(),
    )
    assert r.status_code == 200, r.text
    assert len(r.text.splitlines()) == 4  # 3 hours on day 1, 1 on day 2


@pytest.mark.asyncio
async def test_client_disconnect_mid_stream_returns_the_connection(seeded_db, monkeypatch):
    """Starlette leaves the body generator suspended on disconnect; the
    response must close it so the export's connection and transaction end."""
    seed = await _seed(seeded_db)
    monkeypatch.setattr(focus_export, "STREAM_BATCH", 1)
    query = (
        f"start={(seed['day'] - timedelta(days=1)).isoformat()}"
        "&granularity=hour&format=jsonl"
    ).replace("+", "%2B").encode()
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": "GET", "scheme": "http", "path": PATH,
        "raw_path": PATH.encode(), "root_path": "", "query_string": query,
        "headers": [(b"host", b"test"), (b"authorization", f"Bearer {seed['token']}".encode())],
        "client": ("127.0.0.1", 1), "server": ("test", 80),
    }
    bodies: list[bytes] = []
    checked_out_while_streaming: list[int] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            assert message["status"] == 200
        elif message["type"] == "http.response.body":
            bodies.append(message["body"])
            if len(bodies) == 1:
                checked_out_while_streaming.append(seeded_db.pool.checkedout())
                raise OSError("client went away")  # what servers raise on spec 2.4

    with pytest.raises(Exception):  # noqa: B017 - Starlette's ClientDisconnect
        await app(scope, receive, send)
    assert len(bodies) == 1
    assert checked_out_while_streaming and checked_out_while_streaming[0] >= 1
    assert seeded_db.pool.checkedout() == 0


@pytest.mark.asyncio
async def test_stalled_client_cannot_hold_the_export_transaction(seeded_db, monkeypatch):
    """The cursor's transaction gets an idle-in-transaction timeout, so a
    client that stops reading does not hold a snapshot back from vacuum."""
    import asyncio

    seed = await _seed(seeded_db)
    monkeypatch.setattr(focus_export, "STREAM_BATCH", 1)
    monkeypatch.setattr(focus_export, "STREAM_IDLE_TIMEOUT", "300ms")
    stream = focus_export.stream_export(
        seeded_db, tenant_id=seed["tenant_a"], tenant_name="Tenant A",
        start=seed["day"] - timedelta(days=1), end=datetime.now(UTC),
        granularity="hour", cost_mode="estimated", fmt="jsonl",
    )
    try:
        assert await anext(stream)  # first row; the transaction is open
        await asyncio.sleep(1.5)  # the client stops reading
        with pytest.raises(Exception):  # noqa: B017 - the backend ended the session
            async for _ in stream:
                pass
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_export_works_under_rls(seeded_db):
    seed = await _seed(seeded_db, admin_info=rls.bypass_info("test-seed"))
    async with seeded_db.begin() as conn:
        await rls.enable(conn)
    r = await _get(PATH, EXPORT_KEY, start=(seed["day"] - timedelta(days=1)).isoformat())
    assert r.status_code == 200, r.text
    rows = _csv(r)
    assert len(rows) == 3
    assert {row["BillingAccountName"] for row in rows} == {"Tenant A"}
