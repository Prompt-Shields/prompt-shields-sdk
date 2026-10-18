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
- Non-token units (`usage_units` + `usage_unit_type`, as the gateway emits
  them): OCR pages, speech characters, transcription seconds, rerank search
  units, and generated images by quality and size. See `estimate_unit_cost`.
"""

import math

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
    # The gateway's provider slug for Mistral La Plateforme.
    "mistral-ai": "mistral",
}

# Non-token usage units (#71), named as the gateway's ps-telemetry
# `usageUnits()` emits `usage_unit_type`, plus "images" for image generation.
UNIT_SECONDS = "seconds"
UNIT_CHARACTERS = "characters"
UNIT_PAGES = "pages"
UNIT_SEARCH_UNITS = "search_units"
UNIT_IMAGES = "images"
USAGE_UNIT_TYPES = frozenset(
    {UNIT_SECONDS, UNIT_CHARACTERS, UNIT_PAGES, UNIT_SEARCH_UNITS, UNIT_IMAGES}
)

# USD per unit, keyed (vendor, model, usage_unit_type). List prices checked
# 2026-10-18 against the vendor page named on each entry (as indexed by web
# search; the pages themselves were not reachable from the build host) and
# cross-checked with LiteLLM 1.105.1 `model_prices_and_context_window.json`;
# every entry below matches it. Models billed by tokens rather than by unit
# are left out on purpose (their unit counts cannot be priced exactly):
# gpt-4o-transcribe, gpt-4o-mini-transcribe and gpt-4o-mini-tts. So are
# prices that could not be confirmed: Mistral OCR 2505 and the moving
# `mistral-ocr-latest` alias, Deepgram (Nova-3 and Aura-2 voice-specific
# model ids), ElevenLabs text to speech (the vendor page and LiteLLM
# disagree, $0.10 vs $0.18 / 1K characters for Multilingual v2 / v3, and
# Flash / Turbo are not in LiteLLM), and DALL-E 2/3 (retired from the OpenAI
# API on 2026-05-12).
UNIT_PRICING: dict[tuple[str, str, str], float] = {
    # OpenAI, https://platform.openai.com/docs/pricing (2026-10-18):
    # Whisper $0.006 / minute; TTS $15 / 1M characters, TTS HD $30 / 1M.
    ("openai", "whisper-1", UNIT_SECONDS): 0.006 / 60,
    ("openai", "tts-1", UNIT_CHARACTERS): 15.0 / 1_000_000,
    ("openai", "tts-1-hd", UNIT_CHARACTERS): 30.0 / 1_000_000,
    # Mistral OCR 3, https://mistral.ai/news/mistral-ocr-3 and
    # https://mistral.ai/pricing (2026-10-18): $2 / 1,000 pages (annotations
    # are billed apart and not included).
    ("mistral", "mistral-ocr-2512", UNIT_PAGES): 2.0 / 1000,
    # ElevenLabs, https://elevenlabs.io/pricing/api (2026-10-18):
    # Scribe v1 / v2 (batch speech to text) $0.22 / hour.
    ("elevenlabs", "scribe_v1", UNIT_SECONDS): 0.22 / 3600,
    ("elevenlabs", "scribe_v2", UNIT_SECONDS): 0.22 / 3600,
    # Cohere Rerank 3.5, https://cohere.com/pricing (2026-10-18): $2 / 1,000
    # searches (one search unit is a query over up to 100 documents). The
    # Cohere page was not indexed; the rate is from LiteLLM and two price
    # trackers that agree on it.
    ("cohere", "rerank-v3.5", UNIT_SEARCH_UNITS): 2.0 / 1000,
}

# USD per generated image, keyed (vendor, model) then (quality, size).
# OpenAI gpt-image-1, https://platform.openai.com/docs/pricing "Image
# generation" (2026-10-18). gpt-image-1 bills image output tokens; these are
# OpenAI's published per-image equivalents, used when only an image count is
# known.
IMAGE_PRICING: dict[tuple[str, str], dict[tuple[str, str], float]] = {
    ("openai", "gpt-image-1"): {
        ("low", "1024x1024"): 0.011,
        ("low", "1024x1536"): 0.016,
        ("low", "1536x1024"): 0.016,
        ("medium", "1024x1024"): 0.042,
        ("medium", "1024x1536"): 0.063,
        ("medium", "1536x1024"): 0.063,
        ("high", "1024x1024"): 0.167,
        ("high", "1024x1536"): 0.25,
        ("high", "1536x1024"): 0.25,
    },
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


def _unit_rate(
    table: dict,
    vendor: str,
    model: str,
    unit_type: str,
    image_quality: str | None,
    image_size: str | None,
) -> float | None:
    """The USD rate for one unit, from a custom table or the defaults.

    A custom table prices units under `(vendor, model, usage_unit_type)`
    keys: a float per unit, or for images a `{(quality, size): usd}` dict.
    """
    if table is not None:
        rate = table.get((vendor, model, unit_type))
    elif unit_type == UNIT_IMAGES:
        rate = IMAGE_PRICING.get((vendor, model))
    else:
        rate = UNIT_PRICING.get((vendor, model, unit_type))
    if isinstance(rate, dict):
        rate = rate.get((image_quality, image_size))
    if isinstance(rate, (int, float)) and not isinstance(rate, bool):
        return float(rate)
    return None


def estimate_unit_cost(
    vendor: str,
    model: str | None,
    usage_units: float | None,
    usage_unit_type: str | None,
    pricing_table: dict | None = None,
    *,
    image_quality: str | None = None,
    image_size: str | None = None,
    service_tier: str | None = None,
) -> float | None:
    """Estimate USD cost of a call billed by non-token units.

    `usage_unit_type` is one of `USAGE_UNIT_TYPES`: "seconds" (speech to
    text), "characters" (text to speech), "pages" (OCR), "search_units"
    (rerank) or "images" (image generation, priced by `image_quality` and
    `image_size`, e.g. "medium" and "1024x1024"). Units are priced as
    reported, without rounding.

    Returns None when the model, unit count or unit type is missing, the
    (vendor, model, unit type) has no price, or the service tier is not a
    standard one.
    """
    if model is None or usage_unit_type is None:
        return None
    if (
        not isinstance(usage_units, (int, float))
        or isinstance(usage_units, bool)
        or not math.isfinite(usage_units)
        or usage_units < 0
    ):
        return None

    rate = _unit_rate(
        pricing_table, vendor, model, usage_unit_type, image_quality, image_size
    )
    if rate is None and vendor in PRICING_VENDOR_FALLBACK:
        vendor = PRICING_VENDOR_FALLBACK[vendor]
        rate = _unit_rate(
            pricing_table, vendor, model, usage_unit_type, image_quality, image_size
        )
    if rate is None:
        return None

    # Unit prices are standard-tier list prices; a batch or flex discount on
    # them is not known, so any other tier is unpriced.
    if service_tier is not None and service_tier not in STANDARD_SERVICE_TIERS:
        return None
    return round(usage_units * rate, 6)


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
    A call with `usage_units` and `usage_unit_type` is priced per unit when
    its (vendor, model, unit type) has a price, else from its tokens.
    """
    provider_cost = fields.get("provider_cost")
    if provider_cost is not None:
        return round(float(provider_cost), 6)
    if fields.get("usage_unit_type") is not None:
        unit_cost = estimate_unit_cost(
            vendor,
            model,
            fields.get("usage_units"),
            fields.get("usage_unit_type"),
            pricing_table,
            image_quality=fields.get("image_quality"),
            image_size=fields.get("image_size"),
            service_tier=fields.get("service_tier"),
        )
        if unit_cost is not None:
            return unit_cost
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
    "usage_units",
    "usage_unit_type",
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
