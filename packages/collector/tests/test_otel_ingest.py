"""OTLP/HTTP GenAI ingest. No Postgres: tenant auth and the writer are mocked."""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from collector import otel_ingest
from collector.app import app
from collector.otel_mapping import iter_spans, map_span, safe_attributes
from httpx import ASGITransport, AsyncClient

URL = "/ingest/otel/v1/traces"
AUTH = {"Authorization": "Bearer ps-test-key-12345"}
TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
SECRET = "my social security number is 123-45-6789"


def _attr(key, value):
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    else:
        v = {"stringValue": value}
    return {"key": key, "value": v}


def _span(attrs, *, name="chat gpt-4o", status=None, events=None):
    span = {
        "traceId": "5b8efff798038103d269b633813fc60c",
        "spanId": "eee19b7ec3c1b174",
        "name": name,
        "kind": 3,
        "startTimeUnixNano": "1700000000000000000",
        "endTimeUnixNano": "1700000000450000000",
        "attributes": [_attr(k, v) for k, v in attrs.items()],
    }
    if status is not None:
        span["status"] = status
    if events is not None:
        span["events"] = events
    return span


def _payload(*spans, resource=None):
    resource = resource or {
        "service.name": "hr-screener",
        "deployment.environment.name": "prod",
    }
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_attr(k, v) for k, v in resource.items()]},
                "scopeSpans": [
                    {
                        "scope": {"name": "opentelemetry.instrumentation.openai"},
                        "spans": list(spans),
                    }
                ],
            }
        ]
    }


GENAI_ATTRS = {
    "gen_ai.operation.name": "chat",
    "gen_ai.provider.name": "openai",
    "gen_ai.request.model": "gpt-4o",
    "gen_ai.response.model": "gpt-4o-2024-08-06",
    "gen_ai.usage.input_tokens": 150,
    "gen_ai.usage.output_tokens": 300,
}

CONTENT_ATTRS = {
    "gen_ai.prompt.0.role": "user",
    "gen_ai.prompt.0.content": SECRET,
    "gen_ai.completion.0.content": SECRET,
    "gen_ai.input.messages": json.dumps(
        [{"role": "user", "parts": [{"content": SECRET}]}]
    ),
    "gen_ai.output.messages": SECRET,
    "gen_ai.system_instructions": SECRET,
}

CONTENT_EVENTS = [
    {"name": "gen_ai.user.message", "attributes": [_attr("content", SECRET)]},
    {"name": "gen_ai.choice", "attributes": [_attr("message", SECRET)]},
]


# --- mapping -----------------------------------------------------------------


def test_maps_genai_semconv_attributes():
    span, res = next(iter_spans(_payload(_span(GENAI_ATTRS))))
    m = map_span(span, res)
    assert m.vendor == "openai"
    assert m.requested_model == "gpt-4o"
    assert m.served_model == "gpt-4o-2024-08-06"
    assert m.model == "gpt-4o"
    assert m.api_surface == "chat"
    assert (m.tokens_in, m.tokens_out) == (150, 300)
    assert m.latency_ms == 450
    assert m.is_error is False
    assert m.calling_service == "hr-screener"
    assert m.environment == "prod"


def test_legacy_gen_ai_system_and_token_names():
    attrs = {
        "gen_ai.system": "Anthropic",
        "gen_ai.request.model": "claude-x",
        "gen_ai.usage.prompt_tokens": 10,
        "gen_ai.usage.completion_tokens": 20,
    }
    span, res = next(iter_spans(_payload(_span(attrs))))
    m = map_span(span, res)
    assert m.vendor == "anthropic"
    assert m.model == "claude-x"
    assert m.served_model is None
    assert (m.tokens_in, m.tokens_out) == (10, 20)


@pytest.mark.parametrize(
    "status", [{"code": 2, "message": "boom"}, {"code": "STATUS_CODE_ERROR"}]
)
def test_error_status(status):
    span, res = next(iter_spans(_payload(_span(GENAI_ATTRS, status=status))))
    assert map_span(span, res).is_error is True


def test_non_genai_span_ignored():
    span, res = next(iter_spans(_payload(_span({"http.method": "GET"}, name="GET /"))))
    assert map_span(span, res) is None


def test_genai_span_without_vendor_ignored():
    span, res = next(iter_spans(_payload(_span({"gen_ai.operation.name": "chat"}))))
    assert map_span(span, res) is None


def test_content_attributes_dropped():
    raw = _span({**GENAI_ATTRS, **CONTENT_ATTRS}, events=CONTENT_EVENTS)["attributes"]
    safe = safe_attributes(raw)
    assert not any(k.startswith(("gen_ai.prompt", "gen_ai.completion")) for k in safe)
    assert "gen_ai.input.messages" not in safe
    assert "gen_ai.output.messages" not in safe
    assert "gen_ai.system_instructions" not in safe
    assert SECRET not in repr(safe)

    span, res = next(
        iter_spans(
            _payload(_span({**GENAI_ATTRS, **CONTENT_ATTRS}, events=CONTENT_EVENTS))
        )
    )
    assert SECRET not in repr(map_span(span, res))


def test_malformed_shapes_are_skipped():
    assert list(iter_spans({"resourceSpans": "nope"})) == []
    assert list(iter_spans({"resourceSpans": [1, {"scopeSpans": {"x": 1}}]})) == []


# --- route -------------------------------------------------------------------


@pytest.fixture
def mocked():
    writer = AsyncMock(return_value=1)
    with (
        patch.object(otel_ingest, "resolve_tenant", AsyncMock(return_value=TENANT)),
        patch.object(otel_ingest, "write_spans", writer),
        patch.object(otel_ingest, "record_otel", AsyncMock()),
    ):
        yield writer


async def _post(content=None, headers=None, **kw):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(URL, content=content, headers=headers or {}, **kw)


async def test_requires_auth():
    resp = await _post(json.dumps(_payload()), {"Content-Type": "application/json"})
    assert resp.status_code == 401


async def test_ingests_genai_spans_and_ignores_others(mocked):
    body = _payload(
        _span(GENAI_ATTRS), _span({"http.method": "GET"}), _span(GENAI_ATTRS)
    )
    resp = await _post(json.dumps(body), {**AUTH, "Content-Type": "application/json"})
    assert resp.status_code == 200
    assert resp.json() == {}
    mocked.assert_awaited_once()
    _db, tenant_id, spans = mocked.await_args.args
    assert tenant_id == TENANT
    assert len(spans) == 2
    assert all(s.vendor == "openai" for s in spans)


async def test_no_genai_spans_skips_writer(mocked):
    body = _payload(_span({"http.method": "GET"}))
    resp = await _post(json.dumps(body), {**AUTH, "Content-Type": "application/json"})
    assert resp.status_code == 200
    mocked.assert_not_awaited()


async def test_content_never_reaches_writer(mocked):
    body = _payload(_span({**GENAI_ATTRS, **CONTENT_ATTRS}, events=CONTENT_EVENTS))
    resp = await _post(json.dumps(body), {**AUTH, "Content-Type": "application/json"})
    assert resp.status_code == 200
    assert SECRET not in repr(mocked.await_args)


async def test_protobuf_rejected(mocked):
    resp = await _post(b"\x0a\x00", {**AUTH, "Content-Type": "application/x-protobuf"})
    assert resp.status_code == 415
    mocked.assert_not_awaited()


async def test_invalid_json_rejected(mocked):
    resp = await _post(b"{not json", {**AUTH, "Content-Type": "application/json"})
    assert resp.status_code == 400


async def test_body_size_bounded(mocked, monkeypatch):
    monkeypatch.setattr(otel_ingest, "MAX_BODY_BYTES", 100)
    resp = await _post(
        json.dumps(_payload(_span(GENAI_ATTRS))),
        {**AUTH, "Content-Type": "application/json"},
    )
    assert resp.status_code == 413
    mocked.assert_not_awaited()


async def test_span_count_bounded(mocked, monkeypatch):
    monkeypatch.setattr(otel_ingest, "MAX_SPANS", 2)
    body = _payload(*[_span(GENAI_ATTRS) for _ in range(3)])
    resp = await _post(json.dumps(body), {**AUTH, "Content-Type": "application/json"})
    assert resp.status_code == 413
    mocked.assert_not_awaited()


# --- writer ------------------------------------------------------------------


async def test_write_spans_stores_metadata_only():
    span, res = next(iter_spans(_payload(_span({**GENAI_ATTRS, **CONTENT_ATTRS}))))
    mapped = map_span(span, res)
    asset = SimpleNamespace(id=uuid.uuid4(), embedding=None)
    db = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    find = AsyncMock(return_value=(asset, True))
    flows = AsyncMock()
    with (
        patch.object(otel_ingest, "find_or_create_asset", find),
        patch.object(otel_ingest, "lock_asset_keys", AsyncMock()),
        patch.object(otel_ingest, "get_embedding", AsyncMock(return_value=None)),
        patch.object(otel_ingest, "record_event_flows", flows),
        patch.object(otel_ingest, "AssetChanges") as changes,
    ):
        changes.return_value.enqueue = AsyncMock()
        created = await otel_ingest.write_spans(db, TENANT, [mapped])

    assert created == 1
    kwargs = find.await_args.kwargs
    assert kwargs["source"] == "otel"
    assert kwargs["vendor"] == "openai"
    assert kwargs["model"] == "gpt-4o"
    event = db.add.call_args.args[0]
    assert event.source == "otel"
    assert (event.tokens_in, event.tokens_out, event.latency_ms) == (150, 300, 450)
    assert event.prompt_hash is None
    flows.assert_awaited_once_with(
        db, TENANT, asset, event, calling_service="hr-screener"
    )
    db.commit.assert_awaited_once()
    changes.return_value.record.assert_called_once_with(asset, True)
    changes.return_value.enqueue.assert_awaited_once_with(db, TENANT)


def test_route_reuses_shared_tenant_auth():
    # Sanity: the route reuses the shared tenant resolver, not a copy.
    from collector import auth

    assert otel_ingest.resolve_tenant is auth.resolve_tenant
