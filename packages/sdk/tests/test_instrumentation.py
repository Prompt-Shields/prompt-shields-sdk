"""Tests for prompt_shields.instrument() / uninstrument().

Most tests patch fake provider modules placed in sys.modules, so they run
whether or not openai/anthropic are installed. The last group drives the real
SDKs (when installed) through httpx.MockTransport. No network: telemetry
senders are replaced with recorders.
"""

import asyncio
import json
import sys
from types import ModuleType
from types import SimpleNamespace as NS
from typing import ClassVar
from unittest.mock import MagicMock

import httpx
import prompt_shields
import pytest
from prompt_shields import instrumentation as inst

MESSAGES = [{"role": "user", "content": "email me at jane@example.com"}]


class RecordingSender:
    """Stands in for TelemetrySender / AtlasTelemetrySender."""

    instances: ClassVar[list["RecordingSender"]] = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.events: list[dict] = []
        self.flushes = 0
        RecordingSender.instances.append(self)

    def enqueue(self, event):
        self.events.append(event)

    def flush_sync(self):
        self.flushes += 1

    async def flush(self):
        self.flushes += 1


# --- fake provider SDKs -------------------------------------------------------


def _oa_response(model="gpt-4o-2024-08-06"):
    tc = NS(function=NS(name="lookup"))
    return NS(
        model=model,
        usage=NS(prompt_tokens=11, completion_tokens=22),
        choices=[NS(message=NS(tool_calls=[tc]))],
    )


def _anthropic_response():
    return NS(
        model="claude-test",
        usage=NS(input_tokens=5, output_tokens=7),
        content=[NS(type="text"), NS(type="tool_use", name="get_weather")],
    )


def _responses_response():
    return NS(
        model="gpt-4.1-2025-04-14",
        usage=NS(input_tokens=3, output_tokens=4),
        output=[NS(type="function_call", name="search"), NS(type="message")],
    )


def _oa_chunks():
    def chunk(content):
        return NS(model="gpt-4o", usage=None,
                  choices=[NS(index=0, delta=NS(content=content, tool_calls=None))])
    return [
        chunk("Hel"),
        chunk("lo"),
        NS(model="gpt-4o", choices=[],
           usage=NS(prompt_tokens=12, completion_tokens=34)),
    ]


class FakeClient:
    def __init__(self, api_key="sk-test", base_url="https://api.openai.com/v1"):
        self.api_key = api_key
        self.base_url = base_url


def _make_resource(name, response_factory, is_async):
    calls = []

    if is_async:
        async def create(self, **kwargs):
            calls.append(kwargs)
            if kwargs.get("boom"):
                raise RuntimeError("upstream down")
            if kwargs.get("stream"):
                return _AsyncIter(_oa_chunks())
            return response_factory()
    else:
        def create(self, **kwargs):
            calls.append(kwargs)
            if kwargs.get("boom"):
                raise RuntimeError("upstream down")
            if kwargs.get("stream"):
                return iter(_oa_chunks())
            return response_factory()

    def __init__(self, client=None):
        self._client = client or FakeClient()

    cls = type(name, (), {"create": create, "__init__": __init__})
    cls.calls = calls
    return cls


class _AsyncIter:
    def __init__(self, items):
        self._items = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration from None


@pytest.fixture
def fakes(monkeypatch):
    """Fake openai/anthropic resource modules plus recording telemetry."""
    chat = ModuleType("openai.resources.chat.completions")
    chat.Completions = _make_resource("Completions", _oa_response, False)
    chat.AsyncCompletions = _make_resource("AsyncCompletions", _oa_response, True)
    responses = ModuleType("openai.resources.responses")
    responses.Responses = _make_resource("Responses", _responses_response, False)
    responses.AsyncResponses = _make_resource(
        "AsyncResponses", _responses_response, True)
    messages = ModuleType("anthropic.resources.messages")
    messages.Messages = _make_resource("Messages", _anthropic_response, False)
    messages.AsyncMessages = _make_resource(
        "AsyncMessages", _anthropic_response, True)
    for mod in (chat, responses, messages):
        monkeypatch.setitem(sys.modules, mod.__name__, mod)

    RecordingSender.instances = []
    monkeypatch.setattr(inst, "TelemetrySender", RecordingSender)
    monkeypatch.setattr(inst, "AtlasTelemetrySender", RecordingSender)
    for var in ("PS_API_KEY", "PS_COLLECTOR_URL", "PS_ATLAS_URL",
                "PS_ATLAS_API_KEY", *inst._METADATA_ENV.values()):
        monkeypatch.delenv(var, raising=False)
    yield NS(chat=chat, responses=responses, messages=messages)
    prompt_shields.uninstrument()


def _events():
    return RecordingSender.instances[0].events


# --- lifecycle ----------------------------------------------------------------


def test_instrument_is_idempotent_and_reversible(fakes):
    original = fakes.chat.Completions.__dict__["create"]

    assert prompt_shields.instrument(ps_api_key="ps_key") is True
    patched = fakes.chat.Completions.__dict__["create"]
    assert patched is not original
    assert prompt_shields.instrument(ps_api_key="ps_key") is False
    assert fakes.chat.Completions.__dict__["create"] is patched

    fakes.chat.Completions().create(model="gpt-4o", messages=MESSAGES)
    assert len(_events()) == 1

    assert prompt_shields.uninstrument() is True
    assert prompt_shields.uninstrument() is False
    for mod, name in ((fakes.chat, "Completions"), (fakes.chat, "AsyncCompletions"),
                      (fakes.responses, "Responses"),
                      (fakes.messages, "AsyncMessages")):
        create = getattr(mod, name).__dict__["create"]
        assert not hasattr(create, "__ps_original__")

    fakes.chat.Completions().create(model="gpt-4o", messages=MESSAGES)
    assert len(_events()) == 1  # nothing new after uninstrument

    # Can instrument again after uninstrument.
    assert prompt_shields.instrument(ps_api_key="ps_key") is True


def test_instrument_requires_api_key(fakes):
    with pytest.raises(ValueError):
        prompt_shields.instrument()
    assert not inst.is_instrumented()


def test_missing_provider_packages_are_skipped(fakes, monkeypatch):
    for name in ("openai.resources.chat.completions", "openai.resources.responses",
                 "anthropic.resources.messages"):
        monkeypatch.setitem(sys.modules, name, None)  # import raises ImportError
    assert prompt_shields.instrument(ps_api_key="ps_key") is True
    assert inst._state.patches == []


def test_vendors_filter(fakes):
    prompt_shields.instrument(ps_api_key="ps_key", vendors=("anthropic",))
    patched = {cls.__name__ for cls, *_ in inst._state.patches}
    assert patched == {"Messages", "AsyncMessages"}


def test_config_from_environment(fakes, monkeypatch):
    monkeypatch.setenv("PS_API_KEY", "ps_env")
    monkeypatch.setenv("PS_COLLECTOR_URL", "http://collector:9000")
    monkeypatch.setenv("PS_USE_CASE", "support-bot")
    monkeypatch.setenv("PS_BUSINESS_UNIT", "cx")
    prompt_shields.instrument(owner="a@b.c")

    assert RecordingSender.instances[0].args == ("http://collector:9000", "ps_env")
    fakes.chat.Completions().create(model="gpt-4o", messages=MESSAGES)
    event = _events()[0]
    assert event["use_case_name"] == "support-bot"
    assert event["business_unit"] == "cx"
    assert event["owner_email"] == "a@b.c"


# --- events -------------------------------------------------------------------


def test_openai_chat_sync_event(fakes):
    prompt_shields.instrument(ps_api_key="ps_key")
    resp = fakes.chat.Completions().create(model="gpt-4o", messages=MESSAGES)

    assert resp.model == "gpt-4o-2024-08-06"
    event = _events()[0]
    assert event["vendor"] == "openai"
    assert event["source"] == "sdk"
    assert event["requested_model"] == "gpt-4o"
    assert event["served_model"] == "gpt-4o-2024-08-06"
    assert (event["tokens_in"], event["tokens_out"]) == (11, 22)
    assert event["tool_calls_used"] == [{"name": "lookup", "type": "function"}]
    assert event["detected_pii_types"] == ["email"]
    assert len(event["api_key_fingerprint"]) == 16
    assert "prompt_text" not in event
    assert RecordingSender.instances[0].flushes == 1


def test_anthropic_async_event(fakes):
    prompt_shields.instrument(ps_api_key="ps_key")
    resp = asyncio.run(fakes.messages.AsyncMessages().create(
        model="claude-test", messages=MESSAGES, max_tokens=10))

    assert resp.model == "claude-test"
    event = _events()[0]
    assert event["vendor"] == "anthropic"
    assert (event["tokens_in"], event["tokens_out"]) == (5, 7)
    assert event["tool_calls_used"] == [{"name": "get_weather", "type": "tool_use"}]


def test_responses_api_event(fakes):
    prompt_shields.instrument(ps_api_key="ps_key", send_prompt_text=True)
    fakes.responses.Responses().create(
        model="gpt-4.1", instructions="be brief", input="call 555-123-4567")

    event = _events()[0]
    assert event["served_model"] == "gpt-4.1-2025-04-14"
    assert (event["tokens_in"], event["tokens_out"]) == (3, 4)
    assert event["tool_calls_used"] == [{"name": "search", "type": "function"}]
    assert event["prompt_text"] == "be brief call 555-123-4567"


def test_sync_stream_emits_once_and_hides_injected_usage_chunk(fakes):
    prompt_shields.instrument(ps_api_key="ps_key")
    resource = fakes.chat.Completions()
    stream = resource.create(model="gpt-4o", messages=MESSAGES, stream=True)

    assert resource.calls[-1]["stream_options"] == {"include_usage": True}
    assert _events() == []
    chunks = list(stream)
    assert len(chunks) == 2  # usage-only chunk was injected by us, so hidden
    event = _events()[0]
    assert (event["tokens_in"], event["tokens_out"]) == (12, 34)
    assert event["stream"] is True and event["stream_completed"] is True
    assert len(_events()) == 1


def test_no_stream_options_for_non_openai_hosts(fakes):
    prompt_shields.instrument(ps_api_key="ps_key")
    resource = fakes.chat.Completions(FakeClient(base_url="http://gateway:8787/v1"))
    list(resource.create(model="gpt-4o", messages=MESSAGES, stream=True))
    assert "stream_options" not in resource.calls[-1]


def test_async_stream_emits_once(fakes):
    prompt_shields.instrument(ps_api_key="ps_key")

    async def run():
        stream = await fakes.chat.AsyncCompletions().create(
            model="gpt-4o", messages=MESSAGES, stream=True)
        return [c async for c in stream]

    assert len(asyncio.run(run())) == 2
    assert len(_events()) == 1
    assert _events()[0]["tokens_out"] == 34


def test_caller_kwargs_are_not_mutated(fakes):
    prompt_shields.instrument(ps_api_key="ps_key")
    kwargs = {"model": "gpt-4o", "messages": MESSAGES, "stream": True}
    list(fakes.chat.Completions().create(**kwargs))
    assert "stream_options" not in kwargs


# --- fail-open and pass-through ----------------------------------------------


def test_telemetry_errors_never_break_calls(fakes):
    prompt_shields.instrument(ps_api_key="ps_key")
    RecordingSender.instances[0].enqueue = MagicMock(side_effect=RuntimeError("x"))

    resp = fakes.chat.Completions().create(model="gpt-4o", messages=MESSAGES)
    assert resp.model == "gpt-4o-2024-08-06"
    chunks = list(fakes.chat.Completions().create(
        model="gpt-4o", messages=MESSAGES, stream=True))
    assert len(chunks) == 2


def test_event_build_errors_never_break_calls(fakes, monkeypatch):
    prompt_shields.instrument(ps_api_key="ps_key")
    monkeypatch.setattr(inst, "_event", MagicMock(side_effect=RuntimeError("x")))
    resp = fakes.messages.Messages().create(model="m", messages=MESSAGES)
    assert resp.model == "claude-test"


def test_upstream_errors_propagate_without_event(fakes):
    prompt_shields.instrument(ps_api_key="ps_key")
    with pytest.raises(RuntimeError, match="upstream down"):
        fakes.chat.Completions().create(model="gpt-4o", messages=MESSAGES, boom=True)
    assert _events() == []


def test_raw_response_calls_pass_through(fakes):
    prompt_shields.instrument(ps_api_key="ps_key")
    fakes.chat.Completions().create(
        model="gpt-4o", messages=MESSAGES,
        extra_headers={"X-Stainless-Raw-Response": "true"})
    assert _events() == []


def test_suppress_instrumentation(fakes):
    prompt_shields.instrument(ps_api_key="ps_key")
    with prompt_shields.suppress_instrumentation():
        fakes.chat.Completions().create(model="gpt-4o", messages=MESSAGES)
    assert _events() == []


# --- no double events with ShieldsClient --------------------------------------


def _sender_recorders(client):
    events = []
    client._telemetry.enqueue = events.append
    client._telemetry.flush_sync = lambda: None

    async def flush():
        return None

    client._telemetry.flush = flush
    return events


def test_shields_client_does_not_double_emit(fakes):
    from prompt_shields import ShieldsClient

    prompt_shields.instrument(ps_api_key="ps_key")
    # Build without touching the real openai package: inject the upstream.
    original_make = ShieldsClient._make_upstream
    try:
        ShieldsClient._make_upstream = lambda self, api_key, **kw: NS(
            chat=NS(completions=fakes.chat.Completions()), base_url=None)
        client = ShieldsClient(api_key="sk", ps_api_key="ps", vendor="openai")
    finally:
        ShieldsClient._make_upstream = original_make
    client_events = _sender_recorders(client)

    client.chat.completions.create(model="gpt-4o", messages=MESSAGES)
    assert len(client_events) == 1
    assert _events() == []

    # A direct call still emits through instrumentation.
    fakes.chat.Completions().create(model="gpt-4o", messages=MESSAGES)
    assert len(_events()) == 1


def test_async_shields_client_does_not_double_emit(fakes):
    from prompt_shields import AsyncShieldsClient

    prompt_shields.instrument(ps_api_key="ps_key")
    original_make = AsyncShieldsClient._make_upstream
    try:
        AsyncShieldsClient._make_upstream = lambda self, api_key, **kw: NS(
            chat=NS(completions=fakes.chat.AsyncCompletions()), base_url=None)
        client = AsyncShieldsClient(api_key="sk", ps_api_key="ps", vendor="openai")
    finally:
        AsyncShieldsClient._make_upstream = original_make
    client_events = _sender_recorders(client)

    asyncio.run(client.chat.completions.create(model="gpt-4o", messages=MESSAGES))
    assert len(client_events) == 1
    assert _events() == []


# --- real SDKs over a mock transport (skipped when not installed) -------------


@pytest.fixture
def recording(monkeypatch):
    RecordingSender.instances = []
    monkeypatch.setattr(inst, "TelemetrySender", RecordingSender)
    monkeypatch.delenv("PS_ATLAS_URL", raising=False)
    yield
    prompt_shields.uninstrument()


def _http(sdk):
    """The httpx-compatible module an SDK build uses (newer ones use httpx2)."""
    return getattr(sdk._base_client, "httpx2", None) or httpx


def _json_transport(http, body):
    return http.MockTransport(lambda request: http.Response(200, json=body))


def test_real_openai_chat_and_stream(recording):
    openai = pytest.importorskip("openai")
    chat_body = {
        "id": "c1", "object": "chat.completion", "created": 0,
        "model": "gpt-4o-2024-08-06",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
    }
    prompt_shields.instrument(ps_api_key="ps_key", vendors=("openai",))
    http = _http(openai)
    client = openai.OpenAI(
        api_key="sk-test",
        http_client=http.Client(transport=_json_transport(http, chat_body)))
    resp = client.chat.completions.create(model="gpt-4o", messages=MESSAGES)
    assert resp.choices[0].message.content == "hi"
    assert (_events()[0]["tokens_in"], _events()[0]["tokens_out"]) == (9, 2)

    seen = {}

    def sse(request):
        seen["body"] = json.loads(request.content)
        chunks = [
            {"id": "c2", "object": "chat.completion.chunk", "created": 0,
             "model": "gpt-4o", "choices": [{"index": 0, "delta": {"content": "yo"}}]},
            {"id": "c2", "object": "chat.completion.chunk", "created": 0,
             "model": "gpt-4o", "choices": [],
             "usage": {"prompt_tokens": 5, "completion_tokens": 1,
                       "total_tokens": 6}},
        ]
        text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
        text += "data: [DONE]\n\n"
        return http.Response(200, text=text,
                              headers={"content-type": "text/event-stream"})

    client = openai.OpenAI(api_key="sk-test",
                           http_client=http.Client(transport=http.MockTransport(sse)))
    with client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, stream=True) as stream:
        chunks = list(stream)
    assert seen["body"]["stream_options"] == {"include_usage": True}
    assert len(chunks) == 1
    event = _events()[1]
    assert (event["tokens_in"], event["tokens_out"]) == (5, 1)
    assert len(_events()) == 2


def test_real_anthropic_async(recording):
    anthropic = pytest.importorskip("anthropic")
    body = {
        "id": "m1", "type": "message", "role": "assistant",
        "model": "claude-test", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "hi"}],
        "usage": {"input_tokens": 4, "output_tokens": 3},
    }
    prompt_shields.instrument(ps_api_key="ps_key", vendors=("anthropic",))

    http = _http(anthropic)

    async def run():
        client = anthropic.AsyncAnthropic(
            api_key="sk-ant-test",
            http_client=http.AsyncClient(transport=_json_transport(http, body)))
        return await client.messages.create(
            model="claude-test", max_tokens=10, messages=MESSAGES)

    assert asyncio.run(run()).content[0].text == "hi"
    event = _events()[0]
    assert (event["vendor"], event["tokens_in"], event["tokens_out"]) == (
        "anthropic", 4, 3)
