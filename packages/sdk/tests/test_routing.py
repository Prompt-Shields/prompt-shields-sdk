"""Tests for SDK-side route hints (RouteHint).

The SDK never routes; it only emits *intent* as X-PS-* headers that the
gateway acts on. These tests exercise header emission and the telemetry
fields that let the collector prove requested-vs-served savings.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from prompt_shields import AsyncShieldsClient, RouteHint, ShieldsClient


# --- RouteHint.to_headers -------------------------------------------------


def test_empty_hint_emits_no_headers():
    assert RouteHint().to_headers() == {}


def test_quality_maps_to_header():
    assert RouteHint(quality="draft").to_headers() == {"X-PS-Quality": "draft"}


def test_max_cost_maps_to_header():
    headers = RouteHint(max_cost=0.02).to_headers()
    assert headers["X-PS-Max-Cost"] == "0.02"


def test_model_group_maps_to_route_override_header():
    assert RouteHint(model_group="frontier").to_headers() == {
        "X-PS-Route": "frontier"
    }


def test_allow_cache_true_is_default_and_emits_nothing():
    assert "X-PS-Cache" not in RouteHint().to_headers()
    assert "X-PS-Cache" not in RouteHint(allow_cache=True).to_headers()


def test_allow_cache_false_emits_off_header():
    assert RouteHint(allow_cache=False).to_headers()["X-PS-Cache"] == "off"


def test_multiple_hints_combine():
    headers = RouteHint(quality="critical", max_cost=0.5).to_headers()
    assert headers == {"X-PS-Quality": "critical", "X-PS-Max-Cost": "0.5"}


# --- telemetry: requested vs served model ---------------------------------


def _mock_openai_response(model="gpt-4o-mini", prompt_tokens=10, completion_tokens=20):
    resp = MagicMock()
    resp.model = model
    resp.usage.prompt_tokens = prompt_tokens
    resp.usage.completion_tokens = completion_tokens
    resp.choices = []
    return resp


def test_event_records_requested_and_served_model():
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test")
    event = client._build_event(
        model="auto",
        messages=[{"role": "user", "content": "hi"}],
        response=_mock_openai_response(model="gpt-4o-mini"),
        latency_ms=10,
    )
    assert event["requested_model"] == "auto"
    assert event["served_model"] == "gpt-4o-mini"


def test_served_model_falls_back_to_requested_when_absent():
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test")
    resp = MagicMock(spec=[])  # no .model attribute
    resp_usage = MagicMock()
    event = client._build_event(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        response=_mock_openai_response(model=None),
        latency_ms=10,
    )
    # model=None on response → served falls back to the requested model
    assert event["served_model"] == "gpt-4o"


# --- create() wires hints into upstream extra_headers ---------------------


def _client_with_mock_upstream(vendor="openai"):
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test", vendor=vendor)
    client._upstream = MagicMock()
    if vendor == "openai":
        client._upstream.chat.completions.create.return_value = _mock_openai_response()
    # Telemetry is fail-open; stub the network flush so tests stay offline.
    client._telemetry = MagicMock()
    return client


def test_create_forwards_route_headers_to_openai_upstream():
    client = _client_with_mock_upstream()
    client.chat.completions.create(
        model="auto",
        messages=[{"role": "user", "content": "hi"}],
        route=RouteHint(quality="draft", max_cost=0.01),
    )
    _, kwargs = client._upstream.chat.completions.create.call_args
    assert kwargs["extra_headers"]["X-PS-Quality"] == "draft"
    assert kwargs["extra_headers"]["X-PS-Max-Cost"] == "0.01"


def test_create_merges_route_headers_with_caller_headers():
    client = _client_with_mock_upstream()
    client.chat.completions.create(
        model="auto",
        messages=[{"role": "user", "content": "hi"}],
        route=RouteHint(model_group="frontier"),
        extra_headers={"X-Trace-Id": "abc123"},
    )
    _, kwargs = client._upstream.chat.completions.create.call_args
    assert kwargs["extra_headers"]["X-Trace-Id"] == "abc123"
    assert kwargs["extra_headers"]["X-PS-Route"] == "frontier"


def test_create_without_route_sends_no_ps_routing_headers():
    client = _client_with_mock_upstream()
    client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
    )
    _, kwargs = client._upstream.chat.completions.create.call_args
    assert not any(
        k.lower().startswith("x-ps-") for k in kwargs.get("extra_headers", {})
    )


@pytest.mark.asyncio
async def test_async_create_forwards_route_headers():
    client = AsyncShieldsClient(api_key="sk-test", ps_api_key="ps-test")
    client._upstream = MagicMock()
    client._upstream.chat.completions.create = AsyncMock(
        return_value=_mock_openai_response()
    )
    client._telemetry = MagicMock()
    client._telemetry.flush = AsyncMock()

    await client.chat.completions.create(
        model="auto",
        messages=[{"role": "user", "content": "hi"}],
        route=RouteHint(quality="balanced"),
    )
    _, kwargs = client._upstream.chat.completions.create.call_args
    assert kwargs["extra_headers"]["X-PS-Quality"] == "balanced"
