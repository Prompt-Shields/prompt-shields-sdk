"""Ingest key verification cache and the failed-auth limiter.

Every tenant-key request used to pay a bcrypt check (BCRYPT_ROUNDS=10, tens
of ms of CPU), which capped ingest at roughly 15 requests per second per
core. Two in-process pieces cut that cost (see collector/auth.py):

Verified key cache
------------------
After a key passes bcrypt, is live and its tenant is enabled, the cache keeps
HMAC-SHA256(process-random secret, presented key) → (tenant id, key id,
stored bcrypt hash). The plaintext key is never stored, and the HMAC secret
never leaves the process. Auth still runs the lookup-prefix query on every
request, so revocation, expiry, rotation and tenant disable apply at once;
bcrypt is skipped only when the cached entry names the same key id, tenant
and stored hash as the row found now. Failures are never cached. Entries
live ``PS_INGEST_KEY_CACHE_TTL_SECONDS`` (default 30; 0 turns the cache
off) and at most ``PS_INGEST_KEY_CACHE_MAX_ENTRIES`` are kept (least
recently used go first). Revoking or rotating a key through the admin API
also drops its entries in this process.

Failed-auth limiter
-------------------
Unknown and wrong keys still pay a full bcrypt check (so they look the same
as each other), which let an unauthenticated caller spend CPU at will. The
limiter counts failed authentications per client address (an IPv6 address
by its /64) per minute. Past ``PS_AUTH_FAILURE_LIMIT_PER_MINUTE`` (default
60; 0 turns it off), requests from that address that would need bcrypt get
429 with Retry-After instead, without the check. A key found in the cache
is not affected. The client address is ``request.client.host``, which
uvicorn takes from X-Forwarded-For only for proxies listed in
``FORWARDED_ALLOW_IPS`` (default 127.0.0.1): behind a load balancer not in
that list, every client shares the balancer's address. At most
``PS_AUTH_FAILURE_TRACKED_CLIENTS`` addresses are tracked.

Both are per process, like the rate limiters in partner_rate_limit.py.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from uuid import UUID

from collector.config import settings

FAILURE_WINDOW_SECONDS = 60


@dataclass(frozen=True)
class _Entry:
    tenant_id: UUID
    key_id: str
    stored_hash: str
    expires_at: float


def cache_key_id(key_id: UUID | None, tenant_id: UUID) -> str:
    """Stable id of the stored secret: the tenant_api_keys row, or the
    tenant row for a legacy key."""
    return str(key_id) if key_id is not None else f"legacy:{tenant_id}"


class VerifiedKeyCache:
    """Bounded LRU of keys that passed bcrypt (see the module doc)."""

    def __init__(
        self,
        max_entries: int,
        ttl_seconds: Callable[[], float] | float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_entries = max_entries
        self._ttl = ttl_seconds
        self._clock = clock
        self._secret = secrets.token_bytes(32)
        self._entries: OrderedDict[bytes, _Entry] = OrderedDict()
        self._lock = Lock()

    def ttl(self) -> float:
        return self._ttl() if callable(self._ttl) else self._ttl

    def _digest(self, plaintext: str) -> bytes:
        return hmac.new(self._secret, plaintext.encode(), hashlib.sha256).digest()

    def __len__(self) -> int:
        return len(self._entries)

    def hit(
        self, plaintext: str, tenant_id: UUID, key_id: str, stored_hash: str
    ) -> bool:
        """True when ``plaintext`` was verified against this same stored
        hash, for this key and tenant, within the TTL."""
        if self.ttl() <= 0 or self.max_entries <= 0:
            return False
        digest = self._digest(plaintext)
        now = self._clock()
        with self._lock:
            entry = self._entries.get(digest)
            if entry is None:
                return False
            if entry.expires_at <= now:
                del self._entries[digest]
                return False
            self._entries.move_to_end(digest)
        # Non-short-circuiting, constant-time comparisons.
        same_hash = hmac.compare_digest(
            entry.stored_hash.encode(), stored_hash.encode()
        )
        same_key = hmac.compare_digest(entry.key_id.encode(), key_id.encode())
        same_tenant = entry.tenant_id == tenant_id
        return same_hash & same_key & same_tenant

    def put(
        self, plaintext: str, tenant_id: UUID, key_id: str, stored_hash: str
    ) -> None:
        """Remember a successful verification. Call only on full success."""
        ttl = self.ttl()
        if ttl <= 0 or self.max_entries <= 0:
            return
        digest = self._digest(plaintext)
        entry = _Entry(tenant_id, key_id, stored_hash, self._clock() + ttl)
        with self._lock:
            self._entries[digest] = entry
            self._entries.move_to_end(digest)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def _drop(self, match: Callable[[_Entry], bool]) -> int:
        with self._lock:
            gone = [d for d, e in self._entries.items() if match(e)]
            for d in gone:
                del self._entries[d]
        return len(gone)

    def invalidate_key(self, key_id: UUID) -> int:
        """Drop every entry for a tenant_api_keys row (revoke, rotate)."""
        kid = str(key_id)
        return self._drop(lambda e: e.key_id == kid)

    def invalidate_tenant(self, tenant_id: UUID) -> int:
        return self._drop(lambda e: e.tenant_id == tenant_id)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def client_key(host: str | None) -> str:
    """Limiter key for a client address: IPv4 as is, IPv6 by its /64 (one
    host usually holds a whole /64)."""
    if not host:
        return "-"
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.IPv6Network((addr, 64), strict=False))
    return str(addr)


class FailedAuthLimiter:
    """Failed authentications per client per fixed window, LRU-bounded."""

    def __init__(
        self,
        limit: Callable[[], int] | int,
        max_clients: int,
        window: int = FAILURE_WINDOW_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._limit = limit
        self.max_clients = max_clients
        self.window = window
        self._clock = clock
        # client → (window start, failures in that window)
        self._counts: OrderedDict[str, tuple[int, int]] = OrderedDict()
        self._lock = Lock()

    def limit(self) -> int:
        return self._limit() if callable(self._limit) else self._limit

    def __len__(self) -> int:
        return len(self._counts)

    def retry_after(self, client: str) -> int:
        """Seconds until ``client`` may try a bcrypt check again; 0 now."""
        limit = self.limit()
        if limit <= 0:
            return 0
        now = self._clock()
        start = int(now) // self.window * self.window
        with self._lock:
            entry = self._counts.get(client)
        if entry is None or entry[0] != start or entry[1] < limit:
            return 0
        return max(1, int(start + self.window - now))

    def record_failure(self, client: str) -> None:
        if self.limit() <= 0 or self.max_clients <= 0:
            return
        start = int(self._clock()) // self.window * self.window
        with self._lock:
            entry = self._counts.get(client)
            count = entry[1] + 1 if entry is not None and entry[0] == start else 1
            self._counts[client] = (start, count)
            self._counts.move_to_end(client)
            while len(self._counts) > self.max_clients:
                self._counts.popitem(last=False)

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


_KEY_CACHE = VerifiedKeyCache(
    max_entries=settings.ingest_key_cache_max_entries,
    ttl_seconds=lambda: settings.ingest_key_cache_ttl_seconds,
)
_FAILED_AUTH = FailedAuthLimiter(
    limit=lambda: settings.auth_failure_limit_per_minute,
    max_clients=settings.auth_failure_tracked_clients,
)


def get_key_cache() -> VerifiedKeyCache:
    return _KEY_CACHE


def get_failed_auth_limiter() -> FailedAuthLimiter:
    return _FAILED_AUTH
