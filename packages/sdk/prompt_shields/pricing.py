"""Token-to-USD cost estimation per (vendor, model).

Pricing tiers are point-in-time snapshots and update over time. The SDK
ships sensible defaults; customers can override by passing a custom
`pricing_table` to ShieldsClient at construction.

Returns USD as a Decimal-compatible float. Returns None when the model is
unknown — the collector treats None cost as "unmetered" rather than zero.

Cached input and cache writes are priced separately when their counts are
given. `tokens_in` is the total prompt size *including* cached and
cache-write tokens (the SDK's provider adapters normalise to this), so only
the remainder is charged at the full input rate. Reasoning/thinking tokens
are already inside `tokens_out` and are charged at the output rate.

A pricing-table entry is `(input_per_1k, output_per_1k)` or, to override the
vendor defaults below, `(input_per_1k, output_per_1k, cached_input_per_1k)`,
`(..., cached_input_per_1k, cache_write_per_1k)` or
`(..., cache_write_per_1k, cache_write_1h_per_1k)`.

Further pricing dimensions (#71):
- `service_tier`: batch and flex traffic is discounted (`SERVICE_TIER_MULTIPLIER`).
  A tier with no known price (e.g. OpenAI `priority`, `scale`) returns None
  rather than a wrong standard-rate number.
- 1-hour cache writes (`tokens_cache_write_1h`, a subset of
  `tokens_cache_write`) at their own rate.
- Long-context bands (`LONG_CONTEXT_PRICING`): prompts above a threshold are
  priced at the band's rates. Applies to the default table only.
- Audio tokens (`tokens_audio_in` / `tokens_audio_out`, subsets of
  `tokens_in` / `tokens_out`) at `AUDIO_PRICING` rates. Audio tokens on a model
  with no audio rate return None.
- Per-request tool fees (`web_search_requests`), not discounted by tier.
- A cost the provider reports (OpenRouter `usage.cost`) is preferred over
  the table by `usage_cost` and recorded as `cost_source="provider_reported"`.
"""

# Cache rates as a multiple of the input rate, when a table entry does not
# give its own. OpenAI: cached input at 50% (gpt-4o / o1 families). Anthropic:
# cache reads at 10%, 5-minute cache writes at 125%. Google: Gemini 1.5
# context-cache reads at 25%. Vendors not listed bill cache tokens at the
# full input rate.
CACHE_READ_MULTIPLIER: dict[str, float] = {
    "openai": 0.5,
    "anthropic": 0.1,
    "google": 0.25,
}
CACHE_WRITE_MULTIPLIER: dict[str, float] = {
    "anthropic": 1.25,
}
# Anthropic 1-hour TTL cache writes are billed at 2x the input rate. Vendors
# not listed bill them at the (5-minute) cache-write rate.
CACHE_WRITE_1H_MULTIPLIER: dict[str, float] = {
    "anthropic": 2.0,
}

# Service tiers, as a multiplier on every token rate (cache rates included;
# Anthropic documents that batch and caching discounts stack). Standard
# tiers ("default", "standard", "auto", None) cost 1.0 for every vendor. Any
# other tier not listed here is unpriced, so the cost is None. OpenAI
# "priority" is not a flat multiple of standard and needs per-model rates.
STANDARD_SERVICE_TIERS = frozenset({"default", "standard", "auto"})
SERVICE_TIER_MULTIPLIER: dict[str, dict[str, float]] = {
    "openai": {"batch": 0.5, "flex": 0.5},
    "anthropic": {"batch": 0.5},
    "google": {"batch": 0.5},
}

# Per-request server tool fees in USD. Anthropic web search: $10 / 1,000.
WEB_SEARCH_FEE: dict[str, float] = {
    "anthropic": 0.01,
}

# Long-context bands: (threshold, rates). When `tokens_in` (the whole prompt,
# cache reads and writes included) exceeds `threshold`, the whole request is
# priced at `rates` (same tuple layout as DEFAULT_PRICING).
LONG_CONTEXT_PRICING: dict[tuple[str, str], tuple[int, tuple[float, ...]]] = {
    # Gemini 2.5 Pro, prompts > 200k: $2.50 in / $15 out / $0.25 cached.
    ("google", "gemini-2.5-pro"): (200_000, (0.0025, 0.015, 0.00025, 0.0025)),
    # Claude Sonnet 4 1M context, prompts > 200k: $6 in / $22.50 out; cache
    # multipliers apply to the $6 rate ($0.60 read, $7.50 / $12 write).
    ("anthropic", "claude-sonnet-4-20250514"): (200_000, (0.006, 0.0225)),
}

# Audio token rates (input_per_1k, output_per_1k) in USD, for models that
# bill audio tokens apart from text tokens.
AUDIO_PRICING: dict[tuple[str, str], tuple[float, float]] = {
    ("openai", "gpt-4o-audio-preview"): (0.040, 0.080),
    ("openai", "gpt-4o-mini-audio-preview"): (0.010, 0.020),
}

# Vendors that resell another vendor's models at its list prices: a missing
# (vendor, model) entry is looked up under the fallback vendor, cache
# multipliers included. Azure OpenAI global standard deployments are billed
# at OpenAI list prices.
PRICING_VENDOR_FALLBACK: dict[str, str] = {
    "azure_openai": "openai",
}

# (input_per_1k_tokens, output_per_1k_tokens) in USD
# Source: public pricing pages as of 2026-Q1. Override at runtime if needed.
DEFAULT_PRICING: dict[tuple[str, str], tuple[float, ...]] = {
    # OpenAI
    ("openai", "gpt-4o"): (0.0025, 0.010),
    ("openai", "gpt-4o-mini"): (0.00015, 0.0006),
    ("openai", "gpt-4-turbo"): (0.010, 0.030),
    ("openai", "gpt-4"): (0.030, 0.060),
    ("openai", "gpt-3.5-turbo"): (0.0005, 0.0015),
    ("openai", "o1"): (0.015, 0.060),
    ("openai", "o1-mini"): (0.003, 0.012),
    # o3 / o4-mini cached input is 25% of input, not the OpenAI default 50%.
    ("openai", "o3"): (0.002, 0.008, 0.0005),
    ("openai", "o4-mini"): (0.0011, 0.0044, 0.000275),
    # Text rates; audio tokens are priced from AUDIO_PRICING.
    ("openai", "gpt-4o-audio-preview"): (0.0025, 0.010),
    ("openai", "gpt-4o-mini-audio-preview"): (0.00015, 0.0006),

    # Anthropic
    ("anthropic", "claude-opus-4-20250514"): (0.015, 0.075),
    ("anthropic", "claude-sonnet-4-20250514"): (0.003, 0.015),
    ("anthropic", "claude-3-5-sonnet-20241022"): (0.003, 0.015),
    ("anthropic", "claude-3-5-haiku-20241022"): (0.0008, 0.004),
    ("anthropic", "claude-3-opus-20240229"): (0.015, 0.075),

    # Google
    ("google", "gemini-1.5-pro"): (0.00125, 0.005),
    ("google", "gemini-1.5-flash"): (0.000075, 0.0003),
    ("google", "gemini-2.0-flash"): (0.0001, 0.0004),
    # Gemini 2.5 cached input is 10% of input (2.0 and 1.5: 25%).
    ("google", "gemini-2.5-flash"): (0.0003, 0.0025, 0.00003, 0.0003),
    ("google", "gemini-2.5-pro"): (0.00125, 0.010, 0.000125, 0.00125),  # <= 200k

    # Amazon Bedrock (on-demand, us-east-1). Cache rates are per model:
    # Claude reads 10% / writes 125%, Nova reads 25% / no write charge.
    ("bedrock", "anthropic.claude-sonnet-4-20250514-v1:0"):
        (0.003, 0.015, 0.0003, 0.00375),
    ("bedrock", "anthropic.claude-3-5-sonnet-20241022-v2:0"):
        (0.003, 0.015, 0.0003, 0.00375),
    ("bedrock", "anthropic.claude-3-5-haiku-20241022-v1:0"):
        (0.0008, 0.004, 0.00008, 0.001),
    ("bedrock", "anthropic.claude-3-haiku-20240307-v1:0"): (0.00025, 0.00125),
    ("bedrock", "amazon.nova-pro-v1:0"): (0.0008, 0.0032, 0.0002, 0.0),
    ("bedrock", "amazon.nova-lite-v1:0"): (0.00006, 0.00024, 0.000015, 0.0),
    ("bedrock", "amazon.nova-micro-v1:0"): (0.000035, 0.00014, 0.00000875, 0.0),
    ("bedrock", "meta.llama3-1-70b-instruct-v1:0"): (0.00072, 0.00072),

    # Mistral (La Plateforme)
    ("mistral", "mistral-large-latest"): (0.002, 0.006),
    ("mistral", "mistral-medium-latest"): (0.0004, 0.002),
    ("mistral", "mistral-small-latest"): (0.0001, 0.0003),
    ("mistral", "codestral-latest"): (0.0003, 0.0009),

    # Cohere
    ("cohere", "command-a-03-2025"): (0.0025, 0.010),
    ("cohere", "command-r-plus-08-2024"): (0.0025, 0.010),
    ("cohere", "command-r-08-2024"): (0.00015, 0.0006),
    ("cohere", "command-r7b-12-2024"): (0.0000375, 0.00015),

    # Azure OpenAI: priced via PRICING_VENDOR_FALLBACK (OpenAI list prices).
}


def _tier_multiplier(vendor: str, service_tier: str | None) -> float | None:
    if service_tier is None or service_tier in STANDARD_SERVICE_TIERS:
        return 1.0
    return SERVICE_TIER_MULTIPLIER.get(vendor, {}).get(service_tier)


def estimate_cost(
    vendor: str,
    model: str | None,
    tokens_in: int | None,
    tokens_out: int | None,
    pricing_table: dict | None = None,
    tokens_cached: int | None = None,
    tokens_cache_write: int | None = None,
    *,
    tokens_cache_write_1h: int | None = None,
    tokens_audio_in: int | None = None,
    tokens_audio_out: int | None = None,
    web_search_requests: int | None = None,
    service_tier: str | None = None,
) -> float | None:
    """Estimate USD cost for a single call.

    Returns None when:
    - model is None
    - (vendor, model) not in pricing table
    - either token count is None
    - the service tier, or audio tokens, have no known price
    """
    if model is None or tokens_in is None or tokens_out is None:
        return None

    table = pricing_table if pricing_table is not None else DEFAULT_PRICING
    rates = table.get((vendor, model))
    if rates is None and vendor in PRICING_VENDOR_FALLBACK:
        vendor = PRICING_VENDOR_FALLBACK[vendor]
        rates = table.get((vendor, model))
    if rates is None:
        return None

    tier = _tier_multiplier(vendor, service_tier)
    if tier is None:
        return None

    if pricing_table is None:
        band = LONG_CONTEXT_PRICING.get((vendor, model))
        if band is not None and tokens_in > band[0]:
            rates = band[1]

    audio_in = tokens_audio_in or 0
    audio_out = tokens_audio_out or 0
    audio_in_rate = audio_out_rate = 0.0
    if audio_in or audio_out:
        audio_rates = AUDIO_PRICING.get((vendor, model))
        if audio_rates is None:
            return None
        audio_in_rate, audio_out_rate = audio_rates

    in_rate, out_rate = rates[0], rates[1]
    cached_rate = (
        rates[2] if len(rates) > 2
        else in_rate * CACHE_READ_MULTIPLIER.get(vendor, 1.0)
    )
    write_rate = (
        rates[3] if len(rates) > 3
        else in_rate * CACHE_WRITE_MULTIPLIER.get(vendor, 1.0)
    )
    if len(rates) > 4:
        write_1h_rate = rates[4]
    elif vendor in CACHE_WRITE_1H_MULTIPLIER:
        write_1h_rate = in_rate * CACHE_WRITE_1H_MULTIPLIER[vendor]
    else:
        write_1h_rate = write_rate
    cached = tokens_cached or 0
    cache_write = tokens_cache_write or 0
    write_1h = min(tokens_cache_write_1h or 0, cache_write)
    # Never charge a negative remainder if counts are inconsistent.
    uncached = max(tokens_in - cached - cache_write - audio_in, 0)
    text_out = max(tokens_out - audio_out, 0)
    token_cost = (
        uncached * in_rate
        + cached * cached_rate
        + (cache_write - write_1h) * write_rate
        + write_1h * write_1h_rate
        + audio_in * audio_in_rate
        + text_out * out_rate
        + audio_out * audio_out_rate
    ) / 1000.0
    fees = (web_search_requests or 0) * WEB_SEARCH_FEE.get(vendor, 0.0)
    return round(token_cost * tier + fees, 6)


# Adapter fields, besides tokens_in/out, that estimate_cost prices.
PRICING_DIMENSIONS = (
    "tokens_cached",
    "tokens_cache_write",
    "tokens_cache_write_1h",
    "tokens_audio_in",
    "tokens_audio_out",
    "web_search_requests",
    "service_tier",
)


def usage_cost(
    vendor: str,
    model: str | None,
    fields: dict,
    pricing_table: dict | None = None,
) -> float | None:
    """Cost of one call from adapter fields.

    A cost the provider reported (`provider_cost`, e.g. OpenRouter
    `usage.cost`) wins over the pricing table, even for unknown models.
    """
    provider_cost = fields.get("provider_cost")
    if provider_cost is not None:
        return round(float(provider_cost), 6)
    return estimate_cost(
        vendor,
        model,
        fields.get("tokens_in"),
        fields.get("tokens_out"),
        pricing_table,
        **{key: fields.get(key) for key in PRICING_DIMENSIONS},
    )


USAGE_DETAIL_FIELDS = (
    "tokens_cached",
    "tokens_cache_write",
    "tokens_cache_write_1h",
    "tokens_reasoning",
    "tokens_audio_in",
    "tokens_audio_out",
    "web_search_requests",
    "service_tier",
)

# `cost_source` values: how the event's `cost` was produced.
COST_SOURCE_PRICING_TABLE = "pricing_table"
COST_SOURCE_PRICING_TABLE_ESTIMATED_TOKENS = "pricing_table_estimated_tokens"
COST_SOURCE_PROVIDER_REPORTED = "provider_reported"


def add_usage_detail_fields(event: dict, adapter_fields: dict) -> None:
    """Copy cached/reasoning counts onto `event` and set `cost_source`.

    Detail counts are only added when the provider reported them. When
    `cost` is None (unknown model or tokens), `cost_source` is omitted.
    """
    for key in USAGE_DETAIL_FIELDS:
        value = adapter_fields.get(key)
        if value is not None:
            event[key] = value
    if event.get("cost") is not None:
        if adapter_fields.get("provider_cost") is not None:
            event["cost_source"] = COST_SOURCE_PROVIDER_REPORTED
            return
        event["cost_source"] = (
            COST_SOURCE_PRICING_TABLE_ESTIMATED_TOKENS
            if adapter_fields.get("tokens_estimated")
            else COST_SOURCE_PRICING_TABLE
        )
