from typing import TypedDict, NotRequired, Literal


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
    atlas_url: NotRequired[str]
    atlas_api_key: NotRequired[str]
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
