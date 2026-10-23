"""Audit trail units (no database): hashing, details sanitising, the
auth-failure rate limiter, request ids and the audit.* webhook routing."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone

from collector import audit_trail, webhooks
from collector.partner_scopes import LEGACY_REGISTRY_READ, SCOPES, expand_scopes
from db import audit_chain

ROW = {
    "id": uuid.UUID("00000000-0000-4000-8000-000000000001"),
    "tenant_id": uuid.UUID("00000000-0000-4000-8000-0000000000aa"),
    "seq": 1,
    "created_at": datetime(2026, 10, 20, 12, 0, 0, 123456, tzinfo=UTC),
    "action": "provider_credential.rotate",
    "outcome": "success",
    "actor_type": "tenant_key",
    "actor_id": "abcd1234",
    "target_type": "provider_credential",
    "target_id": "openai",
    "request_id": "req-1",
    "status_code": 200,
    "details": {"provider": "openai"},
}


def test_row_hash_is_stable_and_covers_every_chained_field():
    h = audit_chain.compute_row_hash(audit_chain.GENESIS_HASH, ROW)
    # Pinned: the canonical form is a contract with external verifiers.
    assert h == audit_chain.compute_row_hash(audit_chain.GENESIS_HASH, dict(ROW))
    assert len(h) == 64
    assert audit_chain.canonical_row(ROW) == (
        b'{"action":"provider_credential.rotate","actor_id":"abcd1234",'
        b'"actor_type":"tenant_key","created_at":"2026-10-20T12:00:00.123456Z",'
        b'"details":{"provider":"openai"},"id":"00000000-0000-4000-8000-000000000001",'
        b'"outcome":"success","request_id":"req-1","seq":1,"status_code":200,'
        b'"target_id":"openai","target_type":"provider_credential",'
        b'"tenant_id":"00000000-0000-4000-8000-0000000000aa","v":1}'
    )
    for name in audit_chain.CHAINED_FIELDS:
        changed = {**ROW, name: "x" if name != "details" else {"provider": "x"}}
        assert audit_chain.compute_row_hash(audit_chain.GENESIS_HASH, changed) != h, name
    assert audit_chain.compute_row_hash("1" * 64, ROW) != h


def test_timestamps_canonicalise_to_utc():
    naive = ROW["created_at"].replace(tzinfo=None)
    cet = ROW["created_at"].astimezone(timezone(timedelta(hours=2)))
    expected = audit_chain.compute_row_hash(audit_chain.GENESIS_HASH, ROW)
    for value in (naive, cet, "2026-10-20T12:00:00.123456Z"):
        assert audit_chain.compute_row_hash(
            audit_chain.GENESIS_HASH, {**ROW, "created_at": value}
        ) == expected


def test_details_never_keep_secrets_or_long_text():
    out = audit_trail.sanitize_details(
        {
            "admin_key": "sk-live-123",
            "client_secret": "s",
            "token": "t",
            "api_key_id": "key_abc",
            "rows_deleted": 3,
            "when": datetime(2026, 1, 1, tzinfo=UTC),
            "who": uuid.UUID(int=5),
            "note": "x" * 1000,
            "ratio": 0.5,
            "scopes": {"b", "a"},
            "nested": {"password": "p", "count": 1},
            # Counts under secret-looking names (an erasure's per-table rows).
            "admin_tokens": 2,
            "tenant_api_keys": 0,
            "api_key": None,
        }
    )
    assert out["admin_key"] == out["client_secret"] == out["token"] == "[REDACTED]"
    assert out["api_key_id"] == "key_abc"
    assert out["rows_deleted"] == 3
    assert out["when"] == "2026-01-01T00:00:00.000000Z"
    assert out["who"] == str(uuid.UUID(int=5))
    assert len(out["note"]) == audit_trail.MAX_DETAIL_STRING
    assert out["ratio"] == "0.5"
    assert out["scopes"] == ["a", "b"]
    assert out["nested"] == {"password": "[REDACTED]", "count": 1}
    assert (out["admin_tokens"], out["tenant_api_keys"], out["api_key"]) == (2, 0, None)


def test_auth_failure_limiter():
    lim = audit_trail.AuthFailureLimiter(per_source=2, total=3, window=60)
    a, b, c = ("operator", "1.1.1.1"), ("operator", "2.2.2.2"), ("partner", "3.3.3.3")
    assert lim.allow(a, now=0) == (True, 0)
    assert lim.allow(a, now=1) == (True, 0)
    assert lim.allow(a, now=2) == (False, 0)  # per source
    assert lim.allow(a, now=3) == (False, 0)
    assert lim.allow(b, now=4) == (True, 0)
    assert lim.allow(c, now=5) == (False, 0)  # global cap
    # After the window: written again, with the count it dropped.
    assert lim.allow(a, now=61) == (True, 2)
    assert lim.allow(c, now=66) == (True, 1)


def test_request_ids():
    assert audit_trail.clean_request_id("abc-123_x.y:z") == "abc-123_x.y:z"
    for bad in (None, "", "has space", "x" * 129, "semi;colon", "ünï"):
        assert audit_trail.clean_request_id(bad) is None


def test_actor_names():
    assert audit_trail.actor_from_name("operator-api") == audit_trail.OPERATOR
    assert audit_trail.actor_from_name("cli") == audit_trail.Actor("system", "cli")


def test_audit_events_need_their_own_scope():
    assert "audit:read" in SCOPES
    assert "audit:read" not in LEGACY_REGISTRY_READ
    assert "audit:read" not in expand_scopes(["registry:read"])
    assert webhooks.required_scope("audit.provider_credential.rotate") == "audit:read"
    assert webhooks.required_scope(webhooks.AUDIT_EVENTS) == "audit:read"
    assert webhooks.required_scope("asset.created") == "changes:read"
    # Not a default event type: a new subscription without event_types
    # keeps getting asset events only.
    assert webhooks.AUDIT_EVENTS not in webhooks.EVENT_TYPES
    assert webhooks.AUDIT_EVENTS in webhooks.SUBSCRIBABLE_EVENT_TYPES


def test_subscription_matching():
    wants = webhooks.subscription_wants
    assert wants(["audit.*"], "audit.tenant.erase")
    assert not wants(["asset.created"], "audit.tenant.erase")
    assert not wants(["audit.*"], "asset.created")
    assert wants(["asset.created", "audit.*"], "asset.created")
