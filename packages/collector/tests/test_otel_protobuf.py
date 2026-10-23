"""OTLP/HTTP protobuf on /ingest/otel/v1/traces, and the OpenLLMetry span
fixtures. No Postgres: tenant auth and the writer are mocked.

Fixtures in tests/fixtures/otel/:

* ``openllmetry-0.33-openai-joke-workflow.json``: the trace OpenLLMetry's
  demo app (the Traceloop "pirate joke" workflow, traceloop-sdk 0.33 with
  ``opentelemetry-instrumentation-openai``) exports, in OTLP/JSON. Four
  workflow/task spans (``traceloop.entity.input``/``output`` content, no
  ``gen_ai.*``) and three OpenAI spans (two chat, one legacy completion)
  with the pre-1.27 attributes OpenLLMetry still sends: ``gen_ai.system``
  "OpenAI", ``gen_ai.usage.prompt_tokens``/``completion_tokens`` and
  ``gen_ai.prompt.N.*``/``gen_ai.completion.N.*`` content.
* ``openllmetry-0.33-openai-joke-workflow.pb``: the same request as the
  protobuf ``ExportTraceServiceRequest`` the default
  (``http/protobuf``) exporter sends. Regenerate it with
  ``python packages/collector/tests/test_otel_protobuf.py`` after editing
  the JSON; ``test_protobuf_fixture_matches_json`` fails until you do.
"""

from __future__ import annotations

import base64
import copy
import json
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from collector import otel_ingest
from collector.app import app
from collector.otel_ingest import classify_spans
from collector.otlp_protobuf import decode_traces
from collector.sanitize import MAX_JSON_DEPTH
from google.protobuf.json_format import ParseDict
from httpx import ASGITransport, AsyncClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

FIXTURES = Path(__file__).parent / "fixtures" / "otel"
OPENLLMETRY_JSON = FIXTURES / "openllmetry-0.33-openai-joke-workflow.json"
OPENLLMETRY_PB = FIXTURES / "openllmetry-0.33-openai-joke-workflow.pb"

URL = "/ingest/otel/v1/traces"
AUTH = {"Authorization": "Bearer ps-test-key-12345"}
PROTOBUF = {**AUTH, "Content-Type": "application/x-protobuf"}
TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")

# Strings from the fixture's prompts, completions and entity input/output.
FIXTURE_CONTENT = (
    "Tell me a joke about opentelemetry",
    "OpenTelemetry collector",
    "pirate-like english",
    "Captain Telemetry",
)


def otlp_json_to_protobuf(payload: dict) -> bytes:
    """Encode an OTLP/JSON request as protobuf. OTLP/JSON writes trace and
    span ids as hex, protobuf's JSON mapping as base64."""
    payload = copy.deepcopy(payload)
    for rs in payload.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                for key in ("traceId", "spanId", "parentSpanId"):
                    if key in span:
                        span[key] = base64.b64encode(bytes.fromhex(span[key])).decode()
    message = ParseDict(payload, ExportTraceServiceRequest())
    return message.SerializeToString(deterministic=True)


def _attr(key, value):
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": value}}


def _payload(*attr_sets):
    spans = [
        {
            "traceId": "5b8efff798038103d269b633813fc60c",
            "spanId": "eee19b7ec3c1b174",
            "name": "chat",
            "kind": 3,
            "startTimeUnixNano": "1700000000000000000",
            "endTimeUnixNano": "1700000000450000000",
            "attributes": [_attr(k, v) for k, v in attrs.items()],
        }
        for attrs in attr_sets
    ]
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_attr("service.name", "svc")]},
                "scopeSpans": [{"scope": {"name": "x"}, "spans": spans}],
            }
        ]
    }


GENAI = {
    "gen_ai.operation.name": "chat",
    "gen_ai.provider.name": "openai",
    "gen_ai.request.model": "gpt-4o",
    "gen_ai.usage.input_tokens": 150,
    "gen_ai.usage.output_tokens": 300,
}


def _fixture_json() -> dict:
    return json.loads(OPENLLMETRY_JSON.read_text())


# --- fixtures and mapping ----------------------------------------------------


def test_protobuf_fixture_matches_json():
    assert OPENLLMETRY_PB.read_bytes() == otlp_json_to_protobuf(_fixture_json())


EXPECTED_OPENLLMETRY = [
    # vendor, requested, served, tokens in/out, latency, service
    ("openai", "gpt-3.5-turbo", "gpt-3.5-turbo-0125", 15, 27, 1283),
    ("openai", "gpt-3.5-turbo", "gpt-3.5-turbo-0125", 46, 33, 1185),
    ("openai", "davinci-002", "davinci-002", 42, 9, 632),
]


@pytest.mark.parametrize(
    "decode",
    [
        pytest.param(lambda: _fixture_json(), id="json"),
        pytest.param(lambda: decode_traces(OPENLLMETRY_PB.read_bytes()), id="protobuf"),
    ],
)
def test_openllmetry_spans_map_to_usage_events(decode):
    mapped, counts = classify_spans(decode())
    got = [
        (
            s.vendor,
            s.requested_model,
            s.served_model,
            s.tokens_in,
            s.tokens_out,
            s.latency_ms,
        )
        for s in mapped
    ]
    assert got == [row[:6] for row in EXPECTED_OPENLLMETRY]
    assert {s.calling_service for s in mapped} == {"joke_generation_service"}
    assert {s.model for s in mapped} == {"gpt-3.5-turbo", "davinci-002"}
    assert not any(s.is_error for s in mapped)
    # The workflow span and its three task spans are not GenAI spans.
    (c,) = counts.values()
    assert c.received == 3
    assert c.ignored == {"span:non_genai": 4}
    for text in FIXTURE_CONTENT:
        assert text not in repr(mapped)


def test_protobuf_decodes_to_the_otlp_json_shape():
    payload = decode_traces(OPENLLMETRY_PB.read_bytes())
    span = payload["resourceSpans"][0]["scopeSpans"][1]["spans"][0]
    assert span["kind"] == "SPAN_KIND_CLIENT"
    assert span["startTimeUnixNano"] == "1729675200004000000"
    assert {"key": "gen_ai.usage.prompt_tokens", "value": {"intValue": "15"}} in span[
        "attributes"
    ]


# --- route -------------------------------------------------------------------


@pytest.fixture
def mocked():
    writer = AsyncMock(return_value=2)
    with (
        patch.object(otel_ingest, "resolve_tenant", AsyncMock(return_value=TENANT)),
        patch.object(otel_ingest, "write_spans", writer),
        patch.object(otel_ingest, "record_otel", AsyncMock()),
    ):
        yield writer


async def _post(content, headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(URL, content=content, headers=headers)


async def test_requires_auth():
    resp = await _post(
        OPENLLMETRY_PB.read_bytes(), {"Content-Type": "application/x-protobuf"}
    )
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "content_type",
    ["application/x-protobuf", "application/protobuf", "Application/X-Protobuf; v=1"],
)
async def test_protobuf_ingest(mocked, content_type):
    resp = await _post(
        OPENLLMETRY_PB.read_bytes(), {**AUTH, "Content-Type": content_type}
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/x-protobuf"
    parsed = ExportTraceServiceResponse.FromString(resp.content)
    assert not parsed.HasField("partial_success")
    _db, tenant_id, spans = mocked.await_args.args
    assert tenant_id == TENANT
    assert [s.model for s in spans] == ["gpt-3.5-turbo", "gpt-3.5-turbo", "davinci-002"]
    for text in FIXTURE_CONTENT:
        assert text not in repr(mocked.await_args)


async def test_json_and_protobuf_fixtures_write_the_same_spans(mocked):
    await _post(
        OPENLLMETRY_JSON.read_bytes(), {**AUTH, "Content-Type": "application/json"}
    )
    from_json = mocked.await_args.args[2]
    await _post(OPENLLMETRY_PB.read_bytes(), PROTOBUF)
    assert mocked.await_args.args[2] == from_json


async def test_protobuf_partial_success(mocked):
    body = otlp_json_to_protobuf(_payload(GENAI, {"gen_ai.operation.name": "chat"}))
    resp = await _post(body, PROTOBUF)
    assert resp.status_code == 200
    parsed = ExportTraceServiceResponse.FromString(resp.content)
    assert parsed.partial_success.rejected_spans == 1
    assert "gen_ai.provider.name" in parsed.partial_success.error_message


async def test_protobuf_without_genai_spans(mocked):
    body = otlp_json_to_protobuf(_payload({"http.method": "GET"}))
    resp = await _post(body, PROTOBUF)
    assert resp.status_code == 200
    assert resp.content == b""  # empty ExportTraceServiceResponse
    mocked.assert_not_awaited()


async def test_empty_protobuf_request(mocked):
    resp = await _post(b"", PROTOBUF)
    assert resp.status_code == 200
    mocked.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        b"{not protobuf",
        b"\x0a\xff\xff\xff\xff\x0f",  # length past the end of the body
        b"\xff",  # field number 0 is invalid
    ],
)
async def test_invalid_protobuf_rejected(mocked, body):
    resp = await _post(body, PROTOBUF)
    assert resp.status_code == 400
    assert "protobuf" in resp.json()["detail"]["detail"]
    mocked.assert_not_awaited()


async def test_protobuf_invalid_utf8_rejected(mocked):
    # A lone surrogate (CESU-8 ED A0 80) cannot arrive as protobuf text: the
    # request is a 400, like invalid UTF-8 in a JSON body.
    good = otlp_json_to_protobuf(_payload({**GENAI, "gen_ai.request.model": "MODEL"}))
    resp = await _post(good.replace(b"MODEL", b"MO\xed\xa0\x80"), PROTOBUF)
    assert resp.status_code == 400
    mocked.assert_not_awaited()


async def test_protobuf_nul_dropped_per_attribute(mocked):
    """A NUL is valid UTF-8, so it reaches the mapper and is dropped per
    attribute exactly as in a JSON body (#217/#224)."""
    body = otlp_json_to_protobuf(
        _payload(
            {**GENAI, "gen_ai.request.model": "gpt\x00-4o"},
            {**GENAI, "gen_ai.provider.name": "open\x00ai"},
        )
    )
    with patch.object(otel_ingest, "record_otel", AsyncMock()) as record:
        resp = await _post(body, PROTOBUF)
    assert resp.status_code == 200
    spans = mocked.await_args.args[2]
    assert len(spans) == 1
    assert spans[0].requested_model is None
    assert "\x00" not in repr(spans)
    counts = record.await_args.args[2]
    (c,) = counts.values()
    assert c.ignored["gen_ai.request.model:invalid"] == 1
    assert c.rejected == {"no_vendor": 1}
    assert ExportTraceServiceResponse.FromString(resp.content).partial_success.rejected_spans == 1


def _nested_any_value(levels: int) -> dict:
    value: dict = {"stringValue": "x"}
    for _ in range(levels):
        value = {"arrayValue": {"values": [value]}}
    return value


async def test_protobuf_depth_bounded_like_json(mocked):
    """A protobuf body whose decoded form nests deeper than the JSON limit is
    the same 400 a JSON body that deep gets. Each array level is three JSON
    containers but two messages, so this one is within the protobuf
    parser's own limit and only the JSON depth check stops it."""
    payload = _payload(GENAI)
    span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    deep = _nested_any_value(MAX_JSON_DEPTH // 3 + 2)
    span["attributes"].append({"key": "deep", "value": deep})
    as_json = await _post(json.dumps(payload), {**AUTH, "Content-Type": "application/json"})
    as_pb = await _post(otlp_json_to_protobuf(payload), PROTOBUF)
    assert as_json.status_code == as_pb.status_code == 400
    assert as_pb.json() == as_json.json()
    mocked.assert_not_awaited()


async def test_protobuf_past_parser_recursion_limit(mocked):
    # Hand-encoded: ExportTraceServiceRequest > ResourceSpans > ScopeSpans >
    # Span > KeyValue > AnyValue, then 300 levels of ArrayValue > AnyValue,
    # far past the parser's own nesting limit.
    def field(number: int, payload: bytes) -> bytes:
        tag = bytes([(number << 3) | 2])
        n, length = len(payload), b""
        while True:
            byte = n & 0x7F
            n >>= 7
            length += bytes([byte | (0x80 if n else 0)])
            if not n:
                break
        return tag + length + payload

    value = field(1, b"x")  # AnyValue.string_value
    for _ in range(300):
        value = field(5, field(1, value))  # AnyValue.array_value > values
    key_value = field(1, b"deep") + field(2, value)
    body = field(1, field(2, field(2, field(9, key_value))))
    resp = await _post(body, PROTOBUF)
    assert resp.status_code == 400
    mocked.assert_not_awaited()


async def test_protobuf_body_size_bounded(mocked, monkeypatch):
    monkeypatch.setattr(otel_ingest, "MAX_BODY_BYTES", 100)
    resp = await _post(OPENLLMETRY_PB.read_bytes(), PROTOBUF)
    assert resp.status_code == 413
    mocked.assert_not_awaited()


async def test_protobuf_span_count_bounded(mocked, monkeypatch):
    monkeypatch.setattr(otel_ingest, "MAX_SPANS", 5)
    resp = await _post(OPENLLMETRY_PB.read_bytes(), PROTOBUF)
    assert resp.status_code == 413
    mocked.assert_not_awaited()


async def test_other_media_types_still_415(mocked):
    resp = await _post(b"x", {**AUTH, "Content-Type": "text/plain"})
    assert resp.status_code == 415
    assert "application/x-protobuf" in resp.json()["detail"]["detail"]


if __name__ == "__main__":  # regenerate the protobuf fixture
    OPENLLMETRY_PB.write_bytes(otlp_json_to_protobuf(_fixture_json()))
    sys.exit(0)
