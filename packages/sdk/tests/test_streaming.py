"""Tests for streaming telemetry (stream=True).

Upstream streams are fakes built from SimpleNamespace chunks shaped like the
OpenAI and Anthropic SDK objects. No network: the telemetry senders' enqueue
and flush are replaced with recorders.
"""

from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
from prompt_shields import AsyncShieldsClient, ShieldsClient
from prompt_shields.streaming import request_openai_stream_usage

MESSAGES = [{"role": "user", "content": "hello there, how are you"}]


# --- fake chunks ------------------------------------------------------------


def _oa_chunk(content=None, tool_name=None, finish=None):
    tool_calls = None
    if tool_name is not None:
        tool_calls = [NS(index=0, function=NS(name=tool_name, arguments=""))]
    delta = NS(content=content, tool_calls=tool_calls)
    return NS(
        model="gpt-4o-2024-08-06",
        usage=None,
        choices=[NS(index=0, delta=delta, finish_reason=finish)],
    )


def _oa_usage_chunk(prompt=12, completion=34):
    return NS(
        model="gpt-4o-2024-08-06",
        choices=[],
        usage=NS(prompt_tokens=prompt, completion_tokens=completion),
    )


def _oa_chunks(with_usage=True):
    chunks = [_oa_chunk("Hel"), _oa_chunk("lo"), _oa_chunk(finish="stop")]
    if with_usage:
        chunks.append(_oa_usage_chunk())
    return chunks


def _anthropic_events():
    return [
        NS(
            type="message_start",
            message=NS(
                model="claude-sonnet-4-5", usage=NS(input_tokens=15, output_tokens=1)
            ),
        ),
        NS(type="content_block_start", index=0, content_block=NS(type="text", text="")),
        NS(type="content_block_delta", index=0, delta=NS(type="text_delta", text="Hi")),
        NS(type="content_block_stop", index=0),
        NS(
            type="content_block_start",
            index=1,
            content_block=NS(type="tool_use", name="get_weather"),
        ),
        NS(type="content_block_stop", index=1),
        NS(
            type="message_delta",
            delta=NS(stop_reason="tool_use"),
            usage=NS(output_tokens=42),
        ),
        NS(type="message_stop"),
    ]


class FakeStream:
    """Sync stream with close(), like openai.Stream / anthropic.Stream."""

    def __init__(self, chunks, error_after=None):
        self._chunks = list(chunks)
        self._error_after = error_after
        self.closed = False
        self.response = "raw-http-response"

    def __iter__(self):
        for i, chunk in enumerate(self._chunks):
            if self._error_after is not None and i == self._error_after:
                raise ConnectionError("stream dropped")
            yield chunk

    def close(self):
        self.closed = True


class FakeAsyncStream:
    def __init__(self, chunks, error_after=None):
        self._chunks = list(chunks)
        self._error_after = error_after
        self.closed = False

    async def _gen(self):
        for i, chunk in enumerate(self._chunks):
            if self._error_after is not None and i == self._error_after:
                raise ConnectionError("stream dropped")
            yield chunk

    def __aiter__(self):
        return self._gen()

    async def close(self):
        self.closed = True


# --- client fixtures -------------------------------------------------------


def _sync_client(vendor, stream):
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test", vendor=vendor)
    events: list[dict] = []
    client._telemetry.enqueue = events.append
    client._telemetry.flush_sync = lambda: None
    client._upstream = MagicMock()
    client._upstream.chat.completions.create.return_value = stream
    client._upstream.messages.create.return_value = stream
    return client, events


def _async_client(vendor, stream):
    client = AsyncShieldsClient(api_key="sk-test", ps_api_key="ps-test", vendor=vendor)
    events: list[dict] = []
    client._telemetry.enqueue = events.append

    async def noop():
        return None

    client._telemetry.flush = noop

    async def fake_upstream(**kwargs):
        client.upstream_kwargs = kwargs
        return stream

    client.chat.completions._call_upstream = fake_upstream
    return client, events


# --- stream_options injection ----------------------------------------------


def test_include_usage_injected_when_unset():
    kwargs = {"stream": True}
    assert request_openai_stream_usage(kwargs) is True
    assert kwargs["stream_options"] == {"include_usage": True}


def test_include_usage_merged_into_existing_options():
    kwargs = {"stream_options": {"other": 1}}
    assert request_openai_stream_usage(kwargs) is True
    assert kwargs["stream_options"] == {"other": 1, "include_usage": True}


@pytest.mark.parametrize("value", [True, False])
def test_explicit_include_usage_is_respected(value):
    kwargs = {"stream_options": {"include_usage": value}}
    assert request_openai_stream_usage(kwargs) is False
    assert kwargs["stream_options"] == {"include_usage": value}


# --- sync: OpenAI ----------------------------------------------------------


def test_sync_openai_stream_emits_one_event_with_usage():
    chunks = _oa_chunks()
    client, events = _sync_client("openai", FakeStream(chunks))

    stream = client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, stream=True
    )
    assert events == []  # nothing emitted before the stream is consumed
    received = list(stream)

    # Injected usage-only chunk is hidden; all other chunks pass through as-is.
    assert received == chunks[:3]
    assert all(a is b for a, b in zip(received, chunks))
    _, kwargs = client._upstream.chat.completions.create.call_args
    assert kwargs["stream_options"] == {"include_usage": True}

    assert len(events) == 1
    event = events[0]
    assert event["stream"] is True
    assert event["stream_completed"] is True
    assert event["tokens_in"] == 12
    assert event["tokens_out"] == 34
    assert event["served_model"] == "gpt-4o-2024-08-06"
    assert event["cost"] is not None
    assert "tokens_estimated" not in event
    assert "stream_error" not in event


def test_sync_openai_usage_chunk_kept_when_caller_asked_for_it():
    chunks = _oa_chunks()
    client, events = _sync_client("openai", FakeStream(chunks))
    stream = client.chat.completions.create(
        model="gpt-4o",
        messages=MESSAGES,
        stream=True,
        stream_options={"include_usage": True},
    )
    assert list(stream) == chunks
    assert events[0]["tokens_out"] == 34


def test_sync_openai_without_usage_estimates_tokens():
    chunks = _oa_chunks(with_usage=False)
    client, events = _sync_client("openai", FakeStream(chunks))
    stream = client.chat.completions.create(
        model="gpt-4o",
        messages=MESSAGES,
        stream=True,
        stream_options={"include_usage": False},
    )
    assert list(stream) == chunks
    event = events[0]
    assert event["tokens_estimated"] is True
    assert event["tokens_out"] == 2  # "Hello" -> 5 chars -> ceil(5/4)
    assert event["tokens_in"] == 6  # 24 chars of message text


def test_sync_openai_tool_calls_collected():
    chunks = [
        _oa_chunk(tool_name="lookup"),
        _oa_chunk(finish="tool_calls"),
        _oa_usage_chunk(),
    ]
    client, events = _sync_client("openai", FakeStream(chunks))
    list(client.chat.completions.create(model="gpt-4o", messages=MESSAGES, stream=True))
    assert events[0]["tool_calls_used"] == [{"name": "lookup", "type": "function"}]


def test_sync_stream_closed_early_marks_incomplete():
    upstream = FakeStream(_oa_chunks())
    client, events = _sync_client("openai", upstream)
    stream = client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, stream=True
    )
    first = next(stream)
    assert first.choices[0].delta.content == "Hel"
    stream.close()
    stream.close()  # idempotent

    assert upstream.closed
    assert len(events) == 1
    assert events[0]["stream_completed"] is False
    assert events[0]["tokens_estimated"] is True
    assert "stream_error" not in events[0]


def test_sync_stream_context_manager_break_emits_once():
    upstream = FakeStream(_oa_chunks())
    client, events = _sync_client("openai", upstream)
    with client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, stream=True
    ) as stream:
        for _ in stream:
            break
    assert upstream.closed
    assert len(events) == 1
    assert events[0]["stream_completed"] is False


def test_sync_stream_context_manager_full_consumption_emits_once():
    client, events = _sync_client("openai", FakeStream(_oa_chunks()))
    with client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, stream=True
    ) as stream:
        list(stream)
    assert len(events) == 1
    assert events[0]["stream_completed"] is True


def test_sync_stream_error_is_reraised_and_recorded():
    client, events = _sync_client("openai", FakeStream(_oa_chunks(), error_after=1))
    stream = client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, stream=True
    )
    with pytest.raises(ConnectionError):
        list(stream)
    assert len(events) == 1
    assert events[0]["stream_completed"] is False
    assert events[0]["stream_error"] == "ConnectionError"


def test_sync_stream_delegates_attributes():
    client, _ = _sync_client("openai", FakeStream(_oa_chunks()))
    stream = client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, stream=True
    )
    assert stream.response == "raw-http-response"


def test_sync_stream_is_fail_open_when_telemetry_breaks():
    chunks = _oa_chunks()
    client, _ = _sync_client("openai", FakeStream(chunks))

    def boom(event):
        raise RuntimeError("telemetry down")

    client._telemetry.enqueue = boom
    stream = client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, stream=True
    )
    assert list(stream) == chunks[:3]


def test_sync_stream_is_fail_open_when_observe_breaks(monkeypatch):
    from prompt_shields import streaming

    def boom(self, chunk):
        raise RuntimeError("bad chunk")

    monkeypatch.setattr(streaming.StreamAccumulator, "observe", boom)
    chunks = _oa_chunks()
    client, events = _sync_client("openai", FakeStream(chunks))
    stream = client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, stream=True
    )
    assert list(stream) == chunks  # nothing hidden when observation fails
    assert len(events) == 1


def test_non_stream_call_is_not_wrapped():
    response = MagicMock()
    response.usage.prompt_tokens = 1
    response.usage.completion_tokens = 2
    response.model = "gpt-4o"
    response.choices = []
    client, events = _sync_client("openai", response)
    assert client.chat.completions.create(model="gpt-4o", messages=MESSAGES) is response
    _, kwargs = client._upstream.chat.completions.create.call_args
    assert "stream_options" not in kwargs
    assert "stream" not in events[0]


# --- sync: Anthropic -------------------------------------------------------


def test_sync_anthropic_stream_usage_from_message_events():
    chunks = _anthropic_events()
    client, events = _sync_client("anthropic", FakeStream(chunks))
    stream = client.chat.completions.create(
        model="claude-sonnet-4-5", messages=MESSAGES, stream=True
    )
    assert list(stream) == chunks

    _, kwargs = client._upstream.messages.create.call_args
    assert "stream_options" not in kwargs

    event = events[0]
    assert event["vendor"] == "anthropic"
    assert event["stream_completed"] is True
    assert event["tokens_in"] == 15
    assert event["tokens_out"] == 42
    assert event["served_model"] == "claude-sonnet-4-5"
    assert event["tool_calls_used"] == [{"name": "get_weather", "type": "tool_use"}]
    assert "tokens_estimated" not in event


def test_sync_anthropic_truncated_keeps_partial_usage():
    client, events = _sync_client("anthropic", FakeStream(_anthropic_events()))
    stream = client.chat.completions.create(
        model="claude-sonnet-4-5", messages=MESSAGES, stream=True
    )
    for _ in range(3):
        next(stream)
    stream.close()
    event = events[0]
    assert event["stream_completed"] is False
    assert event["tokens_in"] == 15
    assert event["tokens_out"] == 1  # from message_start; no message_delta yet


# --- async -----------------------------------------------------------------


async def test_async_openai_stream_emits_one_event_with_usage():
    chunks = _oa_chunks()
    client, events = _async_client("openai", FakeAsyncStream(chunks))
    stream = await client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, stream=True
    )
    received = [c async for c in stream]
    assert received == chunks[:3]
    assert client.upstream_kwargs["stream_options"] == {"include_usage": True}
    assert len(events) == 1
    assert events[0]["stream_completed"] is True
    assert events[0]["tokens_in"] == 12
    assert events[0]["tokens_out"] == 34


async def test_async_anthropic_stream():
    chunks = _anthropic_events()
    client, events = _async_client("anthropic", FakeAsyncStream(chunks))
    stream = await client.chat.completions.create(
        model="claude-sonnet-4-5", messages=MESSAGES, stream=True
    )
    assert [c async for c in stream] == chunks
    assert events[0]["tokens_out"] == 42


async def test_async_context_manager_break_marks_incomplete():
    upstream = FakeAsyncStream(_oa_chunks())
    client, events = _async_client("openai", upstream)
    async with await client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, stream=True
    ) as stream:
        async for _ in stream:
            break
    assert upstream.closed
    assert len(events) == 1
    assert events[0]["stream_completed"] is False


async def test_async_stream_error_is_reraised_and_recorded():
    client, events = _async_client(
        "openai", FakeAsyncStream(_oa_chunks(), error_after=2)
    )
    stream = await client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, stream=True
    )
    with pytest.raises(ConnectionError):
        async for _ in stream:
            pass
    assert len(events) == 1
    assert events[0]["stream_error"] == "ConnectionError"
    await stream.close()  # no second event
    assert len(events) == 1


async def test_async_stream_is_fail_open_when_telemetry_breaks():
    chunks = _oa_chunks()
    client, _ = _async_client("openai", FakeAsyncStream(chunks))

    async def boom():
        raise RuntimeError("telemetry down")

    client._telemetry.flush = boom
    stream = await client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, stream=True
    )
    assert [c async for c in stream] == chunks[:3]
