"""Payload logging (#352) without a database: redaction, the tenant setting
parser, sampling, route matching and per-tenant sealing."""

from __future__ import annotations

import time
import uuid

import pytest
from collector import payload_logging, payload_policy
from collector.config import settings
from collector.payload_redaction import find_spans, luhn_ok, redact_text, redact_value


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mail jane.doe+x@example.co.uk now", "mail [EMAIL] now"),
        ("card 4111111111111111.", "card [CARD]."),
        ("card 4111 1111 1111 1111!", "card [CARD]!"),
        ("card 4111-1111-1111-1111", "card [CARD]"),
        ("Ref 12 4111 1111 1111 1111", "Ref 12 [CARD]"),
        ("amex 378282246310005", "amex [CARD]"),
        ("iban SE45 5000 0000 0583 9825 7466", "iban [IBAN]"),
        ("ssn 123-45-6789", "ssn [SSN]"),
        ("call (415) 555-0100 or +46 70 174 06 12", "call [PHONE] or [PHONE]"),
        ("from 10.0.0.12", "from [IP_ADDRESS]"),
        # Not PII: left alone.
        ("version 1.2.3.4567 and 999.1.1.1", "version 1.2.3.4567 and 999.1.1.1"),
        ("order 4111111111111112", "order 4111111111111112"),  # fails Luhn
        ("tokens: 1234", "tokens: 1234"),
    ],
)
def test_redact_text(text, expected):
    assert redact_text(text) == expected


def test_redact_value_walks_keys_values_and_numbers():
    value = {
        "messages": [{"content": "I am a@b.io"}, {"content": ["x", "4111111111111111"]}],
        "a@b.io": 4111111111111111,
        "n": 1.5,
        "ok": True,
        "none": None,
        "count": 42,
    }
    out, counts = redact_value(value)
    assert out == {
        "messages": [{"content": "I am [EMAIL]"}, {"content": ["x", "[CARD]"]}],
        "[EMAIL]": "[CARD]",
        "n": 1.5,
        "ok": True,
        "none": None,
        "count": 42,
    }
    assert counts == {"card": 2, "email": 2}


def test_overlapping_spans_are_replaced_once():
    assert find_spans("x 4111 1111 1111 1111 y") == [(2, 21, "card")]
    assert luhn_ok("4111111111111111") and not luhn_ok("4111111111111112")


def test_redaction_is_fast_on_hostile_input():
    started = time.monotonic()
    redact_text("a@" * 50_000 + "1 " * 50_000 + "a" * 100_000)
    assert time.monotonic() - started < 5


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"mode": "loud"},
        {"mode": "full", "sample_rate": 2},
        {"mode": "full", "sample_rate": True},
        {"mode": "full", "routes": "/v1"},
        {"mode": "full", "routes": ["has space"]},
        {"mode": "full", "on_error_only": "yes"},
    ],
)
def test_absent_or_invalid_setting_is_off(raw):
    assert payload_policy.config_from_settings({"payload_logging": raw}).mode == "off"
    assert payload_policy.config_from_settings(None).mode == "off"


def test_valid_setting_and_retention(monkeypatch):
    cfg = payload_policy.config_from_settings(
        {"payload_logging": {"mode": "redacted", "sample_rate": 0.25, "routes": ["/v1/*"],
                             "retention_days": 7}}
    )
    assert (cfg.mode, cfg.sample_rate, cfg.routes, cfg.retention_days) == (
        "redacted", 0.25, ("/v1/*",), 7
    )
    assert payload_policy.retention_days_for({}) == 30
    monkeypatch.setattr(settings, "payload_retention_days_default", 90)
    assert payload_policy.retention_days_for({"payload_logging": {"mode": "off"}}) == 90
    assert payload_policy.retention_days_for(
        {"payload_logging": {"mode": "off", "retention_days": 0}}
    ) == 90


def test_legal_hold():
    assert not payload_policy.on_legal_hold({})
    assert not payload_policy.on_legal_hold({"legal_hold": True})
    assert payload_policy.on_legal_hold({"legal_hold": {"since": "2026-11-24T00:00:00Z"}})


def test_route_matching():
    assert payload_policy.route_logged("/v1/chat", ())
    assert payload_policy.route_logged("/v1/chat", ("/v1/*",))
    assert payload_policy.route_logged("/v1/chat", ("/v1/chat",))
    assert not payload_policy.route_logged("/v1/chats", ("/v1/chat",))
    assert not payload_policy.route_logged(None, ("/v1/*",))


def test_sample_rate_over_ten_thousand_calls():
    tenant = uuid.UUID("00000000-0000-0000-0000-000000000352")
    config = payload_policy.PayloadLoggingConfig(mode="metadata", sample_rate=0.1)
    calls = [f"call-{uuid.uuid4().hex}" for _ in range(10_000)]
    kept = sum(
        payload_logging.skip_reason(config, tenant, payload_logging.parse_payload({"call_id": c}))
        is None
        for c in calls
    )
    assert abs(kept / len(calls) - 0.1) <= 0.02
    # Deterministic: a resent call gets the same answer.
    assert [payload_policy.sampled(tenant, c, 0.1) for c in calls[:100]] == [
        payload_policy.sampled(tenant, c, 0.1) for c in calls[:100]
    ]
    assert payload_policy.sampled(tenant, "x", 1) and not payload_policy.sampled(tenant, "x", 0)


def test_sealed_per_tenant_and_call(monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret_keys", "k1:" + "s" * 40)
    a, b = uuid.uuid4(), uuid.uuid4()
    token = payload_logging.seal_payload(a, "c-1", {"request": "x", "response": None})
    assert payload_logging.unseal_payload(a, "c-1", token) == {"request": "x", "response": None}
    for tenant, call in ((b, "c-1"), (a, "c-2")):
        with pytest.raises(ValueError):
            payload_logging.unseal_payload(tenant, call, token)
    monkeypatch.setattr(settings, "webhook_secret_keys", "")
    assert not payload_logging.keyring_ready()
    config = payload_policy.PayloadLoggingConfig(mode="redacted")
    reason = payload_logging.skip_reason(config, a, payload_logging.parse_payload({"call_id": "c"}))
    assert reason == "encryption_unavailable"
