"""Kong http-log entries mapped to ingest events (collector/kong_ingest.py).

Fixtures in tests/fixtures/kong/, one per Kong log shape:

* ``kong-3.6-ai-proxy.json``: analytics keyed by plugin name
  (``ai["ai-proxy"]``, ``ai["ai-request-transformer"]``) with meta and
  token usage only, as ``kong/llm/drivers/shared.lua`` writes them
  (``ai.<plugin_name>.meta|usage|payload``) and changelog/3.6.0 describes.
  ``max_batch_size: 1``, so a single object.
* ``kong-3.9-ai-proxy-batch.json``: the ``ai.proxy`` namespace from
  ``kong/llm/plugin/shared-filters/serialize-analytics.lua`` (Kong master,
  the last OSS line is 3.9), with the stats of Kong's own
  ``spec/03-plugins/38-ai-proxy/02-openai_integration_spec.lua``, Azure's
  extra meta (``kong/llm/drivers/azure.lua``), a prompt-template payload
  and a non-AI entry. An array, as http-log sends with a batch size > 1.
* ``kong-3.14-ai-proxy-advanced-batch.json``: an Enterprise ai-proxy-advanced
  shape. ``request_mode``, ``time_to_first_token``, ``tried_targets`` and the
  cache fields come from Kong's docs, not source (Enterprise code is not
  public), so they are unverified; the mapper must ignore them either way.

The request envelope (request/response/route/service/consumer/latencies/
workspace) is ``kong.log.serialize()`` in ``kong/pdk/log.lua``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from collector.dedup import compute_confidence
from collector.kong_ingest import KONG_SOURCE, map_entry, map_payload
from collector.loss_accounting import source_of

FIXTURES = Path(__file__).parent / "fixtures" / "kong"
SECRETS = ("SECRETKEY", "123-45-6789", "10.20.30.40", "10.1.2.3", "2001:db8::42")


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


def _fields(event) -> dict:
    return {k: v for k, v in event.model_dump().items() if v is not None}


def test_kong_3_6_plugin_named_namespaces():
    mapped = map_entry(_load("kong-3.6-ai-proxy.json"))
    assert mapped.rejected == {} and mapped.ignored == {}
    proxy, transformer = mapped.events
    assert _fields(proxy) == {
        "vendor": "openai",
        "model": "gpt-4",
        "requested_model": "gpt-4",
        "served_model": "gpt-4-0613",
        "source": "kong",
        "tokens_in": 120,
        "tokens_out": 40,
        # No llm_latency before 3.8: Kong's upstream time instead.
        "latency_ms": 812,
        "calling_service": "hr-screener",
        "business_unit": "Human Resources",
        "use_case_name": "cv-screening",
        "owner_email": "hr-platform@bank.example",
        "data_classification": "confidential",
        "environment": "production",
    }
    # The transformer's own LLM call is a second event for the same request.
    assert (transformer.vendor, transformer.model, transformer.tokens_in) == (
        "anthropic", "claude-2.1", 30
    )
    assert transformer.business_unit == "Human Resources"


def test_kong_3_9_proxy_namespace_batch():
    mapped = map_payload(_load("kong-3.9-ai-proxy-batch.json"))
    assert mapped.ignored == {"entry:non_ai": 1}
    assert mapped.rejected == {}
    openai, azure = mapped.events
    assert _fields(openai) == {
        "vendor": "openai",
        "model": "gpt-3.5-turbo",
        "requested_model": "gpt-3.5-turbo",
        "served_model": "gpt-3.5-turbo-0613",
        "source": "kong",
        "tokens_in": 25,
        "tokens_out": 12,
        "latency_ms": 618,
        "cost": 0.00037,
        "cost_source": "kong_model_options",
        "calling_service": "support-bot",
        "business_unit": "Retail Banking",
        "use_case_name": "customer-support",
        "environment": "production",
    }
    # Kong's "azure" is the SDK's azure_openai; no consumer: the route names
    # the caller. Azure's extra meta (instance, deployment) is not stored.
    assert _fields(azure) == {
        "vendor": "azure_openai",
        "model": "gpt-4o",
        "requested_model": "gpt-4o",
        "served_model": "gpt-4o-2024-05-13",
        "source": "kong",
        "tokens_in": 400,
        "tokens_out": 100,
        "latency_ms": 870,
        "calling_service": "payments-assistant",
    }


def test_kong_3_14_enterprise_shape():
    mapped = map_payload(_load("kong-3.14-ai-proxy-advanced-batch.json"))
    assert mapped.rejected == {"no_vendor": 1}
    assert mapped.ignored == {}
    bedrock, gemini = mapped.events
    assert _fields(bedrock) == {
        "vendor": "bedrock",
        "model": "anthropic.claude-sonnet-4-20250514-v1:0",
        "requested_model": "anthropic.claude-sonnet-4-20250514-v1:0",
        "served_model": "anthropic.claude-sonnet-4-20250514-v1:0",
        "source": "kong",
        "tokens_in": 1200,
        "tokens_out": 300,
        "latency_ms": 2250,
        "cost": 0.0081,
        "cost_source": "kong_model_options",
        # consumer.custom_id when the consumer has no username
        "calling_service": "payments-fraud-explainer",
        # first value of a repeated header; header names in any case
        "business_unit": "Payments",
        "owner_email": "payments-ai@bank.example",
        "data_classification": "restricted",
    }
    # UNSPECIFIED is Kong's placeholder, not a model; proxy latency -1 means
    # no upstream and is dropped without being counted as invalid.
    assert _fields(gemini) == {
        "vendor": "google",
        "model": "gemini-2.5-flash",
        "requested_model": "gemini-2.5-flash",
        "source": "kong",
        "tokens_in": 0,
        "tokens_out": 0,
        "calling_service": "gemini-sandbox",
        "environment": "staging",
    }


@pytest.mark.parametrize(
    "name",
    [
        "kong-3.6-ai-proxy.json",
        "kong-3.9-ai-proxy-batch.json",
        "kong-3.14-ai-proxy-advanced-batch.json",
    ],
)
def test_bodies_headers_and_client_ip_never_reach_an_event(name):
    payload = _load(name)
    raw = json.dumps(payload)
    assert all(s in raw for s in ("SECRETKEY", "123-45-6789"))  # fixture sanity
    for event in map_payload(payload).events:
        dumped = json.dumps(event.model_dump())
        for secret in SECRETS:
            assert secret not in dumped
        assert event.prompt_text is None
        assert event.tool_calls_used is None


def test_only_contract_headers_are_read():
    entry = {
        "request": {
            "headers": {
                "x-ps-route": "cheapest",
                "x-ps-unknown": "x",
                "authorization": "Bearer sk-live-SECRETKEY",
                "x-ps-use-case": "a" * 257,  # too long: dropped, not truncated
                "x-ps-business-unit": "  ",
            }
        },
        "ai": {"proxy": {"meta": {"provider_name": "openai", "request_model": "m"}}},
    }
    (event,) = map_entry(entry).events
    assert event.use_case_name is None and event.business_unit is None
    assert "SECRETKEY" not in json.dumps(event.model_dump())


def test_malformed_values_are_dropped_and_counted():
    entry = {
        "ai": {
            "proxy": {
                "meta": {
                    "provider_name": "OpenAI",
                    "request_model": "gpt-4o",
                    "llm_latency": "slow",
                },
                "usage": {"prompt_token": 7, "completion_tokens": -3, "cost": "free"},
            },
            "ai-response-transformer": {"meta": {"provider_name": 42}},
            "ai-prompt-guard": "not a table",
        },
        "consumer": {"username": ["not", "a", "string"]},
        "route": {"name": "fallback-route"},
    }
    mapped = map_entry(entry)
    (event,) = mapped.events
    assert (event.vendor, event.tokens_in, event.tokens_out, event.cost) == (
        "openai", 7, None, None
    )
    assert event.calling_service == "fallback-route"
    assert mapped.rejected == {"no_vendor": 1}
    assert mapped.ignored == {
        "tokens_out:invalid": 1,
        "cost:invalid": 1,
        "latency_ms:invalid": 1,
    }


@pytest.mark.parametrize("bad", ["gpt\u00004o", "gpt\ud8004o"])
def test_strings_postgres_cannot_store_are_dropped(bad):
    # NUL and lone surrogates would fail the batch's write (500, Kong retries
    # the whole batch until it drops it); they are dropped like any bad value.
    entry = {
        "request": {"headers": {"x-ps-use-case": bad}},
        "consumer": {"username": bad},
        "route": {"name": "r"},
        "ai": {
            "proxy": {"meta": {"provider_name": "openai", "request_model": bad}},
            "ai-request-transformer": {"meta": {"provider_name": bad}},
        },
    }
    mapped = map_entry(entry)
    (event,) = mapped.events
    assert (event.model, event.requested_model, event.use_case_name) == (None, None, None)
    assert event.calling_service == "r"
    assert mapped.rejected == {"no_vendor": 1}


@pytest.mark.parametrize("entry", [{}, {"ai": None}, {"ai": {"payload": {"request": "x"}}}])
def test_entries_without_ai_analytics_are_ignored(entry):
    mapped = map_entry(entry)
    assert mapped.events == [] and mapped.ignored == {"entry:non_ai": 1}


def test_non_object_entries_are_rejected():
    mapped = map_payload([1, "x", None])
    assert mapped.events == [] and mapped.rejected == {"not_an_object": 3}


def test_kong_is_a_high_confidence_source_with_its_own_coverage():
    assert compute_confidence([KONG_SOURCE]) == "high"
    assert compute_confidence([KONG_SOURCE, "sdk"]) == "verified"
    assert source_of("kong", "kong") == "kong"
