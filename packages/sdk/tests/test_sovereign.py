"""Named OpenAI-compatible (EU sovereign) providers in ShieldsRouter (#20).

The end-to-end tests run the real OpenAI SDK against a local stub server
that speaks the chat-completions API; telemetry events are captured instead
of sent. No network beyond 127.0.0.1.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from prompt_shields import PolicyViolation, RouterError, ShieldsRouter
from prompt_shields.catalog import load_catalog
from prompt_shields.route_policy import tier_complies
from prompt_shields.sovereign import (
    OPENAI_COMPATIBLE_PROVIDERS,
    SovereignProviderError,
    catalog_tier,
    provider_config,
    resolve_base_url,
)

ROOT = Path(__file__).resolve().parents[3]
EU = ("mistral", "berget", "infercom", "regolo", "ionos", "stackit",
      "scaleway", "ovhcloud")
HI = [{"role": "user", "content": "hi"}]


# --- registry ------------------------------------------------------------------


def test_registry_lists_the_eu_providers_from_the_catalog():
    assert set(EU) <= set(OPENAI_COMPATIBLE_PROVIDERS)
    assert "openai" not in OPENAI_COMPATIBLE_PROVIDERS
    assert "anthropic" not in OPENAI_COMPATIBLE_PROVIDERS
    source = json.loads((ROOT / "schemas/catalog/source.v1.json").read_text())
    for name, p in OPENAI_COMPATIBLE_PROVIDERS.items():
        assert p.base_url == source["providers"][name]["api_base"]
        assert p.base_url.startswith("https://")
        assert p.auth == "bearer"
    assert OPENAI_COMPATIBLE_PROVIDERS["scaleway"].jurisdiction == "EEA"
    assert OPENAI_COMPATIBLE_PROVIDERS["infercom"].base_url == "https://api.infercom.ai/v1"


def test_region_selects_only_a_catalogued_base_url():
    assert resolve_base_url("ionos", "de-txl") == (
        "https://openai.inference.de-txl.ionos.com/v1"
    )
    with pytest.raises(SovereignProviderError, match="region 'es-mad'"):
        resolve_base_url("ionos", "es-mad")
    with pytest.raises(SovereignProviderError, match="unknown"):
        resolve_base_url("nope")


def test_provider_config_builds_a_router_entry():
    assert provider_config("scaleway", "k", timeout=5) == {
        "api_key": "k",
        "vendor": "openai",
        "base_url": "https://api.scaleway.ai/v1",
        "timeout": 5,
    }


def test_catalog_tier_copies_verified_facts_and_leaves_nulls_out():
    tier = catalog_tier("scaleway", "gpt-oss-120b",
                        input_cost_per_1k=0.0002, output_cost_per_1k=0.0007)
    assert tier == {
        "model": "gpt-oss-120b",
        "provider": "scaleway",
        "input_cost_per_1k": 0.0002,
        "output_cost_per_1k": 0.0007,
        "jurisdiction": "EEA",
        "us_cloud_act_exposure": False,
    }  # zdr is unverified (null) in the catalog: left out, fails closed
    assert not tier_complies(tier, {"jurisdictions": ["EEA"], "require_zdr": True})
    assert tier_complies(tier, {"jurisdictions": ["EEA"],
                                "us_cloud_act_exposure": False})
    # Mistral lists USD prices, so they are taken from the catalog.
    mistral = catalog_tier("mistral", "mistral-small-latest")
    entry = load_catalog()["models"]["mistral/mistral-small-latest"]
    assert mistral["input_cost_per_1k"] == round(entry["input_cost_per_token"] * 1000, 12)
    assert "us_cloud_act_exposure" not in mistral  # null in the catalog
    with pytest.raises(SovereignProviderError, match="USD"):
        catalog_tier("scaleway", "gpt-oss-120b")  # EUR list price
    with pytest.raises(SovereignProviderError, match="not in the catalog"):
        catalog_tier("scaleway", "nope")


# --- router configuration ---------------------------------------------------------


def _policy(*tiers, data_policy=None) -> dict:
    doc = {
        "schema_version": "route-policy.v1",
        "policy_version": "eu-1",
        "default_group": "balanced",
        "quality_to_group": {"draft": "balanced", "balanced": "balanced",
                             "critical": "balanced"},
        "groups": {"balanced": list(tiers)},
    }
    if data_policy:
        doc["sensitivity"] = {
            "levels": ["public", "confidential"],
            "unclassified_default": "confidential",
        }
        doc["data_policy"] = data_policy
    return doc


US_TIER = {"model": "gpt-4o-mini", "provider": "openai",
           "input_cost_per_1k": 0.00015, "output_cost_per_1k": 0.0006,
           "jurisdiction": "US", "us_cloud_act_exposure": True}


def test_named_provider_gets_its_catalog_base_url():
    tier = catalog_tier("scaleway", "gpt-oss-120b",
                        input_cost_per_1k=0.0002, output_cost_per_1k=0.0007)
    r = ShieldsRouter(ps_api_key="ps", policy=_policy(tier),
                      providers={"scaleway": {"api_key": "k"}})
    p = r._providers["scaleway"]
    assert p.options["base_url"] == "https://api.scaleway.ai/v1"
    assert p.catalog == "scaleway"
    r = ShieldsRouter(
        ps_api_key="ps", policy=_policy({**tier, "provider": "eu"}),
        providers={"eu": {"api_key": "k", "catalog_provider": "ionos",
                          "region": "de-txl"}},
    )
    assert r._providers["eu"].options["base_url"].startswith(
        "https://openai.inference.de-txl.ionos.com"
    )


@pytest.mark.parametrize("config, match", [
    # Unknown name and no base_url: never send this key to api.openai.com.
    ({"other": {"api_key": "k"}}, "not a known OpenAI-compatible"),
    ({"scaleway": {"api_key": "k", "region": "nl-ams"}}, "region"),
    ({"scaleway": {"api_key": "k", "base_url": "https://x/v1", "region": "fr-par"}},
     "cannot be combined"),
    ({"x": {"api_key": "k", "catalog_provider": "nope"}}, "unknown"),
])
def test_bad_named_provider_config_is_rejected(config, match):
    with pytest.raises(RouterError, match=match):
        ShieldsRouter(ps_api_key="ps", policy=_policy(US_TIER), providers=config)


# --- end to end against a stub OpenAI-compatible server ------------------------------


class _Stub:
    """A local OpenAI-compatible server recording what it receives."""

    def __init__(self, usage: dict):
        self.requests: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                stub.requests.append({
                    "path": self.path,
                    "authorization": self.headers.get("authorization"),
                    "body": body,
                })
                payload = json.dumps({
                    "id": "cmpl-1", "object": "chat.completion",
                    "created": 1700000000, "model": body["model"],
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": "hej"}}],
                    "usage": usage,
                }).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def no_proxy(monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)


def _capture(router, name):
    events: list[dict] = []
    client = router._client(name)
    client._telemetry.enqueue = events.append
    client._telemetry.flush_sync = lambda: None
    return events


SOVEREIGN_POLICY = {"public": {"jurisdictions": ["any"]},
                    "confidential": {"jurisdictions": ["EEA"],
                                     "us_cloud_act_exposure": False}}


def test_confidential_call_is_served_by_the_sovereign_provider(no_proxy):
    stub = _Stub({"prompt_tokens": 100, "completion_tokens": 20,
                  "total_tokens": 120,
                  "prompt_tokens_details": {"cached_tokens": 40}})
    try:
        eu_tier = catalog_tier("scaleway", "gpt-oss-120b",
                               input_cost_per_1k=0.0002, output_cost_per_1k=0.0007)
        r = ShieldsRouter(
            ps_api_key="ps",
            policy=_policy(US_TIER, eu_tier, data_policy=SOVEREIGN_POLICY),
            providers={
                "openai": {"api_key": "sk-us"},
                # A test stands in for api.scaleway.ai; the catalog facts apply.
                "scaleway": {"api_key": "scw-key", "base_url": stub.url},
            },
        )
        events = _capture(r, "scaleway")
        resp = r.chat.completions.create(messages=HI, data_classification="confidential")
    finally:
        stub.close()

    (req,) = stub.requests
    assert req["path"] == "/v1/chat/completions"
    assert req["authorization"] == "Bearer scw-key"
    assert req["body"]["model"] == "gpt-oss-120b"
    assert resp.choices[0].message.content == "hej"
    assert resp.ps.route.provider == "scaleway"
    assert resp.ps.route.jurisdiction == "EEA"
    (event,) = events
    assert event["served_provider"] == "scaleway"
    assert event["served_jurisdiction"] == "EEA"
    assert event["sensitivity_level"] == "confidential"
    assert (event["tokens_in"], event["tokens_out"], event["tokens_cached"]) == (100, 20, 40)
    # EUR list price: no USD catalog rate, so the tier's USD rates price it.
    assert event["cost_source"] == "route_policy"
    assert event["cost"] == pytest.approx(100 / 1000 * 0.0002 + 20 / 1000 * 0.0007)


def test_unverified_fact_fails_closed_and_sends_nothing(no_proxy):
    stub = _Stub({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
    try:
        tier = catalog_tier("scaleway", "gpt-oss-120b",
                            input_cost_per_1k=0.0002, output_cost_per_1k=0.0007)
        r = ShieldsRouter(
            ps_api_key="ps",
            policy=_policy(US_TIER, tier, data_policy={
                "public": {"jurisdictions": ["any"]},
                # zdr is null in the catalog, so no tier can meet this.
                "confidential": {"jurisdictions": ["EEA"], "require_zdr": True},
            }),
            providers={"openai": {"api_key": "sk"},
                       "scaleway": {"api_key": "k", "base_url": stub.url}},
        )
        with pytest.raises(PolicyViolation):
            r.chat.completions.create(messages=HI, data_classification="confidential")
    finally:
        stub.close()
    assert stub.requests == []


def test_openai_price_never_applies_to_a_compatible_providers_model(no_proxy):
    # The provider serves a model id that OpenAI's table also prices; it is
    # dispatched as vendor "openai", but OpenAI's rate must not apply.
    stub = _Stub({"prompt_tokens": 1000, "completion_tokens": 1000,
                  "total_tokens": 2000})
    try:
        tier = {"model": "gpt-4o-mini", "provider": "scaleway",
                "input_cost_per_1k": 0.5, "output_cost_per_1k": 0.25}
        r = ShieldsRouter(ps_api_key="ps", policy=_policy(tier),
                          providers={"scaleway": {"api_key": "k", "base_url": stub.url}})
        events = _capture(r, "scaleway")
        r.chat.completions.create(messages=HI)
    finally:
        stub.close()
    (event,) = events
    assert event["cost"] == pytest.approx(0.75)
    assert event["cost_source"] == "route_policy"


def test_catalog_usd_rate_prices_a_compatible_provider(no_proxy):
    # Mistral lists USD prices in the catalog: the call is priced at them.
    stub = _Stub({"prompt_tokens": 1000, "completion_tokens": 1000,
                  "total_tokens": 2000})
    try:
        tier = catalog_tier("mistral", "mistral-small-latest")
        r = ShieldsRouter(
            ps_api_key="ps", policy=_policy(tier),
            providers={"mistral": {"api_key": "k", "base_url": stub.url}},
        )
        events = _capture(r, "mistral")
        resp = r.chat.completions.create(messages=HI, model="mistral-small-latest")
    finally:
        stub.close()
    (event,) = events
    entry = load_catalog()["models"]["mistral/mistral-small-latest"]
    expected = 1000 * entry["input_cost_per_token"] + 1000 * entry["output_cost_per_token"]
    assert event["cost"] == pytest.approx(expected, rel=1e-6)
    assert event["cost_source"] == "pricing_table"
    assert event["served_provider"] == "mistral"
    assert event["served_jurisdiction"] == "EEA"
    assert resp.ps.cost.usd == event["cost"]


def test_provider_reported_cost_is_kept(no_proxy):
    stub = _Stub({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                  "cost": 0.0042})
    try:
        tier = catalog_tier("regolo", "gpt-oss-120b",
                            input_cost_per_1k=0.001, output_cost_per_1k=0.001)
        r = ShieldsRouter(ps_api_key="ps", policy=_policy(tier),
                          providers={"regolo": {"api_key": "k", "base_url": stub.url}})
        events = _capture(r, "regolo")
        r.chat.completions.create(messages=HI)
    finally:
        stub.close()
    (event,) = events
    assert event["cost"] == 0.0042
    assert event["cost_source"] == "provider_reported"
