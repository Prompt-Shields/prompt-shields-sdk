"""Service tier, 1-hour cache writes, audio tokens, tool fees and
provider-reported cost, from provider responses to event cost (#71).

Expected costs are computed by hand from list prices (USD per 1M tokens);
the per-dimension arithmetic is in schemas/pricing/cost-vectors.v1.json.
"""

from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
from prompt_shields import ShieldsClient
from prompt_shields.instrumentation import _extract_responses
from prompt_shields.providers import AnthropicAdapter, OpenAIAdapter

MESSAGES = [{"role": "user", "content": "hello"}]


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


def _stream(chunks):
    stream = MagicMock()
    stream.__iter__.return_value = iter(chunks)
    return stream


def _openai(model, usage, service_tier=None):
    return NS(model=model, choices=[], usage=usage, service_tier=service_tier)


# --- service tier ----------------------------------------------------------


def test_openai_flex_tier_is_discounted():
    response = _openai(
        "o3", NS(prompt_tokens=10000, completion_tokens=2000), "flex"
    )
    client, events = _client("openai", response)
    client.chat.completions.create(model="o3", messages=MESSAGES)
    event = events[0]
    assert event["service_tier"] == "flex"
    # 10000 x $1.00 + 2000 x $4.00 (flex list price)
    assert event["cost"] == pytest.approx(0.018)
    assert event["cost_source"] == "pricing_table"


def test_openai_priority_tier_is_unmetered_not_standard():
    response = _openai(
        "gpt-4o", NS(prompt_tokens=1000, completion_tokens=2000), "priority"
    )
    client, events = _client("openai", response)
    client.chat.completions.create(model="gpt-4o", messages=MESSAGES)
    event = events[0]
    assert event["service_tier"] == "priority"
    assert event["cost"] is None
    assert "cost_source" not in event


def test_streamed_openai_chunks_carry_service_tier():
    chunks = [
        NS(model="o3", service_tier="flex", usage=None,
           choices=[NS(index=0, delta=NS(content="hi", tool_calls=None))]),
        NS(model="o3", service_tier="flex", choices=[],
           usage=NS(prompt_tokens=10000, completion_tokens=2000)),
    ]
    client, events = _client("openai", _stream(chunks))
    list(client.chat.completions.create(model="o3", messages=MESSAGES, stream=True))
    assert events[0]["service_tier"] == "flex"
    assert events[0]["cost"] == pytest.approx(0.018)


def test_responses_api_reads_service_tier():
    response = NS(
        model="o3", output=[], service_tier="flex",
        usage=NS(input_tokens=10, output_tokens=5),
    )
    assert _extract_responses(response)["service_tier"] == "flex"


def test_non_string_service_tier_is_ignored():
    response = MagicMock()
    response.usage = NS(prompt_tokens=1, completion_tokens=1)
    response.choices = []
    assert OpenAIAdapter().extract(response)["service_tier"] is None


# --- audio tokens -----------------------------------------------------------


def test_openai_audio_tokens_priced_at_audio_rates():
    usage = NS(
        prompt_tokens=1000, completion_tokens=600,
        prompt_tokens_details=NS(cached_tokens=0, audio_tokens=800),
        completion_tokens_details=NS(reasoning_tokens=0, audio_tokens=500),
    )
    client, events = _client("openai", _openai("gpt-4o-audio-preview", usage))
    client.chat.completions.create(model="gpt-4o-audio-preview", messages=MESSAGES)
    event = events[0]
    assert event["tokens_audio_in"] == 800
    assert event["tokens_audio_out"] == 500
    # 200 x $2.50 + 800 x $40 + 100 x $10 + 500 x $80
    assert event["cost"] == pytest.approx(0.0735)


def test_zero_audio_tokens_are_not_reported():
    usage = NS(
        prompt_tokens=10, completion_tokens=5,
        prompt_tokens_details=NS(cached_tokens=0, audio_tokens=0),
        completion_tokens_details=NS(reasoning_tokens=0, audio_tokens=0),
    )
    fields = OpenAIAdapter().extract(_openai("gpt-4o", usage))
    assert fields["tokens_audio_in"] is None
    assert fields["tokens_audio_out"] is None


# --- provider-reported cost --------------------------------------------------


def test_openrouter_reported_cost_wins_and_is_labelled():
    usage = NS(prompt_tokens=1000, completion_tokens=1000, cost=0.00123)
    model = "meta-llama/llama-3.3-70b-instruct"
    client, events = _client("openai", _openai(model, usage))
    client.chat.completions.create(model=model, messages=MESSAGES)
    event = events[0]
    assert event["cost"] == pytest.approx(0.00123)
    assert event["cost_source"] == "provider_reported"


def test_reported_cost_wins_over_estimated_stream_tokens():
    chunks = [
        NS(model="m", usage=None,
           choices=[NS(index=0, delta=NS(content="hi", tool_calls=None))]),
        NS(model="m", choices=[], usage=NS(cost=0.5)),
    ]
    client, events = _client("openai", _stream(chunks))
    list(client.chat.completions.create(model="m", messages=MESSAGES, stream=True))
    assert events[0]["tokens_estimated"] is True
    assert events[0]["cost"] == 0.5
    assert events[0]["cost_source"] == "provider_reported"


# --- Anthropic 1-hour cache writes and web search ----------------------------


def _anthropic_usage(**extra):
    return NS(
        input_tokens=2000, output_tokens=1000,
        cache_read_input_tokens=0, cache_creation_input_tokens=8000,
        cache_creation=NS(ephemeral_5m_input_tokens=0,
                          ephemeral_1h_input_tokens=8000),
        **extra,
    )


def test_anthropic_1h_cache_writes_and_web_search_fee():
    usage = _anthropic_usage(
        server_tool_use=NS(web_search_requests=3), service_tier="standard"
    )
    response = NS(model="claude-sonnet-4-20250514", content=[], usage=usage)
    fields = AnthropicAdapter().extract(response)
    assert fields["tokens_cache_write_1h"] == 8000
    assert fields["web_search_requests"] == 3
    assert fields["service_tier"] == "standard"

    client, events = _client("anthropic", response)
    client.chat.completions.create(
        model="claude-sonnet-4-20250514", messages=MESSAGES
    )
    # 2000 x $3 + 8000 x $6 (1h write) + 1000 x $15 + 3 x $0.01
    assert events[0]["cost"] == pytest.approx(0.099)


def test_anthropic_batch_result_is_discounted():
    usage = NS(input_tokens=10000, output_tokens=1000, service_tier="batch")
    response = NS(model="claude-sonnet-4-20250514", content=[], usage=usage)
    client, events = _client("anthropic", response)
    client.chat.completions.create(
        model="claude-sonnet-4-20250514", messages=MESSAGES
    )
    # 10000 x $1.50 + 1000 x $7.50 (batch list price)
    assert events[0]["cost"] == pytest.approx(0.0225)


def test_streamed_anthropic_1h_cache_and_web_search():
    chunks = [
        NS(type="message_start", message=NS(
            model="claude-sonnet-4-20250514",
            usage=_anthropic_usage(service_tier="standard"),
        )),
        NS(type="content_block_delta", index=0, delta=NS(text="hi")),
        NS(type="message_delta", delta=NS(stop_reason="end_turn"),
           usage=NS(output_tokens=1000,
                    server_tool_use=NS(web_search_requests=3))),
        NS(type="message_stop"),
    ]
    client, events = _client("anthropic", _stream(chunks))
    list(client.chat.completions.create(
        model="claude-sonnet-4-20250514", messages=MESSAGES, stream=True
    ))
    event = events[0]
    assert event["tokens_cache_write_1h"] == 8000
    assert event["web_search_requests"] == 3
    assert event["cost"] == pytest.approx(0.099)
