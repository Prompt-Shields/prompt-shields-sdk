"""OTLP/HTTP GenAI ingest against a real PostgreSQL database
(PS_TEST_DATABASE_URL). Exercises tenant auth, the writer and asset dedup
end to end, and checks that no content reaches any stored row."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from collector.app import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

URL = "/ingest/otel/v1/traces"
HEADERS = {"Authorization": "Bearer ps-test-key-12345", "Content-Type": "application/json"}
SECRET = "my social security number is 123-45-6789"


def _attr(key, value):
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": value}}


def _span(attrs, start="1700000000000000000", end="1700000000450000000", events=None):
    return {
        "traceId": "5b8efff798038103d269b633813fc60c",
        "spanId": "eee19b7ec3c1b174",
        "name": "chat",
        "kind": 3,
        "startTimeUnixNano": start,
        "endTimeUnixNano": end,
        "attributes": [_attr(k, v) for k, v in attrs.items()],
        "events": events or [],
    }


def _payload(*spans, resource=None):
    resource = {"service.name": "hr-screener"} if resource is None else resource
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_attr(k, v) for k, v in resource.items()]},
                "scopeSpans": [{"scope": {"name": "x"}, "spans": list(spans)}],
            }
        ]
    }


GENAI = {
    "gen_ai.operation.name": "chat",
    "gen_ai.provider.name": "openai",
    "gen_ai.request.model": "gpt-4o",
    "gen_ai.response.model": "gpt-4o-2024-08-06",
    "gen_ai.usage.input_tokens": 150,
    "gen_ai.usage.output_tokens": 300,
    "gen_ai.conversation.id": "conv-1",
}

CONTENT = {
    "gen_ai.prompt.0.content": SECRET,
    "gen_ai.completion.0.content": SECRET,
    "gen_ai.input.messages": SECRET,
    "gen_ai.output.messages": SECRET,
    "gen_ai.system_instructions": SECRET,
    "gen_ai.tool.call.arguments": SECRET,
    "gen_ai.tool.call.result": SECRET,
    "gen_ai.tool.definitions": SECRET,
    "llm.prompts": SECRET,
    "input.value": SECRET,
    "output.value": SECRET,
    "llm.input_messages.0.message.content": SECRET,
}

EVENTS = [
    {"name": "gen_ai.user.message", "attributes": [_attr("content", SECRET)]},
    {"name": "gen_ai.choice", "attributes": [_attr("message", SECRET)]},
]


async def _dump(conn) -> str:
    rows = []
    for table in ("ai_assets", "ai_usage_events"):
        res = await conn.execute(text(f"select row_to_json(t)::text from {table} t"))
        rows += [r[0] for r in res]
    return "\n".join(rows)


@pytest.mark.asyncio
async def test_otel_ingest_writes_metadata_only_rows(seeded_db):
    body = _payload(
        _span({**GENAI, **CONTENT}, events=EVENTS),
        _span({"http.method": "GET", "http.url": SECRET}),
        _span({**GENAI, "gen_ai.usage.input_tokens": 10, "gen_ai.usage.output_tokens": 20}),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        unauth = await c.post(URL, content=json.dumps(body),
                              headers={"Content-Type": "application/json"})
        resp = await c.post(URL, content=json.dumps(body), headers=HEADERS)
    assert unauth.status_code == 401
    assert resp.status_code == 200, resp.text
    assert resp.json() == {}

    async with seeded_db.connect() as conn:
        assets = (await conn.execute(text(
            "select a.vendor, a.model, a.calling_service, a.discovery_source, a.tenant_id = t.id "
            "from ai_assets a join tenants t on t.id = a.tenant_id"
        ))).all()
        events = (await conn.execute(text(
            "select tokens_in, tokens_out, latency_ms, source, session_id, prompt_hash, "
            "tool_calls_used, timestamp > now() - interval '1 minute' "
            "from ai_usage_events order by tokens_in"
        ))).all()
        dump = await _dump(conn)

    # Two GenAI spans with the same vendor/model dedup to one asset.
    assert len(assets) == 1
    vendor, model, service, sources, same_tenant = assets[0]
    assert (vendor, model, service, sources, same_tenant) == (
        "openai", "gpt-4o", "hr-screener", ["otel"], True
    )
    assert [tuple(e) for e in events] == [
        (10, 20, 450, "otel", "conv-1", None, None, True),
        (150, 300, 450, "otel", "conv-1", None, None, True),
    ]
    assert SECRET not in dump
    assert "123-45-6789" not in dump


@pytest.mark.asyncio
async def test_otel_and_sdk_events_share_one_asset(seeded_db):
    """The SDK keys assets on the requested model; OTel spans for the same
    deployment must land on the same asset, not a per-snapshot duplicate."""
    sdk_event = {"vendor": "openai", "model": "gpt-4o", "source": "sdk",
                 "calling_service": "hr-screener", "tokens_in": 1, "tokens_out": 1}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r1 = await c.post("/ingest/events", json={"events": [sdk_event]},
                          headers={"Authorization": HEADERS["Authorization"]})
        r2 = await c.post(URL, content=json.dumps(_payload(_span(GENAI))), headers=HEADERS)
    assert r1.status_code == 200 and r2.status_code == 200

    async with seeded_db.connect() as conn:
        assets = (await conn.execute(text(
            "select model, discovery_source, confidence from ai_assets"
        ))).all()
        n_events = (await conn.execute(text("select count(*) from ai_usage_events"))).scalar()
    assert len(assets) == 1, assets
    assert assets[0][0] == "gpt-4o"
    assert sorted(assets[0][1]) == ["otel", "sdk"]
    assert n_events == 2


@pytest.mark.asyncio
async def test_out_of_range_numbers_do_not_drop_the_batch(seeded_db):
    """Values that do not fit the INTEGER usage columns (or are not finite)
    are stored as NULL instead of failing the INSERT for every span."""
    raw = json.dumps(_payload(
        _span({**GENAI, "gen_ai.usage.input_tokens": 99999999999}),
        _span({**GENAI, "gen_ai.usage.input_tokens": -5}, start="0", end="9000000000000000000"),
        _span({**GENAI, "gen_ai.usage.input_tokens": 7}),
    )).replace('{"intValue": "300"}', '{"doubleValue": Infinity}', 1)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(URL, content=raw, headers=HEADERS)
    assert resp.status_code == 200, resp.text

    async with seeded_db.connect() as conn:
        rows = (await conn.execute(text(
            "select tokens_in, tokens_out, latency_ms from ai_usage_events "
            "order by tokens_in nulls first, latency_ms nulls first"
        ))).all()
    assert [tuple(r) for r in rows] == [(None, 300, None), (None, None, 450), (7, 300, 450)]


FIXTURES = Path(__file__).parent / "fixtures" / "otel"
OPENLLMETRY = "openllmetry-0.33-openai-joke-workflow"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("suffix", "content_type"),
    [(".pb", "application/x-protobuf"), (".json", "application/json")],
)
async def test_openllmetry_demo_spans_create_assets(seeded_db, suffix, content_type):
    """#92: the trace OpenLLMetry's demo app exports, in its default
    protobuf encoding and in JSON, creates one asset per model."""
    body = (FIXTURES / (OPENLLMETRY + suffix)).read_bytes()
    headers = {**HEADERS, "Content-Type": content_type}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(URL, content=body, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith(content_type)

    async with seeded_db.connect() as conn:
        assets = (await conn.execute(text(
            "select vendor, model, calling_service, discovery_source from ai_assets "
            "order by model"
        ))).all()
        events = (await conn.execute(text(
            "select tokens_in, tokens_out, latency_ms, source, served_model "
            "from ai_usage_events order by tokens_in"
        ))).all()
        dump = await _dump(conn)
    assert [tuple(a) for a in assets] == [
        ("openai", "davinci-002", "joke_generation_service", ["otel"]),
        ("openai", "gpt-3.5-turbo", "joke_generation_service", ["otel"]),
    ]
    assert [tuple(e) for e in events] == [
        (15, 27, 1283, "otel", "gpt-3.5-turbo-0125"),
        (42, 9, 632, "otel", "davinci-002"),
        (46, 33, 1185, "otel", "gpt-3.5-turbo-0125"),
    ]
    for content in ("Tell me a joke", "OpenTelemetry collector", "Captain Telemetry"):
        assert content not in dump
