"""Shared helpers for framework integrations. Imports no framework."""

import asyncio
import logging
from typing import Any

from prompt_shields import instrumentation
from prompt_shields.instrumentation import FrameworkCall, apply_framework_fields
from prompt_shields.telemetry import build_atlas_event

logger = logging.getLogger("prompt_shields.integrations")

# Framework provider names -> SDK vendor names (the keys of
# `providers.ADAPTERS` and the pricing table, as `ShieldsClient` reports them).
_VENDORS = {
    "openai": "openai",
    "text-completion-openai": "openai",
    "anthropic": "anthropic",
    "azure": "azure_openai",
    "azure_text": "azure_openai",
    "azure_openai": "azure_openai",
    "bedrock": "bedrock",
    "bedrock_converse": "bedrock",
    "amazon_bedrock": "bedrock",
    "google": "google",
    "google_genai": "google",
    "google_vertexai": "google",
    "gemini": "google",
    "vertex_ai": "google",
    "vertex_ai_beta": "google",
    "cohere": "cohere",
    "cohere_chat": "cohere",
    "mistral": "mistral",
    "mistralai": "mistral",
}

# Strong references for fire-and-forget flush tasks.
_pending_flushes: set = set()


def normalize_vendor(provider: Any) -> str:
    if not isinstance(provider, str) or not provider:
        return "custom"
    key = provider.lower()
    if key.startswith("vertex_ai"):
        return "google"
    return _VENDORS.get(key, key)


def text_of(content: Any) -> str:
    """Plain text of a message content: a string or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return " ".join(parts)
    return ""


def claim_outer_call(framework: str) -> FrameworkCall | None:
    """Take over the unclaimed call an enclosing framework call announced.

    A framework called from another one's run (LiteLLM or LlamaIndex under
    LangChain, LangChain under LlamaIndex), or from a deferred stream of its
    own framework while it is iterated, reports the call itself: the outer
    call is marked captured so its integration emits nothing. Returns the
    outer call (for its use-case hint).
    """
    with instrumentation._claim_lock:
        outer = instrumentation._claimable_framework_call()
        if outer is None or (outer.framework == framework
                             and outer.frame is None):
            return None
        outer.captured = True
    return outer


def emit(fc: FrameworkCall, *, vendor: str, model: Any, messages: list,
         latency_ms: int, adapter_fields: dict,
         cost_fallback: Any = None) -> None:
    """Build and send one event for a call `instrument()` did not capture.

    `cost_fallback` is the framework's own cost, used (with `cost_source`
    set to the framework name) only when the pricing table has no price.
    """
    state = instrumentation._state
    if state is None or fc.captured:
        return
    event = instrumentation.build_event(
        state, vendor=vendor, model=model, messages=messages,
        latency_ms=latency_ms, adapter_fields=adapter_fields,
    )
    if event.get("cost") is None and isinstance(cost_fallback, (int, float)) \
            and not isinstance(cost_fallback, bool) and cost_fallback > 0:
        event["cost"] = float(cost_fallback)
        event["cost_source"] = fc.framework
    apply_framework_fields(event, fc)
    state.telemetry.enqueue(event)
    if state.atlas is not None:
        state.atlas.enqueue(build_atlas_event(event, messages))
    _flush(state)


def _flush(state: Any) -> None:
    # Inside an event loop (async callbacks run inline), never block it.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    senders = [state.telemetry] + ([state.atlas] if state.atlas else [])
    for sender in senders:
        if loop is None:
            sender.flush_sync()
        else:
            task = loop.create_task(sender.flush())
            _pending_flushes.add(task)
            task.add_done_callback(_pending_flushes.discard)
