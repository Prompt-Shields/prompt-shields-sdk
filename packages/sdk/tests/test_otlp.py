"""OTLP/HTTP span export from the SDK (#386): config from the standard
OTEL_* variables, the Langfuse preset and its recorded-payload contract,
protobuf/JSON encoding, the bounded fail-open queue under an outage, content
capture off by default, and dual export next to the collector."""

import base64
import gzip
import json
import struct
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from prompt_shields import AsyncShieldsClient, ShieldsClient, otlp
from prompt_shields.otlp import (
    OTLPConfig,
    OTLPSpanExporter,
    build_request,
    build_span,
    encode_protobuf,
    encode_request,
    langfuse_config,
    redact_pii,
    user_hash,
)

FIXTURES = Path(__file__).parent / "fixtures" / "otlp"


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    for name in ("PS_OTLP_EXPORT", "PS_LANGFUSE_EXPORT", "OTEL_SDK_DISABLED",
                 "PS_ATLAS_URL", "PS_ATLAS_API_KEY", "PS_OTLP_CAPTURE_CONTENT",
                 "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
                 "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                 "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
                 "OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
                 "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST",
                 "LANGFUSE_BASE_URL", "PS_OTLP_USER_HASH_SALT"):
        monkeypatch.delenv(name, raising=False)
    otlp.shutdown_otlp(timeout=0)
    otlp._env_checked = True  # tests configure explicitly
    yield
    otlp.shutdown_otlp(timeout=0)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Recorder:
    """Transport stand-in: records each POST and answers with `status`."""

    def __init__(self, status=200, fail=False, headers=None):
        self.status = status
        self.fail = fail
        self.headers = headers or {}
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        if self.fail:
            raise httpx.ConnectError("backend down")
        self.calls.append((url, body, dict(headers)))
        return self.status, self.headers


EVENT = {
    "vendor": "openai",
    "model": "gpt-4o",
    "requested_model": "gpt-4o",
    "served_model": "gpt-4o-2024-08-06",
    "source": "sdk",
    "tokens_in": 120,
    "tokens_out": 30,
    "latency_ms": 850,
    "cost": 0.0006,
    "cost_source": "pricing_table",
    "call_id": "call-1",
    "session_id": "sess-42",
    "user_id": "alice@example.com",
    "environment": "prod",
    "business_unit": "support",
    "use_case_name": "triage",
}
MESSAGES = [{"role": "system", "content": "You are terse."},
            {"role": "user", "content": "Mail alice@example.com the plan"}]


def _attrs(span):
    out = {}
    for a in span["attributes"]:
        (kind, value), = a["value"].items()
        if kind == "intValue":
            value = int(value)
        elif kind == "arrayValue":
            value = [next(iter(v.values())) for v in value["values"]]
        out[a["key"]] = value
    return out


def _cfg(**kw):
    return OTLPConfig(endpoint="http://otel.test/v1/traces", **kw)


# --- configuration ------------------------------------------------------------------


def test_from_env_reads_standard_variables():
    env = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otel.example.com/",
        "OTEL_EXPORTER_OTLP_HEADERS": "x-a=1,authorization=Bearer%20abc",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "x-a=2",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
        "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT": "2500",
        "OTEL_EXPORTER_OTLP_COMPRESSION": "gzip",
        "OTEL_BSP_MAX_QUEUE_SIZE": "100",
        "OTEL_BSP_MAX_EXPORT_BATCH_SIZE": "10",
        "OTEL_BSP_SCHEDULE_DELAY": "250",
        "OTEL_SERVICE_NAME": "billing-bot",
        "OTEL_RESOURCE_ATTRIBUTES": "team=payments,service.name=ignored",
    }
    cfg = OTLPConfig.from_env(env)
    assert cfg.endpoint == "https://otel.example.com/v1/traces"
    assert cfg.headers == {"x-a": "2", "authorization": "Bearer abc"}
    assert cfg.protocol == "http/json"
    assert cfg.timeout == 2.5
    assert cfg.compression == "gzip"
    assert (cfg.max_queue_size, cfg.max_export_batch_size) == (100, 10)
    assert cfg.schedule_delay == 0.25
    assert cfg.service_name == "billing-bot"
    assert cfg.resource_attributes["team"] == "payments"
    assert cfg.capture_content is False
    assert "abc" not in repr(cfg)


def test_from_env_traces_endpoint_used_as_is_and_defaults():
    cfg = OTLPConfig.from_env({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://c:4318/x"})
    assert cfg.endpoint == "http://c:4318/x"
    assert cfg.protocol == "http/protobuf"  # the OTLP default
    assert (cfg.max_queue_size, cfg.max_export_batch_size, cfg.schedule_delay) == (
        2048, 512, 5.0)
    assert cfg.compression is None


def test_from_env_rejects_missing_endpoint_and_grpc():
    with pytest.raises(ValueError):
        OTLPConfig.from_env({})
    with pytest.raises(ValueError):
        OTLPConfig.from_env({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c",
                             "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"})


def test_content_capture_flags():
    base = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c"}
    assert OTLPConfig.from_env(base).capture_content is False
    assert OTLPConfig.from_env({**base, "PS_OTLP_CAPTURE_CONTENT": "true"}).capture_content
    assert OTLPConfig.from_env(
        {**base, "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "1"}
    ).capture_content


def test_langfuse_config_endpoint_and_basic_auth():
    env = {"LANGFUSE_PUBLIC_KEY": "pk-lf-1", "LANGFUSE_SECRET_KEY": "sk-lf-2",
           "LANGFUSE_HOST": "https://lf.internal.example/",
           # A platform-wide OTLP endpoint must not swallow the Langfuse one.
           "OTEL_EXPORTER_OTLP_ENDPOINT": "http://other",
           "OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer other",
           "OTEL_BSP_MAX_QUEUE_SIZE": "64"}
    cfg = langfuse_config(env=env)
    assert cfg.endpoint == "https://lf.internal.example/api/public/otel/v1/traces"
    expected = base64.b64encode(b"pk-lf-1:sk-lf-2").decode()
    assert cfg.headers == {"Authorization": f"Basic {expected}"}
    assert cfg.profile == "langfuse"
    assert cfg.max_queue_size == 64
    assert langfuse_config("a", "b", env={}).endpoint == (
        "https://cloud.langfuse.com/api/public/otel/v1/traces")
    with pytest.raises(ValueError):
        langfuse_config(env={})


def test_otel_sdk_disabled_turns_configure_off(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert otlp.configure_otlp(endpoint="http://c") is None
    assert otlp.exporters() == []


def test_env_switch_configures_once(monkeypatch):
    monkeypatch.setenv("PS_OTLP_EXPORT", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://c")
    monkeypatch.setenv("PS_LANGFUSE_EXPORT", "1")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    otlp._env_checked = False
    otlp.configure_from_env()
    otlp.configure_from_env()
    endpoints = sorted(e.config.endpoint for e in otlp.exporters())
    assert endpoints == ["http://c/v1/traces",
                         "https://cloud.langfuse.com/api/public/otel/v1/traces"]


def test_env_switch_with_bad_settings_never_raises(monkeypatch, caplog):
    monkeypatch.setenv("PS_OTLP_EXPORT", "true")  # no endpoint
    otlp._env_checked = False
    otlp.export_event(EVENT, MESSAGES)
    assert otlp.exporters() == []
    assert "OTLP export is off" in caplog.text


# --- span mapping -------------------------------------------------------------------


def test_span_carries_model_tokens_cost_session_and_user_hash():
    span = build_span(EVENT, MESSAGES, _cfg(), end_ns=2_000_000_000_000)
    attrs = _attrs(span)
    assert span["name"] == "chat gpt-4o"
    assert span["kind"] == 3
    assert int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"]) == 850_000_000
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.provider.name"] == "openai"
    assert attrs["gen_ai.request.model"] == "gpt-4o"
    assert attrs["gen_ai.response.model"] == "gpt-4o-2024-08-06"
    assert attrs["gen_ai.usage.input_tokens"] == 120
    assert attrs["gen_ai.usage.output_tokens"] == 30
    assert attrs["gen_ai.usage.cost"] == pytest.approx(0.0006)
    assert attrs["gen_ai.conversation.id"] == attrs["session.id"] == "sess-42"
    assert attrs["ps.user.hash"] == attrs["user.id"] == user_hash("alice@example.com")
    assert "alice@example.com" not in json.dumps(span)
    assert not any(k.startswith("langfuse.") for k in attrs)
    assert "gen_ai.input.messages" not in attrs


def test_cloud_platform_region_vendor_and_cost_tags():
    """Claude on Vertex AI / Bedrock (#388): the provider, where the call ran
    and dict cost tags (langgraph_node) are on the span."""
    vertex = {**EVENT, "vendor": "google-vertex-ai", "region": "europe-west1",
              "served_jurisdiction": "EEA", "model_family": "anthropic",
              "endpoint_scope": "regional", "cloud_project": "proj-1",
              "tags": {"langgraph_node": "planner", "bad": 3}}
    a = _attrs(build_span(vertex, [], _cfg()))
    assert a["gen_ai.provider.name"] == "gcp.vertex_ai"
    assert (a["cloud.region"], a["cloud.provider"]) == ("europe-west1", "gcp")
    assert a["ps.served_jurisdiction"] == "EEA"
    assert a["ps.model_family"] == "anthropic"
    assert a["ps.endpoint_scope"] == "regional"
    assert a["ps.cloud_project"] == "proj-1"
    assert a["ps.tag.langgraph_node"] == "planner"
    assert "ps.tag.bad" not in a
    bedrock = {**EVENT, "vendor": "aws-bedrock", "region": "us-east-1",
               "cloud_account": "123456789012"}
    b = _attrs(build_span(bedrock, [], _cfg()))
    assert b["gen_ai.provider.name"] == "aws.bedrock"
    assert (b["cloud.provider"], b["cloud.account.id"]) == ("aws", "123456789012")
    assert "cloud.region" not in _attrs(build_span(EVENT, [], _cfg()))


def test_user_hash_salt_is_hmac():
    assert user_hash("u", "salt") != user_hash("u")
    span = build_span(EVENT, [], _cfg(user_hash_salt="s3"))
    assert _attrs(span)["ps.user.hash"] == user_hash("alice@example.com", "s3")


def test_cost_comes_from_the_shared_price_table():
    """The span's cost is the event's, which the client prices with
    prompt_shields.pricing (the shared price table)."""
    from prompt_shields.pricing import estimate_cost

    expected = estimate_cost("openai", "gpt-4o", 1000, 500)
    client, recorder, _ = _client_with_exporter(_cfg(protocol="http/json"))
    _call(client, tokens_in=1000, tokens_out=500)
    otlp.exporters()[0].flush()
    span = _decode_json(recorder.calls[0][1])["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert _attrs(span)["gen_ai.usage.cost"] == pytest.approx(expected)


def test_span_joins_the_active_trace(monkeypatch):
    monkeypatch.setattr(otlp, "_active_parent", lambda: ("ab" * 16, "cd" * 8))
    span = build_span(EVENT, [], _cfg())
    assert span["traceId"] == "ab" * 16
    assert span["parentSpanId"] == "cd" * 8


def test_unpriced_and_missing_fields_are_left_out():
    span = build_span({"vendor": "mistral", "model": "m", "tokens_in": None,
                       "cost": None, "user_id": "", "session_id": None}, [], _cfg())
    attrs = _attrs(span)
    assert attrs["gen_ai.provider.name"] == "mistral_ai"
    for key in ("gen_ai.usage.input_tokens", "gen_ai.usage.cost", "user.id",
                "session.id"):
        assert key not in attrs


def test_resource_groups_by_environment():
    cfg = _cfg(service_name="svc", resource_attributes={"team": "a"})
    req = build_request([(build_span(EVENT, [], cfg), "prod"),
                         (build_span(EVENT, [], cfg), None),
                         (build_span(EVENT, [], cfg), "prod")], cfg)
    assert len(req["resourceSpans"]) == 2
    prod = req["resourceSpans"][0]
    res = _attrs(prod["resource"])
    assert res["service.name"] == "svc"
    assert res["deployment.environment.name"] == "prod"
    assert res["team"] == "a"
    assert len(prod["scopeSpans"][0]["spans"]) == 2


# --- encoding ----------------------------------------------------------------------


def _read_varint(buf, i):
    shift = n = 0
    while True:
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return n, i


def _fields(buf):
    """Minimal protobuf wire decoder: [(field, wire type, value)]."""
    out, i = [], 0
    while i < len(buf):
        key, i = _read_varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _read_varint(buf, i)
        elif wire == 1:
            value, i = buf[i:i + 8], i + 8
        elif wire == 2:
            n, i = _read_varint(buf, i)
            value, i = buf[i:i + n], i + n
        elif wire == 5:
            value, i = buf[i:i + 4], i + 4
        else:
            raise AssertionError(f"wire type {wire}")
        out.append((field, wire, value))
    return out


def _one(fields, number):
    values = [v for f, _, v in fields if f == number]
    assert len(values) == 1, (number, values)
    return values[0]


def test_protobuf_encoding_matches_the_otlp_schema():
    cfg = _cfg(profile="langfuse")
    span = build_span(EVENT, [], cfg, end_ns=1_700_000_000_123_456_789,
                      parent=("0af7651916cd43dd8448eb211c80319c", "b7ad6b7169203331"))
    req = build_request([(span, "prod")], cfg)
    rs = _fields(_one(_fields(encode_protobuf(req)), 1))
    resource = _fields(_one(rs, 1))
    keys = {_one(_fields(kv), 1).decode() for f, _, kv in resource if f == 1}
    assert {"service.name", "deployment.environment.name"} <= keys
    ss = _fields(_one(rs, 2))
    assert _one(_fields(_one(ss, 1)), 1) == b"prompt_shields"
    sp = _fields(_one(ss, 2))
    assert _one(sp, 1).hex() == "0af7651916cd43dd8448eb211c80319c"
    assert _one(sp, 4).hex() == "b7ad6b7169203331"
    assert len(_one(sp, 2)) == 8
    assert _one(sp, 5) == b"chat gpt-4o"
    assert _one(sp, 6) == 3
    assert struct.unpack("<Q", _one(sp, 8))[0] == 1_700_000_000_123_456_789
    attrs = {}
    for f, _, kv in sp:
        if f != 9:
            continue
        kvf = _fields(kv)
        (vf, _, v), = _fields(_one(kvf, 2))
        attrs[_one(kvf, 1).decode()] = (vf, v)
    assert attrs["gen_ai.usage.input_tokens"] == (3, 120)
    assert attrs["gen_ai.request.model"] == (1, b"gpt-4o")
    assert struct.unpack("<d", attrs["gen_ai.usage.cost"][1])[0] == pytest.approx(0.0006)


def test_protobuf_round_trips_through_opentelemetry_proto():
    pb = pytest.importorskip(
        "opentelemetry.proto.collector.trace.v1.trace_service_pb2")
    cfg = _cfg(profile="langfuse", capture_content=True)
    span = build_span({**EVENT, "tags": ["a", "b"], "tokens_in": -1}, MESSAGES, cfg)
    req = build_request([(span, "prod")], cfg)
    msg = pb.ExportTraceServiceRequest.FromString(encode_protobuf(req))
    got = msg.resource_spans[0].scope_spans[0].spans[0]
    assert got.trace_id.hex() == span["traceId"]
    assert got.name == "chat gpt-4o"
    attrs = {a.key: a.value for a in got.attributes}
    assert attrs["gen_ai.usage.input_tokens"].int_value == -1
    assert [v.string_value for v in attrs["ps.tags"].array_value.values] == ["a", "b"]
    assert attrs["gen_ai.usage.cost"].double_value == pytest.approx(0.0006)


def test_json_and_gzip_bodies():
    cfg = _cfg(protocol="http/json", compression="gzip")
    req = build_request([(build_span(EVENT, [], cfg), None)], cfg)
    body, headers = encode_request(req, cfg)
    assert headers == {"Content-Type": "application/json", "Content-Encoding": "gzip"}
    assert json.loads(gzip.decompress(body)) == req
    body, headers = encode_request(req, _cfg())
    assert headers == {"Content-Type": "application/x-protobuf"}


# --- Langfuse contract (recorded payload) ---------------------------------------------


def _deterministic(monkeypatch):
    ids = iter(["0af7651916cd43dd8448eb211c80319c", "b7ad6b7169203331"] * 10)
    monkeypatch.setattr(otlp, "_id", lambda n: next(ids))
    monkeypatch.setattr(otlp, "_active_parent", lambda: None)


def _normalise(req):
    """Drop the version fields (they change every release)."""
    req = json.loads(json.dumps(req))
    for rs in req["resourceSpans"]:
        rs["resource"]["attributes"] = [
            a for a in rs["resource"]["attributes"] if a["key"] != "telemetry.sdk.version"]
        for ss in rs["scopeSpans"]:
            ss["scope"].pop("version", None)
    return req


def test_langfuse_payload_matches_recorded_contract(monkeypatch):
    """The SDK's Langfuse export, byte for byte the recorded payload in
    tests/fixtures/otlp/langfuse_sdk_request.json (OTLP JSON, protocol
    http/json). Regenerate deliberately when the mapping changes."""
    _deterministic(monkeypatch)
    cfg = langfuse_config("pk-lf-test", "sk-lf-test", env={
        "OTEL_SERVICE_NAME": "support-bot", "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json"})
    span = build_span(EVENT, MESSAGES, cfg, end_ns=1_767_225_600_000_000_000)
    got = _normalise(build_request([(span, "prod")], cfg))
    recorded = json.loads((FIXTURES / "langfuse_sdk_request.json").read_text())
    assert got == recorded["request"]


def test_recorded_payload_has_every_attribute_langfuse_maps():
    """Langfuse's OTel ingestion (/api/public/otel) reads these keys: model,
    usage, cost, session and user, observation type, environment."""
    recorded = json.loads((FIXTURES / "langfuse_sdk_request.json").read_text())
    rs = recorded["request"]["resourceSpans"][0]
    attrs = _attrs(rs["scopeSpans"][0]["spans"][0])
    for key, expected in recorded["langfuse_mapping"].items():
        assert key in attrs, key
        if expected is not None:
            assert attrs[key] == expected, key
    assert json.loads(attrs["langfuse.observation.usage_details"]) == {
        "input": 120, "output": 30}
    assert json.loads(attrs["langfuse.observation.cost_details"]) == {"total": 0.0006}
    raw = json.dumps(recorded)
    assert "Mail alice" not in raw and "alice@example.com" not in raw


def test_gateway_recorded_payload_has_the_same_langfuse_keys():
    """The gateway's recorded span (gateway/src/middlewares/psOtel.langfuse
    test writes the same fixture shape) groups by the same keys."""
    recorded = json.loads((FIXTURES / "langfuse_gateway_request.json").read_text())
    span = recorded["request"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    attrs = _attrs(span)
    for key in ("gen_ai.request.model", "gen_ai.usage.input_tokens",
                "gen_ai.usage.output_tokens", "gen_ai.usage.cost", "session.id",
                "gen_ai.conversation.id", "user.id"):
        assert key in attrs, key


# --- delivery: bounded, fail-open, retries ---------------------------------------------


def test_outage_buffers_at_most_the_queue_and_drops_no_calls():
    """Acceptance: a 30 s backend outage. Calls keep succeeding without
    waiting on the exporter; the exporter holds at most max_queue_size spans
    (the oldest dropped and counted); after recovery the queue drains."""
    clock = Clock()
    recorder = Recorder(fail=True)
    cfg = _cfg(max_queue_size=20, max_export_batch_size=5, protocol="http/json")
    client, _, collector = _client_with_exporter(cfg, recorder=recorder, clock=clock)
    exporter = otlp.exporters()[0]

    results = []
    for second in range(30):
        clock.now += 1.0
        for _ in range(2):
            results.append(_call(client))
        exporter.flush()  # what the background thread does on its schedule
        assert exporter.stats()["queued"] <= 20

    assert len(results) == 60 and all(r is not None for r in results)
    assert len(collector) == 60  # the collector got every call
    stats = exporter.stats()
    assert stats["queued"] == 20
    assert stats["dropped"]["queue_full"] == 40
    assert stats["exported"] == 0

    recorder.fail = False
    clock.now += otlp.MAX_BACKOFF_SECONDS
    assert exporter.flush() is True
    assert exporter.stats()["exported"] == 20
    assert sum(len(_spans(body)) for _, body, _ in recorder.calls) == 20
    assert all(len(_spans(body)) <= 5 for _, body, _ in recorder.calls)


def test_backoff_doubles_and_honours_retry_after():
    clock = Clock()
    rec = Recorder(fail=True)
    exp = OTLPSpanExporter(_cfg(), transport=rec, clock=clock, start_thread=False)
    exp.export_event(EVENT)
    assert exp.flush() is False
    assert exp.stats()["retry_after_seconds"] == 1.0
    assert exp.flush() is False  # still backing off: no request
    clock.now += 1.0
    exp.flush()
    assert exp.stats()["retry_after_seconds"] == 2.0
    rec.fail, rec.status, rec.headers = False, 429, {"Retry-After": "7"}
    clock.now += 2.0
    exp.flush()
    assert exp.stats()["retry_after_seconds"] == 7.0
    rec.status, rec.headers = 200, {}
    clock.now += 7.0
    assert exp.flush() is True
    assert exp.stats()["queued"] == 0 and exp.stats()["exported"] == 1


def test_non_retryable_rejection_drops_the_batch():
    rec = Recorder(status=400)
    exp = OTLPSpanExporter(_cfg(), transport=rec, start_thread=False)
    exp.export_event(EVENT)
    assert exp.flush() is True
    assert exp.stats()["dropped"]["rejected"] == 1


def test_export_event_never_raises_or_sends_on_the_caller():
    rec = Recorder()
    exp = OTLPSpanExporter(_cfg(), transport=rec, start_thread=False)
    exp.export_event({"model": object(), "latency_ms": "x"}, [object()])
    exp.export_event(EVENT, MESSAGES)
    assert rec.calls == []  # no I/O on enqueue
    assert exp.stats()["queued"] >= 1


def test_background_thread_sends_and_shutdown_counts_leftovers():
    rec = Recorder()
    exp = OTLPSpanExporter(_cfg(schedule_delay=0.01, max_export_batch_size=1),
                           transport=rec)
    exp.export_event(EVENT)
    for _ in range(500):
        if rec.calls:
            break
        import time
        time.sleep(0.01)
    assert rec.calls
    exp.shutdown(timeout=1)
    assert exp.stats()["exported"] == 1
    exp.export_event(EVENT)
    assert exp.stats()["dropped"]["shutdown"] == 1


def test_real_http_post_uses_configured_headers(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200)

    exp = OTLPSpanExporter(langfuse_config("pk", "sk", env={}), start_thread=False)
    exp._client = httpx.Client(transport=httpx.MockTransport(handler))
    exp.export_event(EVENT)
    assert exp.flush() is True
    req = seen[0]
    assert req.url.path == "/api/public/otel/v1/traces"
    assert req.headers["authorization"] == "Basic " + base64.b64encode(b"pk:sk").decode()
    assert req.headers["content-type"] == "application/x-protobuf"


# --- privacy: content capture ----------------------------------------------------------

SECRET_PROMPT = "Draft the merger memo for Project Nightjar, send to bob@acme.io"
COMPLETION = "NIGHTJAR-COMPLETION-TEXT"


@pytest.mark.parametrize("protocol", ["http/json", "http/protobuf"])
def test_no_prompt_or_completion_text_leaves_with_capture_off(protocol):
    """Acceptance: with content capture off, the exported bytes hold no
    prompt or completion text (and no raw user id), in either protocol,
    even with the collector's send_prompt_text opt-in on."""
    client, rec, collector = _client_with_exporter(
        _cfg(protocol=protocol, profile="langfuse"), send_prompt_text=True)
    _call(client, content=SECRET_PROMPT, user_id="bob@acme.io")
    otlp.exporters()[0].flush()
    body = rec.calls[0][1]
    for needle in ("Nightjar", "merger", "bob@acme.io", COMPLETION, "Draft the"):
        assert needle.encode() not in body, needle
    assert b"gen_ai.input.messages" not in body
    assert b"langfuse.observation.input" not in body
    assert SECRET_PROMPT in collector[0]  # collector opt-in unaffected


def test_capture_on_sends_redacted_input_only():
    client, rec, _ = _client_with_exporter(
        _cfg(protocol="http/json", capture_content=True, profile="langfuse"))
    _call(client, content=SECRET_PROMPT + " card 4111 1111 1111 1111")
    otlp.exporters()[0].flush()
    attrs = _attrs(_spans(rec.calls[0][1])[0])
    messages = json.loads(attrs["gen_ai.input.messages"])
    text = messages[0]["parts"][0]["content"]
    assert "Project Nightjar" in text
    assert "bob@acme.io" not in text and "[EMAIL]" in text
    assert "4111" not in text and "[CREDIT_CARD]" in text
    assert attrs["langfuse.observation.input"] == attrs["gen_ai.input.messages"]
    assert COMPLETION.encode() not in rec.calls[0][1]


def test_redact_pii_uses_gateway_placeholders():
    assert redact_pii("ip 10.1.2.3 mail a@b.co") == "ip [IP_ADDRESS] mail [EMAIL]"
    assert redact_pii("nothing here") == "nothing here"


# --- dual export ------------------------------------------------------------------------


def test_dual_export_collector_and_two_backends():
    client, rec, collector = _client_with_exporter(_cfg(protocol="http/json"))
    second = Recorder()
    otlp.configure_langfuse("pk", "sk", transport=second, start_thread=False,
                            protocol="http/json")
    _call(client)
    for exp in otlp.exporters():
        exp.flush()
    assert len(collector) == 1
    assert len(_spans(rec.calls[0][1])) == 1
    lf = _attrs(_spans(second.calls[0][1])[0])
    assert lf["langfuse.observation.type"] == "generation"
    assert second.calls[0][0].endswith("/api/public/otel/v1/traces")
    assert [s["endpoint"] for s in otlp.otlp_stats()] == [
        "http://otel.test/v1/traces",
        "https://cloud.langfuse.com/api/public/otel/v1/traces"]


@pytest.mark.asyncio
async def test_async_client_exports_too(monkeypatch):
    rec = Recorder()
    otlp.configure_otlp(_cfg(protocol="http/json"), transport=rec, start_thread=False)
    client = AsyncShieldsClient(api_key="sk", ps_api_key="ps")
    client._telemetry._async_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))

    async def upstream(**kwargs):
        return _response()

    monkeypatch.setattr(client.chat.completions, "_call_upstream", upstream)
    await client.chat.completions.create(model="gpt-4o", messages=MESSAGES,
                                         ps_metadata={"session_id": "s"})
    otlp.exporters()[0].flush()
    attrs = _attrs(_spans(rec.calls[0][1])[0])
    assert attrs["session.id"] == "s"


def test_instrument_path_exports(monkeypatch):
    from prompt_shields import instrumentation

    rec = Recorder()
    otlp.configure_otlp(_cfg(protocol="http/json"), transport=rec, start_thread=False)
    state = MagicMock()
    state.atlas = None
    instrumentation._send_later({"state": state, "messages": MESSAGES}, dict(EVENT))
    state.telemetry.enqueue.assert_called_once()
    otlp.exporters()[0].flush()
    assert len(_spans(rec.calls[0][1])) == 1


# --- helpers ------------------------------------------------------------------------------


def _decode_json(body):
    return json.loads(body)


def _spans(body):
    return [s for rs in json.loads(body)["resourceSpans"]
            for ss in rs["scopeSpans"] for s in ss["spans"]]


def _response(tokens_in=10, tokens_out=20):
    resp = MagicMock()
    resp.model = "gpt-4o"
    resp.usage.prompt_tokens = tokens_in
    resp.usage.completion_tokens = tokens_out
    resp.usage.prompt_tokens_details = None
    resp.usage.completion_tokens_details = None
    choice = MagicMock()
    choice.message.content = COMPLETION
    choice.finish_reason = "stop"
    resp.choices = [choice]
    return resp


def _client_with_exporter(cfg, *, recorder=None, clock=None, send_prompt_text=False):
    recorder = recorder or Recorder()
    kwargs = {"transport": recorder, "start_thread": False}
    if clock is not None:
        kwargs["clock"] = clock
    otlp.configure_otlp(cfg, **kwargs)
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test",
                           send_prompt_text=send_prompt_text)
    collector: list[str] = []

    def handler(request):
        collector.append(request.content.decode())
        return httpx.Response(200, json={})

    client._telemetry._sync_client = httpx.Client(transport=httpx.MockTransport(handler))
    client._next_response = {}
    return client, recorder, collector


def _call(client, *, content="hello", user_id="u-1", tokens_in=10, tokens_out=20):
    client.chat.completions._call_upstream = (
        lambda **kwargs: _response(tokens_in, tokens_out))
    return client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": content}],
        ps_metadata={"session_id": "s-1", "user_id": user_id})
