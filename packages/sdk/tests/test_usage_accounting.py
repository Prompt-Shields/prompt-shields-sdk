"""Cached, reasoning and streamed usage accounting (#9).

Fixtures in `fixtures/usage/` are provider responses with the cost a
customer would be billed at public list prices; the SDK estimate must be
within 2% of it.
"""

import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
from prompt_shields import ShieldsClient
from prompt_shields.pricing import estimate_cost
from prompt_shields.providers import (
    AnthropicAdapter,
    GeminiAdapter,
    OpenAIAdapter,
    gemini_usage_fields,
    get_adapter,
)
from prompt_shields.streaming import StreamAccumulator

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "usage").glob("*.json"))
ADAPTERS = {
    "openai": OpenAIAdapter(),
    "anthropic": AnthropicAdapter(),
    "google": GeminiAdapter(),
}
MESSAGES = [{"role": "user", "content": "hello"}]


def _ns(value):
    """JSON -> attribute objects, like the vendor SDKs' response models."""
    if isinstance(value, dict):
        return NS(**{k: _ns(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_ns(v) for v in value]
    return value


def _load(path):
    return json.loads(path.read_text())


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_fixture_fields_and_cost_within_2_percent(path):
    fx = _load(path)
    adapter = get_adapter(fx["vendor"])
    fields = adapter.extract(_ns(fx["response"]))
    for key, expected in fx["expected_fields"].items():
        assert fields[key] == expected, key

    model = adapter.pricing_model(fx["model"], fields["served_model"])
    cost = estimate_cost(
        fx["vendor"], model, fields["tokens_in"], fields["tokens_out"],
        tokens_cached=fields["tokens_cached"],
        tokens_cache_write=fields["tokens_cache_write"],
    )
    expected = fx["expected_cost_usd"]
    assert abs(cost - expected) / expected <= 0.02


def test_gemini_adapter_accepts_raw_json_dicts():
    fx = _load(next(p for p in FIXTURES if "gemini" in p.stem))
    fields = ADAPTERS["google"].extract(fx["response"])
    assert fields["tokens_cached"] == fx["expected_fields"]["tokens_cached"]
    assert fields["served_model"] == "gemini-1.5-pro-002"


def test_ignoring_cache_would_overcharge():
    """Sanity check that the fixtures exercise the cache discount."""
    fx = _load(next(p for p in FIXTURES if "anthropic" in p.stem))
    fields = AnthropicAdapter().extract(_ns(fx["response"]))
    naive = estimate_cost(
        "anthropic", fx["model"], fields["tokens_in"], fields["tokens_out"]
    )
    assert naive > fx["expected_cost_usd"] * 1.5


# --- adapters --------------------------------------------------------------


def test_openai_adapter_without_details_leaves_them_none():
    response = NS(
        model="gpt-4o", choices=[], usage=NS(prompt_tokens=10, completion_tokens=5)
    )
    fields = OpenAIAdapter().extract(response)
    assert fields["tokens_cached"] is None
    assert fields["tokens_reasoning"] is None
    assert fields["tokens_cache_write"] is None


def test_anthropic_adapter_without_cache_keeps_input_tokens():
    response = NS(
        model="claude", content=[], usage=NS(input_tokens=10, output_tokens=5)
    )
    fields = AnthropicAdapter().extract(response)
    assert fields["tokens_in"] == 10
    assert fields["tokens_cached"] is None


def test_gemini_thinking_tokens_are_output():
    fields = gemini_usage_fields({
        "promptTokenCount": 100,
        "candidatesTokenCount": 40,
        "thoughtsTokenCount": 60,
    })
    assert fields["tokens_out"] == 100
    assert fields["tokens_reasoning"] == 60
    assert fields["tokens_cached"] is None


def test_gemini_snake_case_usage_metadata():
    response = _ns({
        "usage_metadata": {
            "prompt_token_count": 10,
            "cached_content_token_count": 4,
            "candidates_token_count": 3,
        },
        "candidates": [
            {"content": {"parts": [{"function_call": {"name": "lookup"}}]}}
        ],
    })
    fields = GeminiAdapter().extract(response)
    assert fields["tokens_in"] == 10
    assert fields["tokens_cached"] == 4
    assert fields["tokens_out"] == 3
    assert fields["tool_calls_used"] == [{"name": "lookup", "type": "function"}]


# --- pricing ---------------------------------------------------------------


def test_custom_cache_rates_override_vendor_defaults():
    table = {("openai", "m"): (1.0, 2.0, 0.1)}
    # 900 uncached @1 + 100 cached @0.1 + 1000 out @2 (per 1k)
    assert estimate_cost(
        "openai", "m", 1000, 1000, pricing_table=table, tokens_cached=100
    ) == pytest.approx(0.9 + 0.01 + 2.0)


def test_cache_counts_larger_than_input_do_not_go_negative():
    cost = estimate_cost("openai", "gpt-4o", 10, 0, tokens_cached=50)
    assert cost >= 0


# --- events ----------------------------------------------------------------


def _client(vendor, response):
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test", vendor=vendor)
    events: list[dict] = []
    client._telemetry.enqueue = events.append
    client._telemetry.flush_sync = lambda: None
    client._upstream = MagicMock()
    client._upstream.base_url = "https://api.openai.com/v1/"
    client._upstream.chat.completions.create.return_value = response
    client._upstream.messages.create.return_value = response
    return client, events


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
def test_non_streamed_event_carries_usage_details(vendor):
    fx = _load(next(p for p in FIXTURES if _load(p)["vendor"] == vendor))
    client, events = _client(vendor, _ns(fx["response"]))
    client.chat.completions.create(model=fx["model"], messages=MESSAGES)
    event = events[0]
    for key, expected in fx["expected_fields"].items():
        assert event[key] == expected, key
    assert event["cost"] == pytest.approx(fx["expected_cost_usd"], rel=0.02)
    assert event["cost_source"] == "pricing_table"


def test_event_omits_cost_source_when_model_unpriced():
    response = NS(
        model="x", choices=[], usage=NS(prompt_tokens=1, completion_tokens=1)
    )
    client, events = _client("openai", response)
    client.chat.completions.create(model="unpriced-model", messages=MESSAGES)
    assert events[0]["cost"] is None
    assert "cost_source" not in events[0]
    assert "tokens_cached" not in events[0]


def _stream(chunks):
    stream = MagicMock()
    stream.__iter__.return_value = iter(chunks)
    return stream


def test_streamed_openai_event_carries_cached_and_reasoning_tokens():
    usage = NS(
        prompt_tokens=1200,
        completion_tokens=2500,
        prompt_tokens_details=NS(cached_tokens=1024),
        completion_tokens_details=NS(reasoning_tokens=2048),
    )
    chunks = [
        NS(model="o1", usage=None,
           choices=[NS(index=0, delta=NS(content="hi", tool_calls=None))]),
        NS(model="o1", usage=usage, choices=[]),
    ]
    client, events = _client("openai", _stream(chunks))
    list(client.chat.completions.create(model="o1", messages=MESSAGES, stream=True))
    event = events[0]
    assert event["tokens_cached"] == 1024
    assert event["tokens_reasoning"] == 2048
    assert event["cost"] == pytest.approx(0.16032, rel=0.02)
    assert event["cost_source"] == "pricing_table"


def test_streamed_anthropic_accumulates_cache_and_message_delta():
    chunks = [
        NS(type="message_start", message=NS(
            model="claude-sonnet-4-20250514",
            usage=NS(input_tokens=50, cache_read_input_tokens=10000,
                     cache_creation_input_tokens=2000, output_tokens=1),
        )),
        NS(type="content_block_delta", index=0, delta=NS(text="hi")),
        NS(type="message_delta", delta=NS(stop_reason="end_turn"),
           usage=NS(output_tokens=500)),
        NS(type="message_stop"),
    ]
    client, events = _client("anthropic", _stream(chunks))
    list(client.chat.completions.create(
        model="claude-sonnet-4-20250514", messages=MESSAGES, stream=True
    ))
    event = events[0]
    assert event["tokens_in"] == 12050
    assert event["tokens_out"] == 500
    assert event["tokens_cached"] == 10000
    assert event["tokens_cache_write"] == 2000
    assert event["cost"] == pytest.approx(0.01815, rel=0.02)


def test_anthropic_message_delta_cache_counts_override_start():
    acc = StreamAccumulator("anthropic", MESSAGES)
    acc.observe(NS(type="message_start", message=NS(
        model="c", usage=NS(input_tokens=5, output_tokens=1),
    )))
    acc.observe(NS(type="message_delta", usage=NS(
        output_tokens=9, input_tokens=5, cache_read_input_tokens=100,
    )))
    fields = acc.adapter_fields()
    assert fields["tokens_in"] == 105
    assert fields["tokens_cached"] == 100
    assert fields["tokens_out"] == 9


def test_streamed_estimated_tokens_mark_cost_source():
    chunks = [
        NS(model="gpt-4o", usage=None,
           choices=[NS(index=0, delta=NS(content="hello", tool_calls=None))]),
    ]
    client, events = _client("openai", _stream(chunks))
    list(client.chat.completions.create(
        model="gpt-4o", messages=MESSAGES, stream=True,
        stream_options={"include_usage": False},
    ))
    event = events[0]
    assert event["tokens_estimated"] is True
    assert event["cost_source"] == "pricing_table_estimated_tokens"
