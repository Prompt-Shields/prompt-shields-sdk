"""Provider adapters — translate vendor-specific response shapes into a uniform
event metadata structure.

Each provider class wraps the upstream SDK and exposes a single `extract(response)`
method that returns a dict of fields the collector understands. New providers
add ~20 lines.
"""

from typing import Any


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
        tokens_in = getattr(usage, "prompt_tokens", None) if usage else None
        tokens_out = getattr(usage, "completion_tokens", None) if usage else None

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
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tool_calls_used": tool_calls or None,
        }


class AnthropicAdapter(ProviderAdapter):
    vendor = "anthropic"

    def extract(self, response: Any) -> dict:
        usage = getattr(response, "usage", None)
        tokens_in = getattr(usage, "input_tokens", None) if usage else None
        tokens_out = getattr(usage, "output_tokens", None) if usage else None

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
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tool_calls_used": tool_calls or None,
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
