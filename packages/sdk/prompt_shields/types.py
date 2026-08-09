from dataclasses import dataclass
from typing import TypedDict, NotRequired, Literal


# Requested quality intent — an *intent*, never a model name. The gateway
# maps intent to a concrete model group; the SDK only expresses the hint.
Quality = Literal["draft", "balanced", "critical"]


@dataclass
class RouteHint:
    """Explicit cost/quality hints for a single LLM call.

    The SDK never routes — it only emits these as ``X-PS-*`` headers that the
    gateway acts on. All fields are optional; an empty hint yields no headers
    and preserves today's transparent, gateway-default behavior.

    Precedence at the gateway:
        model_group (explicit override)  >  quality / max_cost (hints)
        >  gateway default policy (transparent).
    """

    quality: Quality | None = None      # intent → gateway maps to a model group
    max_cost: float | None = None       # per-call USD ceiling
    model_group: str | None = None      # explicit override → bypasses the router
    allow_cache: bool = True            # opt a creative/non-deterministic call out of cache

    def to_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.quality is not None:
            headers["X-PS-Quality"] = self.quality
        if self.max_cost is not None:
            headers["X-PS-Max-Cost"] = str(self.max_cost)
        if self.model_group is not None:
            headers["X-PS-Route"] = self.model_group
        if not self.allow_cache:
            headers["X-PS-Cache"] = "off"
        return headers


# Per-request metadata users can attach to a single LLM call
class PSMetadata(TypedDict, total=False):
    data_sources: list[str]
    output_destination: str
    risk_tags: list[str]
    session_id: str
    user_id: str  # opaque identifier (hashed by client before send)


# Client-level config (set once on construction)
class PSConfig(TypedDict):
    ps_api_key: str
    ps_collector_url: str
    business_unit: NotRequired[str]
    use_case: NotRequired[str]
    owner: NotRequired[str]
    data_classification: NotRequired[str]
    environment: NotRequired[str]
    calling_service: NotRequired[str]


# Discovery sources recognized by the collector
DiscoverySource = Literal[
    "sdk",
    "gateway",
    "browser_extension",
    "macos_app",
    "platform_signal",
    "survey",
]


# Data classification levels (highest wins on conflict resolution)
DataClassification = Literal["public", "internal", "confidential", "restricted"]


# Recognized AI vendors (extend as needed; "custom" for self-hosted/other)
Vendor = Literal[
    "openai",
    "anthropic",
    "google",
    "microsoft",
    "meta",
    "cohere",
    "mistral",
    "custom",
]
