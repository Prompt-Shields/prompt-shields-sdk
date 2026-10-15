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
vendor defaults below, `(input_per_1k, output_per_1k, cached_input_per_1k)`
or `(..., cached_input_per_1k, cache_write_per_1k)`.
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
    ("google", "gemini-2.5-flash"): (0.0003, 0.0025),
    ("google", "gemini-2.5-pro"): (0.00125, 0.010),  # prompts <= 200k

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


def estimate_cost(
    vendor: str,
    model: str | None,
    tokens_in: int | None,
    tokens_out: int | None,
    pricing_table: dict | None = None,
    tokens_cached: int | None = None,
    tokens_cache_write: int | None = None,
) -> float | None:
    """Estimate USD cost for a single call.

    Returns None when:
    - model is None
    - (vendor, model) not in pricing table
    - either token count is None
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

    in_rate, out_rate = rates[0], rates[1]
    cached_rate = (
        rates[2] if len(rates) > 2
        else in_rate * CACHE_READ_MULTIPLIER.get(vendor, 1.0)
    )
    write_rate = (
        rates[3] if len(rates) > 3
        else in_rate * CACHE_WRITE_MULTIPLIER.get(vendor, 1.0)
    )
    cached = tokens_cached or 0
    cache_write = tokens_cache_write or 0
    # Never charge a negative uncached remainder if counts are inconsistent.
    uncached = max(tokens_in - cached - cache_write, 0)
    cost = (
        uncached * in_rate
        + cached * cached_rate
        + cache_write * write_rate
        + tokens_out * out_rate
    ) / 1000.0
    return round(cost, 6)


USAGE_DETAIL_FIELDS = ("tokens_cached", "tokens_cache_write", "tokens_reasoning")

# `cost_source` values: how the event's `cost` was produced.
COST_SOURCE_PRICING_TABLE = "pricing_table"
COST_SOURCE_PRICING_TABLE_ESTIMATED_TOKENS = "pricing_table_estimated_tokens"


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
        event["cost_source"] = (
            COST_SOURCE_PRICING_TABLE_ESTIMATED_TOKENS
            if adapter_fields.get("tokens_estimated")
            else COST_SOURCE_PRICING_TABLE
        )
