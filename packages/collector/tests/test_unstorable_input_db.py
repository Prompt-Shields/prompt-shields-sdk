"""Strings the database cannot store, and deeply nested JSON, on every route
that stores client input (PS_TEST_DATABASE_URL). See collector/sanitize.py.

A NUL or a lone surrogate used to fail the whole write with a 500, which
senders retry until they give up, losing the good events in the same batch.
Ingest routes now drop the value per field (or skip the one event that
cannot be stored without it) and store the rest. Admin and partner routes
answer 422. A JSON body nested deeper than MAX_JSON_DEPTH is a 400
everywhere.
"""

from __future__ import annotations

import json

import pytest
from collector import webhooks
from collector.app import app
from collector.config import settings
from collector.sanitize import MAX_JSON_DEPTH
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

KEY = "ps-test-key-12345"
AUTH = {"Authorization": f"Bearer {KEY}"}
JSON_HEADERS = {**AUTH, "Content-Type": "application/json"}
# "QQ" marks a bad value in the stored rows; it cannot occur in a UUID.
BAD = {"nul": "QQ\u0000QQ", "surrogate": "QQ\ud800QQ"}
TOO_DEEP = "[" * (MAX_JSON_DEPTH + 1) + "]" * (MAX_JSON_DEPTH + 1)
VERY_DEEP = "[" * 100_000 + "]" * 100_000


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _body(value) -> str:
    # json.dumps escapes a lone surrogate as \ud800, as a client would;
    # httpx's json= would fail to encode it.
    return json.dumps(value)


async def _post(client: AsyncClient, url: str, value, headers=JSON_HEADERS):
    return await client.post(url, content=_body(value), headers=headers)


async def _scalar(engine, sql: str):
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).scalar()


async def _all(engine, sql: str):
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).all()


async def _dump(engine) -> str:
    """Every stored row of the tables ingest writes, as JSON text."""
    rows = []
    for table in (
        "ai_assets",
        "ai_usage_events",
        "data_flows",
        "telemetry_client_stats",
        "mcp_tool_usage",
        "partner_credentials",
    ):
        rows += [r[0] for r in await _all(engine, f"select row_to_json(t)::text from {table} t")]
    return "\n".join(rows)


def _assert_nothing_unstorable(dump: str) -> None:
    assert "QQ" not in dump  # no part of a bad value was stored


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 0)
    monkeypatch.setattr(settings, "kong_rate_limit_per_minute", 0)


# ---------------------------------------------------------------------------
# /ingest/events
# ---------------------------------------------------------------------------

GOOD_EVENT = {"vendor": "openai", "model": "gpt-4o", "tokens_in": 10}

# Every kind of field an event stores: the asset merge key, other asset
# and event strings, the lenient extended-usage strings, free JSON, and the
# data-flow metadata.
EVENT_FIELDS = {
    "model": lambda bad: bad,
    "use_case_name": lambda bad: bad,
    "business_unit": lambda bad: bad,
    "environment": lambda bad: bad,
    "owner_email": lambda bad: bad,
    "calling_service": lambda bad: bad,
    "data_classification": lambda bad: bad,
    "source": lambda bad: bad,
    "session_id": lambda bad: bad,
    "requested_model": lambda bad: bad,
    "served_model": lambda bad: bad,
    "framework": lambda bad: bad,
    "api_key_fingerprint": lambda bad: bad,
    "tool_calls_used": lambda bad: [{"name": "search", "arguments": {"q": bad}}],
    "data_sources": lambda bad: ["s3://ok", bad],
    "output_destination": lambda bad: bad,
    "detected_pii_types": lambda bad: [bad],
    "region": lambda bad: bad,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(BAD))
@pytest.mark.parametrize("field", sorted(EVENT_FIELDS))
async def test_events_drop_an_unstorable_field_and_keep_the_batch(seeded_db, field, kind):
    bad_event = {
        "vendor": "anthropic",
        "model": "claude-3-5-sonnet",
        field: EVENT_FIELDS[field](BAD[kind]),
    }
    async with _client() as c:
        resp = await _post(c, "/ingest/events", {"events": [GOOD_EVENT, bad_event]})
        loss = (await c.get("/telemetry/loss", headers=AUTH)).json()
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ingested": 2, "assets_created": 2, "rejected": 0}
    assert await _scalar(seeded_db, "select count(*) from ai_usage_events") == 2
    _assert_nothing_unstorable(await _dump(seeded_db))
    (client,) = loss["clients"]
    if field != "source":  # has a default ("sdk") to fall back to
        assert client["ignored_fields"].get(f"{field}:invalid") == 1


@pytest.mark.asyncio
async def test_events_drop_unstorable_json_keys(seeded_db):
    events = [
        {**GOOD_EVENT, "tool_calls_used": [{"name": "ok"}]},
        {**GOOD_EVENT, "tool_calls_used": [{"na\u0000me": "x"}]},
    ]
    async with _client() as c:
        resp = await _post(c, "/ingest/events", {"events": events})
    assert resp.status_code == 200, resp.text
    stored = await _all(seeded_db, "select tool_calls_used from ai_usage_events")
    assert sorted(map(str, (r[0] for r in stored))) == ["None", "[{'name': 'ok'}]"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(BAD))
async def test_events_skip_only_the_event_whose_vendor_cannot_be_stored(seeded_db, kind):
    events = [
        GOOD_EVENT,
        {"vendor": BAD[kind], "model": "gpt-4o"},
        {"vendor": "anthropic", "model": "claude-3-haiku", "region": BAD[kind]},
    ]
    async with _client() as c:
        resp = await _post(
            c, "/ingest/events", {"events": events, "metadata": {"client_id": "app-1"}}
        )
        loss = (await c.get("/telemetry/loss", headers=AUTH)).json()
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ingested": 2, "assets_created": 2, "rejected": 1}
    vendors = await _all(seeded_db, "select vendor from ai_assets order by vendor")
    assert [v[0] for v in vendors] == ["anthropic", "openai"]
    _assert_nothing_unstorable(await _dump(seeded_db))
    (client,) = loss["clients"]
    assert client["client_id"] == "app-1"
    assert client["received_events"] == 2
    assert client["collector_rejected"] == {"unstorable": 1}
    # Field counts line up with the stored events, not the skipped one.
    assert client["ignored_fields"] == {"region:invalid": 1}


@pytest.mark.asyncio
async def test_events_with_only_unstorable_vendors(seeded_db):
    async with _client() as c:
        resp = await _post(c, "/ingest/events", {"events": [{"vendor": BAD["nul"]}]})
    assert resp.json() == {"ingested": 0, "assets_created": 0, "rejected": 1}
    assert await _scalar(seeded_db, "select count(*) from ai_usage_events") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(BAD))
async def test_events_unstorable_metadata_and_unknown_field_names(seeded_db, kind):
    bad = BAD[kind]
    body = {
        "events": [{**GOOD_EVENT, f"extra_{bad}": 1, "extra_ok": 1}],
        "metadata": {"client_id": bad, "seq": 1, "dropped": {bad: 3, "queue_full": 2}},
    }
    async with _client() as c:
        resp = await _post(c, "/ingest/events", body)
        sdk_named = await _post(
            c,
            "/ingest/events",
            {"events": [GOOD_EVENT], "metadata": {"client_id": "app-2", "seq": 1, "sdk": bad,
                                                   "dropped": {bad: 3, "queue_full": 2}}},
        )
        loss = (await c.get("/telemetry/loss", headers=AUTH)).json()
    assert resp.status_code == 200, resp.text
    assert sdk_named.status_code == 200, sdk_named.text
    _assert_nothing_unstorable(await _dump(seeded_db))
    clients = {x["client_id"]: x for x in loss["clients"]}
    # An unstorable client id falls back to the unidentified client.
    assert set(clients) == {"unidentified", "app-2"}
    assert clients["unidentified"]["ignored_fields"] == {"extra_ok": 1}
    assert clients["app-2"]["sdk"] is None
    assert clients["app-2"]["sdk_dropped"] == {"queue_full": 2}


@pytest.mark.asyncio
async def test_events_prompt_text_with_a_lone_surrogate_is_hashed(seeded_db):
    events = [{**GOOD_EVENT, "prompt_text": "hello\ud800"}, {**GOOD_EVENT, "prompt_text": "a\u0000b"}]
    async with _client() as c:
        resp = await _post(c, "/ingest/events", {"events": events})
    assert resp.status_code == 200, resp.text
    hashes = await _all(seeded_db, "select prompt_hash from ai_usage_events")
    assert all(h[0] and len(h[0]) == 64 for h in hashes)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [TOO_DEEP, VERY_DEEP], ids=["over-limit", "recursion"])
async def test_events_deep_nesting_is_a_400(seeded_db, body):
    payload = '{"events": [{"vendor": "openai", "tool_calls_used": ' + body + "}]}"
    async with _client() as c:
        resp = await c.post("/ingest/events", content=payload, headers=JSON_HEADERS)
        at_limit = await c.post(
            "/ingest/events",
            content='{"events": [{"vendor": "openai", "tool_calls_used": [{"a": '
            + "[" * (MAX_JSON_DEPTH - 5) + "]" * (MAX_JSON_DEPTH - 5)
            + "}]}]}",
            headers=JSON_HEADERS,
        )
    assert resp.status_code == 400
    assert resp.json()["detail"]["detail"] == (
        f"JSON body is nested deeper than {MAX_JSON_DEPTH} levels"
    )
    assert at_limit.status_code == 200, at_limit.text
    assert await _scalar(seeded_db, "select count(*) from ai_usage_events") == 1


@pytest.mark.asyncio
async def test_events_validation_error_echoing_a_surrogate_is_a_422(seeded_db):
    # Missing vendor: FastAPI's 422 echoes the event, which holds a lone
    # surrogate in a field it does not drop (unknown fields are kept).
    async with _client() as c:
        resp = await _post(c, "/ingest/events", {"events": [{"other": "x\ud800"}]})
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["input"] == {"other": "x�"}


# ---------------------------------------------------------------------------
# /ingest/otel/v1/traces
# ---------------------------------------------------------------------------

OTEL_URL = "/ingest/otel/v1/traces"


def _attr(key, value):
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": value}}


def _span(attrs: dict) -> dict:
    return {
        "traceId": "5b8efff798038103d269b633813fc60c",
        "spanId": "eee19b7ec3c1b174",
        "name": "chat",
        "kind": 3,
        "startTimeUnixNano": "1700000000000000000",
        "endTimeUnixNano": "1700000000450000000",
        "attributes": [_attr(k, v) for k, v in attrs.items()],
    }


def _otel(*spans, resource=None) -> dict:
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
    "gen_ai.conversation.id": "conv-1",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(BAD))
@pytest.mark.parametrize(
    "key",
    [
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.conversation.id",
        "gen_ai.operation.name",
    ],
)
async def test_otel_drops_an_unstorable_attribute_and_keeps_the_request(seeded_db, key, kind):
    bad_span = _span({**GENAI, "gen_ai.provider.name": "anthropic", key: BAD[kind]})
    async with _client() as c:
        resp = await _post(c, OTEL_URL, _otel(_span(GENAI), bad_span))
        loss = (await c.get("/telemetry/loss", headers=AUTH)).json()
    assert resp.status_code == 200, resp.text
    assert resp.json() == {}
    assert await _scalar(seeded_db, "select count(*) from ai_usage_events") == 2
    _assert_nothing_unstorable(await _dump(seeded_db))
    (client,) = loss["clients"]
    assert client["received_events"] == 2
    if key != "gen_ai.operation.name":  # read, never stored
        assert client["ignored_fields"] == {f"{key}:invalid": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(BAD))
async def test_otel_span_with_an_unstorable_vendor_is_rejected_alone(seeded_db, kind):
    bad_span = _span({**GENAI, "gen_ai.provider.name": BAD[kind]})
    async with _client() as c:
        resp = await _post(c, OTEL_URL, _otel(_span(GENAI), bad_span))
    assert resp.status_code == 200, resp.text
    assert resp.json()["partialSuccess"]["rejectedSpans"] == 1
    assert await _scalar(seeded_db, "select count(*) from ai_usage_events") == 1
    _assert_nothing_unstorable(await _dump(seeded_db))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(BAD))
async def test_otel_unstorable_resource_attributes(seeded_db, kind):
    resource = {"service.name": BAD[kind], "deployment.environment.name": BAD[kind]}
    async with _client() as c:
        resp = await _post(c, OTEL_URL, _otel(_span(GENAI), resource=resource))
        loss = (await c.get("/telemetry/loss", headers=AUTH)).json()
    assert resp.status_code == 200, resp.text
    rows = await _all(seeded_db, "select calling_service, environment from ai_assets")
    assert [tuple(r) for r in rows] == [(None, None)]
    # No service name to tell clients apart: the shared "otel" client.
    assert [x["client_id"] for x in loss["clients"]] == ["otel"]
    _assert_nothing_unstorable(await _dump(seeded_db))


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [TOO_DEEP, VERY_DEEP], ids=["over-limit", "recursion"])
async def test_otel_deep_nesting_is_a_400(seeded_db, body):
    async with _client() as c:
        resp = await c.post(
            OTEL_URL, content='{"resourceSpans": ' + body + "}", headers=JSON_HEADERS
        )
    assert resp.status_code == 400
    assert "nested deeper" in resp.json()["detail"]["detail"]


# ---------------------------------------------------------------------------
# /ingest/kong
# ---------------------------------------------------------------------------


def _kong(model="gpt-4o", consumer="payments-app", business_unit="Payments") -> dict:
    return {
        "consumer": {"username": consumer},
        "request": {"headers": {"x-ps-business-unit": business_unit}},
        "ai": {"proxy": {"meta": {"provider_name": "openai", "request_model": model}}},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(BAD))
@pytest.mark.parametrize("field", ["model", "consumer", "business_unit"])
async def test_kong_drops_an_unstorable_field_and_keeps_the_batch(seeded_db, field, kind):
    async with _client() as c:
        resp = await _post(c, "/ingest/kong", [_kong(), _kong(**{field: BAD[kind]})])
    assert resp.status_code == 200, resp.text
    assert resp.json()["ingested"] == 2
    assert await _scalar(seeded_db, "select count(*) from ai_usage_events") == 2
    _assert_nothing_unstorable(await _dump(seeded_db))


@pytest.mark.asyncio
async def test_kong_nesting_over_the_limit_is_a_400(seeded_db):
    # Within Python's recursion limit, so json.loads alone would accept it.
    async with _client() as c:
        resp = await c.post("/ingest/kong", content=TOO_DEEP, headers=JSON_HEADERS)
    assert resp.status_code == 400
    assert "nested deeper" in resp.json()["detail"]["detail"]


# ---------------------------------------------------------------------------
# /ingest/mcp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(BAD))
@pytest.mark.parametrize(
    "field", ["tool_name", "server_name", "calling_service", "environment", "transport"]
)
async def test_mcp_cleans_unstorable_names_and_keeps_the_batch(seeded_db, field, kind):
    good = {"tool_name": "search", "server_name": "docs", "transport": "stdio"}
    bad = {**good, "tool_name": "fetch", field: f"x{BAD[kind][2]}y"}
    async with _client() as c:
        resp = await _post(c, "/ingest/mcp", {"events": [good, bad]})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ingested": 2, "skipped": 0}
    rows = await _all(
        seeded_db,
        "select server_name, tool_name, calling_service, environment, transport "
        "from mcp_tool_usage",
    )
    assert len(rows) == 2
    # The NUL or surrogate is removed (it is not printable); the rest is kept.
    assert any("xy" in (v or "") for row in rows for v in row)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [TOO_DEEP, VERY_DEEP], ids=["over-limit", "recursion"])
async def test_mcp_deep_nesting_is_a_400(seeded_db, body):
    async with _client() as c:
        resp = await c.post(
            "/ingest/mcp", content='{"events": ' + body + "}", headers=JSON_HEADERS
        )
    assert resp.status_code == 400
    assert "nested deeper" in resp.json()["detail"]["detail"]


# ---------------------------------------------------------------------------
# Admin and partner write routes: 422
# ---------------------------------------------------------------------------

PARTNERS = "/api/v1/admin/partner-credentials"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(BAD))
@pytest.mark.parametrize(
    "body",
    [
        lambda bad: {"partner_name": bad},
        lambda bad: {"partner_name": "ardoq", "metadata": {"note": bad}},
        lambda bad: {"partner_name": "ardoq", "metadata": {bad: "x"}},
        lambda bad: {"partner_name": "ardoq", "metadata": {"a": [{"b": bad}]}},
    ],
    ids=["name", "metadata-value", "metadata-key", "metadata-nested"],
)
async def test_partner_credentials_refuse_unstorable_strings(seeded_db, body, kind):
    async with _client() as c:
        resp = await _post(c, PARTNERS, body(BAD[kind]))
        good = await _post(c, PARTNERS, {"partner_name": "ardoq", "metadata": {"a": [1]}})
    # Our message for NUL; pydantic's own str check refuses a lone
    # surrogate first. Either way the echoed input is encodable.
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"][:2] == ["body", body(BAD[kind]).popitem()[0]]
    assert good.status_code == 201, good.text
    assert await _scalar(seeded_db, "select count(*) from partner_credentials") == 1


@pytest.mark.asyncio
async def test_partner_credentials_deep_metadata_is_a_400(seeded_db):
    async with _client() as c:
        resp = await c.post(
            PARTNERS,
            content='{"partner_name": "p", "metadata": {"a": ' + TOO_DEEP + "}}",
            headers=JSON_HEADERS,
        )
    assert resp.status_code == 400
    assert "nested deeper" in resp.json()["detail"]["detail"]
    assert await _scalar(seeded_db, "select count(*) from partner_credentials") == 0


@pytest.fixture
def webhooks_on(monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret_key", "s" * 40)
    monkeypatch.setattr(settings, "webhook_secret_keys", "")

    async def public(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(webhooks, "_resolve_host", public)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(BAD))
async def test_webhook_subscriptions_refuse_unstorable_strings(seeded_db, webhooks_on, kind):
    bad = BAD[kind]
    subs = "/api/v1/admin/webhooks/subscriptions"
    url = "https://hooks.partner.example/ps"
    async with _client() as c:
        partner = await _post(
            c, PARTNERS, {"partner_name": "ardoq", "scopes": ["registry:read"]}
        )
        partner_id = partner.json()["id"]
        bad_url = await _post(c, subs, {"partner_id": partner_id, "url": url + bad})
        bad_desc = await _post(
            c, subs, {"partner_id": partner_id, "url": url, "description": bad}
        )
        good = await _post(c, subs, {"partner_id": partner_id, "url": url, "description": "d"})
        patched = await c.patch(
            f"{subs}/{good.json()['id']}",
            content=_body({"url": url + bad}),
            headers=JSON_HEADERS,
        )
    assert (bad_url.status_code, bad_desc.status_code) == (422, 422)
    assert good.status_code == 201, good.text
    assert patched.status_code == 422, patched.text
    assert await _scalar(seeded_db, "select count(*) from webhook_subscriptions") == 1
    assert await _scalar(seeded_db, "select url from webhook_subscriptions") == url


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(BAD))
@pytest.mark.parametrize("field", ["attested_by", "comment"])
async def test_attestations_refuse_unstorable_strings(seeded_db, field, kind):
    async with _client() as c:
        await _post(c, "/ingest/events", {"events": [{**GOOD_EVENT, "owner_email": "o@test.com"}]})
        asset_id = await _scalar(seeded_db, "select id from ai_assets")
        url = f"/api/v1/registry/assets/{asset_id}/attestations"
        good = {"field": "owner", "state": "attested", "attested_by": "ciso@test.com"}
        resp = await _post(c, url, {**good, field: BAD[kind]})
        ok = await _post(c, url, good)
    assert resp.status_code == 422, resp.text
    assert ok.status_code == 201, ok.text
    assert await _scalar(seeded_db, "select count(*) from asset_attestations") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(BAD))
async def test_risk_review_refuses_an_unstorable_reviewer(seeded_db, kind):
    url = "/api/v1/admin/risk-mappings/5b191df3-913b-44fe-9670-1f1ace212867/review"
    async with _client() as c:
        resp = await _post(c, url, {"status": "confirmed", "reviewed_by": BAD[kind]})
        unknown = await _post(c, url, {"status": "confirmed", "reviewed_by": "a@test.com"})
    assert resp.status_code == 422, resp.text
    assert unknown.status_code == 404  # a valid body reaches the lookup


@pytest.mark.asyncio
async def test_oauth_token_with_a_nul_client_id_is_invalid_client(seeded_db):
    async with _client() as c:
        resp = await c.post(
            "/oauth/token",
            data={"grant_type": "client_credentials", "client_id": "a\u0000b",
                  "client_secret": "x"},
        )
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "invalid_client"


@pytest.mark.asyncio
async def test_provider_key_link_with_a_nul_key_id_is_a_400(seeded_db):
    async with _client() as c:
        resp = await c.put(
            "/api/v1/admin/provider-key-fingerprints/openai/key%00id",
            content=_body({"api_key_fingerprint": "0123456789abcdef"}),
            headers=JSON_HEADERS,
        )
    assert resp.status_code == 400
    assert await _scalar(seeded_db, "select count(*) from provider_key_fingerprints") == 0
