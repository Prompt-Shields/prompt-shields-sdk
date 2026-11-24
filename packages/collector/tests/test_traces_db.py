"""Agent traces (#385) against a real PostgreSQL database
(PS_TEST_DATABASE_URL) with row-level security enforced: the spans of a
LangChain-style agent turn, sent by the SDK and by the gateway in separate
OTLP requests, are stored for the ingest key's tenant with their parents,
read back through /partner/v1/traces with the ``traces:read`` scope, purged
with the tenant's retention period, cleared by user erasure, and the
migration goes down and up."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from collector import retention, rls
from collector import traces as tr
from collector.app import app
from collector.config import settings
from db.models import AIUsageEvent, PartnerCredential, TenantSecret, TraceSpan
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, select, update

from tests.test_rls_db import (  # noqa: F401 - fixture and keys
    KEY_A,
    KEY_B,
    PARTNER_KEY_A,
    PARTNER_KEY_B,
    rls_db,
)

URL = "/ingest/otel/v1/traces"
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
OTHER_TRACE = "00f067aa0ba902b700f067aa0ba902b7"
APP_PARENT = "00f067aa0ba902b7"  # the app's own tracer's span (inbound traceparent)
SECRET = "my social security number is 123-45-6789"
USER = "user-42"


def _head(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _attr(key, value):
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    else:
        v = {"stringValue": value}
    return {"key": key, "value": v}


def _ns(dt: datetime) -> str:
    return str(int(dt.timestamp() * 1_000_000_000))


BASE = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)


def _span(span_id, name, attrs, *, parent=None, trace=TRACE, start=0, ms=100,
          kind=1, status=None):
    span = {
        "traceId": trace,
        "spanId": span_id,
        "name": name,
        "kind": kind,
        "startTimeUnixNano": _ns(BASE + timedelta(milliseconds=start)),
        "endTimeUnixNano": _ns(BASE + timedelta(milliseconds=start + ms)),
        "attributes": [_attr(k, v) for k, v in attrs.items()],
    }
    if parent:
        span["parentSpanId"] = parent
    if status is not None:
        span["status"] = status
    return span


def _payload(spans, service):
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        _attr("service.name", service),
                        _attr("deployment.environment.name", "prod"),
                    ]
                },
                "scopeSpans": [{"scope": {"name": "test"}, "spans": spans}],
            }
        ]
    }


def _chat(i, span_id, parent, *, start, provider="openai", **extra):
    return _span(
        span_id,
        "chat gpt-4o",
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": provider,
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.usage.input_tokens": 100 + i,
            "gen_ai.usage.output_tokens": 10 + i,
            "gen_ai.conversation.id": "sess-1",
            "user.id": USER,
            # Content: dropped before mapping.
            "gen_ai.input.messages": SECRET,
            **extra,
        },
        parent=parent,
        start=start,
        kind=3,
    )


def _tool(span_id, tool, parent, *, start, status=None):
    return _span(
        span_id,
        f"execute_tool {tool}",
        {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": tool,
            "gen_ai.tool.call.arguments": SECRET,
            "gen_ai.tool.call.result": SECRET,
            "user.id": USER,
        },
        parent=parent,
        start=start,
        status=status,
    )


# SDK spans: one agent turn under the app's own span, three model calls and
# two tools; the HTTP call inside the second tool is a plain OTel span.
SDK_SPANS = [
    _span("a000000000000001", "invoke_agent support", {"gen_ai.operation.name": "invoke_agent"},
          parent=APP_PARENT, ms=2000),
    _chat(1, "c000000000000001", "a000000000000001", start=10),
    _tool("b000000000000001", "lookup_order", "a000000000000001", start=300),
    _chat(2, "c000000000000002", "a000000000000001", start=500),
    _tool("b000000000000002", "refund", "a000000000000001", start=800,
          status={"code": 2, "message": SECRET}),
    _span("e000000000000001", "POST", {"http.request.method": "POST",
          "url.full": f"https://billing.internal/refund?note={SECRET}"},
          parent="b000000000000002", start=810, ms=50, kind=3),
    _chat(3, "c000000000000003", "a000000000000001", start=1200),
]
# Gateway spans: one CLIENT span per model call, child of the SDK's chat
# span (the traceparent the SDK sent).
GATEWAY_SPANS = [
    _chat(i, f"d00000000000000{i}", f"c00000000000000{i}", start=20 + 400 * i)
    for i in (1, 2, 3)
]


async def _rows(factory, tenant) -> list[TraceSpan]:
    async with factory() as s:
        await rls.set_tenant(s, tenant)
        return list(
            (
                await s.execute(
                    select(TraceSpan).where(TraceSpan.name != "seed").order_by(TraceSpan.span_id)
                )
            ).scalars()
        )


async def _ingest(c, key=KEY_A):
    r = await c.post(URL, json=_payload(SDK_SPANS, "support-agent"), headers=_head(key))
    assert r.status_code == 200 and r.json() == {}, r.text
    r = await c.post(
        URL, json=_payload(GATEWAY_SPANS, "prompt-shields-gateway"), headers=_head(key)
    )
    assert r.status_code == 200 and r.json() == {}, r.text


async def _grant(db):
    async with db["admin"]() as s:
        await s.execute(update(PartnerCredential).values(scopes=["traces:read"]))
        await s.commit()


async def test_agent_turn_is_one_trace_with_every_span_parented(rls_db):  # noqa: F811
    a, b = rls_db["a"], rls_db["b"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await _ingest(c)
        # A trace with no GenAI span is not kept.
        r = await c.post(
            URL,
            json=_payload(
                [_span("f000000000000001", "GET /health", {"http.request.method": "GET"},
                       trace=OTHER_TRACE)],
                "web",
            ),
            headers=_head(KEY_A),
        )
        assert r.status_code == 200

    rows = await _rows(rls_db["plain"], a)
    assert {r.trace_id for r in rows} == {TRACE}
    by_id = {r.span_id: r for r in rows}
    assert len(by_id) == 10  # agent, 3 SDK chats, 2 tools, 1 HTTP, 3 gateway chats
    root = by_id["a000000000000001"]
    # The app's own tracer's span id is kept as the parent (inbound traceparent).
    assert root.parent_span_id == APP_PARENT and root.operation == "invoke_agent"
    for i in (1, 2, 3):
        sdk, gw = by_id[f"c00000000000000{i}"], by_id[f"d00000000000000{i}"]
        assert sdk.parent_span_id == root.span_id
        assert gw.parent_span_id == sdk.span_id
        assert gw.service_name == "prompt-shields-gateway"
        assert (sdk.kind, sdk.operation, sdk.provider, sdk.request_model) == (
            "client", "chat", "openai", "gpt-4o"
        )
        assert (sdk.tokens_in, sdk.tokens_out) == (100 + i, 10 + i)
        assert sdk.session_id == "sess-1"
    for tool in ("b000000000000001", "b000000000000002"):
        assert by_id[tool].parent_span_id == root.span_id
        assert by_id[tool].operation == "execute_tool"
    assert by_id["b000000000000002"].status == "error"
    assert by_id["b000000000000001"].status == "unset"
    http = by_id["e000000000000001"]
    assert http.parent_span_id == "b000000000000002" and http.operation is None
    assert root.duration_ms == 2000 and root.environment == "prod"

    # The user id is stored as the salted hash only.
    async with rls_db["plain"]() as s:
        await rls.set_tenant(s, a)
        salt = (await s.execute(select(TenantSecret.user_id_salt))).scalar_one()
        events = (
            await s.execute(select(AIUsageEvent).where(AIUsageEvent.source == "otel"))
        ).scalars().all()
    expected = hmac.new(salt.encode(), USER.encode(), hashlib.sha256).hexdigest()
    assert by_id["c000000000000001"].user_id_hash == expected
    # Usage events from the same spans link to the trace.
    assert len(events) == 6 and {e.trace_id for e in events} == {TRACE}

    # No content anywhere: not in any column of any span.
    for row in rows:
        for column in TraceSpan.__table__.columns:
            assert SECRET not in str(getattr(row, column.name)), column.name

    # Tenant isolation.
    assert await _rows(rls_db["plain"], b) == []


async def test_resent_and_late_spans(rls_db):  # noqa: F811
    a = rls_db["a"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await _ingest(c)
        await _ingest(c)  # an exporter retry: stored once
        late = _span("e000000000000009", "GET", {"http.request.method": "GET"},
                     parent="b000000000000001", start=320, ms=5, kind=3)
        r = await c.post(URL, json=_payload([late], "support-agent"), headers=_head(KEY_A))
        assert r.status_code == 200
    rows = await _rows(rls_db["plain"], a)
    assert len(rows) == 11
    assert "e000000000000009" in {r.span_id for r in rows}


async def test_protobuf_ids_are_stored_as_hex(rls_db):  # noqa: F811
    pb = pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")
    msg = pb.ExportTraceServiceRequest()
    span = msg.resource_spans.add().scope_spans.add().spans.add()
    span.trace_id = bytes.fromhex(TRACE)
    span.span_id = bytes.fromhex("c000000000000001")
    span.parent_span_id = bytes.fromhex("a000000000000001")
    span.name = "chat gpt-4o"
    span.kind = 3
    span.start_time_unix_nano = int(BASE.timestamp() * 1e9)
    span.end_time_unix_nano = span.start_time_unix_nano + 10**8
    for k, v in (("gen_ai.operation.name", "chat"), ("gen_ai.provider.name", "openai")):
        kv = span.attributes.add()
        kv.key, kv.value.string_value = k, v
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            URL,
            content=msg.SerializeToString(),
            headers={**_head(KEY_A), "Content-Type": "application/x-protobuf"},
        )
        assert r.status_code == 200, r.text
    (row,) = await _rows(rls_db["plain"], rls_db["a"])
    assert (row.trace_id, row.span_id, row.parent_span_id) == (
        TRACE, "c000000000000001", "a000000000000001"
    )


async def test_store_can_be_turned_off(rls_db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "trace_store_enabled", False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await _ingest(c)
    assert await _rows(rls_db["plain"], rls_db["a"]) == []


async def test_trace_api_scope_isolation_filters_and_paging(rls_db):  # noqa: F811
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await _ingest(c)
        other = [_chat(9, "c000000000000009", None, start=5000, provider="anthropic")]
        for s in other:
            s["traceId"] = OTHER_TRACE
        await c.post(URL, json=_payload(other, "batch-job"), headers=_head(KEY_A))
        # Tenant b has a trace with the same id: never visible to a.
        await c.post(URL, json=_payload(SDK_SPANS[:2], "b-agent"), headers=_head(KEY_B))

        h = _head(PARTNER_KEY_A)
        r = await c.get("/partner/v1/traces", headers=h)
        assert r.status_code == 403  # registry:read does not grant it
        await _grant(rls_db)

        r = await c.get("/partner/v1/traces", headers=h)
        assert r.status_code == 200, r.text
        page = r.json()
        assert [t["trace_id"] for t in page["data"]] == [OTHER_TRACE, TRACE]
        assert page["meta"]["has_more"] is False
        turn = page["data"][1]
        assert turn["span_count"] == 10 and turn["error_count"] == 1
        assert turn["status"] == "error"
        assert turn["root_name"] == "invoke_agent support"
        assert turn["root_service_name"] == "support-agent"
        assert turn["session_id"] == "sess-1"
        assert turn["tokens_in"] == 2 * (101 + 102 + 103)
        assert turn["duration_ms"] == 2000

        r = await c.get("/partner/v1/traces", params={"status": "ok"}, headers=h)
        assert [t["trace_id"] for t in r.json()["data"]] == [OTHER_TRACE]
        r = await c.get("/partner/v1/traces", params={"session_id": "sess-1"}, headers=h)
        ids = [t["trace_id"] for t in r.json()["data"]]
        assert ids == [OTHER_TRACE, TRACE]  # both have chats in sess-1
        user_hash = turn["user_id_hash"]
        r = await c.get("/partner/v1/traces", params={"user_id_hash": user_hash}, headers=h)
        assert len(r.json()["data"]) == 2
        r = await c.get("/partner/v1/traces", params={"user_id_hash": "0" * 64}, headers=h)
        assert r.json()["data"] == []
        r = await c.get(
            "/partner/v1/traces",
            params={"since": "2020-01-01T00:00:00Z", "until": "2020-01-02T00:00:00Z"},
            headers=h,
        )
        assert r.json()["data"] == []

        got, cursor = [], None
        while True:
            params = {"limit": 1, **({"cursor": cursor} if cursor else {})}
            page = (await c.get("/partner/v1/traces", params=params, headers=h)).json()
            got += [t["trace_id"] for t in page["data"]]
            cursor = page["meta"]["next_cursor"]
            if not page["meta"]["has_more"]:
                break
        assert got == [OTHER_TRACE, TRACE]

        r = await c.get(f"/partner/v1/traces/{TRACE}", headers=h)
        assert r.status_code == 200, r.text
        trace = r.json()
        assert trace["trace_id"] == TRACE and trace["span_count"] == 10
        assert trace["spans"][0]["span_id"] == "a000000000000001"
        assert set(trace["spans"][0]) == set(tr.SPAN_FIELDS) | {"start_time", "end_time"}
        r = await c.get(f"/partner/v1/traces/{TRACE.upper()}", headers=h)
        assert r.status_code == 200

        # Tenant b's partner sees only its own copy of the trace.
        await _grant(rls_db)
        r = await c.get(f"/partner/v1/traces/{TRACE}", headers=_head(PARTNER_KEY_B))
        assert r.json()["span_count"] == 2
        r = await c.get(f"/partner/v1/traces/{OTHER_TRACE}", headers=_head(PARTNER_KEY_B))
        assert r.status_code == 404

        for bad in ("nothex", "0" * 32, "!!"):
            r = await c.get(f"/partner/v1/traces/{bad}", headers=h)
            assert r.status_code == 400, bad
        r = await c.get("/partner/v1/traces", params={"cursor": "!!"}, headers=h)
        assert r.status_code == 400
        r = await c.get("/partner/v1/traces", params={"status": "maybe"}, headers=h)
        assert r.status_code == 422


async def test_user_erasure_clears_user_fields_on_spans(rls_db):  # noqa: F811
    a, b = rls_db["a"], rls_db["b"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await _ingest(c)
        await _ingest(c, KEY_B)
        r = await c.post("/api/v1/admin/users/erase", headers=_head(KEY_A),
                         json={"user_id": USER})
        assert r.status_code == 200, r.text
        # 3 SDK chats, 2 tools and 3 gateway chats carry the user id.
        assert r.json()["rows_erased"]["trace_spans"] == 8
        r = await c.post("/api/v1/admin/users/erase", headers=_head(KEY_A),
                         json={"user_id": USER})
        assert r.json()["rows_erased"]["trace_spans"] == 0
    rows = await _rows(rls_db["plain"], a)
    assert len(rows) == 10
    assert all(r.user_id_hash is None and r.session_id is None for r in rows)
    assert {r.tokens_in for r in rows if r.span_id.startswith("c")} == {101, 102, 103}
    # The other tenant's spans keep theirs.
    assert sum(1 for r in await _rows(rls_db["plain"], b) if r.user_id_hash) == 8


async def test_retention_purges_old_spans(rls_db):  # noqa: F811
    a = rls_db["a"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await _ingest(c)
    long_ago = datetime.now(UTC) - timedelta(days=400)
    async with rls_db["admin"]() as s:
        await s.execute(
            update(TraceSpan)
            .where(TraceSpan.span_id.like("d%"))
            .values(start_time=long_ago)
        )
        await s.execute(
            update(TraceSpan)
            .where(TraceSpan.span_id == "a000000000000001")
            .values(received_at=long_ago)
        )
        await s.commit()
    async with rls_db["plain"]() as s:
        await rls.set_tenant(s, a)
        dry = await retention.purge_tenant_events(s, a, dry_run=True)
        result = await retention.purge_tenant_events(s, a)
        await s.commit()
    assert dry.trace_spans_deleted == 4
    assert result.trace_spans_deleted == 4
    assert len(await _rows(rls_db["plain"], a)) == 6


async def test_tenant_erasure_removes_spans(rls_db):  # noqa: F811
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await _ingest(c, KEY_B)
    async with rls_db["admin"]() as s:
        result = await retention.erase_tenant(s, rls_db["b"])
    assert result.rows_deleted["trace_spans"] == 10 + 1  # with the seed span
    assert await _rows(rls_db["plain"], rls_db["b"]) == []


# ──────────────────────────────────────────────────────────────────────
# Migration
# ──────────────────────────────────────────────────────────────────────


def _load_migration():
    pytest.importorskip("alembic")
    versions = Path(__file__).resolve().parents[2] / "db" / "alembic" / "versions"
    (path,) = versions.glob("*_trace_spans.py")
    spec = importlib.util.spec_from_file_location("migration_trace_spans", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_matches_the_model():
    m = _load_migration()
    assert m.TABLE == TraceSpan.__tablename__
    assert m.revision == "055"


async def test_migration_downgrade_and_upgrade(seeded_db):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    m = _load_migration()

    def run(sync_conn, step):
        ctx = MigrationContext.configure(sync_conn)
        with Operations.context(ctx):
            step()

    def describe(sync_conn):
        insp = inspect(sync_conn)
        if m.TABLE not in insp.get_table_names():
            return None
        return (
            {c["name"]: c["nullable"] for c in insp.get_columns(m.TABLE)},
            {i["name"] for i in insp.get_indexes(m.TABLE)},
            {u["name"] for u in insp.get_unique_constraints(m.TABLE)},
        )

    async with seeded_db.begin() as conn:
        model = await conn.run_sync(describe)
        await conn.run_sync(run, m.downgrade)
        assert await conn.run_sync(describe) is None
    async with seeded_db.begin() as conn:
        await conn.run_sync(run, m.upgrade)
        migrated = await conn.run_sync(describe)
        policies = (
            await conn.exec_driver_sql(
                f"SELECT policyname FROM pg_policies WHERE tablename = '{m.TABLE}'"
            )
        ).scalars().all()
    assert migrated == model
    assert policies == ["tenant_isolation"]
