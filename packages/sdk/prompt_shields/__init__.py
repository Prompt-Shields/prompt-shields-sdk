"""Prompt Shields SDK — discover, classify, and govern AI usage in your codebase.

Public surface:
    Sync clients:
        ShieldsClient        — generic; pass vendor="openai" | "anthropic" |
                               "google-vertex-ai" | "aws-bedrock"
        ShieldsOpenAI        — typed convenience for OpenAI
        ShieldsAnthropic     — typed convenience for Anthropic
        ShieldsAnthropicVertex / ShieldsAnthropicBedrock — Claude on
                               Vertex AI / Amazon Bedrock

    Async clients:
        AsyncShieldsClient   — generic
        AsyncShieldsOpenAI   — typed convenience
        AsyncShieldsAnthropic — typed convenience
        AsyncShieldsAnthropicVertex / AsyncShieldsAnthropicBedrock

    Routing (opt-in; the clients above never pick a model):
        ShieldsRouter        — picks the model per call under a route policy
        AsyncShieldsRouter   — async variant
        PolicyViolation      — raised when no allowed model is left (fail closed)

    Spend budgets (budget.v1; ShieldsRouter and the clients take budgets=):
        BudgetEnforcer       — checks budgets before a call, records cost after
        InProcessBudgetStore — default store; per-process counters
        RedisBudgetStore     — shared counters (pip install 'prompt-shields[optimizer-redis]')
        BudgetExceeded       — raised when a block budget is exhausted
        BudgetThreshold      — passed to on_threshold when a budget crosses warn / its limit

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

    OTLP export (#386; see prompt_shields.otlp):
        configure_otlp       — also send every call as a GenAI span to an OTLP backend
        configure_langfuse   — the same, preset for Langfuse's OTel endpoint
        otlp_stats / shutdown_otlp

    Feedback (#43):
        send_feedback / send_feedback_async — feedback on one call by its call_id
        last_call_id         — call_id of the latest call in this thread or task
        FEEDBACK_REASONS, FeedbackError

    Utilities:
        detect_pii_categories — pattern-based PII detection
        detect_pii_categories_extended — opt-in: also names, addresses and
                                multilingual health/financial vocabulary
        estimate_cost         — token-to-USD cost estimator
        estimate_unit_cost    — per-unit (page, character, second, image) cost
"""

from prompt_shields.async_client import (
    AsyncShieldsAnthropic,
    AsyncShieldsAnthropicBedrock,
    AsyncShieldsAnthropicVertex,
    AsyncShieldsClient,
    AsyncShieldsOpenAI,
)
from prompt_shields.baseline import (
    Baseline,
    BaselineError,
    freeze_baseline,
    load_baseline,
)
from prompt_shields.budgets import (
    BudgetEnforcer,
    BudgetError,
    BudgetExceeded,
    BudgetStore,
    BudgetThreshold,
    InProcessBudgetStore,
    RedisBudgetStore,
)
from prompt_shields.client import (
    ShieldsAnthropic,
    ShieldsAnthropicBedrock,
    ShieldsAnthropicVertex,
    ShieldsClient,
    ShieldsOpenAI,
)
from prompt_shields.feedback import (
    FEEDBACK_REASONS,
    FeedbackError,
    last_call_id,
    send_feedback,
    send_feedback_async,
)
from prompt_shields.instrumentation import (
    instrument,
    suppress_instrumentation,
    uninstrument,
)
from prompt_shields.otlp import (
    configure_langfuse,
    configure_otlp,
    otlp_stats,
    shutdown_otlp,
)
from prompt_shields.pii import detect_pii_categories, scan_messages
from prompt_shields.pii_extended import detect_pii_categories_extended
from prompt_shields.pricing import estimate_cost, estimate_unit_cost
from prompt_shields.pseudonymise import Pseudonymisation, pseudonymise
from prompt_shields.route_policy import PolicyViolation
from prompt_shields.router import (
    AsyncRoutedStream,
    AsyncShieldsRouter,
    RoutedChatCompletion,
    RoutedStream,
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

__version__ = "0.5.0"

__all__ = [
    "FEEDBACK_REASONS",
    "AsyncRoutedStream",
    "AsyncShieldsAnthropic",
    "AsyncShieldsAnthropicBedrock",
    "AsyncShieldsAnthropicVertex",
    "AsyncShieldsClient",
    "AsyncShieldsOpenAI",
    "AsyncShieldsRouter",
    "Baseline",
    "BaselineError",
    "BudgetEnforcer",
    "BudgetError",
    "BudgetExceeded",
    "BudgetStore",
    "BudgetThreshold",
    "DataClassification",
    "DiscoverySource",
    "FeedbackError",
    "InProcessBudgetStore",
    "PSConfig",
    "PSMetadata",
    "PolicyViolation",
    "Pseudonymisation",
    "Quality",
    "RedisBudgetStore",
    "RouteHint",
    "RoutedChatCompletion",
    "RoutedStream",
    "RouterError",
    "ShieldsAnthropic",
    "ShieldsAnthropicBedrock",
    "ShieldsAnthropicVertex",
    "ShieldsClient",
    "ShieldsOpenAI",
    "ShieldsRouter",
    "Vendor",
    "configure_langfuse",
    "configure_otlp",
    "detect_pii_categories",
    "detect_pii_categories_extended",
    "estimate_cost",
    "estimate_unit_cost",
    "freeze_baseline",
    "instrument",
    "last_call_id",
    "load_baseline",
    "otlp_stats",
    "pseudonymise",
    "scan_messages",
    "send_feedback",
    "send_feedback_async",
    "shutdown_otlp",
    "suppress_instrumentation",
    "uninstrument",
]
