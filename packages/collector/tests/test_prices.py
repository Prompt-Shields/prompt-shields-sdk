"""Negotiated prices (#348): the ``prices`` v1 policy section and pricing
rules, without a database."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from collector import cost_reconciliation, policy_doc, prices

ROOT = Path(__file__).resolve().parents[3]


def _doc(section: dict) -> dict:
    return {"schema": "ps-policy.v1", "sections": {"prices": {"version": 1, **section}}}


def _problems(section: dict, scope: str | None = "tenant") -> list[str]:
    try:
        policy_doc.validate_document(_doc(section), scope)
    except policy_doc.PolicyDocumentError as e:
        return e.problems
    return []


RATE = {
    "provider": "openai",
    "model": "gpt-4o",
    "valid_from": "2026-11-01",
    "input_per_1k": 0.002,
    "output_per_1k": 0.008,
}


def test_valid_section():
    assert _problems({"overrides": [RATE]}) == []
    assert _problems(
        {
            "currency": "EUR",
            "usd_per_unit": 1.08,
            "overrides": [
                {**RATE, "valid_to": "2027-01-01T00:00:00+01:00", "cached_input_per_1k": 0.001},
                {"provider": "anthropic", "model": "*", "valid_from": "2026-11-01", "multiplier": 0.85},
            ],
        }
    ) == []


@pytest.mark.parametrize(
    "section, needle",
    [
        ({"overrides": [{**RATE, "multiplier": 0.9}]}, "either multiplier or per-1k rates"),
        ({"overrides": [{k: v for k, v in RATE.items() if k != "output_per_1k"}]}, "needs a multiplier"),
        ({"overrides": [{**RATE, "input_per_1k": -1}]}, "at least 0"),
        ({"overrides": [{**RATE, "valid_from": "2026-11-01T00:00:00"}]}, "valid_from"),
        ({"overrides": [{**RATE, "valid_to": "2026-10-01"}]}, "after valid_from"),
        ({"overrides": [{**RATE, "provider": "OpenAI"}]}, "provider"),
        ({"overrides": [{**RATE, "unit": "tokens"}]}, "unknown keys"),
        ({"currency": "EUR", "overrides": []}, "usd_per_unit"),
        ({"currency": "usd", "overrides": []}, "ISO 4217"),
        (
            {"overrides": [{"provider": "openai", "model": "*", "valid_from": "2026-01-01", "multiplier": 0}]},
            "multiplier must be",
        ),
        ({"overrides": [RATE, {**RATE, "valid_from": "2027-01-01"}]}, "overlap"),
        ({"overrides": "x"}, "must be an array"),
    ],
)
def test_invalid_sections(section, needle):
    assert any(needle in p for p in _problems(section)), _problems(section)


def test_consecutive_windows_do_not_overlap():
    a = {**RATE, "valid_to": "2027-01-01"}
    b = {**RATE, "valid_from": "2027-01-01", "input_per_1k": 0.001}
    assert _problems({"overrides": [b, a]}) == []


def test_only_tenant_scope_may_carry_prices():
    assert any("only a tenant-scope" in p for p in _problems({"overrides": []}, "team"))
    assert any("only a tenant-scope" in p for p in _problems({"overrides": []}, "project"))
    assert _problems({"overrides": []}, None) == []


def test_prices_is_no_longer_reserved():
    assert "prices" in policy_doc.SECTIONS
    assert "prices" not in policy_doc.RESERVED_SECTIONS


def _book(*overrides, **section):
    return prices.parse_section({"version": 1, **section, "overrides": list(overrides)})


NOV = datetime(2026, 11, 5, tzinfo=UTC)
OCT = datetime(2026, 10, 31, 23, 59, tzinfo=UTC)


def _event(**kw):
    base = {
        "tokens_in": 1000,
        "tokens_out": 1000,
        "tokens_cached": None,
        "tokens_cache_write": None,
        "tokens_cache_write_1h": None,
        "service_tier": None,
        "usage_unit_type": None,
        "served_model": None,
        "requested_model": None,
        "cost": 0.0125,
        "cost_source": "pricing_table",
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_override_applies_only_on_or_after_valid_from():
    book = _book(RATE)
    before = _event()
    assert not prices.reprice(book, before, "openai", "gpt-4o", OCT)
    assert (before.cost, before.cost_source) == (0.0125, "pricing_table")
    at_start = _event()
    assert prices.reprice(book, at_start, "openai", "gpt-4o", datetime(2026, 11, 1, tzinfo=UTC))
    assert (at_start.cost, at_start.cost_source) == (0.01, "negotiated")


def test_valid_to_is_exclusive():
    book = _book({**RATE, "valid_to": "2026-12-01"})
    assert prices.reprice(book, _event(), "openai", "gpt-4o", datetime(2026, 11, 30, 23, tzinfo=UTC))
    assert not prices.reprice(book, _event(), "openai", "gpt-4o", datetime(2026, 12, 1, tzinfo=UTC))


def test_rates_price_cache_tokens_at_the_provider_ratios():
    book = _book(RATE)
    e = _event(tokens_in=1000, tokens_cached=400, tokens_out=0)
    assert prices.reprice(book, e, "openai", "gpt-4o", NOV)
    # 600 uncached at 0.002 + 400 cached at 0.001 (0.5x) per 1k.
    assert e.cost == pytest.approx(0.0012 + 0.0004)
    anthropic = _book({**RATE, "provider": "anthropic", "model": "claude-x"})
    e = _event(tokens_in=1000, tokens_cache_write=500, tokens_cache_write_1h=100, tokens_out=0)
    assert prices.reprice(anthropic, e, "anthropic", "claude-x", NOV)
    # 500 uncached x0.002 + 400 5m writes x0.0025 + 100 1h writes x0.004.
    assert e.cost == pytest.approx(0.001 + 0.001 + 0.0004)


def test_batch_tier_gets_the_tier_multiplier_and_unknown_tiers_stay_at_list():
    book = _book(RATE)
    e = _event(service_tier="batch")
    assert prices.reprice(book, e, "openai", "gpt-4o", NOV) and e.cost == pytest.approx(0.005)
    e = _event(service_tier="priority")
    assert not prices.reprice(book, e, "openai", "gpt-4o", NOV)


def test_multiplier_discounts_the_list_price_and_needs_one():
    book = _book({"provider": "anthropic", "model": "*", "valid_from": "2026-11-01", "multiplier": 0.8})
    e = _event(cost=1.0)
    assert prices.reprice(book, e, "anthropic", "claude-sonnet-4", NOV)
    assert (e.cost, e.cost_source) == (0.8, "negotiated")
    # No list price to discount.
    assert not prices.reprice(book, _event(cost=None, cost_source=None), "anthropic", "m", NOV)
    # Per-unit list prices are discounted too.
    e = _event(cost=2.0, usage_unit_type="seconds", tokens_in=None, tokens_out=None)
    assert prices.reprice(book, e, "anthropic", "m", NOV) and e.cost == 1.6


def test_kept_cost_sources_are_never_repriced():
    book = _book(RATE)
    for source in ("provider_reported", "cache", "negotiated", "langchain"):
        e = _event(cost_source=source)
        assert not prices.reprice(book, e, "openai", "gpt-4o", NOV), source


def test_rates_price_unmetered_calls():
    e = _event(cost=None, cost_source=None)
    assert prices.reprice(_book(RATE), e, "openai", "gpt-4o", NOV) and e.cost == 0.01


def test_matching_by_served_model_date_suffix_and_exact_before_wildcard():
    book = _book(
        RATE,
        {"provider": "openai", "model": "*", "valid_from": "2026-11-01", "multiplier": 0.5},
    )
    e = _event(served_model="gpt-4o-2024-08-06")
    assert prices.reprice(book, e, "openai", "unknown", NOV) and e.cost == 0.01
    e = _event(served_model="gpt-4o-mini")
    assert prices.reprice(book, e, "openai", "gpt-4o-mini", NOV) and e.cost == 0.00625
    # Another vendor's contract does not apply.
    assert not prices.reprice(book, _event(), "azure_openai", "gpt-4o", NOV)


def test_vendor_spelling_is_normalised():
    book = _book({**RATE, "provider": "azure_openai"})
    assert prices.reprice(book, _event(), "azure-openai", "gpt-4o", NOV)


def test_non_usd_rates_are_converted():
    book = _book(RATE, currency="EUR", usd_per_unit=1.5)
    e = _event()
    assert prices.reprice(book, e, "openai", "gpt-4o", NOV) and e.cost == pytest.approx(0.015)


def test_cache_multipliers_match_the_gateway():
    src = (ROOT / "gateway/src/middlewares/psPricing.ts").read_text()
    block = src.split("const CACHE_READ_MULTIPLIER")[1].split("};")[0]
    for vendor, m in prices.CACHE_READ_MULTIPLIER.items():
        assert f"{vendor}: {m}" in block
    assert "anthropic: 1.25" in src and "anthropic: 2.0" in src


def test_collector_list_prices_are_the_catalog_standard_rates():
    gateway = json.loads((ROOT / "gateway/src/middlewares/psPricingTables.json").read_text())
    assert cost_reconciliation.list_prices() == {
        k: tuple(v[:2]) for k, v in gateway["tokenPricing"].items()
    }


@pytest.mark.parametrize(
    "raw, key",
    [
        ("gpt-4o-2024-08-06", "gpt-4o"),
        ("claude-3-5-sonnet-20241022", "claude-3-5-sonnet"),
        ("claude-3-5-sonnet-latest", "claude-3-5-sonnet"),
        ("gpt-4o", "gpt-4o"),
        (None, "(unknown)"),
    ],
)
def test_reconciliation_model_key(raw, key):
    assert cost_reconciliation.model_key(raw) == key


def test_reconciliation_config_falls_back_on_invalid_values():
    cfg = cost_reconciliation.config_for(
        {"cost_reconciliation": {"threshold_pct": 10, "min_usd": "x", "min_tokens": 1.5, "enabled": 1}}
    )
    assert (cfg.threshold_pct, cfg.min_usd, cfg.min_tokens, cfg.enabled) == (10.0, 1.0, 100_000, True)


def test_month_helpers():
    assert cost_reconciliation.previous_month(datetime(2026, 1, 15, tzinfo=UTC)).isoformat() == "2025-12-01"
    start, end = cost_reconciliation.month_bounds(cost_reconciliation.parse_month("2026-12"))
    assert (start.month, end.year, end.month) == (12, 2027, 1)
    with pytest.raises(ValueError):
        cost_reconciliation.parse_month("2026-13")
