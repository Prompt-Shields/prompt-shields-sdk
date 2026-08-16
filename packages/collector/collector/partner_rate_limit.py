"""In-process rate limiter for the Partner API.

Implements a fixed-window counter per (partner_id, minute). Bucket keys
expire after 2 minutes to bound memory. Suitable for single-process
deployments; production should use Redis with an atomic INCR + EXPIRE.

Returns three pieces of information so callers can populate the
X-RateLimit-* headers and the Retry-After header on 429.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from threading import Lock
from uuid import UUID


@dataclass
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_at: int  # epoch seconds
    retry_after: int  # seconds; 0 if allowed


class FixedWindowRateLimiter:
    """Counter per (partner_id, minute_bucket). Thread-safe."""

    def __init__(self) -> None:
        self._counts: dict[tuple[UUID, int], int] = defaultdict(int)
        self._lock = Lock()

    def _gc(self, now_minute: int) -> None:
        """Drop buckets older than the previous minute. O(N) — fine at
        partner-API scale (small partner count, ~once per minute)."""
        cutoff = now_minute - 1
        self._counts = defaultdict(
            int,
            {k: v for k, v in self._counts.items() if k[1] >= cutoff},
        )

    def check(self, partner_id: UUID, limit: int) -> RateLimitResult:
        now = int(time.time())
        now_minute = now // 60
        key = (partner_id, now_minute)

        with self._lock:
            self._counts[key] += 1
            count = self._counts[key]
            # Light-touch GC every ~64 ticks to avoid every-call overhead
            if count % 64 == 0:
                self._gc(now_minute)

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
