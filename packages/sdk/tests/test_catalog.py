"""Catalog v1 (#7) and its generator + drift check (#8)."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest
from prompt_shields import catalog
from prompt_shields.pricing import (
    AUDIO_PRICING,
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_1H_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    DEFAULT_PRICING,
    LONG_CONTEXT_PRICING,
    SERVICE_TIER_MULTIPLIER,
    estimate_cost,
)

ROOT = Path(__file__).resolve().parents[3]


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "build_catalog", ROOT / "scripts" / "build_catalog.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_catalog = _load_builder()

# The literal tables pricing.py held before the catalog (v0.2.0). Every entry
# must still price exactly the same.
LEGACY_DEFAULT_PRICING = {
    ("openai", "gpt-4o"): (0.0025, 0.010),
    ("openai", "gpt-4o-mini"): (0.00015, 0.0006),
    ("openai", "gpt-4-turbo"): (0.010, 0.030),
    ("openai", "gpt-4"): (0.030, 0.060),
    ("openai", "gpt-3.5-turbo"): (0.0005, 0.0015),
    ("openai", "o1"): (0.015, 0.060),
    ("openai", "o1-mini"): (0.003, 0.012),
    ("openai", "o3"): (0.002, 0.008, 0.0005),
    ("openai", "o4-mini"): (0.0011, 0.0044, 0.000275),
    ("openai", "gpt-4o-audio-preview"): (0.0025, 0.010),
    ("openai", "gpt-4o-mini-audio-preview"): (0.00015, 0.0006),
    ("anthropic", "claude-opus-4-20250514"): (0.015, 0.075),
    ("anthropic", "claude-sonnet-4-20250514"): (0.003, 0.015),
    ("anthropic", "claude-3-5-sonnet-20241022"): (0.003, 0.015),
    ("anthropic", "claude-3-5-haiku-20241022"): (0.0008, 0.004),
    ("anthropic", "claude-3-opus-20240229"): (0.015, 0.075),
    ("google", "gemini-1.5-pro"): (0.00125, 0.005),
    ("google", "gemini-1.5-flash"): (0.000075, 0.0003),
    ("google", "gemini-2.0-flash"): (0.0001, 0.0004),
    ("google", "gemini-2.5-flash"): (0.0003, 0.0025, 0.00003, 0.0003),
    ("google", "gemini-2.5-pro"): (0.00125, 0.010, 0.000125, 0.00125),
    ("bedrock", "anthropic.claude-sonnet-4-20250514-v1:0"): (0.003, 0.015, 0.0003, 0.00375),
    ("bedrock", "anthropic.claude-3-5-sonnet-20241022-v2:0"): (0.003, 0.015, 0.0003, 0.00375),
    ("bedrock", "anthropic.claude-3-5-haiku-20241022-v1:0"): (0.0008, 0.004, 0.00008, 0.001),
    ("bedrock", "anthropic.claude-3-haiku-20240307-v1:0"): (0.00025, 0.00125),
    ("bedrock", "amazon.nova-pro-v1:0"): (0.0008, 0.0032, 0.0002, 0.0),
    ("bedrock", "amazon.nova-lite-v1:0"): (0.00006, 0.00024, 0.000015, 0.0),
    ("bedrock", "amazon.nova-micro-v1:0"): (0.000035, 0.00014, 0.00000875, 0.0),
    ("bedrock", "meta.llama3-1-70b-instruct-v1:0"): (0.00072, 0.00072),
    ("mistral", "mistral-large-latest"): (0.002, 0.006),
    ("mistral", "mistral-medium-latest"): (0.0004, 0.002),
    ("mistral", "mistral-small-latest"): (0.0001, 0.0003),
    ("mistral", "codestral-latest"): (0.0003, 0.0009),
    ("cohere", "command-a-03-2025"): (0.0025, 0.010),
    ("cohere", "command-r-plus-08-2024"): (0.0025, 0.010),
    ("cohere", "command-r-08-2024"): (0.00015, 0.0006),
    ("cohere", "command-r7b-12-2024"): (0.0000375, 0.00015),
}
LEGACY_LONG_CONTEXT = {
    ("google", "gemini-2.5-pro"): (200_000, (0.0025, 0.015, 0.00025, 0.0025)),
    ("anthropic", "claude-sonnet-4-20250514"): (200_000, (0.006, 0.0225)),
}
LEGACY_AUDIO = {
    ("openai", "gpt-4o-audio-preview"): (0.040, 0.080),
    ("openai", "gpt-4o-mini-audio-preview"): (0.010, 0.020),
}


def _effective(vendor, rates):
    """(input, output, cache read, cache write, 1h write) as estimate_cost
    resolves them, multipliers included."""
    in_rate, out_rate = rates[0], rates[1]
    read = rates[2] if len(rates) > 2 else in_rate * CACHE_READ_MULTIPLIER.get(vendor, 1.0)
    write = rates[3] if len(rates) > 3 else in_rate * CACHE_WRITE_MULTIPLIER.get(vendor, 1.0)
    if len(rates) > 4:
        write_1h = rates[4]
    elif vendor in CACHE_WRITE_1H_MULTIPLIER:
        write_1h = in_rate * CACHE_WRITE_1H_MULTIPLIER[vendor]
    else:
        write_1h = write
    return (in_rate, out_rate, read, write, write_1h)


# --- #7: estimate_cost stays backward compatible ---------------------------


@pytest.mark.parametrize("key", sorted(LEGACY_DEFAULT_PRICING))
def test_legacy_default_rates_unchanged(key):
    assert key in DEFAULT_PRICING
    old = _effective(key[0], LEGACY_DEFAULT_PRICING[key])
    new = _effective(key[0], DEFAULT_PRICING[key])
    assert new == pytest.approx(old, rel=1e-12, abs=0)


@pytest.mark.parametrize("key", sorted(LEGACY_LONG_CONTEXT))
def test_legacy_long_context_bands_unchanged(key):
    threshold, rates = LEGACY_LONG_CONTEXT[key]
    assert LONG_CONTEXT_PRICING[key][0] == threshold
    assert _effective(key[0], LONG_CONTEXT_PRICING[key][1]) == pytest.approx(
        _effective(key[0], rates), rel=1e-12, abs=0
    )


def test_legacy_audio_rates_unchanged():
    for key, rates in LEGACY_AUDIO.items():
        assert AUDIO_PRICING[key] == pytest.approx(rates, rel=1e-12, abs=0)


USAGES = [
    {"tokens_in": 1000, "tokens_out": 500},
    {
        "tokens_in": 12000,
        "tokens_out": 800,
        "tokens_cached": 5000,
        "tokens_cache_write": 2000,
        "tokens_cache_write_1h": 500,
    },
    {"tokens_in": 250_000, "tokens_out": 4000, "tokens_cached": 100_000},
    {"tokens_in": 4000, "tokens_out": 4000, "service_tier": "batch"},
]


@pytest.mark.parametrize("key", sorted(LEGACY_DEFAULT_PRICING))
def test_legacy_costs_unchanged(key):
    vendor, model = key
    for usage in USAGES:
        usage = dict(usage)
        tokens_in = usage.pop("tokens_in")
        tokens_out = usage.pop("tokens_out")
        legacy_rates = LEGACY_DEFAULT_PRICING[key]
        band = LEGACY_LONG_CONTEXT.get(key)
        if band and tokens_in > band[0]:
            legacy_rates = band[1]
        expected = estimate_cost(
            vendor, model, tokens_in, tokens_out, {key: legacy_rates}, **usage
        )
        assert estimate_cost(vendor, model, tokens_in, tokens_out, **usage) == pytest.approx(
            expected, abs=1e-9
        ), usage


def test_estimate_cost_signature_and_custom_table_unchanged():
    assert estimate_cost("openai", "gpt-4o", 1000, 1000) == pytest.approx(0.0125)
    custom = {("acme", "m"): (0.001, 0.002)}
    assert estimate_cost("acme", "m", 1000, 1000, custom) == pytest.approx(0.003)
    assert estimate_cost("openai", "gpt-4o", 1000, 1000, custom) is None
    assert estimate_cost("openai", "no-such-model", 1000, 1000) is None


# --- #7: catalog content -----------------------------------------------------


def test_catalog_shape():
    data = catalog.load_catalog()
    assert data["schema_version"] == 1
    assert catalog.catalog_version() == data["catalog_version"]
    for key, entry in data["models"].items():
        assert key == f"{entry['provider']}/{entry['model']}"
        assert entry["provider"] in data["providers"]
        assert entry["currency"] in ("USD", "EUR")
        assert entry["mode"] == "chat"
        for field in catalog.SOVEREIGNTY_FIELDS:
            assert field in entry, (key, field)


def test_seeded_providers_and_sovereign_supply():
    providers = catalog.load_catalog()["providers"]
    for pid in ("openai", "anthropic", "google"):
        assert providers[pid]["jurisdiction"] == "US"
        assert providers[pid]["us_cloud_act_exposure"] is True
    for pid in ("berget", "mistral", "infercom", "regolo", "ionos", "stackit", "scaleway", "ovhcloud"):
        assert providers[pid]["jurisdiction"] == "EEA", pid
        assert providers[pid]["us_cloud_act_exposure"] is not True, pid
    assert providers["berget"]["hosting_countries"] == ["SE"]
    eu_models = {e["provider"] for e in catalog.iter_models(jurisdiction="EEA")}
    assert {"berget", "mistral", "regolo", "ionos", "stackit", "scaleway", "ovhcloud"} <= eu_models


@pytest.mark.parametrize(
    "provider, model",
    [
        ("openai", "gpt-5"),
        ("openai", "gpt-5-mini"),
        ("openai", "gpt-4.1"),
        ("anthropic", "claude-opus-4-7"),
        ("anthropic", "claude-sonnet-4-6"),
        ("anthropic", "claude-haiku-4-5"),
        ("google", "gemini-3.1-pro-preview"),
        ("google", "gemini-2.5-flash-lite"),
    ],
)
def test_current_frontier_models_are_priced(provider, model):
    assert catalog.get_model(provider, model) is not None
    assert estimate_cost(provider, model, 1000, 1000) > 0


def test_catalog_covers_cache_reasoning_long_context_batch_and_capabilities():
    sonnet = catalog.get_model("anthropic", "claude-sonnet-4-5")
    assert sonnet["cache_read_input_token_cost"] == pytest.approx(3e-7)
    assert sonnet["cache_creation_input_token_cost"] == pytest.approx(3.75e-6)
    assert sonnet["cache_creation_input_token_cost_above_1hr"] == pytest.approx(6e-6)
    assert sonnet["input_cost_per_token_above_200k_tokens"] == pytest.approx(6e-6)
    assert sonnet["input_cost_per_token_batches"] == pytest.approx(1.5e-6)
    flash = catalog.get_model("google", "gemini-2.5-flash")
    assert flash["output_cost_per_reasoning_token"] == flash["output_cost_per_token"]
    assert catalog.capabilities(sonnet) == {
        "tools": True,
        "json_schema": True,
        "vision": True,
        "reasoning": True,
        "prompt_caching": True,
        "max_input_tokens": 1_000_000,
    }
    # Above 200k prompt tokens the band applies: 250k x $6 + 1k x $22.50.
    assert estimate_cost("anthropic", "claude-sonnet-4-5", 250_000, 1000) == pytest.approx(1.5225)


def test_batch_and_flex_prices_match_sdk_tier_multipliers():
    """Where the SDK prices a tier with a multiplier, the catalog's explicit
    tier prices must agree with it (USD entries)."""
    for entry in catalog.iter_models(currency="USD"):
        tiers = SERVICE_TIER_MULTIPLIER.get(entry["provider"], {})
        for tier, suffix in (("batch", "batches"), ("flex", "flex")):
            if tier not in tiers:
                continue
            for side in ("input", "output"):
                field = f"{side}_cost_per_token_{suffix}"
                if field in entry:
                    assert entry[field] == pytest.approx(
                        entry[f"{side}_cost_per_token"] * tiers[tier], rel=1e-9
                    ), (entry["model"], field)


def test_reasoning_tokens_priced_at_output_rate():
    for entry in catalog.iter_models():
        if "output_cost_per_reasoning_token" in entry:
            assert entry["output_cost_per_reasoning_token"] == entry["output_cost_per_token"]


def test_non_usd_entries_stay_unpriced():
    berget = catalog.get_model("berget", "meta-llama/Llama-3.3-70B-Instruct")
    assert berget["currency"] == "EUR"
    assert berget["input_cost_per_token"] == pytest.approx(0.9e-6)
    assert ("berget", "meta-llama/Llama-3.3-70B-Instruct") not in DEFAULT_PRICING
    assert estimate_cost("berget", "meta-llama/Llama-3.3-70B-Instruct", 1000, 1000) is None


def test_model_level_sovereignty_override():
    source = json.loads(build_catalog.SOURCE.read_text())
    source = copy.deepcopy(source)
    source["models"]["bedrock/amazon.nova-pro-v1:0"]["hosting_countries"] = ["DE"]
    built = build_catalog.build_sdk_catalog(source)
    assert built["models"]["bedrock/amazon.nova-pro-v1:0"]["hosting_countries"] == ["DE"]
    assert built["models"]["bedrock/amazon.nova-lite-v1:0"]["hosting_countries"] == ["US"]


# --- #8: generator and drift check ------------------------------------------


def test_generated_files_are_up_to_date():
    """Fails when catalog.json or the gateway defaultPolicy.json drift from
    schemas/catalog/source.v1.json. Fix: python scripts/build_catalog.py"""
    source = json.loads(build_catalog.SOURCE.read_text())
    for path, text in build_catalog.outputs(source).items():
        assert path.read_text() == text, f"{path} is stale; run scripts/build_catalog.py"


def test_check_mode_fails_on_drift_and_regenerates(tmp_path, monkeypatch, capsys):
    sdk = tmp_path / "catalog.json"
    sdk.write_text(catalog.CATALOG_PATH.read_text())
    gateway = tmp_path / "defaultPolicy.json"
    gateway.write_text("{}\n")
    monkeypatch.setattr(build_catalog, "ROOT", tmp_path)
    monkeypatch.setattr(build_catalog, "SDK_CATALOG", sdk)
    monkeypatch.setattr(build_catalog, "GATEWAY_POLICY", gateway)

    assert build_catalog.main(["--check"]) == 1
    assert "defaultPolicy.json is out of date" in capsys.readouterr().err
    assert gateway.read_text() == "{}\n"  # --check never writes

    assert build_catalog.main([]) == 0
    assert build_catalog.main(["--check"]) == 0
    assert json.loads(gateway.read_text())["defaultGroup"] == "balanced"


def test_gateway_policy_prices_match_sdk_pricing():
    policy = json.loads(build_catalog.GATEWAY_POLICY.read_text())
    assert policy["catalogVersion"] == catalog.catalog_version()
    vendors = {
        e["model"]: e["provider"] for e in catalog.load_catalog()["models"].values()
    }
    for group, tiers in policy["groups"].items():
        for tier in tiers:
            assert tier["group"] == group
            rates = DEFAULT_PRICING[(vendors[tier["model"]], tier["model"])]
            assert tier["inputCostPer1k"] == rates[0]
            assert tier["outputCostPer1k"] == rates[1]


def _source():
    return copy.deepcopy(json.loads(build_catalog.SOURCE.read_text()))


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda s: s["providers"]["berget"].pop("zdr"), "missing zdr"),
        (lambda s: s["providers"]["berget"].update(jurisdiction="EU"), "jurisdiction"),
        (lambda s: s["providers"]["berget"].update(hosting_countries=["Sweden"]), "hosting_countries"),
        (
            lambda s: s["models"]["anthropic/claude-haiku-4-5"].pop("cache_read_input_token_cost"),
            "needs cache_read_input_token_cost",
        ),
        (lambda s: s["models"]["openai/gpt-4o"].pop("output_cost_per_token"), "go together"),
        (lambda s: s["models"]["openai/gpt-4o"].update(currency="GBP"), "currency"),
        (lambda s: s["models"]["openai/gpt-4o"].update(input_cost_per_token=-1), ">= 0"),
        (lambda s: s["models"]["openai/gpt-4o"].update(price=1), "unknown fields"),
        (lambda s: s["models"].update({"nobody/x": {"mode": "chat", "source": "x"}}), "unknown provider"),
        (lambda s: s["router"]["groups"]["cheap"].append("openai/nope"), "unknown model"),
        (
            lambda s: s["router"]["groups"]["cheap"].append("berget/openai/gpt-oss-120b"),
            "needs USD prices",
        ),
    ],
)
def test_validation_rejects_bad_source(mutate, message):
    source = _source()
    mutate(source)
    with pytest.raises(build_catalog.CatalogError, match=message):
        build_catalog.validate(source)
