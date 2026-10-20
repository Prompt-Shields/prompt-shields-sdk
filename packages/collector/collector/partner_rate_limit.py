"""In-process rate limiter for the Partner API and tenant ingest.

Implements a fixed-window counter per (key, minute), where the key is a
partner id or a tenant id. Bucket keys
expire after 2 minutes to bound memory. Suitable for single-process
deployments; production should use Redis with an atomic INCR + EXPIRE.

Returns three pieces of information so callers can populate the
X-RateLimit-* headers and the Retry-After header on 429.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Hashable
from dataclasses import dataclass
from threading import Lock


@dataclass
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_at: int  # epoch seconds
    retry_after: int  # seconds; 0 if allowed


class FixedWindowRateLimiter:
    """Counter per (key, minute_bucket). Thread-safe.

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

        reset_at = (now_minute + 1) * 60
        remaining = max(0, limit - count)
        allowed = count <= limit
        retry_after = 0 if allowed else max(1, reset_at - now)

        return RateLimitResult(
            allowed=allowed,
            limit=limit,
            remaining=remaining,
            reset_at=reset_at,
            retry_after=retry_after,
        )


# Module-level singleton — one limiter per worker process
_LIMITER = FixedWindowRateLimiter()


def get_rate_limiter() -> FixedWindowRateLimiter:
    return _LIMITER


# Separate singleton for /ingest/events so tenant and partner counters never
# share buckets. Swap for a shared-store implementation with the same
# ``check`` signature via FastAPI ``dependency_overrides``.
_INGEST_LIMITER = FixedWindowRateLimiter()


def get_ingest_rate_limiter() -> FixedWindowRateLimiter:
    return _INGEST_LIMITER


# /ingest/kong counts apart from /ingest/events (PS_KONG_RATE_LIMIT_PER_MINUTE).
_KONG_LIMITER = FixedWindowRateLimiter()


def get_kong_rate_limiter() -> FixedWindowRateLimiter:
    return _KONG_LIMITER


# /ingest/mcp counts apart from /ingest/events (PS_MCP_RATE_LIMIT_PER_MINUTE).
_MCP_LIMITER = FixedWindowRateLimiter()


def get_mcp_rate_limiter() -> FixedWindowRateLimiter:
    return _MCP_LIMITER
