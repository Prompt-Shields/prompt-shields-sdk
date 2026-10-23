"""Cached, reasoning and streamed usage accounting per provider and call mode
(#9), and pricing at the served model (#6).

Every provider adapter is driven through the code that builds real events,
with no vendor SDK installed:
  - OpenAI and Anthropic through `ShieldsClient` / `AsyncShieldsClient`
    (plain and `stream=True`) with a faked upstream;
  - every provider through the event builders `instrument()` uses for its
    sync and async wrappers (`instrumentation._event`, and the stream
    accumulator inside `ShieldsStream` / `AsyncShieldsStream`).

Payloads are fake responses and stream events in each vendor's documented
shape. `cost` is the list-price bill, worked out by hand from per-million
token rates; the event's cost must be within 2% of it.
"""

import time
from types import SimpleNamespace as NS

import pytest
from prompt_shields import AsyncShieldsClient, ShieldsClient
from prompt_shields import instrumentation as inst
from prompt_shields.pricing import DEFAULT_PRICING, has_price, resolve_pricing_model
from prompt_shields.providers import (
    AzureOpenAIAdapter,
    BedrockAdapter,
    GeminiAdapter,
    OpenAIAdapter,
    get_adapter,
)
from prompt_shields.streaming import AsyncShieldsStream, ShieldsStream

MESSAGES = [{"role": "user", "content": "summarise the attached report"}]


def _ns(value):
    """JSON -> attribute objects, like the vendor SDKs' response models."""
    if isinstance(value, dict):
        return NS(**{k: _ns(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_ns(v) for v in value]
    return value


def _oa_chunk(model, text=None, usage=None):
    choices = [] if text is None else [
        {"index": 0, "delta": {"content": text, "tool_calls": None}}
    ]
    return {"model": model, "choices": choices, "usage": usage}


# --- cases --------------------------------------------------------------------

# OpenAI: a router picked o3 for model="auto". o3: $2 in, $0.50 cached,
# $8 out (reasoning billed as output).
# 976*2 + 1024*0.5 + 800*8 = 8864 -> $0.008864
_OA_USAGE = {
    "prompt_tokens": 2000,
    "completion_tokens": 800,
    "prompt_tokens_details": {"cached_tokens": 1024},
    "completion_tokens_details": {"reasoning_tokens": 600},
}
OPENAI = {
    "vendor": "openai",
    "model": "auto",
    "served_model": "o3-2025-04-16",
    "response": {
        "model": "o3-2025-04-16",
        "choices": [{"index": 0, "message": {"role": "assistant",
                                             "content": "ok", "tool_calls": None}}],
        "usage": _OA_USAGE,
    },
    "chunks": [
        _oa_chunk("o3-2025-04-16", "o"),
        _oa_chunk("o3-2025-04-16", "k"),
        _oa_chunk("o3-2025-04-16", usage=_OA_USAGE),
    ],
    "attrs": True,
    "fields": {"tokens_in": 2000, "tokens_out": 800, "tokens_cached": 1024,
               "tokens_reasoning": 600},
    "cost": 0.008864,
}

# Anthropic: alias requested, dated snapshot served. Sonnet 4: $3 in,
# $0.30 cache read, $3.75 cache write, $15 out.
# 100*3 + 2000*0.3 + 500*3.75 + 400*15 = 8775 -> $0.008775
ANTHROPIC = {
    "vendor": "anthropic",
    "model": "claude-sonnet-4-0",
    "served_model": "claude-sonnet-4-20250514",
    "response": {
        "model": "claude-sonnet-4-20250514",
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 100, "output_tokens": 400,
                  "cache_read_input_tokens": 2000,
                  "cache_creation_input_tokens": 500},
    },
    "chunks": [
        {"type": "message_start", "message": {
            "model": "claude-sonnet-4-20250514",
            "usage": {"input_tokens": 100, "output_tokens": 1,
                      "cache_read_input_tokens": 2000,
                      "cache_creation_input_tokens": 500}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 400}},
        {"type": "message_stop"},
    ],
    "attrs": True,
    "fields": {"tokens_in": 2600, "tokens_out": 400, "tokens_cached": 2000,
               "tokens_cache_write": 500},
    "cost": 0.008775,
}

# Gemini 2.5 Flash: $0.30 in, $0.03 cached, $2.50 out; thinking tokens are
# billed as output. 1000*0.3 + 3000*0.03 + 700*2.5 = 2140 -> $0.00214
GEMINI = {
    "vendor": "google",
    "model": "models/gemini-2.5-flash",
    "served_model": "gemini-2.5-flash",
    "response": {
        "candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]},
                        "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 4000, "candidatesTokenCount": 200,
                          "cachedContentTokenCount": 3000,
                          "thoughtsTokenCount": 500, "totalTokenCount": 4700},
        "modelVersion": "gemini-2.5-flash",
    },
    "chunks": [
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "o"}]}}],
         "usageMetadata": {"promptTokenCount": 4000, "candidatesTokenCount": 1,
                           "cachedContentTokenCount": 3000},
         "modelVersion": "gemini-2.5-flash"},
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "k"}]},
                         "finishReason": "STOP"}],
         "usageMetadata": {"promptTokenCount": 4000, "candidatesTokenCount": 200,
                           "cachedContentTokenCount": 3000,
                           "thoughtsTokenCount": 500, "totalTokenCount": 4700},
         "modelVersion": "gemini-2.5-flash"},
    ],
    "attrs": False,
    "fields": {"tokens_in": 4000, "tokens_out": 700, "tokens_cached": 3000,
               "tokens_reasoning": 500},
    "cost": 0.00214,
}

# Azure OpenAI: an unmapped deployment, priced as the served gpt-4o at
# OpenAI list prices: $2.50 in, $1.25 cached, $10 out.
# 952*2.5 + 2048*1.25 + 250*10 = 7440 -> $0.00744
_AZ_USAGE = {"prompt_tokens": 3000, "completion_tokens": 250,
             "prompt_tokens_details": {"cached_tokens": 2048}}
AZURE = {
    "vendor": "azure_openai",
    "model": "prod-chat",
    "served_model": "gpt-4o-2024-08-06",
    "response": {
        "model": "gpt-4o-2024-08-06",
        "choices": [{"index": 0, "message": {"role": "assistant",
                                             "content": "ok", "tool_calls": None}}],
        "usage": _AZ_USAGE,
    },
    "chunks": [
        _oa_chunk("gpt-4o-2024-08-06", "ok"),
        _oa_chunk("gpt-4o-2024-08-06", usage=_AZ_USAGE),
    ],
    "attrs": True,
    "fields": {"tokens_in": 3000, "tokens_out": 250, "tokens_cached": 2048},
    "cost": 0.00744,
}

# Bedrock Converse, Claude Sonnet 4 through a cross-region profile. Same
# rates and counts as the Anthropic case -> $0.008775
_BR_USAGE = {"inputTokens": 100, "outputTokens": 400,
             "cacheReadInputTokens": 2000, "cacheWriteInputTokens": 500,
             "totalTokens": 3000}
BEDROCK = {
    "vendor": "bedrock",
    "model": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "served_model": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "response": {
        "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
        "stopReason": "end_turn",
        "usage": _BR_USAGE,
    },
    "chunks": [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "ok"}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": _BR_USAGE, "metrics": {"latencyMs": 420}}},
    ],
    "attrs": False,
    "fields": {"tokens_in": 2600, "tokens_out": 400, "tokens_cached": 2000,
               "tokens_cache_write": 500},
    "cost": 0.008775,
}

# Mistral: the served snapshot has no price, so the requested alias is used.
# Large: $2 in, $6 out. 1000*2 + 200*6 = 3200 -> $0.0032
_MI_USAGE = {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200}
MISTRAL = {
    "vendor": "mistral",
    "model": "mistral-large-latest",
    "served_model": "mistral-large-2411",
    "response": {
        "model": "mistral-large-2411",
        "choices": [{"index": 0, "message": {"role": "assistant",
                                             "content": "ok", "tool_calls": None}}],
        "usage": _MI_USAGE,
    },
    # mistralai streams `CompletionEvent`s wrapping Chat Completions chunks.
    "chunks": [
        {"data": _oa_chunk("mistral-large-2411", "ok")},
        {"data": _oa_chunk("mistral-large-2411", usage=_MI_USAGE)},
    ],
    "attrs": True,
    "fields": {"tokens_in": 1000, "tokens_out": 200},
    "cost": 0.0032,
}

# Cohere v2, billed units. Command R+: $2.50 in, $10 out.
# 1000*2.5 + 200*10 = 4500 -> $0.0045
_CO_USAGE = {"billed_units": {"input_tokens": 1000, "output_tokens": 200},
             "tokens": {"input_tokens": 1064, "output_tokens": 200}}
COHERE = {
    "vendor": "cohere",
    "model": "command-r-plus-08-2024",
    "served_model": "command-r-plus-08-2024",
    "response": {
        "id": "c-1",
        "finish_reason": "COMPLETE",
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "ok"}]},
        "usage": _CO_USAGE,
    },
    "chunks": [
        {"type": "message-start", "id": "c-1"},
        {"type": "content-delta", "index": 0,
         "delta": {"message": {"content": {"text": "ok"}}}},
        {"type": "message-end",
         "delta": {"finish_reason": "COMPLETE", "usage": _CO_USAGE}},
    ],
    "attrs": False,
    "fields": {"tokens_in": 1000, "tokens_out": 200},
    "cost": 0.0045,
}

CASES = [OPENAI, ANTHROPIC, GEMINI, AZURE, BEDROCK, MISTRAL, COHERE]
CLIENT_CASES = [OPENAI, ANTHROPIC]
DETAIL_KEYS = ("tokens_cached", "tokens_cache_write", "tokens_reasoning")


def _payload(case, key):
    value = case[key]
    return _ns(value) if case["attrs"] else value


def _visible_chunks(case, client, chunks):
    """The chunks a `ShieldsClient` caller sees. On OpenAI streams the SDK
    asks for `include_usage` itself and hides the usage-only chunk it added,
    so the caller's chunk sequence is unchanged."""
    if case["vendor"] != "openai":
        return chunks
    assert client.upstream_kwargs["stream_options"] == {"include_usage": True}
    return [c for c in chunks if c.choices]


def _assert_event(case, event, stream):
    assert event["vendor"] == case["vendor"]
    assert event["requested_model"] == case["model"]
    assert event["served_model"] == case["served_model"]
    for key in ("tokens_in", "tokens_out"):
        assert event[key] == case["fields"][key], key
    for key in DETAIL_KEYS:
        assert event.get(key) == case["fields"].get(key), key
    assert "tokens_estimated" not in event
    assert event["cost"] == pytest.approx(case["cost"], rel=0.02)
    assert event["cost_source"] == "pricing_table"
    if stream:
        assert event["stream"] is True
        assert event["stream_completed"] is True
    else:
        assert "stream" not in event


# --- ShieldsClient / AsyncShieldsClient (OpenAI, Anthropic) -------------------


class _SyncStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __iter__(self):
        return iter(self._chunks)

    def close(self):
        pass


class _AsyncStream:
    def __init__(self, chunks):
        self._chunks = chunks

    async def _gen(self):
        for chunk in self._chunks:
            yield chunk

    def __aiter__(self):
        return self._gen()

    async def close(self):
        pass


def _sync_client(case, result):
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test",
                           vendor=case["vendor"])
    events: list[dict] = []
    client._telemetry.enqueue = events.append
    client._telemetry.flush_sync = lambda: None

    def upstream(**kwargs):
        client.upstream_kwargs = kwargs
        return result

    client.chat.completions._call_upstream = upstream
    return client, events


def _async_client(case, result):
    client = AsyncShieldsClient(api_key="sk-test", ps_api_key="ps-test",
                                vendor=case["vendor"])
    events: list[dict] = []
    client._telemetry.enqueue = events.append

    async def noop():
        return None

    async def upstream(**kwargs):
        client.upstream_kwargs = kwargs
        return result

    client._telemetry.flush = noop
    client.chat.completions._call_upstream = upstream
    return client, events


@pytest.mark.parametrize("case", CLIENT_CASES, ids=lambda c: c["vendor"])
def test_client_sync(case):
    client, events = _sync_client(case, _payload(case, "response"))
    client.chat.completions.create(model=case["model"], messages=MESSAGES)
    assert len(events) == 1
    _assert_event(case, events[0], stream=False)


@pytest.mark.parametrize("case", CLIENT_CASES, ids=lambda c: c["vendor"])
async def test_client_async(case):
    client, events = _async_client(case, _payload(case, "response"))
    await client.chat.completions.create(model=case["model"], messages=MESSAGES)
    assert len(events) == 1
    _assert_event(case, events[0], stream=False)


@pytest.mark.parametrize("case", CLIENT_CASES, ids=lambda c: c["vendor"])
def test_client_sync_stream(case):
    chunks = _payload(case, "chunks")
    client, events = _sync_client(case, _SyncStream(chunks))
    stream = client.chat.completions.create(
        model=case["model"], messages=MESSAGES, stream=True)
    assert list(stream) == _visible_chunks(case, client, chunks)
    assert len(events) == 1
    _assert_event(case, events[0], stream=True)


@pytest.mark.parametrize("case", CLIENT_CASES, ids=lambda c: c["vendor"])
async def test_client_async_stream(case):
    chunks = _payload(case, "chunks")
    client, events = _async_client(case, _AsyncStream(chunks))
    stream = await client.chat.completions.create(
        model=case["model"], messages=MESSAGES, stream=True)
    assert [chunk async for chunk in stream] == _visible_chunks(case, client, chunks)
    assert len(events) == 1
    _assert_event(case, events[0], stream=True)


# --- instrument() event builders (every provider) -----------------------------


def _call(case):
    state = NS(metadata={}, scan_pii=False, send_prompt_text=False,
               pricing_table=None)
    return {
        "state": state,
        "vendor": case["vendor"],
        "adapter": get_adapter(case["vendor"]),
        "api": "chat",
        "model": case["model"],
        "messages": MESSAGES,
        "api_key": None,
        "framework": None,
    }


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["vendor"])
def test_instrumented_response(case):
    event = inst._event(_call(case), _payload(case, "response"), latency_ms=5)
    _assert_event(case, event, stream=False)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["vendor"])
def test_instrumented_sync_stream(case):
    call = _call(case)
    events: list[dict] = []

    def on_finish(adapter_fields, stream_fields, latency_ms, flush=True):
        events.append(inst._stream_event(call, adapter_fields, stream_fields,
                                         latency_ms))

    chunks = _payload(case, "chunks")
    stream = ShieldsStream(_SyncStream(chunks), inst._accumulator(call),
                           on_finish, time.monotonic())
    assert list(stream) == chunks
    assert len(events) == 1
    _assert_event(case, events[0], stream=True)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["vendor"])
async def test_instrumented_async_stream(case):
    call = _call(case)
    events: list[dict] = []

    async def on_finish(adapter_fields, stream_fields, latency_ms, flush=True):
        events.append(inst._stream_event(call, adapter_fields, stream_fields,
                                         latency_ms))

    chunks = _payload(case, "chunks")
    stream = AsyncShieldsStream(_AsyncStream(chunks), inst._accumulator(call),
                                on_finish, time.monotonic())
    assert [chunk async for chunk in stream] == chunks
    assert len(events) == 1
    _assert_event(case, events[0], stream=True)


# --- #6: price at the served model ----------------------------------------------


def _oa_response(model, prompt=1000, completion=100):
    return NS(model=model, choices=[],
              usage=NS(prompt_tokens=prompt, completion_tokens=completion))


# gpt-4o-mini: $0.15 in, $0.60 out. 1000*0.15 + 100*0.6 = 210 -> $0.00021
MINI_COST = 0.00021


def test_auto_model_is_priced_at_served_model():
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test")
    event = client._build_event(model="auto", messages=MESSAGES,
                                response=_oa_response("gpt-4o-mini-2024-07-18"),
                                latency_ms=1)
    assert event["requested_model"] == "auto"
    assert event["cost"] == pytest.approx(MINI_COST)
    assert event["cost_source"] == "pricing_table"


async def test_auto_model_is_priced_at_served_model_async():
    client = AsyncShieldsClient(api_key="sk-test", ps_api_key="ps-test")
    event = client._build_event(model="auto", messages=MESSAGES,
                                response=_oa_response("gpt-4o-mini-2024-07-18"),
                                latency_ms=1)
    assert event["cost"] == pytest.approx(MINI_COST)


def test_routed_call_is_not_billed_at_the_requested_model():
    # Asked for gpt-4o, a gateway served gpt-4o-mini: bill the mini rates.
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test")
    event = client._build_event(model="gpt-4o", messages=MESSAGES,
                                response=_oa_response("gpt-4o-mini"),
                                latency_ms=1)
    assert event["served_model"] == "gpt-4o-mini"
    assert event["cost"] == pytest.approx(MINI_COST)


def test_unpriced_served_model_falls_back_to_requested():
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test")
    event = client._build_event(model="gpt-4o-mini", messages=MESSAGES,
                                response=_oa_response("acme-private-snapshot"),
                                latency_ms=1)
    assert event["cost"] == pytest.approx(MINI_COST)


def test_unpriced_auto_call_has_no_cost():
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test")
    event = client._build_event(model="auto", messages=MESSAGES,
                                response=_oa_response("acme-private-snapshot"),
                                latency_ms=1)
    assert event["cost"] is None
    assert "cost_source" not in event


def test_custom_pricing_table_is_keyed_by_served_model():
    table = {("openai", "acme-model"): (0.001, 0.002)}
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test",
                           pricing_table=table)
    event = client._build_event(model="auto", messages=MESSAGES,
                                response=_oa_response("acme-model"),
                                latency_ms=1)
    assert event["cost"] == pytest.approx(0.0012)


def test_instrumented_vendor_without_adapter_prices_served_model():
    state = NS(metadata={}, scan_pii=False, send_prompt_text=False,
               pricing_table={("acme", "acme-small"): (0.001, 0.001)})
    event = inst.build_event(
        state, vendor="acme", model="auto", messages=MESSAGES, latency_ms=1,
        adapter_fields={"tokens_in": 1000, "tokens_out": 1000,
                        "served_model": "acme-small"},
    )
    assert event["cost"] == pytest.approx(0.002)


@pytest.mark.parametrize(
    ("adapter", "model", "served", "expected"),
    [
        (OpenAIAdapter(), "auto", "gpt-4o-2024-08-06", "gpt-4o"),
        (OpenAIAdapter(), "gpt-4o", "gpt-4o-mini", "gpt-4o-mini"),
        (OpenAIAdapter(), "gpt-4o", None, "gpt-4o"),
        (GeminiAdapter(), "models/gemini-1.5-pro", "gemini-1.5-pro-002",
         "gemini-1.5-pro"),
        (GeminiAdapter(), "auto", "gemini-2.5-flash", "gemini-2.5-flash"),
        (AzureOpenAIAdapter(), "prod", "gpt-4o-mini-2024-07-18", "gpt-4o-mini"),
        (AzureOpenAIAdapter({"prod": "gpt-4o"}), "prod", "gpt-4o-mini", "gpt-4o"),
        (BedrockAdapter(), "eu.amazon.nova-lite-v1:0", None, "amazon.nova-lite-v1:0"),
    ],
)
def test_pricing_model_prefers_the_served_model(adapter, model, served, expected):
    assert adapter.pricing_model(model, served) == expected


def test_pricing_seam_helpers():
    assert has_price("openai", "gpt-4o")
    assert has_price("azure_openai", "gpt-4o")  # vendor fallback
    assert not has_price("openai", "auto")
    assert not has_price("openai", None)
    assert has_price("openai", "x", {("openai", "x", "images"): 0.04})
    assert resolve_pricing_model("openai", ["nope", "gpt-4o"]) == "gpt-4o"
    assert resolve_pricing_model("openai", ["nope", "auto"]) == "auto"
    assert resolve_pricing_model("openai", []) is None
    assert ("openai", "gpt-4o") in DEFAULT_PRICING
