"""Rate limiters for the Partner API and tenant ingest.

A fixed-window counter per (key, minute), where the key is a partner id or a
tenant id (or a tuple such as ``("user", tenant_id, user_id)``).

Two implementations with the same interface (``await limiter.acheck(key,
limit)``):

* ``FixedWindowRateLimiter`` counts in process memory. Each uvicorn worker
  and each replica counts on its own, so with N processes a limit admits up
  to N times its value. Buckets expire after 2 minutes to bound memory.
* ``RedisRateLimiter`` counts in Redis with an atomic INCR + EXPIRE, so every
  worker and replica pointing at the same Redis shares one count. Used when
  ``PS_COLLECTOR_REDIS_URL`` is set. If Redis errors or exceeds
  ``PS_COLLECTOR_REDIS_TIMEOUT_SECONDS`` the request is counted by an
  in-process fallback instead (fail open, logged, and counted in the
  ``ps_collector_rate_limit_store_errors`` metric), so a Redis outage never
  blocks ingest. After a failure the limiter skips Redis for
  ``_STORE_RETRY_SECONDS`` and counts in-process, so an unreachable or
  blackholed Redis costs one timeout per limiter per interval, not one per
  request.

Each limiter has a namespace (``partner``, ``ingest``, ``kong``, ``mcp``,
``feedback``) so counters for different endpoints never share a bucket. Keys
are hashed before they reach Redis, so no tenant id or user id is stored
there in the clear. The minute window comes from the collector's clock;
keep replica clocks in sync (NTP) as usual.

Returns enough for callers to populate the X-RateLimit-* headers and the
Retry-After header on 429.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import defaultdict
from collections.abc import Hashable
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Any, Protocol

from collector.config import settings

logger = logging.getLogger(__name__)

# Seconds a Redis bucket lives: the current minute plus slack for clock skew.
_BUCKET_TTL_SECONDS = 120

# After a store failure, count in-process for this long before trying Redis
# again, so requests do not each wait out the timeout during an outage.
_STORE_RETRY_SECONDS = 5.0

# INCR, and set the expiry on the first hit, atomically.
_INCR_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


@dataclass
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_at: int  # epoch seconds
    retry_after: int  # seconds; 0 if allowed


class RateLimiter(Protocol):
    async def acheck(self, key: Hashable, limit: int) -> RateLimitResult: ...


def _result(count: int, limit: int, now: int) -> RateLimitResult:
    reset_at = (now // 60 + 1) * 60
    allowed = count <= limit
    return RateLimitResult(
        allowed=allowed,
        limit=limit,
        remaining=max(0, limit - count),
        reset_at=reset_at,
        retry_after=0 if allowed else max(1, reset_at - now),
    )


class FixedWindowRateLimiter:
    """In-process counter per (key, minute_bucket). Thread-safe.

    ``check`` never awaits, so it is also atomic with respect to concurrent
    async requests on the same event loop.
    """

    def __init__(self) -> None:
        self._counts: dict[tuple[Hashable, int], int] = defaultdict(int)
        self._lock = Lock()
        self._last_gc_minute: int | None = None

    def _gc(self, now_minute: int) -> None:
        """Drop buckets older than the previous minute. O(N), run at most
        once per minute, so memory stays bounded by the keys seen in the
        current and previous minute."""
        cutoff = now_minute - 1
        self._counts = defaultdict(
            int,
            {k: v for k, v in self._counts.items() if k[1] >= cutoff},
        )

    def check(self, partner_id: Hashable, limit: int) -> RateLimitResult:
        now = int(time.time())
        now_minute = now // 60
        key = (partner_id, now_minute)

        with self._lock:
            # GC on minute rollover. Keying it off a per-key count would
            # never run when every key stays under the trigger, leaking one
            # bucket per key per minute (many low-volume tenants).
            if self._last_gc_minute != now_minute:
                self._gc(now_minute)
                self._last_gc_minute = now_minute
            self._counts[key] += 1
            count = self._counts[key]

        return _result(count, limit, now)

    async def acheck(self, key: Hashable, limit: int) -> RateLimitResult:
        return self.check(key, limit)


def _key_digest(key: Hashable) -> str:
    """Stable digest of a limiter key. Tuples are encoded part by part with
    a separator that cannot occur in ``str()`` of a UUID or int, and each
    part is length-prefixed, so ("a:b", "c") and ("a", "b:c") differ."""
    parts = key if isinstance(key, tuple) else (key,)
    encoded = "\x1f".join(f"{len(str(p))}:{p}" for p in parts)
    return hashlib.sha256(encoded.encode()).hexdigest()[:32]


class RedisRateLimiter:
    """Fixed window shared through Redis (INCR + EXPIRE in one Lua script).

    ``client`` is a ``redis.asyncio.Redis``; built lazily from ``url`` on the
    first call (so it binds to the serving event loop). ``fallback`` counts
    when Redis fails.
    """

    def __init__(
        self,
        namespace: str,
        *,
        url: str = "",
        client: Any = None,
        prefix: str = "ps:collector:rl",
        timeout: float = 0.5,
        fallback: FixedWindowRateLimiter | None = None,
    ) -> None:
        if client is None and not url:
            raise ValueError("RedisRateLimiter needs a url or a client")
        self.namespace = namespace
        self._url = url
        self._client = client
        self._script: Any = None
        self._prefix = prefix
        self._timeout = timeout
        self.fallback = fallback or FixedWindowRateLimiter()
        self._last_error_log = 0.0
        self._skip_until = 0.0

    def _get_client(self) -> Any:
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(
                self._url,
                socket_timeout=self._timeout,
                socket_connect_timeout=self._timeout,
            )
        return self._client

    def redis_key(self, key: Hashable, now_minute: int) -> str:
        return f"{self._prefix}:{self.namespace}:{_key_digest(key)}:{now_minute}"

    async def acheck(self, key: Hashable, limit: int) -> RateLimitResult:
        now = int(time.time())
        if monotonic() < self._skip_until:
            from collector import metrics

            metrics.record_rate_limit_store_error(self.namespace)
            return self.fallback.check(key, limit)
        try:
            client = self._get_client()
            if self._script is None:
                self._script = client.register_script(_INCR_SCRIPT)
            count = int(
                await asyncio.wait_for(
                    self._script(
                        keys=[self.redis_key(key, now // 60)],
                        args=[_BUCKET_TTL_SECONDS],
                    ),
                    self._timeout,
                )
            )
        except Exception as exc:  # noqa: BLE001 - any store failure falls back
            self._on_error(exc)
            return self.fallback.check(key, limit)
        return _result(count, limit, now)

    def _on_error(self, exc: Exception) -> None:
        from collector import metrics

        metrics.record_rate_limit_store_error(self.namespace)
        now = monotonic()
        self._skip_until = now + _STORE_RETRY_SECONDS
        if now - self._last_error_log >= 60:
            self._last_error_log = now
            logger.warning(
                "Rate limit store unavailable (%s: %s); counting %s requests "
                "per process until it recovers",
                type(exc).__name__,
                exc,
                self.namespace,
            )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


def _make(namespace: str) -> FixedWindowRateLimiter | RedisRateLimiter:
    if settings.collector_redis_url:
        return RedisRateLimiter(
            namespace,
            url=settings.collector_redis_url,
            prefix=settings.collector_redis_key_prefix,
            timeout=settings.collector_redis_timeout_seconds,
        )
    return FixedWindowRateLimiter()


class _Lazy:
    """One limiter per namespace per process, built on first use so the
    settings in effect then (PS_COLLECTOR_REDIS_URL) pick the backend."""

    def __init__(self, namespace: str) -> None:
        self.namespace = namespace
        self.limiter: FixedWindowRateLimiter | RedisRateLimiter | None = None

    def __call__(self) -> FixedWindowRateLimiter | RedisRateLimiter:
        if self.limiter is None:
            self.limiter = _make(self.namespace)
        return self.limiter


_PARTNER = _Lazy("partner")
_INGEST = _Lazy("ingest")
_KONG = _Lazy("kong")
_MCP = _Lazy("mcp")
_FEEDBACK = _Lazy("feedback")


def get_rate_limiter() -> RateLimiter:
    """Partner API, per partner credential."""
    return _PARTNER()


# Separate limiter for /ingest/events so tenant and partner counters never
# share buckets. Tests swap it via FastAPI ``dependency_overrides``.
def get_ingest_rate_limiter() -> RateLimiter:
    return _INGEST()


# /ingest/kong counts apart from /ingest/events (PS_KONG_RATE_LIMIT_PER_MINUTE).
def get_kong_rate_limiter() -> RateLimiter:
    return _KONG()


# /ingest/mcp counts apart from /ingest/events (PS_MCP_RATE_LIMIT_PER_MINUTE).
def get_mcp_rate_limiter() -> RateLimiter:
    return _MCP()


# /ingest/feedback: per-tenant and per-user buckets, apart from the rest.
def get_feedback_rate_limiter() -> RateLimiter:
    return _FEEDBACK()
