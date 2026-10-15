"""Gemini, Bedrock, Azure OpenAI, Mistral and Cohere adapters (#89).

Recorded responses and their billed cost live in `fixtures/usage/` and are
checked by `test_usage_accounting.py`; these tests cover the other response
shapes, raw JSON dicts, pricing-model resolution and the built event.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from prompt_shields.client import ShieldsClient
from prompt_shields.pricing import estimate_cost
from prompt_shields.providers import (
    AzureOpenAIAdapter,
    BedrockAdapter,
    CohereAdapter,
    GeminiAdapter,
    MistralAdapter,
    get_adapter,
)

FIXTURES = Path(__file__).parent / "fixtures" / "usage"


def _fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.mark.parametrize("vendor, cls", [
    ("google", GeminiAdapter),
    ("azure_openai", AzureOpenAIAdapter),
    ("bedrock", BedrockAdapter),
    ("mistral", MistralAdapter),
    ("cohere", CohereAdapter),
])
def test_new_vendors_are_registered(vendor, cls):
    adapter = get_adapter(vendor)
    assert isinstance(adapter, cls)
    assert adapter.vendor == vendor


def test_adapters_do_not_import_vendor_sdks():
    code = (
        "import sys, prompt_shields, prompt_shields.providers\n"
        "bad = [m for m in ('google.genai', 'boto3', 'botocore', 'mistralai',"
        " 'cohere') if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])}
    subprocess.run([sys.executable, "-c", code], check=True, env=env)


@pytest.mark.parametrize("name", [
    "bedrock_converse_claude_cache",
    "bedrock_invoke_nova_headers",
    "azure_openai_gpt4o_cached",
    "mistral_large_tool_call",
    "cohere_command_r_plus_v2",
    "gemini_25_flash_thinking",
])
def test_adapters_read_raw_json_dicts(name):
    """boto3 returns plain dicts; other SDKs' JSON must parse the same way."""
    fx = _fixture(name)
    fields = get_adapter(fx["vendor"]).extract(fx["response"])
    for key, expected in fx["expected_fields"].items():
        assert fields[key] == expected, key


# --- Bedrock ---------------------------------------------------------------


def test_bedrock_invoke_model_anthropic_body():
    body = {
        "id": "msg_bdrk_1",
        "type": "message",
        "model": "claude-3-5-haiku-20241022",
        "content": [{"type": "tool_use", "id": "t1", "name": "search", "input": {}}],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 2000,
            "cache_creation_input_tokens": 300,
        },
    }
    fields = BedrockAdapter().extract(body)
    assert fields["tokens_in"] == 2400
    assert fields["tokens_cached"] == 2000
    assert fields["tokens_cache_write"] == 300
    assert fields["tokens_out"] == 50
    assert fields["tool_calls_used"] == [{"name": "search", "type": "tool_use"}]
    assert fields["served_model"] == "claude-3-5-haiku-20241022"


def test_bedrock_invoke_model_nova_body():
    body = {
        "output": {"message": {"role": "assistant", "content": [{"text": "hi"}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 40, "outputTokens": 10,
                  "cacheReadInputTokenCount": 60, "cacheWriteInputTokenCount": 0},
    }
    fields = BedrockAdapter().extract(body)
    assert fields["tokens_in"] == 100
    assert fields["tokens_cached"] == 60
    assert fields["tokens_out"] == 10


def test_bedrock_invoke_model_llama_body():
    body = {"generation": "hi", "prompt_token_count": 12,
            "generation_token_count": 3, "stop_reason": "stop"}
    fields = BedrockAdapter().extract(body)
    assert fields["tokens_in"] == 12
    assert fields["tokens_out"] == 3
    assert fields["tokens_cached"] is None


def test_bedrock_invoke_model_headers_do_not_read_body():
    class Body:
        def read(self):
            raise AssertionError("adapter must not consume the stream")

    response = {
        "body": Body(),
        "ResponseMetadata": {"HTTPHeaders": {
            "X-Amzn-Bedrock-Input-Token-Count": "7",
            "X-Amzn-Bedrock-Output-Token-Count": "2",
        }},
    }
    fields = BedrockAdapter().extract(response)
    assert fields["tokens_in"] == 7
    assert fields["tokens_out"] == 2
    assert fields["tokens_cached"] is None


def test_bedrock_response_without_usage_is_unmetered():
    fields = BedrockAdapter().extract({"ResponseMetadata": {}})
    assert fields["tokens_in"] is None
    assert fields["tokens_out"] is None


@pytest.mark.parametrize("model", [
    "anthropic.claude-3-5-haiku-20241022-v1:0",
    "us.anthropic.claude-3-5-haiku-20241022-v1:0",
    "eu.anthropic.claude-3-5-haiku-20241022-v1:0",
    "apac.anthropic.claude-3-5-haiku-20241022-v1:0",
])
def test_bedrock_inference_profiles_price_as_base_model(model):
    priced = BedrockAdapter().pricing_model(model, None)
    assert priced == "anthropic.claude-3-5-haiku-20241022-v1:0"
    assert estimate_cost("bedrock", priced, 1000, 1000) == pytest.approx(0.0048)


# --- Azure OpenAI ----------------------------------------------------------


def test_azure_deployment_map_wins_over_served_model():
    adapter = AzureOpenAIAdapter(deployments={"prod-chat": "gpt-4o-mini"})
    assert adapter.pricing_model("prod-chat", "gpt-4o-2024-08-06") == "gpt-4o-mini"


def test_azure_unmapped_deployment_uses_served_model_without_date():
    adapter = AzureOpenAIAdapter()
    assert adapter.pricing_model("prod-chat", "gpt-4o-mini-2024-07-18") == "gpt-4o-mini"
    assert adapter.pricing_model("gpt-4o", None) == "gpt-4o"


def test_azure_prices_fall_back_to_openai_with_cache_discount():
    cost = estimate_cost("azure_openai", "gpt-4o", 2000, 0, tokens_cached=1000)
    assert cost == estimate_cost("openai", "gpt-4o", 2000, 0, tokens_cached=1000)
    assert estimate_cost("azure_openai", "no-such-model", 1, 1) is None


def test_azure_pricing_table_override_is_used_first():
    table = {("azure_openai", "gpt-4o"): (1.0, 1.0), ("openai", "gpt-4o"): (9.0, 9.0)}
    assert estimate_cost("azure_openai", "gpt-4o", 1000, 0, pricing_table=table) == 1.0


# --- Cohere ----------------------------------------------------------------


def test_cohere_v1_response_uses_meta_and_tool_calls():
    response = NS(
        text="",
        meta=NS(billed_units=NS(input_tokens=30, output_tokens=8)),
        tool_calls=[NS(name="query_db", parameters={})],
    )
    fields = CohereAdapter().extract(response)
    assert fields["tokens_in"] == 30
    assert fields["tokens_out"] == 8
    assert fields["tool_calls_used"] == [{"name": "query_db", "type": "function"}]


def test_cohere_falls_back_to_raw_tokens_without_billed_units():
    fields = CohereAdapter().extract(
        {"message": {"content": []}, "usage": {"tokens": {"input_tokens": 5, "output_tokens": 2}}}
    )
    assert fields["tokens_in"] == 5
    assert fields["tokens_out"] == 2
    assert fields["tool_calls_used"] is None


# --- Mistral ---------------------------------------------------------------


def test_mistral_reports_cached_tokens_when_present():
    response = {
        "model": "mistral-small-latest",
        "choices": [{"message": {"content": "ok", "tool_calls": None}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 5,
                  "prompt_tokens_details": {"cached_tokens": 64}},
    }
    fields = MistralAdapter().extract(response)
    assert fields["tokens_cached"] == 64
    assert fields["tool_calls_used"] is None


# --- events ----------------------------------------------------------------


def _event(vendor, adapter, model, response):
    # Same stand-in for a ShieldsClient that auto-instrumentation uses.
    shim = NS(
        _vendor=vendor, _adapter=adapter, _metadata={}, _scan_pii=False,
        _send_prompt_text=False, _pricing_table=None, _api_key_fingerprint=None,
    )
    return ShieldsClient._build_event(
        shim, model=model, messages=[], response=response, latency_ms=1,
    )


def test_azure_event_keeps_deployment_name_and_prices_mapped_model():
    fx = _fixture("azure_openai_gpt4o_cached")
    adapter = AzureOpenAIAdapter(deployments={"prod-chat": "gpt-4o"})
    event = _event("azure_openai", adapter, "prod-chat", fx["response"])
    assert event["vendor"] == "azure_openai"
    assert event["model"] == "prod-chat"
    assert event["served_model"] == "gpt-4o-2024-08-06"
    assert event["tokens_cached"] == 1024
    assert event["cost"] == pytest.approx(fx["expected_cost_usd"], rel=0.02)
    assert event["cost_source"] == "pricing_table"


def test_bedrock_event_with_inference_profile_is_priced():
    fx = _fixture("bedrock_converse_claude_cache")
    event = _event(
        "bedrock", get_adapter("bedrock"),
        "us.anthropic.claude-3-5-sonnet-20241022-v2:0", fx["response"],
    )
    assert event["model"] == "us.anthropic.claude-3-5-sonnet-20241022-v2:0"
    assert event["tokens_cache_write"] == 1000
    assert event["cost"] == pytest.approx(fx["expected_cost_usd"], rel=0.02)
