"""MCP observability for discovery (#77) on PostgreSQL: daily tool-call
metering, MCP servers registered as assets with their owner, cursor
paging on the registry and the Partner API's ``/partner/v1/mcp/*``
(scope ``mcp:read``)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from collector.app import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

KEY = "ps-test-key-12345"
HEADERS = {"Authorization": f"Bearer {KEY}"}
OWNER = "platform-team@example.com"


@pytest.fixture
async def client(seeded_db):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


def _gw(tool="search", server="docs", **kw):
    return {"source": "gateway", "server_name": server, "tool_name": tool,
            "outcome": "ok", "latency_ms": 30, "transport": "streamable-http", **kw}


async def _ingest(client, events):
    resp = await client.post("/ingest/mcp", headers=HEADERS, json={"events": events})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _all(seeded_db, sql):
    async with seeded_db.connect() as conn:
        return [dict(r._mapping) for r in await conn.execute(text(sql))]


async def _partner(client, scopes):
    r = await client.post("/api/v1/admin/partner-credentials", headers=HEADERS,
                          json={"partner_name": "ea-tool", "scopes": scopes})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


async def test_daily_metering_with_bytes(client, seeded_db):
    await _ingest(client, [
        _gw(request_bytes=100, response_bytes=1000),
        _gw(request_bytes=50, response_bytes=500, outcome="error"),
        _gw(outcome="blocked", request_bytes=10),
        _gw(tool="fetch", latency_ms=None),
        _gw(request_bytes=-1, response_bytes=True),  # invalid bytes: dropped
    ])
    rows = await _all(seeded_db, "SELECT * FROM mcp_tool_usage_daily ORDER BY tool_name")
    assert [(r["tool_name"], r["call_count"]) for r in rows] == [("fetch", 1), ("search", 4)]
    search = rows[1]
    assert search["day"] == datetime.now(UTC).date()
    assert (search["error_count"], search["blocked_count"]) == (1, 1)
    assert (search["request_bytes"], search["response_bytes"], search["bytes_count"]) == (
        160, 1500, 3)
    assert (search["latency_ms_total"], search["latency_count"]) == (120, 4)
    # The same day accumulates.
    await _ingest(client, [_gw(request_bytes=1, response_bytes=2)])
    rows = await _all(seeded_db, "SELECT * FROM mcp_tool_usage_daily WHERE tool_name='search'")
    assert (rows[0]["call_count"], rows[0]["request_bytes"]) == (5, 161)


async def test_servers_become_assets_with_their_owner(client, seeded_db):
    await _ingest(client, [
        _gw(server_owner=OWNER.upper()),
        _gw(server="unknown"),  # no identity: no asset
        _gw(server="wiki", server_owner="not an email"),
    ])
    assets = await _all(seeded_db, "SELECT * FROM ai_assets WHERE vendor='mcp' ORDER BY model")
    assert [(a["model"], a["owner_email"], a["discovery_source"], a["confidence"])
            for a in assets] == [
        ("docs", OWNER, ["gateway"], "high"),
        ("wiki", None, ["gateway"], "high"),
    ]

    # An owner set in the registry is never overwritten by telemetry.
    async with seeded_db.begin() as conn:
        await conn.execute(text(
            "UPDATE ai_assets SET owner_email='owner@corp.example' WHERE model='docs'"))
    await _ingest(client, [_gw(server_owner=OWNER), _gw(server="docs", source="sdk")])
    assets = await _all(seeded_db, "SELECT * FROM ai_assets WHERE vendor='mcp' ORDER BY model")
    assert len(assets) == 2
    docs = assets[0]
    assert docs["owner_email"] == "owner@corp.example"
    assert sorted(docs["discovery_source"]) == ["gateway", "sdk"]
    assert docs["confidence"] == "verified"

    data = (await client.get("/api/v1/registry/mcp-tools", headers=HEADERS)).json()["data"]
    by_server = {d["server_name"]: d for d in data}
    assert by_server["docs"]["asset_id"] == str(docs["id"])
    assert by_server["docs"]["owner_email"] == "owner@corp.example"
    assert by_server["unknown"]["asset_id"] is None

    # The server is in the partner asset registry with its owner.
    h = await _partner(client, ["assets:read"])
    r = await client.get("/partner/v1/assets", headers=h, params={"vendor": "mcp"})
    assert r.status_code == 200, r.text
    assert {(a["model"], a["owner_email"]) for a in r.json()["data"]} == {
        ("docs", "owner@corp.example"), ("wiki", None)}


async def test_registry_pages_with_a_cursor(client, seeded_db):
    await _ingest(client, [_gw(tool=f"tool_{i}") for i in range(5)])
    seen, cursor = [], None
    for _ in range(4):
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        body = (await client.get("/api/v1/registry/mcp-tools", headers=HEADERS,
                                 params=params)).json()
        seen += [d["tool_name"] for d in body["data"]]
        cursor = body["meta"]["next_cursor"]
        assert body["meta"]["has_more"] is (cursor is not None)
        if not cursor:
            break
    assert sorted(seen) == [f"tool_{i}" for i in range(5)]
    for bad in ("garbage", "Zm9v"):
        r = await client.get("/api/v1/registry/mcp-tools", headers=HEADERS,
                             params={"cursor": bad})
        assert r.status_code == 400
    r = await client.get("/api/v1/registry/mcp-tools", headers=HEADERS,
                         params={"limit": 1001})
    assert r.status_code == 422


async def test_partner_mcp_routes(client, seeded_db):
    await _ingest(client, [_gw(server_owner=OWNER, request_bytes=10, response_bytes=20),
                           _gw(tool="fetch")])
    # Spread the metering over three days.
    async with seeded_db.begin() as conn:
        await conn.execute(text(
            "INSERT INTO mcp_tool_usage_daily (id, tenant_id, day, source, server_name, "
            "tool_name, call_count) SELECT gen_random_uuid(), tenant_id, day - d, source, "
            "server_name, tool_name, 1 FROM mcp_tool_usage_daily, "
            "generate_series(1, 2) AS d WHERE tool_name = 'search'"))

    denied = await _partner(client, ["assets:read"])
    for path in ("/partner/v1/mcp/tools", "/partner/v1/mcp/usage"):
        assert (await client.get(path, headers=denied)).status_code == 403

    h = await _partner(client, ["mcp:read"])
    r = await client.get("/partner/v1/mcp/tools", headers=h)
    assert r.status_code == 200, r.text
    tools = r.json()["data"]
    assert {t["tool_name"] for t in tools} == {"search", "fetch"}
    assert {t["owner_email"] for t in tools} == {OWNER}

    today = datetime.now(UTC).date()
    days, cursor = [], None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        r = await client.get("/partner/v1/mcp/usage", headers=h, params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        days += [(d["day"], d["tool_name"]) for d in body["data"]]
        cursor = body["meta"]["next_cursor"]
        if not cursor:
            break
    assert len(days) == 4
    assert days[0][0] == today.isoformat()
    assert [d for d, _ in days] == sorted((d for d, _ in days), reverse=True)

    r = await client.get("/partner/v1/mcp/usage", headers=h, params={
        "since": (today - timedelta(days=1)).isoformat(), "tool_name": "search"})
    body = r.json()
    assert [d["day"] for d in body["data"]] == [
        today.isoformat(), (today - timedelta(days=1)).isoformat()]
    first = body["data"][0]
    assert (first["request_bytes"], first["response_bytes"], first["bytes_count"]) == (10, 20, 1)
    assert first["owner_email"] == OWNER and first["asset_id"]
    assert body["meta"]["since"] == (today - timedelta(days=1)).isoformat()

    r = await client.get("/partner/v1/mcp/usage", headers=h, params={
        "since": today.isoformat(), "until": date(2020, 1, 1).isoformat()})
    assert r.status_code == 400
