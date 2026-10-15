"""instrument() streaming for google-genai, mistralai, cohere, Bedrock and the
Anthropic `messages.stream()` helpers.

Real SDKs (skipped when not installed) over mocked HTTP, as in
test_instrumentation_vendors. Each stream emits exactly one event when it
ends or is closed, and the caller's chunks are the SDK's own, unchanged.
"""

import asyncio
import base64
import binascii
import json
import struct

import httpx
import prompt_shields
import pytest
from prompt_shields import instrumentation as inst

from .test_instrumentation_vendors import (  # noqa: F401 - fixtures
    MESSAGES,
    _boto_client,
    _events,
    aws,
    recording,
)


def _sse(http, events, done=False):
    """A MockTransport answering every request with these SSE `data:` events."""
    seen = []

    def handler(request):
        seen.append(request)
        text = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
        if done:
            text += "data: [DONE]\n\n"
        return http.Response(200, text=text,
                             headers={"content-type": "text/event-stream"})

    return http.MockTransport(handler), seen


def _assert_one_stream_event(completed=True):
    assert len(_events()) == 1
    event = _events()[0]
    assert event["stream"] is True
    assert event["stream_completed"] is completed
    return event


# --- google-genai -------------------------------------------------------------

GEMINI_CHUNKS = [
    {"candidates": [{"content": {"role": "model", "parts": [{"text": "hel"}]}}],
     "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 1},
     "modelVersion": "gemini-2.5-flash"},
    {"candidates": [{"content": {"role": "model", "parts": [
        {"text": "lo"}, {"functionCall": {"name": "lookup", "args": {}}}]},
        "finishReason": "STOP"}],
     "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 2,
                       "thoughtsTokenCount": 1, "totalTokenCount": 11},
     "modelVersion": "gemini-2.5-flash"},
]


def _gemini_client(chunks=GEMINI_CHUNKS):
    pytest.importorskip("google.genai")
    from google import genai
    from google.genai import types

    transport, _ = _sse(httpx, chunks)
    atransport, _ = _sse(httpx, chunks)
    return genai.Client(api_key="g-key", http_options=types.HttpOptions(
        httpx_client=httpx.Client(transport=transport),
        httpx_async_client=httpx.AsyncClient(transport=atransport)))


def test_gemini_generate_content_stream_sync():
    client = _gemini_client()
    prompt_shields.instrument(ps_api_key="ps_key")
    stream = client.models.generate_content_stream(
        model="gemini-2.5-flash", contents="mail jane@example.com")
    texts = [c.candidates[0].content.parts[0].text for c in stream]
    assert texts == ["hel", "lo"]

    event = _assert_one_stream_event()
    assert event["vendor"] == "google"
    assert event["served_model"] == "gemini-2.5-flash"
    assert (event["tokens_in"], event["tokens_out"]) == (8, 3)
    assert event["tokens_reasoning"] == 1
    assert "tokens_estimated" not in event
    assert event["tool_calls_used"] == [{"name": "lookup", "type": "function"}]
    assert event["detected_pii_types"] == ["email"]
    assert event["cost"] is not None


def test_gemini_generate_content_stream_async_closed_early():
    client = _gemini_client()
    prompt_shields.instrument(ps_api_key="ps_key")

    async def run():
        stream = await client.aio.models.generate_content_stream(
            model="gemini-2.5-flash", contents="hi")
        async for _chunk in stream:
            break
        await stream.aclose()

    asyncio.run(run())
    event = _assert_one_stream_event(completed=False)
    assert (event["tokens_in"], event["tokens_out"]) == (8, 1)


# --- mistralai ----------------------------------------------------------------

MISTRAL_CHUNKS = [
    {"id": "x", "model": "mistral-small-latest", "object": "chat.completion.chunk",
     "choices": [{"index": 0, "finish_reason": None,
                  "delta": {"role": "assistant", "content": "hel"}}]},
    {"id": "x", "model": "mistral-small-latest", "object": "chat.completion.chunk",
     "choices": [{"index": 0, "finish_reason": "stop", "delta": {"content": "lo"}}],
     "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}},
]


def _mistral_client():
    pytest.importorskip("mistralai")
    import httpx2
    from mistralai.client import Mistral

    transport, _ = _sse(httpx2, MISTRAL_CHUNKS, done=True)
    atransport, _ = _sse(httpx2, MISTRAL_CHUNKS, done=True)
    return Mistral(api_key="m-key", client=httpx2.Client(transport=transport),
                   async_client=httpx2.AsyncClient(transport=atransport))


def test_mistral_chat_stream_sync():
    client = _mistral_client()
    prompt_shields.instrument(ps_api_key="ps_key")
    with client.chat.stream(model="mistral-small-latest", messages=MESSAGES) as s:
        texts = [e.data.choices[0].delta.content for e in s]
    assert texts == ["hel", "lo"]

    event = _assert_one_stream_event()
    assert event["vendor"] == "mistral"
    assert event["served_model"] == "mistral-small-latest"
    assert (event["tokens_in"], event["tokens_out"]) == (7, 2)
    assert event["detected_pii_types"] == ["email"]
    assert len(event["api_key_fingerprint"]) == 16
    assert event["cost"] is not None


def test_mistral_chat_stream_async():
    client = _mistral_client()
    prompt_shields.instrument(ps_api_key="ps_key")

    async def run():
        stream = await client.chat.stream_async(
            model="mistral-small-latest", messages=MESSAGES)
        async with stream:
            return [e async for e in stream]

    assert len(asyncio.run(run())) == 2
    event = _assert_one_stream_event()
    assert (event["tokens_in"], event["tokens_out"]) == (7, 2)


# --- cohere -------------------------------------------------------------------

COHERE_V2_EVENTS = [
    {"type": "message-start", "id": "x",
     "delta": {"message": {"role": "assistant"}}},
    {"type": "content-delta", "index": 0,
     "delta": {"message": {"content": {"text": "hello"}}}},
    {"type": "tool-call-start", "index": 0, "delta": {"message": {"tool_calls": {
        "id": "t", "type": "function",
        "function": {"name": "lookup", "arguments": ""}}}}},
    {"type": "message-end", "delta": {"finish_reason": "COMPLETE", "usage": {
        "billed_units": {"input_tokens": 5, "output_tokens": 2},
        "tokens": {"input_tokens": 50, "output_tokens": 2}}}},
]
COHERE_V1_EVENTS = [
    {"event_type": "stream-start", "generation_id": "g", "is_finished": False},
    {"event_type": "text-generation", "text": "hi", "is_finished": False},
    {"event_type": "stream-end", "finish_reason": "COMPLETE", "is_finished": True,
     "response": {"text": "hi", "generation_id": "g", "response_id": "r",
                  "meta": {"billed_units": {"input_tokens": 4, "output_tokens": 1}}}},
]


def test_cohere_v2_chat_stream_sync():
    cohere = pytest.importorskip("cohere")
    transport, _ = _sse(httpx, COHERE_V2_EVENTS)
    prompt_shields.instrument(ps_api_key="ps_key")
    client = cohere.ClientV2(api_key="co-key",
                             httpx_client=httpx.Client(transport=transport))
    events = list(client.chat_stream(model="command-r-08-2024", messages=MESSAGES))
    assert [e.type for e in events] == [e["type"] for e in COHERE_V2_EVENTS]

    event = _assert_one_stream_event()
    assert event["vendor"] == "cohere"
    assert (event["tokens_in"], event["tokens_out"]) == (5, 2)
    assert event["tool_calls_used"] == [{"name": "lookup", "type": "function"}]
    assert event["detected_pii_types"] == ["email"]
    assert len(event["api_key_fingerprint"]) == 16


def test_cohere_v2_chat_stream_async():
    cohere = pytest.importorskip("cohere")
    transport, _ = _sse(httpx, COHERE_V2_EVENTS)
    prompt_shields.instrument(ps_api_key="ps_key")

    async def run():
        client = cohere.AsyncClientV2(
            api_key="co-key", httpx_client=httpx.AsyncClient(transport=transport))
        return [e async for e in client.chat_stream(
            model="command-r-08-2024", messages=MESSAGES)]

    assert len(asyncio.run(run())) == 4
    event = _assert_one_stream_event()
    assert (event["tokens_in"], event["tokens_out"]) == (5, 2)


def _ndjson(http, events):
    text = "".join(json.dumps(e) + "\n" for e in events)
    return http.MockTransport(lambda request: http.Response(200, text=text))


def test_cohere_v1_chat_stream_sync_and_async():
    cohere = pytest.importorskip("cohere")
    prompt_shields.instrument(ps_api_key="ps_key", send_prompt_text=True)
    client = cohere.Client(api_key="co-key", httpx_client=httpx.Client(
        transport=_ndjson(httpx, COHERE_V1_EVENTS)))
    events = list(client.chat_stream(message="ring 555-123-4567",
                                     preamble="be brief"))
    assert [e.event_type for e in events] == [
        "stream-start", "text-generation", "stream-end"]
    event = _assert_one_stream_event()
    assert (event["tokens_in"], event["tokens_out"]) == (4, 1)
    assert event["prompt_text"] == "be brief ring 555-123-4567"

    async def run():
        aclient = cohere.AsyncClient(api_key="co-key", httpx_client=httpx.AsyncClient(
            transport=_ndjson(httpx, COHERE_V1_EVENTS)))
        return [e async for e in aclient.chat_stream(message="hi")]

    assert len(asyncio.run(run())) == 3
    assert len(_events()) == 2
    assert _events()[1]["tokens_in"] == 4


# --- Bedrock (boto3) ----------------------------------------------------------


def _event_message(event_type: str, payload: dict) -> bytes:
    """One AWS event-stream frame (`application/vnd.amazon.eventstream`)."""
    headers = b""
    for name, value in ((":message-type", "event"), (":event-type", event_type),
                        (":content-type", "application/json")):
        n, v = name.encode(), value.encode()
        headers += struct.pack("B", len(n)) + n + b"\x07" + struct.pack(">H", len(v)) + v
    body = json.dumps(payload).encode()
    total = 12 + len(headers) + len(body) + 4
    prelude = struct.pack(">II", total, len(headers))
    prelude += struct.pack(">I", binascii.crc32(prelude))
    message = prelude + headers + body
    return message + struct.pack(">I", binascii.crc32(message))


def _event_stream_client(frames):
    return _boto_client(
        "bedrock-runtime", 200, b"".join(_event_message(*f) for f in frames),
        headers={"content-type": "application/vnd.amazon.eventstream"})


CONVERSE_STREAM = [
    ("messageStart", {"role": "assistant"}),
    ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "hello"}}),
    ("contentBlockStart", {"contentBlockIndex": 1, "start": {
        "toolUse": {"toolUseId": "t", "name": "lookup"}}}),
    ("contentBlockStop", {"contentBlockIndex": 1}),
    ("messageStop", {"stopReason": "tool_use"}),
    ("metadata", {"usage": {"inputTokens": 10, "outputTokens": 3, "totalTokens": 25,
                            "cacheReadInputTokens": 12},
                  "metrics": {"latencyMs": 5}}),
]


@pytest.mark.usefixtures("aws")
def test_bedrock_converse_stream():
    prompt_shields.instrument(ps_api_key="ps_key")
    client = _event_stream_client(CONVERSE_STREAM)
    resp = client.converse_stream(
        modelId="us.amazon.nova-lite-v1:0",
        messages=[{"role": "user", "content": [{"text": "jane@example.com"}]}])
    assert _events() == []  # nothing until the stream ends
    events = list(resp["stream"])
    assert [next(iter(e)) for e in events] == [f[0] for f in CONVERSE_STREAM]
    assert events[1] == {"contentBlockDelta": {"contentBlockIndex": 0,
                                               "delta": {"text": "hello"}}}

    event = _assert_one_stream_event()
    assert event["vendor"] == "bedrock"
    assert (event["tokens_in"], event["tokens_out"]) == (22, 3)
    assert event["tokens_cached"] == 12
    assert event["tool_calls_used"] == [{"name": "lookup", "type": "tool_use"}]
    assert event["detected_pii_types"] == ["email"]
    assert event["cost"] is not None


@pytest.mark.usefixtures("aws")
def test_bedrock_converse_stream_closed_early():
    prompt_shields.instrument(ps_api_key="ps_key")
    resp = _event_stream_client(CONVERSE_STREAM).converse_stream(
        modelId="us.amazon.nova-lite-v1:0", messages=[])
    stream = resp["stream"]
    next(iter(stream))
    stream.close()
    event = _assert_one_stream_event(completed=False)
    assert event["tokens_estimated"] is True


def _chunk(body: dict) -> tuple:
    return ("chunk", {"bytes": base64.b64encode(json.dumps(body).encode()).decode()})


INVOKE_STREAM = [
    _chunk({"type": "message_start", "message": {
        "model": "claude-3-5-haiku-20241022", "usage": {"input_tokens": 21}}}),
    _chunk({"type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""}}),
    _chunk({"type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "hi"}}),
    _chunk({"type": "message_stop", "amazon-bedrock-invocationMetrics": {
        "inputTokenCount": 21, "outputTokenCount": 4,
        "invocationLatency": 100, "firstByteLatency": 50}}),
]


@pytest.mark.usefixtures("aws")
def test_bedrock_invoke_model_with_response_stream():
    prompt_shields.instrument(ps_api_key="ps_key")
    client = _event_stream_client(INVOKE_STREAM)
    resp = client.invoke_model_with_response_stream(
        modelId="anthropic.claude-3-5-haiku-20241022-v1:0",
        body=json.dumps({"anthropic_version": "bedrock-2023-05-31",
                         "max_tokens": 10, "messages": [
                             {"role": "user", "content": "jane@example.com"}]}))
    bodies = [json.loads(e["chunk"]["bytes"]) for e in resp["body"]]
    assert bodies[2]["delta"]["text"] == "hi"

    event = _assert_one_stream_event()
    assert (event["tokens_in"], event["tokens_out"]) == (21, 4)
    assert event["served_model"] == "claude-3-5-haiku-20241022"
    assert event["detected_pii_types"] == ["email"]
    assert event["cost"] is not None


@pytest.mark.usefixtures("aws")
def test_bedrock_stream_telemetry_failure_is_swallowed(monkeypatch):
    prompt_shields.instrument(ps_api_key="ps_key")
    monkeypatch.setattr(inst, "_stream_event", lambda *a, **k: 1 / 0)
    resp = _event_stream_client(CONVERSE_STREAM).converse_stream(
        modelId="m", messages=[])
    assert len(list(resp["stream"])) == len(CONVERSE_STREAM)
    assert _events() == []


# --- Anthropic messages.stream() helpers --------------------------------------

ANTHROPIC_EVENTS = [
    ("message_start", {"type": "message_start", "message": {
        "id": "m1", "type": "message", "role": "assistant", "content": [],
        "model": "claude-test", "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 9, "output_tokens": 1}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0,
                             "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "text_delta", "text": "hello"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("message_delta", {"type": "message_delta",
                       "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                       "usage": {"output_tokens": 5}}),
    ("message_stop", {"type": "message_stop"}),
]


def _anthropic_transport(http):
    text = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n"
                   for name, data in ANTHROPIC_EVENTS)
    return http.MockTransport(lambda request: http.Response(
        200, text=text, headers={"content-type": "text/event-stream"}))


def _anthropic():
    anthropic = pytest.importorskip("anthropic")
    return anthropic, getattr(anthropic._base_client, "httpx2", None) or httpx


def test_anthropic_messages_stream_helper_sync():
    anthropic, http = _anthropic()
    prompt_shields.instrument(ps_api_key="ps_key")
    client = anthropic.Anthropic(api_key="sk-ant", http_client=http.Client(
        transport=_anthropic_transport(http)))
    with client.messages.stream(model="claude-test", max_tokens=10,
                                messages=MESSAGES) as stream:
        assert list(stream.text_stream) == ["hello"]
        final = stream.get_final_message()
    assert final.usage.output_tokens == 5

    event = _assert_one_stream_event()
    assert event["vendor"] == "anthropic"
    assert (event["tokens_in"], event["tokens_out"]) == (9, 5)
    assert event["served_model"] == "claude-test"
    assert event["detected_pii_types"] == ["email"]


def test_anthropic_messages_stream_helper_closed_early_and_beta():
    anthropic, http = _anthropic()
    prompt_shields.instrument(ps_api_key="ps_key")
    client = anthropic.Anthropic(api_key="sk-ant", http_client=http.Client(
        transport=_anthropic_transport(http)))
    with client.beta.messages.stream(model="claude-test", max_tokens=10,
                                     messages=MESSAGES) as stream:
        next(iter(stream))
    event = _assert_one_stream_event(completed=False)
    assert event["tokens_in"] == 9


def test_anthropic_messages_stream_helper_async():
    anthropic, http = _anthropic()
    prompt_shields.instrument(ps_api_key="ps_key")

    async def run():
        client = anthropic.AsyncAnthropic(api_key="sk-ant", http_client=http.AsyncClient(
            transport=_anthropic_transport(http)))
        async with client.messages.stream(model="claude-test", max_tokens=10,
                                          messages=MESSAGES) as stream:
            return await stream.get_final_text()

    assert asyncio.run(run()) == "hello"
    event = _assert_one_stream_event()
    assert (event["tokens_in"], event["tokens_out"]) == (9, 5)


def test_anthropic_stream_helper_error_emits_nothing():
    anthropic, http = _anthropic()
    prompt_shields.instrument(ps_api_key="ps_key")
    client = anthropic.Anthropic(api_key="sk-ant", max_retries=0, http_client=http.Client(
        transport=http.MockTransport(lambda r: http.Response(
            400, json={"type": "error", "error": {"type": "invalid_request_error",
                                                  "message": "bad"}}))))
    with pytest.raises(anthropic.BadRequestError), client.messages.stream(
            model="claude-test", max_tokens=10, messages=MESSAGES):
        pass
    assert _events() == []


def test_stream_methods_pass_through_when_suppressed():
    client = _gemini_client()
    prompt_shields.instrument(ps_api_key="ps_key")
    with prompt_shields.suppress_instrumentation():
        assert len(list(client.models.generate_content_stream(
            model="gemini-2.5-flash", contents="hi"))) == 2
    assert _events() == []


def test_lazy_stream_request_error_emits_nothing():
    # google-genai and cohere send the request on the first read; a failed
    # request is reported like a failed non-streamed call: no event.
    pytest.importorskip("google.genai")
    cohere = pytest.importorskip("cohere")
    from cohere.core.api_error import ApiError
    from google import genai
    from google.genai import errors, types

    prompt_shields.instrument(ps_api_key="ps_key")

    def failing(status, body):
        return httpx.MockTransport(lambda r: httpx.Response(status, json=body))

    client = genai.Client(api_key="g-key", http_options=types.HttpOptions(
        httpx_client=httpx.Client(transport=failing(400, {"error": {
            "code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}}))))
    with pytest.raises(errors.ClientError):
        list(client.models.generate_content_stream(
            model="gemini-2.5-flash", contents="hi"))

    co = cohere.ClientV2(api_key="co-key", httpx_client=httpx.Client(
        transport=failing(400, {"message": "bad"})))
    with pytest.raises(ApiError):
        list(co.chat_stream(model="command-r-08-2024", messages=MESSAGES))
    assert _events() == []
