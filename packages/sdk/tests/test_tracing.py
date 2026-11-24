"""OpenTelemetry spans for agent traces (#385): chat spans from the SDK
clients with traceparent injection into gateway calls, tool and agent span
helpers, LangChain run trees and the httpx / requests hook."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from unittest.mock import MagicMock

import httpx
import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind, StatusCode
from prompt_shields import ShieldsOpenAI, tracing

SECRET = "my social security number is 123-45-6789"
_EXPORTER = InMemorySpanExporter()
# A private provider (never the global one, so other test modules keep
# OTel's no-op default): the SDK's tracer and the "app" tracer come from it.
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))


def _app_tracer():
    return _PROVIDER.get_tracer("app")


@pytest.fixture(autouse=True)
def spans(monkeypatch):
    monkeypatch.setattr(tracing, "_tracer", lambda: _PROVIDER.get_tracer("prompt_shields"))
    _EXPORTER.clear()
    yield _EXPORTER
    _EXPORTER.clear()
    tracing.uninstrument_http()


def _finished(exporter) -> dict:
    return {s.name: s for s in exporter.get_finished_spans()}


def _traceparent(span) -> str:
    """``00-<trace id>-<span id>`` (the flags vary with the OTel version)."""
    ctx = span.get_span_context()
    return f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}"


# ── chat spans from the SDK clients ──────────────────────────────────────

CHAT_RESPONSE = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 0,
    "model": "gpt-4o-2024-08-06",
    "choices": [
        {"index": 0, "finish_reason": "stop",
         "message": {"role": "assistant", "content": SECRET}}
    ],
    "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
}


def _client(base_url, seen, *, status=200, **kw):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=CHAT_RESPONSE if status == 200 else
                              {"error": {"message": "bad", "type": "server_error"}})

    client = ShieldsOpenAI(
        api_key="sk-test",
        ps_api_key="ps-test",
        base_url=base_url,
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        send_prompt_text=True,  # does not put content on spans
        **kw,
    )
    client._telemetry = MagicMock()
    return client


def test_chat_span_joins_the_active_trace_and_goes_to_the_gateway(spans):
    seen: list[httpx.Request] = []
    client = _client("http://gateway.internal:8787/v1", seen)
    tracer = _app_tracer()
    with tracer.start_as_current_span("app turn") as app_span:
        client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": SECRET}],
            ps_metadata={"session_id": "sess-1"},
        )
    chat = _finished(spans)["chat gpt-4o"]
    assert chat.kind == SpanKind.CLIENT
    assert chat.parent.span_id == app_span.get_span_context().span_id
    assert chat.context.trace_id == app_span.get_span_context().trace_id
    a = dict(chat.attributes)
    assert a["gen_ai.operation.name"] == "chat"
    assert a["gen_ai.provider.name"] == "openai"
    assert a["gen_ai.request.model"] == "gpt-4o"
    assert a["gen_ai.response.model"] == "gpt-4o-2024-08-06"
    assert (a["gen_ai.usage.input_tokens"], a["gen_ai.usage.output_tokens"]) == (12, 5)
    assert a["gen_ai.conversation.id"] == "sess-1"
    # The gateway gets the chat span as its parent.
    assert seen[0].headers["traceparent"][:-3] == _traceparent(chat)
    # No content on any span.
    for span in spans.get_finished_spans():
        assert SECRET not in json.dumps(dict(span.attributes or {}))


def test_no_traceparent_to_a_provider_unless_asked(spans):
    seen: list[httpx.Request] = []
    _client("https://api.openai.com/v1", seen).chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
    )
    assert "traceparent" not in seen[0].headers
    assert "chat gpt-4o" in _finished(spans)  # the span is still made
    _client("https://api.openai.com/v1", seen, propagate_trace=True).chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
    )
    assert "traceparent" in seen[1].headers
    _client("http://gateway.internal/v1", seen, propagate_trace=False).chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
    )
    assert "traceparent" not in seen[2].headers
    # The HTTP hook does not override the client's choice either way.
    tracing.instrument_http(propagate=True)
    _client("http://gateway.internal/v1", seen, propagate_trace=False).chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
    )
    assert "traceparent" not in seen[3].headers
    _client("https://api.openai.com/v1", seen).chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
    )
    assert "traceparent" not in seen[4].headers
    assert not any(s.name == "POST" for s in spans.get_finished_spans())


def test_a_failed_call_ends_its_span_with_error_type_only(spans):
    seen: list[httpx.Request] = []
    client = _client("http://gateway.internal/v1", seen, status=500)
    with pytest.raises(Exception):  # noqa: B017 - the provider SDK's error
        client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
        )
    chat = _finished(spans)["chat gpt-4o"]
    assert chat.status.status_code == StatusCode.ERROR
    assert chat.status.description is None
    assert chat.attributes["error.type"] == "InternalServerError"


def test_inbound_traceparent_from_the_apps_tracer_is_preserved(spans):
    """The app continues a remote trace (e.g. an incoming request's
    traceparent); every SDK span keeps that trace id."""
    from opentelemetry.propagate import extract

    inbound = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    seen: list[httpx.Request] = []
    client = _client("http://gateway.internal/v1", seen)
    ctx = extract({"traceparent": inbound})
    with (
        _app_tracer().start_as_current_span("request", context=ctx),
        tracing.agent_span("support"),
    ):
        client.chat.completions.create(model="gpt-4o", messages=[])
    for span in spans.get_finished_spans():
        assert f"{span.context.trace_id:032x}" == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert seen[0].headers["traceparent"].startswith(
        "00-4bf92f3577b34da6a3ce929d0e0e4736-"
    )


# ── tool and agent spans ─────────────────────────────────────────────────


def test_tool_span_and_decorators(spans):
    @tracing.traced_tool
    def lookup(order_id):
        return {"order": order_id}

    @tracing.traced_tool("refund", tool_type="function")
    async def refund(order_id):
        raise ValueError(SECRET)

    with tracing.agent_span("support", session_id="s-1") as agent:
        assert lookup("o-1") == {"order": "o-1"}
        with pytest.raises(ValueError):
            asyncio.run(refund("o-1"))
        with tracing.tool_span("search", call_id="call_1"):
            pass

    got = _finished(spans)
    root = got["invoke_agent support"]
    assert root.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert root.attributes["gen_ai.conversation.id"] == "s-1"
    for name in ("execute_tool lookup", "execute_tool refund", "execute_tool search"):
        span = got[name]
        assert span.parent.span_id == agent.get_span_context().span_id
        assert span.attributes["gen_ai.operation.name"] == "execute_tool"
        assert span.kind == SpanKind.INTERNAL
    failed = got["execute_tool refund"]
    assert failed.status.status_code == StatusCode.ERROR
    assert failed.attributes["error.type"] == "ValueError"
    assert failed.attributes["gen_ai.tool.type"] == "function"
    assert not failed.events  # no exception event: its message may be content
    assert got["execute_tool search"].attributes["gen_ai.tool.call.id"] == "call_1"


# ── LangChain-style run trees ─────────────────────────────────────────────


def _agent_turn(runs: tracing.RunSpans, http_seen=None):
    """What LangChain's callbacks report for an agent turn with three model
    calls and two tools (an AgentExecutor's run tree, inner chains
    included)."""
    root, inner = uuid.uuid4(), uuid.uuid4()
    runs.start(root, None, "agent", "AgentExecutor", session_id="s-1")
    runs.track(inner, root)  # a RunnableSequence inside the agent

    def model_call(i):
        run = uuid.uuid4()
        runs.start(run, inner, "chat", None, vendor="openai", model="gpt-4o")
        if http_seen is not None:  # the model's request to the gateway
            httpx.Client(transport=httpx.MockTransport(
                lambda r: (http_seen.append(r), httpx.Response(200))[1]
            ), base_url="http://gateway.internal").post("/v1/chat/completions")
        runs.end(run, fields={"tokens_in": 10 * i, "tokens_out": i})

    def tool_call(name, error=None):
        run = uuid.uuid4()
        runs.start(run, root, "tool", name)
        if http_seen is not None:
            httpx.Client(transport=httpx.MockTransport(
                lambda r: (http_seen.append(r), httpx.Response(503))[1]
            )).get(f"https://billing.internal/orders?note={SECRET}")
        runs.end(run, error=error)

    model_call(1)
    tool_call("lookup_order")
    model_call(2)
    tool_call("refund", error=RuntimeError(SECRET))
    model_call(3)
    runs.forget(inner)
    runs.end(root)


def test_run_tree_yields_one_trace_with_five_parented_spans(spans):
    tracing.instrument_http()
    seen: list[httpx.Request] = []
    _agent_turn(tracing.RunSpans(), seen)
    finished = spans.get_finished_spans()
    by_name: dict[str, list] = {}
    for s in finished:
        by_name.setdefault(s.name, []).append(s)
    (root,) = by_name["invoke_agent AgentExecutor"]
    chats = by_name["chat gpt-4o"]
    tools = by_name["execute_tool lookup_order"] + by_name["execute_tool refund"]
    assert len(chats) == 3 and len(tools) == 2
    assert {s.context.trace_id for s in finished} == {root.context.trace_id}
    for s in chats + tools:
        assert s.parent.span_id == root.context.span_id
    assert [c.attributes["gen_ai.usage.input_tokens"] for c in chats] == [10, 20, 30]
    assert by_name["execute_tool refund"][0].attributes["error.type"] == "RuntimeError"
    # Model requests carry their chat span's traceparent (the gateway's
    # parent); HTTP calls inside tools are child CLIENT spans.
    gateway_calls = [r for r in seen if r.url.host == "gateway.internal"]
    assert [r.headers["traceparent"][:-3] for r in gateway_calls] == [
        _traceparent(c) for c in chats
    ]
    http = by_name["GET"]
    assert len(http) == 2
    assert {h.parent.span_id for h in http} == {t.context.span_id for t in tools}
    assert http[0].attributes["server.address"] == "billing.internal"
    assert http[0].attributes["http.response.status_code"] == 503
    assert http[0].status.status_code == StatusCode.ERROR
    for s in finished:
        assert SECRET not in json.dumps(dict(s.attributes or {}))
    # The context is back to where it started.
    assert not trace.get_current_span().get_span_context().is_valid


def test_langchain_handler_builds_the_same_tree(spans):
    pytest.importorskip("langchain_core")
    from prompt_shields.integrations.langchain import PromptShieldsCallbackHandler

    h = PromptShieldsCallbackHandler()
    root, inner = uuid.uuid4(), uuid.uuid4()
    h.on_chain_start({"name": "AgentExecutor"}, {}, run_id=root, parent_run_id=None,
                     metadata={"ps_session_id": "s-1"})
    h.on_chain_start({"name": "RunnableSequence"}, {}, run_id=inner, parent_run_id=root)
    for i in range(3):
        run = uuid.uuid4()
        h.on_chat_model_start({}, [[]], run_id=run, parent_run_id=inner,
                              metadata={"ls_provider": "openai", "ls_model_name": "gpt-4o"})
        h.on_llm_end(MagicMock(generations=[], llm_output={}), run_id=run)
        if i < 2:
            tool = uuid.uuid4()
            h.on_tool_start({"name": f"tool{i}"}, "input", run_id=tool, parent_run_id=root)
            h.on_tool_end("output", run_id=tool)
    h.on_chain_end({}, run_id=inner)
    h.on_chain_end({}, run_id=root)
    finished = spans.get_finished_spans()
    root_span = next(s for s in finished if s.name == "invoke_agent AgentExecutor")
    children = [s for s in finished if s is not root_span]
    assert len(children) == 5
    assert {s.parent.span_id for s in children} == {root_span.context.span_id}
    assert root_span.attributes["gen_ai.conversation.id"] == "s-1"


# ── httpx / requests hook ────────────────────────────────────────────────


def test_http_hook_leaves_calls_outside_spans_alone(spans):
    tracing.instrument_http()
    tracing.instrument_http()  # idempotent
    seen: list[httpx.Request] = []
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: (seen.append(r), httpx.Response(200))[1]))
    client.get("http://example.internal/")
    with _app_tracer().start_as_current_span("not a tool"):
        client.get("http://example.internal/")
    assert all("traceparent" not in r.headers for r in seen)
    assert [s.name for s in spans.get_finished_spans()] == ["not a tool"]
    tracing.uninstrument_http()
    assert httpx.Client.send.__name__ == "send"
    assert not hasattr(httpx.Client.send, "__wrapped__")


def test_http_hook_async_and_requests(spans):
    tracing.instrument_http()
    seen: list[httpx.Request] = []

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: (seen.append(r), httpx.Response(204))[1]
        )) as client:
            with tracing.tool_span("fetch"):
                await client.get("http://svc.internal:9000/x")

    asyncio.run(run())
    requests = pytest.importorskip("requests")
    from requests.adapters import BaseAdapter

    class Adapter(BaseAdapter):
        def send(self, request, **kwargs):
            seen.append(request)
            r = requests.Response()
            r.status_code = 200
            r.request = request
            return r

        def close(self):
            pass

    session = requests.Session()
    session.mount("http://", Adapter())
    with tracing.tool_span("fetch2"):
        session.get("http://legacy.internal/y")
    http = [s for s in spans.get_finished_spans() if s.name == "GET"]
    assert [h.attributes.get("server.port") for h in http] == [9000, None]
    assert [h.attributes["server.address"] for h in http] == ["svc.internal", "legacy.internal"]
    assert seen[0].headers["traceparent"]
    assert seen[1].headers["traceparent"]


# ── overhead ─────────────────────────────────────────────────────────────


def test_overhead_with_the_exporter_off_is_under_2ms_p95(monkeypatch):
    """No TracerProvider configured: OTel hands out no-op spans. The SDK's
    per-call work (span, activation, injection, end) stays far below 2 ms."""
    monkeypatch.setattr(tracing, "_tracer", lambda: trace.NoOpTracer())
    samples = []
    for _ in range(2000):
        t0 = time.perf_counter()
        span = tracing.start_chat_span("openai", "gpt-4o", session_id="s")
        with span.activate():
            span.inject({})
        span.finish({"tokens_in": 1, "tokens_out": 2, "served_model": "gpt-4o"})
        with tracing.tool_span("t"):
            pass
        samples.append(time.perf_counter() - t0)
    samples.sort()
    assert samples[int(len(samples) * 0.95)] < 0.002


def test_helpers_are_no_ops_without_opentelemetry(monkeypatch):
    monkeypatch.setattr(tracing, "AVAILABLE", False)
    assert tracing.start_chat_span("openai", "gpt-4o") is None
    with tracing.tool_span("t") as span:
        assert span is None
    assert tracing.inject({"a": "b"}) == {"a": "b"}
    runs = tracing.RunSpans()
    runs.start(1, None, "agent", "x")
    runs.end(1)
    tracing.instrument_http()
    assert not hasattr(httpx.Client.send, "__wrapped__")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://gateway.internal:8787/v1", True),
        ("https://api.openai.com/v1", False),
        ("https://my-res.openai.azure.com/openai", False),
        ("https://api.anthropic.com", False),
        (None, False),
        ("not a url", False),
    ],
)
def test_propagates_to(url, expected):
    assert tracing.propagates_to(url) is expected
    assert tracing.propagates_to(url, True) is True
    assert tracing.propagates_to(url, False) is False


# ── OTLP export (#386) next to the SDK spans: one span per call ─────────


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append(body)
        return 200, {}


def _otlp_spans(rec):
    return [s for body in rec.calls for rs in json.loads(body)["resourceSpans"]
            for ss in rs["scopeSpans"] for s in ss["spans"]]


@pytest.fixture
def otlp_exporter(monkeypatch):
    from prompt_shields import otlp

    monkeypatch.delenv("PS_OTLP_SDK_SPANS", raising=False)
    otlp.shutdown_otlp(timeout=0)
    otlp._env_checked = True
    rec = _Recorder()

    def configure(**kw):
        return otlp.configure_otlp(
            otlp.OTLPConfig(endpoint="http://otel.test/v1/traces",
                            protocol="http/json", **kw),
            transport=rec, start_thread=False)

    yield configure, rec
    otlp.shutdown_otlp(timeout=0)


def test_otlp_export_attaches_to_the_sdk_span_instead_of_a_second_span(
        spans, otlp_exporter):
    configure, rec = otlp_exporter
    exporter = configure(capture_content=True)
    seen: list[httpx.Request] = []
    client = _client("http://gateway.internal:8787/v1", seen)
    with _app_tracer().start_as_current_span("app turn"):
        client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": SECRET}],
            ps_metadata={"session_id": "sess-1", "user_id": "alice"})
    exporter.flush()
    assert _otlp_spans(rec) == []  # no second span for the call
    assert exporter.stats()["attached_to_sdk_spans"] == 1
    chats = [s for s in spans.get_finished_spans() if s.name == "chat gpt-4o"]
    assert len(chats) == 1
    a = dict(chats[0].attributes)
    assert a["session.id"] == "sess-1"
    assert len(a["user.id"]) == 64 and "alice" not in json.dumps(a)
    assert "gen_ai.usage.cost" in a
    # Content capture never puts text on an SDK span.
    assert "gen_ai.input.messages" not in a
    assert SECRET not in json.dumps(a)


def test_otlp_export_mode_still_exports_its_own_span(spans, otlp_exporter):
    from prompt_shields import otlp

    _, rec = otlp_exporter

    exporter = otlp.configure_otlp(
        otlp.OTLPConfig.from_env({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://o/v1/traces",
                                  "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
                                  "PS_OTLP_SDK_SPANS": "export"}),
        transport=rec, start_thread=False)
    client = _client("http://gateway.internal:8787/v1", [])
    client.chat.completions.create(model="gpt-4o",
                                   messages=[{"role": "user", "content": "hi"}])
    exporter.flush()
    assert len(_otlp_spans(rec)) == 1
    chat = _finished(spans)["chat gpt-4o"]
    assert "ps.cost.usd" not in dict(chat.attributes)
    with pytest.raises(ValueError):
        otlp.OTLPConfig(endpoint="http://o", sdk_spans="both")


def test_otlp_export_without_a_recording_sdk_span_exports_its_own(otlp_exporter):
    from prompt_shields import otlp

    configure, rec = otlp_exporter
    exporter = configure()
    # A span from another tracer (not an SDK chat span) is only the parent.
    with _app_tracer().start_as_current_span("app turn") as app_span:
        otlp.export_event({"model": "gpt-4o", "vendor": "openai", "latency_ms": 5}, [])
    exporter.flush()
    (span,) = _otlp_spans(rec)
    assert span["parentSpanId"] == f"{app_span.get_span_context().span_id:016x}"


def test_instrument_inside_a_langchain_run_attaches_to_its_chat_span(
        spans, otlp_exporter):
    pytest.importorskip("langchain_core")
    from prompt_shields import instrumentation
    from prompt_shields.integrations.langchain import PromptShieldsCallbackHandler

    configure, rec = otlp_exporter
    exporter = configure()
    state = MagicMock()
    state.atlas = None
    h = PromptShieldsCallbackHandler()
    run = uuid.uuid4()
    h.on_chat_model_start({}, [[]], run_id=run, parent_run_id=None,
                          metadata={"ls_provider": "openai", "ls_model_name": "gpt-4o"})
    # instrument() records the model call while the run's chat span is current.
    instrumentation._send_later(
        {"state": state, "messages": []},
        {"model": "gpt-4o", "vendor": "openai", "cost": 0.01, "latency_ms": 5})
    h.on_llm_end(MagicMock(generations=[], llm_output={}), run_id=run)
    exporter.flush()
    assert _otlp_spans(rec) == []
    assert dict(_finished(spans)["chat gpt-4o"].attributes)["gen_ai.usage.cost"] == 0.01


def test_langchain_emit_attaches_before_the_run_span_ends(
        spans, otlp_exporter, monkeypatch):
    pytest.importorskip("langchain_core")
    from prompt_shields import instrumentation
    from prompt_shields.integrations.langchain import PromptShieldsCallbackHandler

    configure, rec = otlp_exporter
    exporter = configure()
    state = MagicMock()
    state.atlas = None
    monkeypatch.setattr(instrumentation, "_state", state)
    monkeypatch.setattr(
        instrumentation, "build_event",
        lambda *a, **k: {"model": "gpt-4o", "vendor": "openai", "cost": 0.02,
                         "latency_ms": 5})
    h = PromptShieldsCallbackHandler()
    run = uuid.uuid4()
    h.on_chat_model_start({}, [[]], run_id=run, parent_run_id=None,
                          metadata={"ls_provider": "openai", "ls_model_name": "gpt-4o"})
    h.on_llm_end(MagicMock(generations=[], llm_output={}), run_id=run)
    exporter.flush()
    state.telemetry.enqueue.assert_called_once()
    state.telemetry.flush_sync.assert_called_once()
    assert _otlp_spans(rec) == []
    assert dict(_finished(spans)["chat gpt-4o"].attributes)["gen_ai.usage.cost"] == 0.02
