"""Token-to-USD cost estimation per (vendor, model).

Pricing tiers are point-in-time snapshots and update over time. The SDK
ships sensible defaults; customers can override by passing a custom
`pricing_table` to ShieldsClient at construction.

Returns USD as a Decimal-compatible float. Returns None when the model is
unknown — the collector treats None cost as "unmetered" rather than zero.
"""

# (input_per_1k_tokens, output_per_1k_tokens) in USD
# Source: public pricing pages as of 2026-Q1. Override at runtime if needed.
DEFAULT_PRICING: dict[tuple[str, str], tuple[float, float]] = {
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
}


def estimate_cost(
    vendor: str,
    model: str | None,
    tokens_in: int | None,
    tokens_out: int | None,
    pricing_table: dict | None = None,
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
    if rates is None:
        return None

    in_rate, out_rate = rates
    return round((tokens_in / 1000.0) * in_rate + (tokens_out / 1000.0) * out_rate, 6)
