"""Unit tests for partner auth helpers.

Pure functions only — no DB, no FastAPI. Covers the bcrypt round-trip,
ID/secret/key generators, JWT issue+verify, and the rate limiter.
"""
from __future__ import annotations

import time
import uuid

import pytest
from fastapi import HTTPException

from collector.partner_auth import (
    JWT_TTL_SECONDS,
    api_key_prefix_sha256,
    generate_api_key,
    generate_client_id,
    generate_secret,
    hash_secret,
    issue_access_token,
    verify_access_token,
    verify_secret,
)
from collector.partner_rate_limit import FixedWindowRateLimiter


# ──────────────────────────────────────────────────────────────────────
# Bcrypt round-trip
# ──────────────────────────────────────────────────────────────────────


class TestSecretHashing:
    def test_hash_and_verify(self):
        plaintext = "sk-partner-secret-abc123"
        hashed = hash_secret(plaintext)
        assert hashed != plaintext
        assert hashed.startswith("$2b$")
        assert verify_secret(plaintext, hashed) is True

    def test_wrong_secret_fails(self):
        hashed = hash_secret("right")
        assert verify_secret("wrong", hashed) is False

    def test_invalid_hash_returns_false(self):
        assert verify_secret("anything", "not-a-bcrypt-hash") is False
        assert verify_secret("anything", "") is False

    def test_different_secrets_different_hashes(self):
        h1 = hash_secret("a")
        h2 = hash_secret("b")
        assert h1 != h2

    def test_same_secret_different_hashes(self):
        # bcrypt uses random salt — same plaintext should yield different hashes
        h1 = hash_secret("same")
        h2 = hash_secret("same")
        assert h1 != h2
        assert verify_secret("same", h1) and verify_secret("same", h2)


# ──────────────────────────────────────────────────────────────────────
# Generators
# ──────────────────────────────────────────────────────────────────────


class TestGenerators:
    def test_client_id_format(self):
        cid = generate_client_id("Ardoq")
        assert cid.startswith("ps-partner-ardoq-")
        assert len(cid) > len("ps-partner-ardoq-")

    def test_client_id_sanitizes_special_chars(self):
        cid = generate_client_id("Foo Bar / Baz!")
        assert "ps-partner-foo-bar---baz-" in cid

    def test_client_id_caps_name_length(self):
        cid = generate_client_id("a" * 100)
        assert "ps-partner-" + ("a" * 20) + "-" in cid

    def test_secret_unique(self):
        secrets = {generate_secret() for _ in range(20)}
        assert len(secrets) == 20

    def test_api_key_has_prefix(self):
        key = generate_api_key("LeanIX")
        assert key.startswith("ps-pk-leanix-")


# ──────────────────────────────────────────────────────────────────────
# JWT issue + verify
# ──────────────────────────────────────────────────────────────────────


class TestJWT:
    def _principal(self):
        return (uuid.uuid4(), uuid.uuid4(), "ps-partner-foo-xyz", ["registry:read"])

    def test_issue_and_verify_round_trip(self):
        tid, pid, cid, scopes = self._principal()
        token, ttl = issue_access_token(tid, pid, cid, scopes)
        assert ttl == JWT_TTL_SECONDS
        payload = verify_access_token(token)
        assert payload["tenant_id"] == str(tid)
        assert payload["partner_id"] == str(pid)
        assert payload["sub"] == cid
        assert payload["scope"] == "registry:read"
        assert payload["aud"] == "partner-api"

    def test_invalid_token_raises_401(self):
        with pytest.raises(HTTPException) as excinfo:
            verify_access_token("not-a-real-jwt")
        assert excinfo.value.status_code == 401

    def test_tampered_token_raises_401(self):
        token, _ = issue_access_token(*self._principal())
        # Corrupt the signature by changing the last char
        tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
        with pytest.raises(HTTPException) as excinfo:
            verify_access_token(tampered)
        assert excinfo.value.status_code == 401

    def test_expired_token_raises_401(self, monkeypatch):
        # Roll time back to simulate an expired token
        original_time = time.time
        monkeypatch.setattr("collector.partner_auth.time.time",
                            lambda: original_time() - JWT_TTL_SECONDS - 60)
        token, _ = issue_access_token(*self._principal())
        monkeypatch.setattr("collector.partner_auth.time.time", original_time)
        with pytest.raises(HTTPException) as excinfo:
            verify_access_token(token)
        assert excinfo.value.status_code == 401

    def test_multiple_scopes_serialized_with_spaces(self):
        tid, pid, cid, _ = self._principal()
        token, _ = issue_access_token(tid, pid, cid, ["a", "b", "c"])
        assert verify_access_token(token)["scope"] == "a b c"


# ──────────────────────────────────────────────────────────────────────
# Rate limiter
# ──────────────────────────────────────────────────────────────────────


class TestRateLimiter:
    def test_under_limit_allowed(self):
        limiter = FixedWindowRateLimiter()
        partner = uuid.uuid4()
        result = limiter.check(partner, limit=10)
        assert result.allowed is True
        assert result.limit == 10
        assert result.remaining == 9
        assert result.retry_after == 0

    def test_at_limit_still_allowed(self):
        limiter = FixedWindowRateLimiter()
        partner = uuid.uuid4()
        for _ in range(9):
            limiter.check(partner, 10)
        result = limiter.check(partner, 10)  # 10th call
        assert result.allowed is True
        assert result.remaining == 0

    def test_over_limit_blocked(self):
        limiter = FixedWindowRateLimiter()
        partner = uuid.uuid4()
        for _ in range(10):
            limiter.check(partner, 10)
        result = limiter.check(partner, 10)  # 11th call
        assert result.allowed is False
        assert result.retry_after > 0

    def test_separate_partners_separate_buckets(self):
        limiter = FixedWindowRateLimiter()
        a, b = uuid.uuid4(), uuid.uuid4()
        for _ in range(10):
            limiter.check(a, 10)
        result_b = limiter.check(b, 10)
        # Partner B should be unaffected by Partner A's usage
        assert result_b.allowed is True
        assert result_b.remaining == 9

    def test_reset_at_is_future(self):
        limiter = FixedWindowRateLimiter()
        result = limiter.check(uuid.uuid4(), 10)
        assert result.reset_at > int(time.time())
        assert result.reset_at <= int(time.time()) + 60


# ──────────────────────────────────────────────────────────────────────
# Misc
# ──────────────────────────────────────────────────────────────────────


def test_api_key_prefix_is_stable():
    prefix1 = api_key_prefix_sha256("ps-pk-test-abc")
    prefix2 = api_key_prefix_sha256("ps-pk-test-abc")
    assert prefix1 == prefix2
    assert len(prefix1) == 16

    different = api_key_prefix_sha256("ps-pk-test-xyz")
    assert different != prefix1
