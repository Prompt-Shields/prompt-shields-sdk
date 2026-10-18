"""Non-token units (`usage_units` + `usage_unit_type`) in the pricing table
(#71). The golden per-unit vectors are in schemas/pricing/cost-vectors.v1.json;
these cover the edges around them.
"""

import pytest
from prompt_shields import estimate_unit_cost
from prompt_shields.pricing import (
    IMAGE_PRICING,
    UNIT_PRICING,
    USAGE_UNIT_TYPES,
    add_usage_detail_fields,
    usage_cost,
)


def test_default_unit_prices_use_known_unit_types():
    assert {key[2] for key in UNIT_PRICING} <= USAGE_UNIT_TYPES
    for prices in IMAGE_PRICING.values():
        assert all(price > 0 for price in prices.values())


@pytest.mark.parametrize(
    "units", [None, -1, True, "90", float("nan"), float("inf")]
)
def test_invalid_unit_counts_are_unmetered(units):
    assert estimate_unit_cost("openai", "whisper-1", units, "seconds") is None


def test_missing_model_or_unit_type_is_unmetered():
    assert estimate_unit_cost("openai", None, 90, "seconds") is None
    assert estimate_unit_cost("openai", "whisper-1", 90, None) is None


def test_unit_type_must_match_the_price():
    # whisper-1 is priced per second, not per character.
    assert estimate_unit_cost("openai", "whisper-1", 90, "characters") is None


def test_fractional_seconds_are_not_rounded():
    # 12.5 s x $0.006 / 60 s
    assert estimate_unit_cost("openai", "whisper-1", 12.5, "seconds") == (
        pytest.approx(0.00125)
    )


def test_zero_units_cost_zero():
    assert estimate_unit_cost("openai", "tts-1", 0, "characters") == 0.0


def test_standard_tiers_are_priced():
    for tier in (None, "default", "standard", "auto"):
        assert estimate_unit_cost(
            "openai", "tts-1", 1000, "characters", service_tier=tier
        ) == pytest.approx(0.015)


def test_image_needs_known_quality_and_size():
    assert estimate_unit_cost(
        "openai", "gpt-image-1", 1, "images",
        image_quality="low", image_size="1024x1536",
    ) == pytest.approx(0.016)
    assert estimate_unit_cost("openai", "gpt-image-1", 1, "images") is None


def test_custom_table_prices_units_and_replaces_defaults():
    table = {
        ("openai", "whisper-1", "seconds"): 0.00005,
        ("acme", "img-v1", "images"): {("hd", "512x512"): 0.02},
        ("acme", "ocr-v1", "pages"): 0.003,
    }
    assert estimate_unit_cost(
        "openai", "whisper-1", 100, "seconds", table
    ) == pytest.approx(0.005)
    assert estimate_unit_cost(
        "acme", "img-v1", 3, "images", table,
        image_quality="hd", image_size="512x512",
    ) == pytest.approx(0.06)
    assert estimate_unit_cost("acme", "ocr-v1", 10, "pages", table) == (
        pytest.approx(0.03)
    )
    # A custom table replaces the defaults, as for token prices.
    assert estimate_unit_cost("openai", "tts-1", 1000, "characters", table) is None


def test_custom_token_table_without_unit_keys_is_unaffected():
    table = {("openai", "gpt-4o"): (0.002, 0.008)}
    assert usage_cost(
        "openai", "gpt-4o", {"tokens_in": 1000, "tokens_out": 1000}, table
    ) == pytest.approx(0.01)


def test_usage_cost_prefers_provider_cost_over_units():
    fields = {"usage_units": 90, "usage_unit_type": "seconds", "provider_cost": 0.5}
    assert usage_cost("openai", "whisper-1", fields) == 0.5


def test_usage_cost_falls_back_to_tokens_when_unit_is_unpriced():
    fields = {
        "tokens_in": 1000,
        "tokens_out": 2000,
        "usage_units": 3,
        "usage_unit_type": "pages",
    }
    assert usage_cost("openai", "gpt-4o", fields) == pytest.approx(0.0225)


def test_event_carries_units_and_cost_source():
    fields = {"usage_units": 90, "usage_unit_type": "seconds"}
    event = {"cost": usage_cost("openai", "whisper-1", fields)}
    add_usage_detail_fields(event, fields)
    assert event == {
        "cost": pytest.approx(0.009),
        "usage_units": 90,
        "usage_unit_type": "seconds",
        "cost_source": "pricing_table",
    }


def test_unpriced_unit_event_has_no_cost_source():
    fields = {"usage_units": 10, "usage_unit_type": "pages"}
    event = {"cost": usage_cost("mistral", "mistral-ocr-latest", fields)}
    add_usage_detail_fields(event, fields)
    assert event["cost"] is None
    assert "cost_source" not in event
    assert event["usage_units"] == 10


# Unit sanity checks: one hand-computed example per unit type, written from
# the vendor's own price unit so a per-minute / per-second (or per-1K /
# per-1M) slip in the table shows up here.
@pytest.mark.parametrize(
    ("vendor", "model", "units", "unit_type", "kwargs", "expected"),
    [
        # whisper-1: $0.006 per minute; 2 min 30 s = 2.5 min -> $0.015
        ("openai", "whisper-1", 150, "seconds", {}, 2.5 * 0.006),
        # Scribe: $0.22 per hour; 2 hours = 7200 s -> $0.44
        ("elevenlabs", "scribe_v1", 7200, "seconds", {}, 2 * 0.22),
        # tts-1: $15 per 1M characters; 1M characters -> $15
        ("openai", "tts-1", 1_000_000, "characters", {}, 15.0),
        # tts-1-hd: $30 per 1M characters; 500K characters -> $15
        ("openai", "tts-1-hd", 500_000, "characters", {}, 15.0),
        # Mistral OCR 3: $2 per 1,000 pages; 1,000 pages -> $2
        ("mistral", "mistral-ocr-2512", 1000, "pages", {}, 2.0),
        # Cohere rerank-v3.5: $2 per 1,000 searches; 1,000 -> $2
        ("cohere", "rerank-v3.5", 1000, "search_units", {}, 2.0),
        # gpt-image-1 low 1024x1024: $0.011 per image; 10 images -> $0.11
        (
            "openai", "gpt-image-1", 10, "images",
            {"image_quality": "low", "image_size": "1024x1024"}, 0.11,
        ),
    ],
)
def test_unit_price_sanity(vendor, model, units, unit_type, kwargs, expected):
    assert estimate_unit_cost(
        vendor, model, units, unit_type, **kwargs
    ) == pytest.approx(expected)


def test_unconfirmed_elevenlabs_tts_is_unpriced():
    for model in ("eleven_multilingual_v2", "eleven_v3", "eleven_flash_v2_5"):
        assert estimate_unit_cost("elevenlabs", model, 1000, "characters") is None


def test_tokens_and_priced_units_are_not_added_together():
    # A call carrying both tokens and a priced unit is priced once, by unit.
    fields = {
        "tokens_in": 1000,
        "tokens_out": 1000,
        "usage_units": 60,
        "usage_unit_type": "seconds",
    }
    assert usage_cost("openai", "whisper-1", fields) == pytest.approx(0.006)


def test_token_billed_transcription_is_not_priced_by_unit():
    # gpt-4o-transcribe bills tokens: its seconds must not add a unit price.
    fields = {"usage_units": 60, "usage_unit_type": "seconds"}
    assert usage_cost("openai", "gpt-4o-transcribe", fields) is None


def test_gateway_mistral_slug_prices_tokens_as_mistral():
    fields = {"tokens_in": 1000, "tokens_out": 1000}
    assert usage_cost("mistral-ai", "mistral-large-latest", fields) == (
        usage_cost("mistral", "mistral-large-latest", fields)
    )
