"""Token-to-USD cost estimation per (vendor, model).

Prices come from the bundled model catalog (``catalog.json``, see
``prompt_shields.catalog``), which is regenerated from the LiteLLM price map.
Customers can override by passing a custom ``pricing_table`` to ShieldsClient
at construction.

Returns USD as a Decimal-compatible float. Returns None when the model is
unknown — the collector treats None cost as "unmetered" rather than zero.
"""

from prompt_shields.catalog import load_catalog, strip_date_suffix

# (input_per_1k_tokens, output_per_1k_tokens) in USD, keyed by (vendor, model),
# including dated aliases. Derived from the catalog; its version is
# PRICING_CATALOG_VERSION.
_CATALOG = load_catalog()
DEFAULT_PRICING: dict[tuple[str, str], tuple[float, float]] = _CATALOG.pricing_table()
PRICING_CATALOG_VERSION: str = _CATALOG.version


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
        rates = table.get((vendor, strip_date_suffix(model)))
    if rates is None:
        return None

    in_rate, out_rate = rates
    return round((tokens_in / 1000.0) * in_rate + (tokens_out / 1000.0) * out_rate, 6)
