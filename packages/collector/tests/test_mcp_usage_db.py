"""MCP tool usage (#63 Phase 0) against a real PostgreSQL database
(PS_TEST_DATABASE_URL): ``POST /ingest/mcp``, MCP spans on the OTLP
receiver and ``GET /api/v1/registry/mcp-tools``. Row-level security for the
table is covered in test_rls_db.py, retention and erasure in
test_retention.py."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from collector import mcp_usage, partner_rate_limit
from collector.app import app
from collector.config import settings
from collector.partner_rate_limit import FixedWindowRateLimiter, get_mcp_rate_limiter
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

KEY = "ps-test-key-12345"
HEADERS = {"Authorization": f"Bearer {KEY}"}
SECRET = "my social security number is 123-45-6789"


def _event(tool="create_issue", **kw):
    return {"server_name": "github", "tool_name": tool, "outcome": "ok",
            "latency_ms": 40, "calling_service": "billing-agent",
            "environment": "prod", **kw}


@pytest.fixture
async def client(seeded_db):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _rows(seeded_db) -> list[dict]:
    async with seeded_db.connect() as conn:
        result = await conn.execute(
            text("SELECT * FROM mcp_tool_usage ORDER BY source, tool_name")
        )
        return [dict(r._mapping) for r in result]


async def test_ingest_aggregates_names_only(client, seeded_db):
    resp = await client.post("/ingest/mcp", headers=HEADERS, json={"events": [
        _event(arguments={"q": SECRET}, result=SECRET),
        _event(outcome="error", latency_ms=60),
        _event(tool="search_issues", latency_ms=None),
        {"tool_name": ""},  # skipped
    ]})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ingested": 3, "skipped": 1}
    resp = await client.post("/ingest/mcp", headers=HEADERS,
                             json={"events": [_event(latency_ms=20)]})
    assert resp.status_code == 200

    rows = await _rows(seeded_db)
    assert [(r["tool_name"], r["call_count"], r["error_count"]) for r in rows] == [
        ("create_issue", 3, 1),
        ("search_issues", 1, 0),
    ]
    first = rows[0]
    assert (first["server_name"], first["source"]) == ("github", "sdk")
    assert (first["calling_service"], first["environment"]) == ("billing-agent", "prod")
    assert (first["latency_ms_total"], first["latency_count"]) == (120, 3)
    assert first["first_seen"] <= first["last_seen"]
    assert SECRET not in json.dumps(rows, default=str)

    resp = await client.get("/api/v1/registry/mcp-tools", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()["data"]
    by_tool = {d["tool_name"]: d for d in data}
    assert by_tool["create_issue"]["avg_latency_ms"] == 40
    assert by_tool["search_issues"]["avg_latency_ms"] is None
    assert set(by_tool["create_issue"]) == {
        "server_name", "tool_name", "source", "calling_service", "environment",
        "transport", "call_count", "error_count", "avg_latency_ms", "first_seen",
        "last_seen",
    }
    resp = await client.get("/api/v1/registry/mcp-tools", headers=HEADERS,
                            params={"server_name": "nope"})
    assert resp.json()["data"] == []


async def test_ingest_requires_an_ingest_key(client):
    resp = await client.post("/ingest/mcp", json={"events": [_event()]})
    assert resp.status_code == 401
    resp = await client.post("/ingest/mcp", json={"events": [_event()]},
                             headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401
    resp = await client.get("/api/v1/registry/mcp-tools")
    assert resp.status_code == 401


@pytest.fixture
def mcp_limit(monkeypatch):
    monkeypatch.setattr(settings, "mcp_rate_limit_per_minute", 2)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 1)
    monkeypatch.setattr(
        partner_rate_limit, "time", SimpleNamespace(time=lambda: 1_700_000_030.0)
    )
    fresh = FixedWindowRateLimiter()
    app.dependency_overrides[get_mcp_rate_limiter] = lambda: fresh
    yield
    app.dependency_overrides.pop(get_mcp_rate_limiter, None)


@pytest.mark.asyncio
async def test_ingest_has_its_own_rate_limit(client, mcp_limit):
    """/ingest/mcp is limited by PS_MCP_RATE_LIMIT_PER_MINUTE, and does not
    use up the /ingest/events budget (PS_RATE_LIMIT_PER_MINUTE)."""
    ok = [await client.post("/ingest/mcp", headers=HEADERS, json={"events": [_event()]})
          for _ in range(2)]
    blocked = await client.post("/ingest/mcp", headers=HEADERS, json={"events": []})
    sdk = await client.post("/ingest/events", headers=HEADERS, json={"events": []})
    assert [r.status_code for r in ok] == [200, 200]
    assert ok[0].headers["X-RateLimit-Limit"] == "2"
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == "10"
    assert sdk.status_code == 200


@pytest.mark.asyncio
async def test_ingest_rate_limit_zero_disables_it(client, mcp_limit, monkeypatch):
    monkeypatch.setattr(settings, "mcp_rate_limit_per_minute", 0)
    codes = [(await client.post("/ingest/mcp", headers=HEADERS,
                                json={"events": []})).status_code for _ in range(4)]
    assert codes == [200] * 4


async def test_ingest_size_limits(client, seeded_db, monkeypatch):
    resp = await client.post(
        "/ingest/mcp", headers=HEADERS,
        json={"events": [_event()] * (mcp_usage.MAX_EVENTS + 1)},
    )
    assert resp.status_code == 413
    monkeypatch.setattr(mcp_usage, "MAX_BODY_BYTES", 100)
    resp = await client.post("/ingest/mcp", headers=HEADERS,
                             json={"events": [_event()] * 5})
    assert resp.status_code == 413
    assert await _rows(seeded_db) == []


@pytest.mark.parametrize("body", [b"not json", b'{"events": {}}', b"[]"])
async def test_ingest_rejects_malformed_bodies(client, body):
    resp = await client.post(
        "/ingest/mcp", content=body,
        headers={**HEADERS, "Content-Type": "application/json"},
    )
    assert resp.status_code in (400, 422)


def _attr(key, value):
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": value}}


def _otlp(spans, service):
    return {"resourceSpans": [{
        "resource": {"attributes": [_attr("service.name", service)]},
        "scopeSpans": [{"spans": [
            {"name": name, "kind": kind, "startTimeUnixNano": "1700000000000000000",
             "endTimeUnixNano": "1700000000250000000",
             "attributes": [_attr(k, v) for k, v in attrs.items()]}
            for name, kind, attrs in spans
        ]}],
    }]}


async def test_otel_mcp_spans(client, seeded_db):
    tool_call = {
        "mcp.method.name": "tools/call",
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": "lookup_customer",
        "gen_ai.tool.call.arguments": json.dumps({"ssn": SECRET}),
        "gen_ai.tool.call.result": SECRET,
        "mcp.session.id": "s-1",
    }
    payload = _otlp([
        ("tools/call lookup_customer", 3,
         {**tool_call, "server.address": "crm.internal", "network.transport": "tcp",
          "network.protocol.name": "http"}),
        ("tools/call lookup_customer", "SPAN_KIND_SERVER",
         {**tool_call, "error.type": "tool_error"}),
        ("tools/list", 3, {"mcp.method.name": "tools/list"}),
    ], service="support-bot")
    resp = await client.post(
        "/ingest/otel/v1/traces", content=json.dumps(payload),
        headers={**HEADERS, "Content-Type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {}  # MCP spans are not rejected as vendor-less

    rows = await _rows(seeded_db)
    got = {(r["server_name"], r["calling_service"], r["error_count"], r["transport"])
           for r in rows}
    assert got == {
        ("crm.internal", "support-bot", 0, "http"),  # client span
        ("support-bot", "", 1, None),  # server span: its own service name
    }
    assert {r["source"] for r in rows} == {"otel"}
    assert {r["latency_ms_total"] for r in rows} == {250}
    assert SECRET not in json.dumps(rows, default=str)
    async with seeded_db.connect() as conn:
        events = (await conn.execute(text("SELECT count(*) FROM ai_usage_events"))).scalar()
    assert events == 0
