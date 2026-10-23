"""Prompt Shields SDK — discover, classify, and govern AI usage in your codebase.

Public surface:
    Sync clients:
        ShieldsClient        — generic; pass vendor="openai" | "anthropic"
        ShieldsOpenAI        — typed convenience for OpenAI
        ShieldsAnthropic     — typed convenience for Anthropic

    Async clients:
        AsyncShieldsClient   — generic
        AsyncShieldsOpenAI   — typed convenience
        AsyncShieldsAnthropic — typed convenience

    Routing (opt-in; the clients above never pick a model):
        ShieldsRouter        — picks the model per call under a route policy
        AsyncShieldsRouter   — async variant
        PolicyViolation      — raised when no allowed model is left (fail closed)

    Types:
        PSMetadata           — per-request metadata TypedDict
        PSConfig             — client config TypedDict

    Auto-instrumentation:
        instrument / uninstrument — patch installed openai / anthropic SDKs
        prompt-shields-instrument — console script: instrument an unmodified app

    Framework integrations (optional imports, see prompt_shields.integrations):
        prompt_shields.integrations.langchain  — LangChain callback handler
        prompt_shields.integrations.litellm    — LiteLLM custom logger
        prompt_shields.integrations.llamaindex — LlamaIndex instrumentation handlers

    Utilities:
        detect_pii_categories — pattern-based PII detection
        estimate_cost         — token-to-USD cost estimator
        estimate_unit_cost    — per-unit (page, character, second, image) cost
"""

from prompt_shields.async_client import (
    AsyncShieldsAnthropic,
    AsyncShieldsClient,
    AsyncShieldsOpenAI,
)
from prompt_shields.client import (
    ShieldsAnthropic,
    ShieldsClient,
    ShieldsOpenAI,
)
from prompt_shields.instrumentation import (
    instrument,
    suppress_instrumentation,
    uninstrument,
)
from prompt_shields.pii import detect_pii_categories, scan_messages
from prompt_shields.pricing import estimate_cost, estimate_unit_cost
from prompt_shields.route_policy import PolicyViolation
from prompt_shields.router import (
    AsyncShieldsRouter,
    RoutedChatCompletion,
    RouterError,
    ShieldsRouter,
)
from prompt_shields.types import (
    DataClassification,
    DiscoverySource,
    PSConfig,
    PSMetadata,
    Quality,
    RouteHint,
    Vendor,
)

__version__ = "0.2.0"

__all__ = [
    "AsyncShieldsAnthropic",
    "AsyncShieldsClient",
    "AsyncShieldsOpenAI",
    "AsyncShieldsRouter",
    "DataClassification",
    "DiscoverySource",
    "PSConfig",
    "PSMetadata",
    "PolicyViolation",
    "Quality",
    "RouteHint",
    "RoutedChatCompletion",
    "RouterError",
    "ShieldsAnthropic",
    "ShieldsClient",
    "ShieldsOpenAI",
    "ShieldsRouter",
    "Vendor",
    "detect_pii_categories",
    "estimate_cost",
    "estimate_unit_cost",
    "instrument",
    "scan_messages",
    "suppress_instrumentation",
    "uninstrument",
]
