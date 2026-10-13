"""Provider adapters — translate vendor-specific response shapes into a uniform
event metadata structure.

Each provider class wraps the upstream SDK and exposes a single `extract(response)`
method that returns a dict of fields the collector understands. New providers
add ~20 lines.
"""

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


class ProviderAdapter:
    """Base class — subclasses MUST set `vendor` and implement `extract`."""

    vendor: str = ""

    def extract(self, response: Any) -> dict:
        """Return event-shaped fields parsed from a provider response."""
        raise NotImplementedError


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
    """Gemini `generateContent` responses (google-genai SDK or raw JSON).

    Not registered in `ADAPTERS`: the wrapped clients have no Gemini upstream
    yet. Usable directly by integrations that observe Gemini responses.
    """

    vendor = "google"

    def extract(self, response: Any) -> dict:
        usage = _field(response, "usage_metadata")
        if usage is None:
            usage = _field(response, "usageMetadata")
        tool_calls: list[dict] = []
        for candidate in _field(response, "candidates") or []:
            content = _field(candidate, "content")
            for part in _field(content, "parts") or []:
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


# Registry — keyed by vendor name
ADAPTERS: dict[str, ProviderAdapter] = {
    "openai": OpenAIAdapter(),
    "anthropic": AnthropicAdapter(),
}


def get_adapter(vendor: str) -> ProviderAdapter:
    """Return the adapter for `vendor`, or raise ValueError if unknown."""
    adapter = ADAPTERS.get(vendor)
    if adapter is None:
        raise ValueError(
            f"Unsupported vendor: {vendor!r}. Available: {sorted(ADAPTERS)}"
        )
    return adapter
