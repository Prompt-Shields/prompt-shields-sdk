"""Provider adapters — translate vendor-specific response shapes into a uniform
event metadata structure.

Each provider class wraps the upstream SDK and exposes a single `extract(response)`
method that returns a dict of fields the collector understands. New providers
add ~20 lines.

Adapters only read response objects and never import a vendor SDK, so every
vendor package stays an optional install. Each accepts the SDK's response
model or the equivalent raw JSON dict.
"""

import re
from typing import Any


def _field(obj: Any, name: str) -> Any:
    """Read `name` from an SDK object or a plain dict (raw JSON responses)."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _int(value: Any) -> int | None:
    """Accept only real integer counts (not bools, mocks, or None)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def openai_usage_fields(usage: Any) -> dict:
    """Token counts from an OpenAI Chat Completions `usage` object.

    `prompt_tokens` already includes cached tokens and `completion_tokens`
    already includes reasoning tokens; the details are reported as subsets.
    """
    prompt_details = _field(usage, "prompt_tokens_details")
    completion_details = _field(usage, "completion_tokens_details")
    return {
        "tokens_in": _int(_field(usage, "prompt_tokens")),
        "tokens_out": _int(_field(usage, "completion_tokens")),
        "tokens_cached": _int(_field(prompt_details, "cached_tokens")),
        "tokens_cache_write": None,
        "tokens_reasoning": _int(_field(completion_details, "reasoning_tokens")),
    }


def anthropic_usage_fields(
    input_tokens: int | None,
    output_tokens: int | None,
    cache_read: int | None,
    cache_write: int | None,
) -> dict:
    """Token counts from Anthropic Messages usage parts.

    Anthropic's `input_tokens` excludes cache reads and writes, so they are
    added back: `tokens_in` is the total prompt size, matching OpenAI (and
    the gateway's `inputTokensOf`). Thinking tokens are billed inside
    `output_tokens` and are not reported separately.
    """
    tokens_in = None
    if input_tokens is not None:
        tokens_in = input_tokens + (cache_read or 0) + (cache_write or 0)
    return {
        "tokens_in": tokens_in,
        "tokens_out": output_tokens,
        "tokens_cached": cache_read,
        "tokens_cache_write": cache_write,
        "tokens_reasoning": None,
    }


def gemini_usage_fields(usage_metadata: Any) -> dict:
    """Token counts from a Gemini `usageMetadata` / `usage_metadata` object.

    `promptTokenCount` includes `cachedContentTokenCount`. Thinking tokens
    (`thoughtsTokenCount`) are billed as output but reported outside
    `candidatesTokenCount`, so they are added to `tokens_out`.
    """

    def get(snake: str, camel: str) -> int | None:
        value = _int(_field(usage_metadata, snake))
        return value if value is not None else _int(_field(usage_metadata, camel))

    candidates = get("candidates_token_count", "candidatesTokenCount")
    thoughts = get("thoughts_token_count", "thoughtsTokenCount")
    tokens_out = None
    if candidates is not None or thoughts is not None:
        tokens_out = (candidates or 0) + (thoughts or 0)
    return {
        "tokens_in": get("prompt_token_count", "promptTokenCount"),
        "tokens_out": tokens_out,
        "tokens_cached": get("cached_content_token_count", "cachedContentTokenCount"),
        "tokens_cache_write": None,
        "tokens_reasoning": thoughts,
    }


def _count(value: Any) -> int | None:
    """An integer count, also from a digit string (HTTP header values) or an
    integral float (the Cohere SDK types its token counts as floats)."""
    if isinstance(value, str):
        value = value.strip()
        return int(value) if value.isascii() and value.isdigit() else None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return _int(value)


def _items(value: Any) -> list | tuple:
    """`value` if it is a list or tuple, else empty (unexpected shapes)."""
    return value if isinstance(value, (list, tuple)) else ()


def _openai_like_extract(response: Any) -> dict:
    """Usage and tool calls from a Chat Completions-shaped response.

    Used for APIs that copy that shape (Azure OpenAI, Mistral). Reads SDK
    objects and raw JSON dicts alike.
    """
    tool_calls: list[dict] = []
    for choice in _items(_field(response, "choices")):
        message = _field(choice, "message")
        for tc in _items(_field(message, "tool_calls")):
            fn = _field(tc, "function")
            if fn is not None:
                tool_calls.append({"name": _field(fn, "name"), "type": "function"})
    model = _field(response, "model")
    return {
        **openai_usage_fields(_field(response, "usage")),
        "tool_calls_used": tool_calls or None,
        "served_model": model if isinstance(model, str) else None,
    }


class ProviderAdapter:
    """Base class — subclasses MUST set `vendor` and implement `extract`."""

    vendor: str = ""

    def extract(self, response: Any) -> dict:
        """Return event-shaped fields parsed from a provider response."""
        raise NotImplementedError

    def pricing_model(self, model: str | None, served_model: str | None) -> str | None:
        """The model name to look up in the pricing table.

        Defaults to the requested model. Adapters override it where the
        requested name is not a priced model id (Azure deployment names,
        Bedrock cross-region inference profiles).
        """
        return model


class OpenAIAdapter(ProviderAdapter):
    vendor = "openai"

    def extract(self, response: Any) -> dict:
        usage = getattr(response, "usage", None)
        usage_fields = openai_usage_fields(usage)

        # Tool/function call extraction — OpenAI shape
        tool_calls: list[dict] = []
        choices = getattr(response, "choices", None) or []
        for choice in choices:
            msg = getattr(choice, "message", None)
            if msg is None:
                continue
            for tc in getattr(msg, "tool_calls", None) or []:
                fn = getattr(tc, "function", None)
                if fn is not None:
                    tool_calls.append({
                        "name": getattr(fn, "name", None),
                        "type": "function",
                    })

        return {
            **usage_fields,
            "tool_calls_used": tool_calls or None,
            "served_model": getattr(response, "model", None),
        }


class AnthropicAdapter(ProviderAdapter):
    vendor = "anthropic"

    def extract(self, response: Any) -> dict:
        usage = getattr(response, "usage", None)
        usage_fields = anthropic_usage_fields(
            _int(_field(usage, "input_tokens")),
            _int(_field(usage, "output_tokens")),
            _int(_field(usage, "cache_read_input_tokens")),
            _int(_field(usage, "cache_creation_input_tokens")),
        )

        # Tool use extraction — Anthropic shape (content blocks)
        tool_calls: list[dict] = []
        content = getattr(response, "content", None) or []
        for block in content:
            block_type = getattr(block, "type", None)
            if block_type == "tool_use":
                tool_calls.append({
                    "name": getattr(block, "name", None),
                    "type": "tool_use",
                })

        return {
            **usage_fields,
            "tool_calls_used": tool_calls or None,
            "served_model": getattr(response, "model", None),
        }


class GeminiAdapter(ProviderAdapter):
    """Gemini `generateContent` responses (google-genai SDK or raw JSON)."""

    vendor = "google"

    def extract(self, response: Any) -> dict:
        usage = _field(response, "usage_metadata")
        if usage is None:
            usage = _field(response, "usageMetadata")
        tool_calls: list[dict] = []
        for candidate in _items(_field(response, "candidates")):
            content = _field(candidate, "content")
            for part in _items(_field(content, "parts")):
                call = _field(part, "function_call") or _field(part, "functionCall")
                if call is not None:
                    tool_calls.append({
                        "name": _field(call, "name"),
                        "type": "function",
                    })
        served_model = _field(response, "model_version") or _field(
            response, "modelVersion"
        )
        return {
            **gemini_usage_fields(usage),
            "tool_calls_used": tool_calls or None,
            "served_model": served_model,
        }

    def pricing_model(self, model: str | None, served_model: str | None) -> str | None:
        # google-genai accepts "models/gemini-2.5-flash" and Vertex resource
        # names ".../publishers/google/models/gemini-2.5-flash".
        return model.rsplit("models/", 1)[-1] if model else model


# Azure echoes the underlying model with a version date, e.g.
# "gpt-4o-2024-08-06"; the pricing table keys the base name.
_DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")


class AzureOpenAIAdapter(ProviderAdapter):
    """Azure OpenAI chat completions (`openai.AzureOpenAI` or raw JSON).

    Azure calls name a *deployment*, not a model. `deployments` maps
    deployment names to model ids for pricing, e.g. `{"prod-chat": "gpt-4o"}`.
    An unmapped deployment is priced by the model Azure reports in the
    response, without its version date. Rates fall back to OpenAI list
    prices (`pricing.PRICING_VENDOR_FALLBACK`).
    """

    vendor = "azure_openai"

    def __init__(self, deployments: dict[str, str] | None = None):
        self.deployments = dict(deployments or {})

    def extract(self, response: Any) -> dict:
        return _openai_like_extract(response)

    def pricing_model(self, model: str | None, served_model: str | None) -> str | None:
        if model in self.deployments:
            return self.deployments[model]
        if served_model and served_model != model:
            return _DATE_SUFFIX.sub("", served_model)
        return model


class MistralAdapter(ProviderAdapter):
    """Mistral `chat.complete` responses (`mistralai` SDK or raw JSON).

    The response has the Chat Completions shape. Reasoning (Magistral)
    tokens are inside `completion_tokens` and not reported separately.
    """

    vendor = "mistral"

    def extract(self, response: Any) -> dict:
        return _openai_like_extract(response)


class CohereAdapter(ProviderAdapter):
    """Cohere chat responses: v2 (`ClientV2.chat`) and v1 (`Client.chat`).

    Counts come from `billed_units` (what Cohere charges), falling back to
    `tokens`. v2 nests them under `usage`, v1 under `meta`. Cohere reports
    no cached or reasoning tokens and no served model.
    """

    vendor = "cohere"

    def extract(self, response: Any) -> dict:
        usage = _field(response, "usage")
        if usage is None:
            usage = _field(response, "meta")
        billed = _field(usage, "billed_units")
        tokens = _field(usage, "tokens")

        def count(name: str) -> int | None:
            value = _count(_field(billed, name))
            return value if value is not None else _count(_field(tokens, name))

        message = _field(response, "message")
        calls = _field(message, "tool_calls")
        if calls is None:
            calls = _field(response, "tool_calls")  # v1
        tool_calls: list[dict] = []
        for tc in _items(calls):
            fn = _field(tc, "function")
            name = _field(fn, "name") if fn is not None else _field(tc, "name")
            tool_calls.append({"name": name, "type": "function"})

        return {
            "tokens_in": count("input_tokens"),
            "tokens_out": count("output_tokens"),
            "tokens_cached": None,
            "tokens_cache_write": None,
            "tokens_reasoning": None,
            "tool_calls_used": tool_calls or None,
            "served_model": None,
        }


# Cross-region inference profiles prefix the model id, e.g.
# "us.anthropic.claude-3-5-haiku-20241022-v1:0"; prices are the base model's.
_BEDROCK_PROFILE_PREFIX = re.compile(r"^(?:us|eu|apac|us-gov|global|jp|au|ca)\.")


class BedrockAdapter(ProviderAdapter):
    """Amazon Bedrock runtime responses from boto3 (`bedrock-runtime`).

    Accepts:
      - `converse` responses (`usage.inputTokens`, `cacheReadInputTokens`, ...);
      - `invoke_model` responses, read from the `x-amzn-bedrock-*-token-count`
        HTTP headers so the streaming `body` is never consumed;
      - an `invoke_model` body the caller already parsed: Anthropic Messages,
        Amazon Nova or Meta Llama shape.

    Bedrock input counts exclude cache reads and writes (Converse
    `totalTokens` = input + output + cache read + cache write), so they are
    added back: `tokens_in` is the total prompt size, as for other vendors.
    Converse does not name the model, so `served_model` is None unless an
    Anthropic body reports it.
    """

    vendor = "bedrock"

    def extract(self, response: Any) -> dict:
        usage = _field(response, "usage")
        served_model = None
        if usage is not None and _field(usage, "input_tokens") is not None:
            # Anthropic Messages body.
            counts = (
                _count(_field(usage, "input_tokens")),
                _count(_field(usage, "output_tokens")),
                _count(_field(usage, "cache_read_input_tokens")),
                _count(_field(usage, "cache_creation_input_tokens")),
            )
            model = _field(response, "model")
            served_model = model if isinstance(model, str) else None
        elif usage is not None:
            # Converse, or an Amazon Nova body (`...TokenCount` cache keys).
            counts = (
                _count(_field(usage, "inputTokens")),
                _count(_field(usage, "outputTokens")),
                _first_count(usage, "cacheReadInputTokens", "cacheReadInputTokenCount"),
                _first_count(usage, "cacheWriteInputTokens", "cacheWriteInputTokenCount"),
            )
        elif _field(response, "prompt_token_count") is not None:
            # Meta Llama body.
            counts = (
                _count(_field(response, "prompt_token_count")),
                _count(_field(response, "generation_token_count")),
                None,
                None,
            )
        else:
            counts = _bedrock_header_counts(response)

        tool_calls: list[dict] = []
        message = _field(_field(response, "output"), "message")
        for block in _items(_field(message, "content")):
            tool_use = _field(block, "toolUse")
            if tool_use is not None:
                tool_calls.append({"name": _field(tool_use, "name"), "type": "tool_use"})
        for block in _items(_field(response, "content")):  # Anthropic body
            if _field(block, "type") == "tool_use":
                tool_calls.append({"name": _field(block, "name"), "type": "tool_use"})

        return {
            **anthropic_usage_fields(*counts),
            "tool_calls_used": tool_calls or None,
            "served_model": served_model,
        }

    def pricing_model(self, model: str | None, served_model: str | None) -> str | None:
        return _BEDROCK_PROFILE_PREFIX.sub("", model) if model else model


def _first_count(obj: Any, *names: str) -> int | None:
    for name in names:
        value = _count(_field(obj, name))
        if value is not None:
            return value
    return None


def _bedrock_header_counts(response: Any) -> tuple:
    """Token counts from an `invoke_model` response's HTTP headers."""
    headers = _field(_field(response, "ResponseMetadata"), "HTTPHeaders")
    if not isinstance(headers, dict):
        headers = vars(headers) if hasattr(headers, "__dict__") else {}
    lowered = {str(k).lower(): v for k, v in headers.items()}

    def header(name: str) -> int | None:
        return _count(lowered.get(f"x-amzn-bedrock-{name}"))

    return (
        header("input-token-count"),
        header("output-token-count"),
        header("cache-read-input-token-count"),
        header("cache-write-input-token-count"),
    )


# Registry — keyed by vendor name
ADAPTERS: dict[str, ProviderAdapter] = {
    "openai": OpenAIAdapter(),
    "anthropic": AnthropicAdapter(),
    "google": GeminiAdapter(),
    "azure_openai": AzureOpenAIAdapter(),
    "bedrock": BedrockAdapter(),
    "mistral": MistralAdapter(),
    "cohere": CohereAdapter(),
}


def get_adapter(vendor: str) -> ProviderAdapter:
    """Return the adapter for `vendor`, or raise ValueError if unknown."""
    adapter = ADAPTERS.get(vendor)
    if adapter is None:
        raise ValueError(
            f"Unsupported vendor: {vendor!r}. Available: {sorted(ADAPTERS)}"
        )
    return adapter
