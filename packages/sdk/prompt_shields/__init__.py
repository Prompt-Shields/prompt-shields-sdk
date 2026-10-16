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
from prompt_shields.pricing import estimate_cost
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
    # Sync clients
    "ShieldsClient",
    "ShieldsOpenAI",
    "ShieldsAnthropic",
    # Async clients
    "AsyncShieldsClient",
    "AsyncShieldsOpenAI",
    "AsyncShieldsAnthropic",
    # Types
    "PSMetadata",
    "PSConfig",
    "RouteHint",
    "Quality",
    "DataClassification",
    "DiscoverySource",
    "Vendor",
    # Auto-instrumentation
    "instrument",
    "uninstrument",
    "suppress_instrumentation",
    # Utilities
    "detect_pii_categories",
    "scan_messages",
    "estimate_cost",
]
