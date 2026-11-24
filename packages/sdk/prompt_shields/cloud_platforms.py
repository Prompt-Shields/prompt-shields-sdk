"""Claude on Google Vertex AI and Amazon Bedrock (#388).

`detect(client)` recognises the Anthropic SDK's cloud platform clients
(`AnthropicVertex`, `AnthropicBedrock`, `AnthropicBedrockMantle` and their
async twins) by class, as `instrumentation._is_azure` recognises Azure
OpenAI clients, without importing the Anthropic SDK. `for_boto3(client)`
describes a boto3 `bedrock-runtime` client the same way.

A `CloudPlatform` gives the call's event:

- `vendor`: "google-vertex-ai" or "aws-bedrock" for the Anthropic SDK
  clients (boto3 calls keep their "bedrock" vendor);
- `model_family`: the model maker ("anthropic" for Claude);
- `region`, and `served_jurisdiction` derived from it (a Bedrock
  geographic inference profile, `eu.` / `us.` / ..., wins over the client
  region, since the profile decides where the call may run; `global` and
  unknown regions give no jurisdiction);
- `cloud_project` (the Vertex project) and `cloud_account` (the AWS account
  of a Bedrock inference profile ARN), when known;
- `endpoint_scope`, the pricing input: "global" for the Vertex `global`
  region and Bedrock `global.` profiles, else "regional"
  (pricing.REGIONAL_ENDPOINT_PREMIUM).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from prompt_shields.pricing import normalise_claude_model

logger = logging.getLogger("prompt_shields.cloud_platforms")

VERTEX_VENDOR = "google-vertex-ai"
BEDROCK_VENDOR = "aws-bedrock"
# boto3 `bedrock-runtime` calls (instrumentation's "bedrock" vendor).
BOTO3_BEDROCK_VENDOR = "bedrock"
# The Anthropic SDK cloud platform vendors (Anthropic Messages responses).
CLAUDE_PLATFORM_VENDORS = (VERTEX_VENDOR, BEDROCK_VENDOR)

# (module prefix, base class) of the Anthropic SDK's platform clients.
_CLIENT_BASES = {
    ("anthropic.lib.vertex", "BaseVertexClient"): VERTEX_VENDOR,
    ("anthropic.lib.bedrock", "BaseBedrockClient"): BEDROCK_VENDOR,
    ("anthropic.lib.bedrock", "BaseMantleClient"): BEDROCK_VENDOR,
}

# Bedrock cross-region inference profile prefix -> jurisdiction. `global.`
# and `apac.` may run in several jurisdictions: none.
_BEDROCK_GEO = {"us": "US", "us-gov": "US", "eu": "EEA", "jp": "JP",
                "au": "AU", "ca": "CA", "in": "IN"}
_BEDROCK_PREFIX = re.compile(r"^(us-gov|us|eu|apac|au|ca|jp|in|global)\.")
_ARN = re.compile(r"^arn:aws[a-z-]*:bedrock:([a-z0-9-]*):(\d{12}):")

# Cloud region -> jurisdiction, in the catalog's terms ("EEA", "US", "UK",
# "CH", ... or a country code). Exact names first, then prefixes; regions
# not listed give none.
_REGION_EXACT = {
    # Google Cloud
    "us": "US", "eu": "EEA",
    "europe-west2": "UK", "europe-west6": "CH",
    "asia-southeast1": "SG", "asia-northeast1": "JP", "asia-northeast2": "JP",
    "asia-northeast3": "KR", "asia-south1": "IN", "asia-south2": "IN",
    "me-central2": "SA",
    # AWS
    "eu-west-2": "UK", "eu-central-2": "CH",
    "ap-southeast-1": "SG", "ap-southeast-2": "AU", "ap-southeast-4": "AU",
    "ap-northeast-1": "JP", "ap-northeast-3": "JP", "ap-northeast-2": "KR",
    "ap-south-1": "IN", "ap-south-2": "IN",
}
_REGION_PREFIX = (
    ("us-", "US"),
    ("europe-", "EEA"),
    ("eu-", "EEA"),
    ("northamerica-northeast", "CA"),
    ("ca-", "CA"),
    ("australia-", "AU"),
)


def region_jurisdiction(region: Any) -> str | None:
    """The jurisdiction of a Google Cloud or AWS region, or None."""
    if not isinstance(region, str) or not region:
        return None
    region = region.lower()
    if region in _REGION_EXACT:
        return _REGION_EXACT[region]
    for prefix, jurisdiction in _REGION_PREFIX:
        if region.startswith(prefix):
            return jurisdiction
    return None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _profile_id(model: str) -> str:
    """A Bedrock model id or inference profile id from a model or ARN."""
    return model.rsplit("/", 1)[-1] if model.startswith("arn:") else model


class CloudPlatform:
    """Where a call ran: one cloud platform client (see the module doc)."""

    __slots__ = ("project", "region", "vendor")

    def __init__(self, vendor: str, region: str | None = None,
                 project: str | None = None):
        self.vendor = vendor
        self.region = region
        self.project = project

    def endpoint_scope(self, model: Any) -> str | None:
        """"global" or "regional" for pricing, or None when unknown."""
        if self.vendor == VERTEX_VENDOR:
            if self.region is None:
                return None
            return "global" if self.region.lower() == "global" else "regional"
        if not isinstance(model, str) or not model:
            return None
        prefix = _BEDROCK_PREFIX.match(_profile_id(model))
        return "global" if prefix and prefix.group(1) == "global" else "regional"

    def model_family(self, model: Any) -> str | None:
        if normalise_claude_model(model) is not None:
            return "anthropic"
        if self.vendor in CLAUDE_PLATFORM_VENDORS:
            return "anthropic"
        if isinstance(model, str):
            name = _BEDROCK_PREFIX.sub("", _profile_id(model))
            maker, sep, _ = name.partition(".")
            if sep and maker:
                return maker
        return None

    def fields(self, model: Any) -> dict:
        """Event fields for a call to `model` (see the module doc)."""
        out: dict[str, Any] = {}
        family = self.model_family(model)
        if family:
            out["model_family"] = family
        region = self.region
        # A Bedrock inference profile's jurisdiction ("" for global. and
        # apac.: the client region does not say where those run).
        profile_jurisdiction = None
        if isinstance(model, str) and self.vendor != VERTEX_VENDOR:
            arn = _ARN.match(model)
            if arn:
                region = region or _str(arn.group(1))
                out["cloud_account"] = arn.group(2)
            prefix = _BEDROCK_PREFIX.match(_profile_id(model))
            if prefix:
                profile_jurisdiction = _BEDROCK_GEO.get(prefix.group(1), "")
        if region:
            out["region"] = region
        jurisdiction = (
            profile_jurisdiction if profile_jurisdiction is not None
            else region_jurisdiction(region)
        )
        if jurisdiction:
            out["served_jurisdiction"] = jurisdiction
        if self.project:
            out["cloud_project"] = self.project
        return out


def detect(client: Any) -> CloudPlatform | None:
    """The cloud platform of an Anthropic SDK client, or None for the
    first-party client (and anything else)."""
    if client is None:
        return None
    try:
        for cls in type(client).__mro__:
            for (module, name), vendor in _CLIENT_BASES.items():
                if cls.__name__ == name and cls.__module__.startswith(module):
                    if vendor == VERTEX_VENDOR:
                        return CloudPlatform(
                            vendor,
                            _str(getattr(client, "region", None)),
                            _str(getattr(client, "project_id", None)),
                        )
                    return CloudPlatform(
                        vendor, _str(getattr(client, "aws_region", None))
                    )
    except Exception as e:  # noqa: BLE001 - fail-open
        logger.debug(f"cloud platform detection error: {e}")
    return None


def for_boto3(client: Any) -> CloudPlatform:
    """A boto3 `bedrock-runtime` client (vendor "bedrock")."""
    region = None
    try:
        region = _str(client.meta.region_name)
    except Exception:  # noqa: BLE001, S110 - no region
        pass
    return CloudPlatform(BOTO3_BEDROCK_VENDOR, region)


_warned_unpriced: set = set()


def flag_unpriced(event: dict, vendor: str, model: Any) -> None:
    """Mark a token-counted call to a Claude platform model with no price:
    `model_unpriced` on the event (its cost stays None, never 0) and one
    warning per (vendor, model)."""
    if event.get("cost") is not None or event.get("tokens_in") is None:
        return
    event["model_unpriced"] = True
    key = (vendor, model if isinstance(model, str) else None)
    if key not in _warned_unpriced and len(_warned_unpriced) < 256:
        _warned_unpriced.add(key)
        logger.warning(
            f"Prompt Shields has no price for {vendor} model {key[1]!r}; "
            "its calls are reported without a cost. Pass a pricing_table to "
            "price it."
        )


def make_client(vendor: str, api_key: str | None, *, is_async: bool,
                **kwargs: Any) -> Any:
    """An Anthropic SDK client for a Claude platform vendor, for
    `ShieldsClient` / `AsyncShieldsClient`: `AnthropicVertex` (pass
    `project_id=` and `region=`; credentials come from Google ADC) or
    `AnthropicBedrock` (`aws_region=`, AWS credentials or a Bedrock API key
    as `api_key`). Needs `anthropic[vertex]` / `anthropic[bedrock]`."""
    if vendor == VERTEX_VENDOR:
        from anthropic import AnthropicVertex, AsyncAnthropicVertex

        # Vertex authenticates with Google credentials, never an API key.
        cls = AsyncAnthropicVertex if is_async else AnthropicVertex
        return cls(**kwargs)
    if vendor == BEDROCK_VENDOR:
        from anthropic import AnthropicBedrock, AsyncAnthropicBedrock

        cls = AsyncAnthropicBedrock if is_async else AnthropicBedrock
        if api_key is not None:
            kwargs["api_key"] = api_key
        return cls(**kwargs)
    raise ValueError(f"Not a Claude platform vendor: {vendor!r}")


def stream_vendor(vendor: str) -> str:
    """The vendor whose stream chunks `vendor` sends (StreamAccumulator)."""
    return "anthropic" if vendor in CLAUDE_PLATFORM_VENDORS else vendor


def apply_platform(event: dict, platform: CloudPlatform | None,
                   model: Any) -> None:
    """Add the platform's fields to a built event (an existing value, such
    as a router's served_jurisdiction, wins) and flag an unpriced Claude
    model."""
    if platform is None:
        return
    for key, value in platform.fields(model).items():
        event.setdefault(key, value)
    if event.get("model_family") == "anthropic":
        flag_unpriced(event, event.get("vendor") or platform.vendor, model)


def with_endpoint_scope(adapter_fields: dict, platform: CloudPlatform | None,
                        model: Any) -> dict:
    """`adapter_fields` with the call's `endpoint_scope` (a pricing input)."""
    if platform is None or "endpoint_scope" in adapter_fields:
        return adapter_fields
    scope = platform.endpoint_scope(model)
    return {**adapter_fields, "endpoint_scope": scope} if scope else adapter_fields
