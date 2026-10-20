"""The prompt-shields Kong plugin (integrations/kong) posts a cut-down
http-log entry to POST /ingest/kong. Its Lua unit tests check that the
plugin builds exactly integrations/kong/unit/contract/event-3.14.json; this
test checks that the collector maps that same file, so a change on either
side that breaks the other fails here or there."""

from __future__ import annotations

import json
from pathlib import Path

from collector.kong_ingest import KONG_SOURCE, map_payload

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "integrations" / "kong" / "unit" / "contract" / "event-3.14.json"
)


def test_plugin_event_maps_to_one_complete_usage_event():
    entry = json.loads(CONTRACT.read_text())
    # The plugin always sends an array (its queue batches).
    mapped = map_payload([entry])

    assert mapped.rejected == {}
    assert mapped.ignored == {}
    (event,) = mapped.events
    assert event.source == KONG_SOURCE
    assert (event.vendor, event.model) == ("openai", "gpt-4o")
    assert (event.requested_model, event.served_model) == ("gpt-4o", "gpt-4o-2024-08-06")
    assert (event.tokens_in, event.tokens_out) == (120, 40)
    assert event.cost == 0.0007
    assert event.cost_source == "kong_model_options"
    assert event.latency_ms == 790  # meta.llm_latency wins over latencies.proxy
    assert event.calling_service == "payments-api"
    assert (
        event.business_unit,
        event.use_case_name,
        event.owner_email,
        event.data_classification,
        event.environment,
    ) == ("Payments", "fraud-explainer", "owner@example.com", "confidential", "production")


def test_plugin_event_has_only_fields_the_collector_reads():
    entry = json.loads(CONTRACT.read_text())
    assert set(entry) == {
        "ai", "consumer", "route", "service", "latencies", "request", "prompt_shields",
    }
    assert set(entry["request"]) == {"headers"}
    assert all(name.startswith("x-ps-") for name in entry["request"]["headers"])
