"""Tests for ShieldsClient — sync, OpenAI vendor.

These exercise pure event construction without making any network calls.
Upstream provider clients are constructed but never invoked.
"""
from unittest.mock import MagicMock

import pytest

from prompt_shields import (
    ShieldsAnthropic,
    ShieldsClient,
    ShieldsOpenAI,
)


# --- construction ---------------------------------------------------------


def test_default_vendor_is_openai():
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test")
    assert client._vendor == "openai"


def test_typed_subclass_sets_vendor():
    openai_client = ShieldsOpenAI(api_key="sk-test", ps_api_key="ps-test")
    assert openai_client._vendor == "openai"
    anthropic_client = ShieldsAnthropic(api_key="sk-test", ps_api_key="ps-test")
    assert anthropic_client._vendor == "anthropic"


def test_unknown_vendor_raises():
    with pytest.raises(ValueError):
        ShieldsClient(api_key="sk", ps_api_key="ps", vendor="nonexistent")


def test_metadata_filters_none():
    client = ShieldsClient(
        api_key="sk-test", ps_api_key="ps-test",
        business_unit="HR",
    )
    assert client._metadata == {"business_unit": "HR"}


def test_metadata_includes_calling_service():
    client = ShieldsClient(
        api_key="sk-test", ps_api_key="ps-test",
        calling_service="hiring-service",
    )
    assert client._metadata["calling_service"] == "hiring-service"


def test_api_key_is_fingerprinted_not_stored():
    client = ShieldsClient(api_key="sk-very-secret-key", ps_api_key="ps-test")
    assert "sk-very-secret-key" not in client._api_key_fingerprint
    assert len(client._api_key_fingerprint) == 16


def test_same_key_produces_same_fingerprint():
    a = ShieldsClient(api_key="sk-shared", ps_api_key="ps-test")
    b = ShieldsClient(api_key="sk-shared", ps_api_key="ps-test")
    assert a._api_key_fingerprint == b._api_key_fingerprint


# --- event construction ---------------------------------------------------


def _mock_openai_response(prompt_tokens=10, completion_tokens=20):
    resp = MagicMock()
    resp.usage.prompt_tokens = prompt_tokens
    resp.usage.completion_tokens = completion_tokens
    resp.choices = []
    return resp


def test_build_event_maps_field_names():
    client = ShieldsClient(
        api_key="sk-test", ps_api_key="ps-test",
        use_case="screening", owner="jane@test.com", business_unit="HR",
    )
    event = client._build_event(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hello"}],
        response=_mock_openai_response(),
        latency_ms=100,
    )
    assert event["use_case_name"] == "screening"
    assert event["owner_email"] == "jane@test.com"
    assert event["business_unit"] == "HR"
    assert "use_case" not in event
    assert "owner" not in event


def test_build_event_includes_fingerprint():
    client = ShieldsClient(api_key="sk-secret", ps_api_key="ps-test")
    event = client._build_event(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}],
        response=_mock_openai_response(), latency_ms=50,
    )
    assert "api_key_fingerprint" in event
    assert event["api_key_fingerprint"] == client._api_key_fingerprint


def test_build_event_calculates_cost_for_known_model():
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test")
    event = client._build_event(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        response=_mock_openai_response(prompt_tokens=1000, completion_tokens=2000),
        latency_ms=10,
    )
    # 1000 * 0.0025/1k + 2000 * 0.010/1k = 0.0025 + 0.020 = 0.0225
    assert event["cost"] == 0.0225


def test_build_event_cost_none_for_unknown_model():
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test")
    event = client._build_event(
        model="gpt-99-future",
        messages=[{"role": "user", "content": "hi"}],
        response=_mock_openai_response(),
        latency_ms=10,
    )
    assert event["cost"] is None


def test_build_event_wires_ps_metadata():
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test")
    event = client._build_event(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        response=_mock_openai_response(),
        latency_ms=10,
        ps_metadata={
            "data_sources": ["candidates_db", "job_descriptions_api"],
            "output_destination": "hiring_dashboard",
            "risk_tags": ["pii", "gdpr"],
            "session_id": "session-xyz",
        },
    )
    assert event["data_sources"] == ["candidates_db", "job_descriptions_api"]
    assert event["output_destination"] == "hiring_dashboard"
    assert event["risk_tags"] == ["pii", "gdpr"]
    assert event["session_id"] == "session-xyz"


def test_build_event_pii_detection_default_on():
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test")
    event = client._build_event(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Email me at jane@acme.com"}],
        response=_mock_openai_response(),
        latency_ms=10,
    )
    assert event.get("detected_pii_types") == ["email"]


def test_build_event_pii_detection_can_be_disabled():
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test", scan_pii=False)
    event = client._build_event(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Email jane@acme.com"}],
        response=_mock_openai_response(),
        latency_ms=10,
    )
    assert "detected_pii_types" not in event


def test_build_event_no_prompt_text_by_default():
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test")
    event = client._build_event(
        model="gpt-4o",
        messages=[{"role": "user", "content": "secret prompt"}],
        response=_mock_openai_response(),
        latency_ms=10,
    )
    assert "prompt_text" not in event


def test_build_event_prompt_text_opt_in():
    client = ShieldsClient(
        api_key="sk-test", ps_api_key="ps-test",
        send_prompt_text=True,
    )
    event = client._build_event(
        model="gpt-4o",
        messages=[{"role": "user", "content": "explicit opt-in"}],
        response=_mock_openai_response(),
        latency_ms=10,
    )
    assert event["prompt_text"] == "explicit opt-in"


def test_build_event_anthropic_vendor():
    client = ShieldsAnthropic(api_key="sk-ant", ps_api_key="ps-test")
    response = MagicMock()
    response.usage.input_tokens = 50
    response.usage.output_tokens = 100
    response.content = []

    event = client._build_event(
        model="claude-sonnet-4-20250514",
        messages=[{"role": "user", "content": "hi"}],
        response=response,
        latency_ms=10,
    )
    assert event["vendor"] == "anthropic"
    assert event["tokens_in"] == 50
    assert event["tokens_out"] == 100
    # 50 * 0.003/1k + 100 * 0.015/1k = 0.00015 + 0.0015 = 0.00165
    assert event["cost"] == 0.00165
