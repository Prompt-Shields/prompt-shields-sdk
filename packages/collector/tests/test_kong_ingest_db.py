"""POST /ingest/kong against a real PostgreSQL database
(PS_TEST_DATABASE_URL): tenant auth, single entries and batches, size
limits, dedup with other sources, loss accounting, and that no body,
secret header or client IP reaches any stored row. Row-level security is
covered in test_rls_db.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from collector.app import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

URL = "/ingest/kong"
KEY = "ps-test-key-12345"
HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
FIXTURES = Path(__file__).parent / "fixtures" / "kong"
SECRETS = ("SECRETKEY", "123-45-6789", "10.20.30.40", "10.1.2.3", "2001:db8::42")


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _dump(conn) -> str:
    rows = []
    for table in ("ai_assets", "ai_usage_events", "data_flows", "telemetry_client_stats"):
        res = await conn.execute(text(f"select row_to_json(t)::text from {table} t"))
        rows += [r[0] for r in res]
    return "\n".join(rows)


@pytest.mark.asyncio
async def test_auth_is_the_tenant_ingest_key(seeded_db):
    body = _fixture("kong-3.6-ai-proxy.json")
    async with _client() as c:
        missing = await c.post(URL, content=body, headers={"Content-Type": "application/json"})
        wrong = await c.post(
            URL, content=body,
            headers={"Authorization": "Bearer ps-wrong", "Content-Type": "application/json"},
        )
        # Kong's apikey header is not an accepted credential.
        apikey = await c.post(
            URL, content=body, headers={"apikey": KEY, "Content-Type": "application/json"}
        )
    assert (missing.status_code, wrong.status_code, apikey.status_code) == (401, 401, 401)
    async with seeded_db.connect() as conn:
        assert (await conn.execute(text("select count(*) from ai_usage_events"))).scalar() == 0


@pytest.mark.asyncio
async def test_single_entry_and_batch_store_metadata_only(seeded_db):
    async with _client() as c:
        single = await c.post(URL, content=_fixture("kong-3.6-ai-proxy.json"), headers=HEADERS)
        batch = await c.post(
            URL, content=_fixture("kong-3.9-ai-proxy-batch.json"),
            headers={**HEADERS, "Content-Type": "application/json; charset=utf-8"},
        )
        enterprise = await c.post(
            URL, content=_fixture("kong-3.14-ai-proxy-advanced-batch.json"), headers=HEADERS
        )
        loss = await c.get("/telemetry/loss", headers=HEADERS)
    assert single.status_code == 200, single.text
    assert single.json() == {"ingested": 2, "assets_created": 2, "ignored": 0, "rejected": 0}
    assert batch.json() == {"ingested": 2, "assets_created": 2, "ignored": 1, "rejected": 0}
    assert enterprise.json() == {
        "ingested": 2, "assets_created": 2, "ignored": 0, "rejected": 1
    }

    async with seeded_db.connect() as conn:
        assets = (await conn.execute(text(
            "select vendor, model, calling_service, business_unit, discovery_source, "
            "confidence from ai_assets order by vendor"
        ))).all()
        events = (await conn.execute(text(
            "select source, tokens_in, tokens_out, cost, latency_ms, requested_model, "
            "served_model, prompt_hash, session_id, tool_calls_used "
            "from ai_usage_events order by tokens_in"
        ))).all()
        dump = await _dump(conn)

    assert sorted((a.vendor, a.model, a.calling_service) for a in assets) == [
        ("anthropic", "claude-2.1", "hr-screener"),
        ("azure_openai", "gpt-4o", "payments-assistant"),
        ("bedrock", "anthropic.claude-sonnet-4-20250514-v1:0", "payments-fraud-explainer"),
        ("google", "gemini-2.5-flash", "gemini-sandbox"),
        ("openai", "gpt-3.5-turbo", "support-bot"),
        ("openai", "gpt-4", "hr-screener"),
    ]
    assert {(tuple(a.discovery_source), a.confidence) for a in assets} == {(("kong",), "high")}
    assert len(events) == 6
    assert {e.source for e in events} == {"kong"}
    assert all(e.prompt_hash is None and e.session_id is None for e in events)
    assert all(e.tool_calls_used is None for e in events)
    for secret in SECRETS:
        assert secret not in dump, secret
    for header in ("apikey", "authorization", "x-api-key", "cookie", "x-ps-route"):
        assert f'"{header}"' not in dump

    (kong,) = [s for s in loss.json()["sources"] if s["source"] == "kong"]
    assert kong["received_events"] == 6
    # The one UNSPECIFIED-provider entry was answered 200: Kong will not
    # retry it, so it counts as lost.
    assert kong["dropped_events"] == 1
    (client,) = [x for x in loss.json()["clients"] if x["client_id"] == "kong"]
    assert client["ignored_fields"] == {"entry:non_ai": 1}
    assert client["collector_rejected"] == {"no_vendor": 1}


@pytest.mark.asyncio
async def test_kong_corroborates_sdk_sightings(seeded_db):
    sdk_event = {
        "vendor": "azure_openai",
        "model": "gpt-4o",
        "source": "sdk",
        "calling_service": "payments-assistant",
    }
    async with _client() as c:
        await c.post("/ingest/events", headers=HEADERS, json={"events": [sdk_event]})
        resp = await c.post(
            URL, content=_fixture("kong-3.9-ai-proxy-batch.json"), headers=HEADERS
        )
        # A repeat delivery (Kong retried) adds usage, not assets.
        again = await c.post(
            URL, content=_fixture("kong-3.9-ai-proxy-batch.json"), headers=HEADERS
        )
    assert resp.json()["assets_created"] == 1  # only the openai one is new
    assert again.json()["assets_created"] == 0
    async with seeded_db.connect() as conn:
        row = (await conn.execute(text(
            "select discovery_source, confidence from ai_assets where vendor = 'azure_openai'"
        ))).one()
    assert (sorted(row.discovery_source), row.confidence) == (["kong", "sdk"], "verified")


@pytest.mark.asyncio
async def test_size_and_shape_limits(seeded_db):
    entry = json.loads(_fixture("kong-3.6-ai-proxy.json"))
    async with _client() as c:
        too_many = await c.post(URL, content=json.dumps([entry] * 1001), headers=HEADERS)
        too_big = await c.post(
            URL, content=b'["' + b"x" * (4 * 1024 * 1024) + b'"]', headers=HEADERS
        )
        declared = await c.post(
            URL, content=b"[]",
            headers={**HEADERS, "Content-Length": str(5 * 1024 * 1024)},
        )
        not_json = await c.post(URL, content=b"{nope", headers=HEADERS)
        scalar = await c.post(URL, content=b'"entry"', headers=HEADERS)
        wrong_type = await c.post(
            URL, content=b"{}", headers={**HEADERS, "Content-Type": "text/plain"}
        )
        empty = await c.post(URL, content=b"[]", headers=HEADERS)
        loss = await c.get("/telemetry/loss", headers=HEADERS)
    assert too_many.status_code == 413
    assert too_big.status_code == 413
    assert declared.status_code == 413
    assert (not_json.status_code, scalar.status_code) == (400, 400)
    assert wrong_type.status_code == 415
    assert empty.json() == {"ingested": 0, "assets_created": 0, "ignored": 0, "rejected": 0}
    (client,) = [x for x in loss.json()["clients"] if x["client_id"] == "kong"]
    assert client["collector_rejected"] == {"too_large": 1001}
    # Kong retries a 413, so it is not counted as lost.
    assert client["lost_events"] == 0
    async with seeded_db.connect() as conn:
        assert (await conn.execute(text("select count(*) from ai_usage_events"))).scalar() == 0
