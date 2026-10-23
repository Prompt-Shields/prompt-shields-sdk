"""Implicit outcome signals (#44): classes only, computed locally."""

import asyncio
import enum
import json
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import prompt_shields
import pytest
from prompt_shields import AsyncShieldsClient, ShieldsClient
from prompt_shields import outcome_signals as osig
from prompt_shields.outcome_signals import (
    OUTCOME_SIGNALS_VERSION,
    SIGNAL_CLASSES,
    RegenerateTracker,
    attach_outcome_signals,
    finish_reasons,
    request_key,
    response_signals,
)
from prompt_shields.streaming import ShieldsStream, StreamAccumulator

from .test_instrumentation import _events, fakes  # noqa: F401 - fixture

SECRET = "my card is 4111 1111 1111 1111, summarise the contract"
MESSAGES = [{"role": "user", "content": SECRET}]


@pytest.fixture(autouse=True)
def _fresh_tracker(monkeypatch):
    monkeypatch.delenv("PS_OUTCOME_SIGNALS", raising=False)
    osig.default_tracker().clear()
    yield
    osig.default_tracker().clear()


def _oa(finish="stop", content="ok", refusal=None, tool_args=None):
    tool_calls = None
    if tool_args is not None:
        tool_calls = [NS(function=NS(name="lookup", arguments=tool_args))]
    message = NS(content=content, refusal=refusal, tool_calls=tool_calls)
    return NS(model="gpt-4o", usage=NS(prompt_tokens=3, completion_tokens=4),
              choices=[NS(finish_reason=finish, message=message)])


# --- response classes -------------------------------------------------------


class _GeminiReason(enum.Enum):
    SAFETY = 3
    MAX_TOKENS = 2


@pytest.mark.parametrize("response, expected", [
    (_oa("length"), {"max_tokens"}),
    (_oa("content_filter"), {"refusal"}),
    (_oa("stop"), set()),
    (_oa("stop", refusal="I can't help with that"), {"refusal"}),
    (NS(stop_reason="max_tokens"), {"max_tokens"}),  # Anthropic
    (NS(stop_reason="refusal"), {"refusal"}),
    (NS(stop_reason="end_turn"), set()),
    (NS(candidates=[NS(finish_reason=_GeminiReason.SAFETY)]), {"refusal"}),
    (NS(candidates=[NS(finish_reason="FinishReason.MAX_TOKENS")]), {"max_tokens"}),
    (NS(candidates=[], prompt_feedback=NS(block_reason="SAFETY")), {"refusal"}),
    ({"stopReason": "max_tokens", "output": {}}, {"max_tokens"}),  # Bedrock
    ({"stopReason": "guardrail_intervened"}, {"refusal"}),
    (NS(finish_reason="MAX_TOKENS"), {"max_tokens"}),  # Cohere
    (NS(status="incomplete", incomplete_details=NS(reason="max_output_tokens"),
        output=[]), {"max_tokens"}),  # Responses API
    (NS(output=[NS(type="message", content=[NS(type="refusal", refusal="no")])]),
     {"refusal"}),
    (None, set()),
])
def test_response_classes(response, expected):
    assert response_signals(response) == expected


def test_finish_reasons_tolerates_mocks():
    assert finish_reasons(MagicMock()) == []
    assert response_signals(MagicMock(), {"response_format": MagicMock()}) == set()


@pytest.mark.parametrize("args, invalid", [
    ('{"q": "x"}', False),
    ('{"q": "x"', True),
    ('["not", "an", "object"]', True),
    ({"already": "parsed"}, False),
])
def test_tool_call_arguments(args, invalid):
    signals = response_signals(_oa("tool_calls", content=None, tool_args=args))
    assert ("tool_call_invalid" in signals) is invalid


def test_responses_function_call_arguments():
    response = NS(output=[NS(type="function_call", name="f", arguments="{oops")])
    assert response_signals(response) == {"tool_call_invalid"}


SCHEMA = {"type": "object", "required": ["name", "age"],
          "properties": {"name": {"type": "string"}}}


@pytest.mark.parametrize("request_, text, invalid", [
    ({"response_format": {"type": "json_object"}}, '{"a": 1}', False),
    ({"response_format": {"type": "json_object"}}, "Sure! {a: 1}", True),
    ({"response_format": {"type": "json_schema",
                          "json_schema": {"name": "p", "schema": SCHEMA}}},
     '{"name": "x", "age": 3}', False),
    ({"response_format": {"type": "json_schema",
                          "json_schema": {"name": "p", "schema": SCHEMA}}},
     '{"name": "x"}', True),
    ({"response_format": {"type": "json_schema",
                          "json_schema": {"name": "p", "schema": SCHEMA}}},
     "[1, 2]", True),
    ({}, "not json at all", False),  # no JSON requested
    ({"response_format": {"type": "text"}}, "plain", False),
])
def test_schema_validity(request_, text, invalid):
    signals = response_signals(_oa("stop", content=text), request_)
    assert ("schema_invalid" in signals) is invalid


def test_responses_text_format_schema():
    response = NS(output=[NS(type="message", content=[
        NS(type="output_text", text='{"name": '), NS(type="output_text", text='"x"}'),
    ])])
    request = {"text": {"format": {"type": "json_schema", "name": "p",
                                   "schema": SCHEMA}}}
    assert response_signals(response, request) == {"schema_invalid"}


def test_refusal_is_not_also_a_schema_failure():
    response = _oa("stop", content=None, refusal="no")
    request = {"response_format": {"type": "json_object"}}
    assert response_signals(response, request) == {"refusal"}


# --- regenerate / retry ------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_regenerate_within_window_only():
    clock = Clock()
    tracker = RegenerateTracker(window_s=60, clock=clock)
    key = request_key("gpt-4o", MESSAGES)
    assert tracker.observe(key) is None
    clock.now += 30
    assert tracker.observe(key) == "regenerate"
    clock.now += 61
    assert tracker.observe(key) is None


def test_retry_after_failure():
    tracker = RegenerateTracker(clock=Clock())
    key = request_key("gpt-4o", MESSAGES)
    tracker.record_failure(key)
    assert tracker.observe(key) == "retry"
    assert tracker.observe(key) == "regenerate"


def test_without_session_only_identical_requests_match():
    a = request_key("gpt-4o", MESSAGES)
    assert a == request_key("gpt-4o", [dict(MESSAGES[0])])
    assert a != request_key("gpt-4o-mini", MESSAGES)
    assert a != request_key("gpt-4o", [{"role": "user", "content": SECRET + "!"}])


def test_session_folds_case_and_whitespace_of_last_user_message():
    history = [{"role": "user", "content": "first"},
               {"role": "assistant", "content": "answer"}]
    a = request_key("m1", history + [{"role": "user", "content": "Try  AGAIN"}], "s1")
    b = request_key("m2", [{"role": "user", "content": "try again "}], "s1")
    assert a == b
    assert a != request_key("m1", [{"role": "user", "content": "try again"}], "s2")


def test_tracker_is_bounded_and_keeps_digests_only():
    tracker = RegenerateTracker(max_entries=3, clock=Clock())
    keys = [request_key("m", [{"role": "user", "content": f"q{i}"}]) for i in range(5)]
    for key in keys:
        tracker.observe(key)
    assert len(tracker._seen) == 3
    assert tracker.observe(keys[0]) is None  # evicted
    assert all(len(k) == 32 and "q" not in k for k in tracker._seen)
    assert request_key("m", []) is None


# --- event shape -------------------------------------------------------------


def test_attach_shape_and_fallback():
    event = {"fallback_chain": ["rate_limited"]}
    attach_outcome_signals(event, model="gpt-4o", messages=MESSAGES,
                           response=_oa("length"), tracker=RegenerateTracker())
    assert event["outcome_signals"] == ["fallback", "max_tokens"]
    assert event["outcome_signals_version"] == OUTCOME_SIGNALS_VERSION


def test_attach_empty_list_when_nothing_happened():
    event: dict = {}
    attach_outcome_signals(event, model="m", messages=MESSAGES, response=_oa())
    assert event["outcome_signals"] == []


def test_stream_classes():
    tracker = RegenerateTracker()
    abandoned = {"stream": True, "stream_completed": False}
    attach_outcome_signals(abandoned, model="m", messages=MESSAGES,
                           reasons=[], tracker=tracker)
    assert abandoned["outcome_signals"] == ["abandoned"]
    errored = {"stream": True, "stream_completed": False,
               "stream_error": "ConnectionError"}
    attach_outcome_signals(errored, model="m", messages=[{"role": "user", "content": "b"}],
                           reasons=["length"], tracker=tracker)
    assert errored["outcome_signals"] == ["max_tokens", "stream_error"]


def test_env_switch_turns_it_off(monkeypatch):
    monkeypatch.setenv("PS_OUTCOME_SIGNALS", "off")
    event: dict = {}
    attach_outcome_signals(event, model="m", messages=MESSAGES, response=_oa("length"))
    assert event == {}


def test_fail_open(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(osig, "response_signals", boom)
    event = {"a": 1}
    attach_outcome_signals(event, model="m", messages=MESSAGES, response=_oa())
    assert event == {"a": 1}


# --- clients -----------------------------------------------------------------


def _client(response=None, error=None):
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test")
    events: list[dict] = []
    client._telemetry.enqueue = events.append
    client._telemetry.flush_sync = lambda: None
    client._upstream = MagicMock()
    create = client._upstream.chat.completions.create
    if error is not None:
        create.side_effect = [error, response]
    else:
        create.return_value = response
    return client, events


def _no_text(events):
    blob = json.dumps(events, default=str)
    assert "4111" not in blob and "contract" not in blob
    for event in events:
        assert set(event["outcome_signals"]) <= SIGNAL_CLASSES


def test_client_regenerate_and_truncation():
    client, events = _client(_oa("length"))
    meta = {"session_id": "sess-1"}
    client.chat.completions.create(model="gpt-4o", messages=MESSAGES, ps_metadata=meta)
    client.chat.completions.create(model="gpt-4o", messages=MESSAGES, ps_metadata=meta)
    assert events[0]["outcome_signals"] == ["max_tokens"]
    assert events[1]["outcome_signals"] == ["max_tokens", "regenerate"]
    _no_text(events)


def test_client_retry_after_upstream_error():
    client, events = _client(_oa(), error=TimeoutError("upstream"))
    with pytest.raises(TimeoutError):
        client.chat.completions.create(model="gpt-4o", messages=MESSAGES)
    client.chat.completions.create(model="gpt-4o", messages=MESSAGES)
    assert len(events) == 1
    assert events[0]["outcome_signals"] == ["retry"]
    _no_text(events)


def test_client_schema_invalid():
    client, events = _client(_oa(content="Here you go: {"))
    client.chat.completions.create(model="gpt-4o", messages=MESSAGES,
                                   response_format={"type": "json_object"})
    assert events[0]["outcome_signals"] == ["schema_invalid"]


def test_async_client_retry():
    client = AsyncShieldsClient(api_key="sk-test", ps_api_key="ps-test")
    events: list[dict] = []
    client._telemetry.enqueue = events.append

    async def noop():
        return None

    client._telemetry.flush = noop
    calls = []

    async def upstream(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise ConnectionError("down")
        return _oa("content_filter")

    client.chat.completions._call_upstream = upstream

    async def run():
        with pytest.raises(ConnectionError):
            await client.chat.completions.create(model="gpt-4o", messages=MESSAGES)
        await client.chat.completions.create(model="gpt-4o", messages=MESSAGES)

    asyncio.run(run())
    assert events[0]["outcome_signals"] == ["refusal", "retry"]


def _chunk(content=None, finish=None):
    return NS(model="gpt-4o", usage=None,
              choices=[NS(index=0, delta=NS(content=content, tool_calls=None),
                          finish_reason=finish)])


def test_accumulator_collects_finish_reasons():
    acc = StreamAccumulator("openai", MESSAGES)
    for chunk in (_chunk("a"), _chunk(finish="length")):
        acc.observe(chunk)
    assert acc.adapter_fields()["finish_reasons"] == ["length"]
    acc = StreamAccumulator("anthropic", MESSAGES)
    acc.observe(NS(type="message_delta", delta=NS(stop_reason="refusal"),
                   usage=NS(output_tokens=1)))
    assert acc.adapter_fields()["finish_reasons"] == ["refusal"]
    acc = StreamAccumulator("bedrock", MESSAGES)
    acc.observe({"messageStop": {"stopReason": "max_tokens"}})
    assert acc.adapter_fields()["finish_reasons"] == ["max_tokens"]


def test_client_stream_truncated():
    client, events = _client([_chunk("a"), _chunk(finish="length")])
    stream = client.chat.completions.create(model="gpt-4o", messages=MESSAGES,
                                            stream=True)
    assert isinstance(stream, ShieldsStream)
    list(stream)
    assert events[0]["outcome_signals"] == ["max_tokens"]
    assert "finish_reasons" not in events[0]
    _no_text(events)


# --- instrument() --------------------------------------------------------------


def test_instrumented_retry_regenerate_and_abandon(fakes):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key")
    resource = fakes.chat.Completions()
    with pytest.raises(RuntimeError):
        resource.create(model="gpt-4o", messages=MESSAGES, boom=True)
    resource.create(model="gpt-4o", messages=MESSAGES)
    resource.create(model="gpt-4o", messages=MESSAGES)
    assert [e["outcome_signals"] for e in _events()] == [["retry"], ["regenerate"]]

    other = [{"role": "user", "content": "something else"}]
    stream = resource.create(model="gpt-4o", messages=other, stream=True)
    next(iter(stream))
    stream.close()
    assert _events()[-1]["outcome_signals"] == ["abandoned"]
    _no_text(_events())


def test_instrumented_async_failure_is_a_retry(fakes):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key")

    async def run():
        resource = fakes.chat.AsyncCompletions()
        with pytest.raises(RuntimeError):
            await resource.create(model="gpt-4o", messages=MESSAGES, boom=True)
        await resource.create(model="gpt-4o", messages=MESSAGES)

    asyncio.run(run())
    assert _events()[0]["outcome_signals"] == ["retry"]
    assert _events()[0]["outcome_signals_version"] == OUTCOME_SIGNALS_VERSION
