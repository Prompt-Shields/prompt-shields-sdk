"""Token estimates and stream usage sources (#389).

The vectors in ``schemas/stream-usage/vectors/`` are shared with the
gateway (gateway/src/middlewares/psUsageEstimate.test.ts): both
implementations must return ``expected`` exactly.
"""

import json
import random
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from prompt_shields.streaming import (
    STREAM_USAGE_HOSTS,
    StreamAccumulator,
    upstream_accepts_stream_options,
)
from prompt_shields.usage_estimate import (
    TokenCounter,
    estimate_messages_tokens,
    estimate_text_tokens,
    token_family,
)

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "schemas" / "stream-usage"
VECTORS = json.loads((SCHEMA_DIR / "vectors" / "estimates.json").read_text())["vectors"]


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_shared_vectors(vector):
    assert estimate_text_tokens(vector["text"], vector["family"]) == vector["expected"]


@pytest.mark.parametrize("family", ["o200k", "cl100k"])
def test_within_ten_percent_of_the_reference_tokenizer(family):
    ref = [v for v in VECTORS if v["family"] == family and "reference" in v]
    assert len(ref) >= 20
    est = sum(v["expected"] for v in ref)
    real = sum(v["reference"]["tokens"] for v in ref)
    assert abs(est - real) / real <= 0.10
    for v in ref:
        assert abs(v["expected"] - v["reference"]["tokens"]) / v["reference"]["tokens"] <= 0.30


def test_counter_pieces_add_up_to_the_whole_text():
    text = " ".join(v["text"] for v in VECTORS if "reference" in v) * 3
    counter = TokenCounter()
    rng = random.Random(7)
    i = 0
    while i < len(text):
        n = rng.randint(1, 9)
        counter.feed(text[i:i + n])
        i += n
    for family in ("o200k", "cl100k"):
        assert counter.tokens(family) == estimate_text_tokens(text, family)


@pytest.mark.parametrize("model,vendor,family", [
    ("gpt-4o-2024-08-06", None, "o200k"),
    ("openai/gpt-5-mini", None, "o200k"),
    ("o3-mini", None, "o200k"),
    ("gemini-2.5-flash", None, "o200k"),
    ("gpt-4-turbo", None, "cl100k"),
    ("claude-sonnet-4-20250514", "anthropic", "cl100k"),
    (None, None, "cl100k"),
])
def test_token_family(model, vendor, family):
    assert token_family(model, vendor) == family


def test_messages_estimate_counts_framing():
    hi = estimate_text_tokens("Say hi to the team", "o200k")
    assert estimate_messages_tokens(
        [{"role": "user", "content": "Say hi to the team"}], "o200k") == hi + 6
    assert estimate_messages_tokens([], "o200k") == 0


def test_sdk_hosts_match_the_shared_allowlist():
    doc = json.loads((SCHEMA_DIR / "include-usage.v1.json").read_text())
    assert sorted(STREAM_USAGE_HOSTS) == sorted(doc["sdk_hosts"])


@pytest.mark.parametrize("base_url,version,expected", [
    ("https://api.openai.com/v1/", None, True),
    ("https://api.groq.com/openai/v1", None, True),
    ("https://api.together.xyz/v1", None, True),
    ("https://api.fireworks.ai/inference/v1", None, True),
    ("https://res.openai.azure.com/openai/", "2024-10-21", True),
    ("https://res.openai.azure.com/openai/", "2024-02-01", False),
    ("https://res.openai.azure.com/openai/", None, False),
    ("https://vllm.internal/v1", None, False),
    ("https://evil-api.openai.com.example/v1", None, False),
])
def test_upstream_accepts_stream_options(base_url, version, expected, monkeypatch):
    monkeypatch.delenv("PS_STREAM_INCLUDE_USAGE", raising=False)
    monkeypatch.delenv("PS_STREAM_INCLUDE_USAGE_HOSTS", raising=False)
    upstream = NS(base_url=base_url, _api_version=version)
    assert upstream_accepts_stream_options(upstream) is expected


def test_stream_usage_env(monkeypatch):
    vllm = NS(base_url="https://vllm.internal/v1")
    monkeypatch.setenv("PS_STREAM_INCLUDE_USAGE_HOSTS", "vllm.internal")
    assert upstream_accepts_stream_options(vllm) is True
    monkeypatch.setenv("PS_STREAM_INCLUDE_USAGE", "false")
    assert upstream_accepts_stream_options(vllm) is False
    assert upstream_accepts_stream_options(NS(base_url="https://api.openai.com/v1")) is False


MESSAGES = [{"role": "user", "content": "Write a short greeting for the team"}]


def _oa_chunk(content=None, usage=None, finish=None):
    choices = [] if content is None and finish is None else [
        NS(index=0, finish_reason=finish, delta=NS(content=content, tool_calls=None))]
    return NS(model="gpt-4o-2024-08-06", service_tier=None, usage=usage, choices=choices)


def test_provider_usage_is_marked_provider():
    acc = StreamAccumulator("openai", MESSAGES, "gpt-4o")
    acc.observe(_oa_chunk("Hello team!"))
    acc.observe(_oa_chunk(finish="stop"))
    acc.observe(_oa_chunk(usage=NS(prompt_tokens=12, completion_tokens=4,
                                   prompt_tokens_details=None,
                                   completion_tokens_details=None)))
    fields = acc.adapter_fields()
    assert (fields["tokens_in"], fields["tokens_out"]) == (12, 4)
    assert fields["usage_source"] == "provider"
    assert "tokens_estimated" not in fields


def test_missing_usage_is_estimated_with_the_tokenizer_estimate():
    acc = StreamAccumulator("openai", MESSAGES, "gpt-4o")
    acc.observe(_oa_chunk("Hello team, have a great week!"))
    fields = acc.adapter_fields(cut=True)
    assert fields["usage_source"] == "estimated"
    assert fields["tokens_estimated"] is True
    assert fields["tokens_in"] == estimate_messages_tokens(MESSAGES, "o200k")
    assert fields["tokens_out"] == estimate_text_tokens(
        "Hello team, have a great week!", "o200k")


def _anthropic(etype, **kw):
    return NS(type=etype, **kw)


def test_cut_anthropic_stream_tops_up_the_placeholder_output_count():
    acc = StreamAccumulator("anthropic", MESSAGES, "claude-sonnet-4")
    acc.observe(_anthropic("message_start", message=NS(
        model="claude-sonnet-4", usage=NS(input_tokens=20, output_tokens=1))))
    text = "Hello team, here is a longer greeting that was cut off midway"
    acc.observe(_anthropic("content_block_delta", delta=NS(text=text)))
    fields = acc.adapter_fields(cut=True)
    assert fields["tokens_in"] == 20
    assert fields["tokens_out"] == estimate_text_tokens(text, "cl100k") > 1
    assert fields["usage_source"] == "estimated"


def test_completed_anthropic_stream_keeps_the_provider_count():
    acc = StreamAccumulator("anthropic", MESSAGES, "claude-sonnet-4")
    acc.observe(_anthropic("message_start", message=NS(
        model="claude-sonnet-4", usage=NS(input_tokens=20, output_tokens=1))))
    acc.observe(_anthropic("content_block_delta", delta=NS(text="Hello team")))
    acc.observe(_anthropic("message_delta", delta=NS(stop_reason="end_turn"),
                           usage=NS(output_tokens=3)))
    for cut in (False, True):
        fields = acc.adapter_fields(cut=cut)
        assert (fields["tokens_in"], fields["tokens_out"]) == (20, 3)
        assert fields["usage_source"] == "provider"
