"""Verified ingest key cache and failed-auth limiter (collector/auth_cache.py).

Unit tests for the cache and limiter, then end-to-end tests against a real
PostgreSQL database (PS_TEST_DATABASE_URL) that count bcrypt checks.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from collector import auth, rls
from collector.app import app
from collector.auth_cache import FailedAuthLimiter, VerifiedKeyCache, client_key
from collector.config import settings
from db.api_keys import verify_api_key
from db.models import Tenant, TenantApiKey
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TENANT_KEY = "ps-test-key-12345"  # the seeded_db tenant's legacy key
T1, T2 = uuid.uuid4(), uuid.uuid4()


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# ──────────────────────────────────────────────────────────────────────
# VerifiedKeyCache
# ──────────────────────────────────────────────────────────────────────


def test_cache_hit_needs_same_key_id_tenant_and_hash():
    cache = VerifiedKeyCache(max_entries=10, ttl_seconds=30)
    cache.put("ps-ik-a", T1, "k1", "$2b$hash-a")
    assert cache.hit("ps-ik-a", T1, "k1", "$2b$hash-a")
    assert not cache.hit("ps-ik-a", T1, "k1", "$2b$hash-rotated")
    assert not cache.hit("ps-ik-a", T1, "k2", "$2b$hash-a")
    assert not cache.hit("ps-ik-a", T2, "k1", "$2b$hash-a")


def test_cache_different_keys_do_not_collide():
    cache = VerifiedKeyCache(max_entries=10, ttl_seconds=30)
    cache.put("ps-ik-a", T1, "k1", "h")
    # Another key presented against the same row is not a hit.
    assert not cache.hit("ps-ik-b", T1, "k1", "h")
    assert not cache.hit("ps-ik-a ", T1, "k1", "h")
    cache.put("ps-ik-b", T2, "k2", "h2")
    assert cache.hit("ps-ik-a", T1, "k1", "h")
    assert cache.hit("ps-ik-b", T2, "k2", "h2")


def test_cache_never_holds_the_plaintext():
    cache = VerifiedKeyCache(max_entries=10, ttl_seconds=30)
    cache.put("ps-ik-secret-value", T1, "k1", "h")
    assert all(b"ps-ik-secret-value" not in d for d in cache._entries)
    # Keyed by a per-process secret: another cache cannot match it.
    other = VerifiedKeyCache(max_entries=10, ttl_seconds=30)
    assert cache._digest("x") != other._digest("x")


def test_cache_ttl():
    clock = Clock()
    cache = VerifiedKeyCache(max_entries=10, ttl_seconds=30, clock=clock)
    cache.put("k", T1, "k1", "h")
    clock.now += 29
    assert cache.hit("k", T1, "k1", "h")
    clock.now += 2
    assert not cache.hit("k", T1, "k1", "h")
    assert len(cache) == 0


def test_cache_ttl_zero_disables():
    cache = VerifiedKeyCache(max_entries=10, ttl_seconds=0)
    cache.put("k", T1, "k1", "h")
    assert len(cache) == 0
    assert not cache.hit("k", T1, "k1", "h")


def test_cache_is_bounded_lru():
    cache = VerifiedKeyCache(max_entries=3, ttl_seconds=30)
    for i in range(3):
        cache.put(f"k{i}", T1, f"id{i}", "h")
    assert cache.hit("k0", T1, "id0", "h")  # k0 is now most recent
    for i in range(3, 6):
        cache.put(f"k{i}", T1, f"id{i}", "h")
    assert len(cache) == 3
    assert not cache.hit("k0", T1, "id0", "h")
    assert cache.hit("k5", T1, "id5", "h")


def test_cache_invalidate_key_and_tenant():
    cache = VerifiedKeyCache(max_entries=10, ttl_seconds=30)
    kid = uuid.uuid4()
    cache.put("a", T1, str(kid), "h")
    cache.put("a-prev", T1, str(kid), "h-prev")
    cache.put("b", T1, "other", "h")
    cache.put("c", T2, "c-id", "h")
    assert cache.invalidate_key(kid) == 2
    assert cache.invalidate_tenant(T1) == 1
    assert len(cache) == 1
    assert cache.hit("c", T2, "c-id", "h")


# ──────────────────────────────────────────────────────────────────────
# FailedAuthLimiter
# ──────────────────────────────────────────────────────────────────────


def test_limiter_blocks_after_limit_until_window_ends():
    clock = Clock(6000.0)  # start of a window
    lim = FailedAuthLimiter(limit=3, max_clients=10, clock=clock)
    for _ in range(2):
        lim.record_failure("1.2.3.4")
    assert lim.retry_after("1.2.3.4") == 0
    lim.record_failure("1.2.3.4")
    clock.now += 15
    assert lim.retry_after("1.2.3.4") == 45
    assert lim.retry_after("5.6.7.8") == 0
    clock.now += 45
    assert lim.retry_after("1.2.3.4") == 0


def test_limiter_zero_disables():
    lim = FailedAuthLimiter(limit=0, max_clients=10)
    for _ in range(100):
        lim.record_failure("1.2.3.4")
    assert lim.retry_after("1.2.3.4") == 0
    assert len(lim) == 0


def test_limiter_is_bounded():
    lim = FailedAuthLimiter(limit=1, max_clients=100)
    for i in range(1000):
        lim.record_failure(f"10.0.{i // 256}.{i % 256}")
    assert len(lim) == 100


def test_client_key():
    assert client_key("203.0.113.9") == "203.0.113.9"
    assert client_key("2001:db8:1:2:aaaa::1") == client_key("2001:db8:1:2:bbbb::2")
    assert client_key("2001:db8:1:2::1") == "2001:db8:1:2::/64"
    assert client_key("2001:db8:1:3::1") != client_key("2001:db8:1:2::1")
    assert client_key("::ffff:203.0.113.9") == "203.0.113.9"
    assert client_key(None) == "-"
    assert client_key("testclient") == "testclient"


# ──────────────────────────────────────────────────────────────────────
# End to end: bcrypt counts, revocation, expiry, the limiter
# ──────────────────────────────────────────────────────────────────────


def H(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "admin_require_scoped_tokens", False)
    monkeypatch.setattr(settings, "ingest_key_cache_ttl_seconds", 30.0)
    monkeypatch.setattr(settings, "auth_failure_limit_per_minute", 60)


@pytest.fixture
def bcrypt_calls(monkeypatch):
    """Plaintexts passed to bcrypt by tenant key auth, in order."""
    calls: list[str] = []

    def counting(plaintext: str, hashed: str) -> bool:
        calls.append(plaintext)
        return verify_api_key(plaintext, hashed)

    monkeypatch.setattr(auth, "verify_api_key", counting)
    return calls


@pytest.fixture
async def client(seeded_db):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


def _admin(engine):
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        info=rls.bypass_info("test"),
    )


async def _status(c, key: str) -> int:
    return (await c.get("/api/v1/registry/assets", headers=H(key))).status_code


async def _issue(c, name: str = "k") -> dict:
    resp = await c.post("/api/v1/admin/api-keys", headers=H(TENANT_KEY), json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_cache_hit_skips_bcrypt(client, bcrypt_calls):
    for _ in range(5):
        assert await _status(client, TENANT_KEY) == 200
    assert bcrypt_calls.count(TENANT_KEY) == 1


async def test_cache_off_checks_every_time(client, bcrypt_calls, monkeypatch):
    monkeypatch.setattr(settings, "ingest_key_cache_ttl_seconds", 0)
    for _ in range(3):
        assert await _status(client, TENANT_KEY) == 200
    assert bcrypt_calls.count(TENANT_KEY) == 3


async def test_two_keys_cached_apart(client, bcrypt_calls):
    a = (await _issue(client, "a"))["key"]
    b = (await _issue(client, "b"))["key"]
    for _ in range(3):
        assert await _status(client, a) == 200
        assert await _status(client, b) == 200
    assert bcrypt_calls.count(a) == bcrypt_calls.count(b) == 1


async def test_wrong_key_never_cached(client, bcrypt_calls):
    for _ in range(3):
        assert await _status(client, TENANT_KEY + "x") == 401
    assert bcrypt_calls.count(TENANT_KEY + "x") == 3


async def test_revoke_through_api_takes_effect_at_once(client, bcrypt_calls):
    created = await _issue(client)
    assert await _status(client, created["key"]) == 200
    assert await _status(client, created["key"]) == 200
    resp = await client.delete(
        f"/api/v1/admin/api-keys/{created['id']}", headers=H(TENANT_KEY)
    )
    assert resp.status_code == 204
    assert await _status(client, created["key"]) == 401


async def test_revoke_elsewhere_takes_effect_despite_cache(client, seeded_db):
    """Revoked by another replica (straight in the database): this process
    still has the cache entry, and the row check refuses the key."""
    created = await _issue(client)
    assert await _status(client, created["key"]) == 200
    async with _admin(seeded_db)() as s:
        await s.execute(
            update(TenantApiKey)
            .where(TenantApiKey.id == uuid.UUID(created["id"]))
            .values(revoked_at=datetime.now(UTC))
        )
        await s.commit()
    resp = await client.get("/api/v1/registry/assets", headers=H(created["key"]))
    assert resp.status_code == 401
    assert resp.json()["detail"]["detail"] == "API key revoked or expired"


async def test_expired_key_rejected_despite_cache(client, seeded_db, bcrypt_calls):
    created = await _issue(client)
    assert await _status(client, created["key"]) == 200
    async with _admin(seeded_db)() as s:
        await s.execute(
            update(TenantApiKey)
            .where(TenantApiKey.id == uuid.UUID(created["id"]))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await s.commit()
    assert await _status(client, created["key"]) == 401
    assert bcrypt_calls.count(created["key"]) == 1  # the cache hit, then refused


async def test_rotation_without_grace_ends_old_secret(client, seeded_db, bcrypt_calls):
    created = await _issue(client)
    assert await _status(client, created["key"]) == 200
    new = (
        await client.post(
            f"/api/v1/admin/api-keys/{created['id']}/rotate",
            headers=H(TENANT_KEY),
            json={"grace_period_seconds": 0},
        )
    ).json()["key"]
    assert await _status(client, created["key"]) == 401
    assert await _status(client, new) == 200
    assert await _status(client, new) == 200
    assert bcrypt_calls.count(new) == 1


async def test_rotation_elsewhere_changes_hash(client, seeded_db, bcrypt_calls):
    """A changed stored hash (rotated by another replica) is not a hit."""
    created = await _issue(client)
    assert await _status(client, created["key"]) == 200
    async with _admin(seeded_db)() as s:
        await s.execute(
            update(TenantApiKey)
            .where(TenantApiKey.id == uuid.UUID(created["id"]))
            .values(key_hash="$2b$10$" + "x" * 53)
        )
        await s.commit()
    assert await _status(client, created["key"]) == 401
    assert bcrypt_calls.count(created["key"]) == 2


async def test_disabled_tenant_refused_despite_cache(client, seeded_db):
    assert await _status(client, TENANT_KEY) == 200
    async with _admin(seeded_db)() as s:
        await s.execute(update(Tenant).values(disabled_at=datetime.now(UTC)))
        await s.commit()
    assert await _status(client, TENANT_KEY) == 403


async def test_failed_auth_limiter(client, bcrypt_calls, monkeypatch):
    monkeypatch.setattr(settings, "auth_failure_limit_per_minute", 3)
    assert await _status(client, TENANT_KEY) == 200  # cached
    for i in range(3):
        assert await _status(client, f"ps-bad-{i}") == 401
    before = len(bcrypt_calls)
    resp = await client.get("/api/v1/registry/assets", headers=H("ps-bad-x"))
    assert resp.status_code == 429
    assert 1 <= int(resp.headers["Retry-After"]) <= 60
    assert resp.json()["detail"]["title"] == "Too Many Requests"
    assert len(bcrypt_calls) == before  # no bcrypt for the refused request
    # A valid key already verified from this address still gets in.
    assert await _status(client, TENANT_KEY) == 200
    assert len(bcrypt_calls) == before


async def test_valid_keys_do_not_count_as_failures(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_failure_limit_per_minute", 2)
    monkeypatch.setattr(settings, "ingest_key_cache_ttl_seconds", 0)
    for _ in range(5):
        assert await _status(client, TENANT_KEY) == 200
    assert await _status(client, "ps-bad") == 401


async def test_failed_auth_limiter_off(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_failure_limit_per_minute", 0)
    for i in range(5):
        assert await _status(client, f"ps-bad-{i}") == 401


async def test_failed_admin_tokens_limited(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_failure_limit_per_minute", 2)
    for i in range(2):
        resp = await client.get("/api/v1/admin/tokens", headers=H(f"ps-at-bad{i}"))
        assert resp.status_code == 401
    resp = await client.get("/api/v1/admin/tokens", headers=H("ps-at-bad"))
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
