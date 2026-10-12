"""Tests for the bundled model & provider catalog (prompt_shields.catalog)."""
import copy
import json

import pytest

from prompt_shields import load_catalog
from prompt_shields.catalog import DEFAULT_CATALOG_PATH, parse_catalog
from prompt_shields.pricing import DEFAULT_PRICING, PRICING_CATALOG_VERSION, estimate_cost

CATALOG = load_catalog()
RAW = json.loads(DEFAULT_CATALOG_PATH.read_text(encoding="utf-8"))


# --- structure ------------------------------------------------------------


def test_bundled_catalog_has_version_and_schema():
    assert RAW["schema_version"] == 1
    assert CATALOG.version == RAW["catalog_version"] == PRICING_CATALOG_VERSION


def test_every_model_has_input_and_output_price():
    for model_id, m in CATALOG.models.items():
        assert m.input_cost_per_token is not None, model_id
        assert m.output_cost_per_token is not None, model_id


def test_seeded_with_current_and_sovereign_supply():
    vendors = {m.vendor for m in CATALOG.models.values()}
    assert {"openai", "anthropic", "google", "mistral", "scaleway", "ovhcloud"} <= vendors
    for pid in ("berget", "mistral", "infercom", "regolo", "ionos", "stackit", "scaleway", "ovhcloud"):
        assert CATALOG.providers[pid].sovereign_supply, pid


# --- lookup ---------------------------------------------------------------


def test_get_exact():
    m = CATALOG.get("openai", "gpt-4o")
    assert m.id == "openai/gpt-4o"
    assert m.pricing["cache_read_input_token_cost"] < m.input_cost_per_token


def test_get_by_alias_and_by_dated_snapshot():
    assert CATALOG.get("anthropic", "claude-sonnet-4-5-20250929").model == "claude-sonnet-4-5"
    assert CATALOG.get("openai", "gpt-4o-2024-08-06").model == "gpt-4o"


def test_get_unknown_returns_none():
    assert CATALOG.get("openai", "gpt-99") is None
    assert CATALOG.get("openai", None) is None
    assert CATALOG.get("nope", "gpt-4o") is None


def test_capabilities_are_affirmative_only():
    m = CATALOG.get("openai", "gpt-4o")
    assert m.supports("function_calling")
    assert not m.supports("teleportation")
    legacy = CATALOG.get("anthropic", "claude-3-5-haiku-20241022")
    assert legacy.source == "legacy-sdk-0.2"
    assert not legacy.supports("function_calling")  # unknown is not "supported"


# --- sovereignty metadata -------------------------------------------------


def test_us_providers_flag_cloud_act_exposure():
    for pid in ("openai", "anthropic", "google"):
        p = CATALOG.providers[pid]
        assert p.jurisdiction == "US"
        assert p.us_cloud_act_exposure is True


def test_unverified_sovereignty_facts_stay_unknown():
    # Unknown must be None (fail-closed for policy), never a guessed False.
    for pid, p in CATALOG.providers.items():
        assert p.zdr is None, pid
        if p.provider_hq_country != "US":
            assert p.us_cloud_act_exposure is None, pid


def test_eu_sovereign_providers_are_eea():
    for pid in ("berget", "mistral", "scaleway", "ovhcloud", "ionos", "stackit", "regolo"):
        assert CATALOG.providers[pid].jurisdiction == "EEA", pid


# --- backward compatibility of pricing.py ---------------------------------


@pytest.mark.parametrize("key", [
    ("openai", "gpt-4o"), ("openai", "gpt-4o-mini"), ("openai", "gpt-4-turbo"),
    ("openai", "gpt-4"), ("openai", "gpt-3.5-turbo"), ("openai", "o1"), ("openai", "o1-mini"),
    ("anthropic", "claude-opus-4-20250514"), ("anthropic", "claude-sonnet-4-20250514"),
    ("anthropic", "claude-3-5-sonnet-20241022"), ("anthropic", "claude-3-5-haiku-20241022"),
    ("anthropic", "claude-3-opus-20240229"),
    ("google", "gemini-1.5-pro"), ("google", "gemini-1.5-flash"),
])
def test_every_sdk_02_model_is_still_priced(key):
    assert key in DEFAULT_PRICING


def test_aliases_are_in_legacy_table():
    assert DEFAULT_PRICING[("anthropic", "claude-sonnet-4-5-20250929")] == \
        DEFAULT_PRICING[("anthropic", "claude-sonnet-4-5")]


def test_estimate_cost_uses_catalog_prices():
    # claude-sonnet-4-5: 3e-6 in, 1.5e-5 out per token
    assert estimate_cost("anthropic", "claude-sonnet-4-5", 1000, 1000) == 0.018
    assert estimate_cost("mistral", "mistral-small-latest", 1_000_000, 0) == 0.15


# --- validation -----------------------------------------------------------


def test_rejects_unknown_schema_version():
    bad = copy.deepcopy(RAW)
    bad["schema_version"] = 99
    with pytest.raises(ValueError, match="schema_version"):
        parse_catalog(bad)


def test_rejects_model_with_unknown_provider():
    bad = copy.deepcopy(RAW)
    bad["models"]["ghost/x"] = {"vendor": "ghost", "model": "x"}
    with pytest.raises(ValueError, match="unknown provider"):
        parse_catalog(bad)


def test_rejects_duplicate_names():
    bad = copy.deepcopy(RAW)
    bad["models"]["openai/gpt-4o"]["aliases"] = ["gpt-4o-mini"]
    with pytest.raises(ValueError, match="listed twice"):
        parse_catalog(bad)
