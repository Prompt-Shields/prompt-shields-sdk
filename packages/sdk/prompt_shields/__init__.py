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
from prompt_shields.pii import detect_pii_categories, scan_messages
from prompt_shields.pricing import estimate_cost
from prompt_shields.types import (
    DataClassification,
    DiscoverySource,
    PSConfig,
    PSMetadata,
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
    "DataClassification",
    "DiscoverySource",
    "Vendor",
    # Utilities
    "detect_pii_categories",
    "scan_messages",
    "estimate_cost",
]
