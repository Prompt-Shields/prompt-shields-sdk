"""Per-tenant ingest rate limits (#345).

Operators override the env defaults for one tenant in
``tenants.settings["ingest_rate_limits"]`` with the operator tenant API
(``PATCH /operator/v1/tenants/{id}`` with ``{"settings":
{"ingest_rate_limits": {...}}}``; ``null`` removes the override)::

    {"events": 1000, "kong": 20000, "mcp": 600, "feedback": 600, "feedback_user": 30}

Each key is optional and falls back to its env default:

==============  =========================================
key             env default
==============  =========================================
events          PS_RATE_LIMIT_PER_MINUTE
kong            PS_KONG_RATE_LIMIT_PER_MINUTE
mcp             PS_MCP_RATE_LIMIT_PER_MINUTE
feedback        PS_FEEDBACK_RATE_LIMIT_PER_MINUTE
feedback_user   PS_FEEDBACK_USER_RATE_LIMIT_PER_MINUTE
==============  =========================================

Values are requests per minute, an integer from 0 to ``MAX_LIMIT``; 0 turns
that limit off for the tenant, as 0 does for the env value.

Overrides are read once per tenant per ``PS_TENANT_LIMITS_CACHE_TTL_SECONDS``
(default 30) in each process. The worker that handles the operator PATCH
drops its copy at once; other workers and replicas pick the change up
within the TTL.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from db.models import Tenant
from sqlalchemy import select

from collector.config import settings

SETTINGS_KEY = "ingest_rate_limits"
MAX_LIMIT = 10_000_000

# Override key -> Settings attribute holding the env default.
ENV_DEFAULTS: dict[str, str] = {
    "events": "rate_limit_per_minute",
    "kong": "kong_rate_limit_per_minute",
    "mcp": "mcp_rate_limit_per_minute",
    "feedback": "feedback_rate_limit_per_minute",
    "feedback_user": "feedback_user_rate_limit_per_minute",
}

_MAX_CACHED = 10_000
_cache: dict[UUID, tuple[float, dict[str, int]]] = {}


def settings_errors(value: Any) -> list[str]:
    """Problems with an ``ingest_rate_limits`` value (None removes it)."""
    if value is None:
        return []
    if not isinstance(value, Mapping):
        return [f"settings.{SETTINGS_KEY} must be an object"]
    errors = []
    unknown = sorted(set(value) - set(ENV_DEFAULTS))
    if unknown:
        errors.append(
            f"settings.{SETTINGS_KEY} has unknown keys {unknown}; "
            f"allowed: {sorted(ENV_DEFAULTS)}"
        )
    for key in sorted(set(value) & set(ENV_DEFAULTS)):
        limit = value[key]
        if not isinstance(limit, int) or isinstance(limit, bool) or not (
            0 <= limit <= MAX_LIMIT
        ):
            errors.append(
                f"settings.{SETTINGS_KEY}.{key} must be an integer between 0 and {MAX_LIMIT}"
            )
    return errors


def overrides_from(tenant_settings: Mapping[str, Any] | None) -> dict[str, int]:
    """The valid overrides in a tenant's settings (invalid entries ignored)."""
    raw = (tenant_settings or {}).get(SETTINGS_KEY)
    if not isinstance(raw, Mapping):
        return {}
    return {
        k: v
        for k, v in raw.items()
        if k in ENV_DEFAULTS
        and isinstance(v, int)
        and not isinstance(v, bool)
        and 0 <= v <= MAX_LIMIT
    }


async def overrides(db, tenant_id: UUID) -> dict[str, int]:
    """A tenant's overrides, cached per process. The session must be able
    to read the tenant's row (scoped to it under RLS after resolve_tenant)."""
    ttl = settings.tenant_limits_cache_ttl_seconds
    now = time.monotonic()
    hit = _cache.get(tenant_id)
    if hit is not None and hit[0] > now:
        return hit[1]
    row = (
        await db.execute(select(Tenant.settings).where(Tenant.id == tenant_id))
    ).first()
    found = overrides_from(row[0] if row is not None else None)
    if ttl > 0:
        if len(_cache) >= _MAX_CACHED:
            _cache.clear()
        _cache[tenant_id] = (now + ttl, found)
    return found


async def limit_for(db, tenant_id: UUID, endpoint: str) -> int:
    """Requests per minute for ``endpoint`` (a key of ENV_DEFAULTS): the
    tenant's override, else the env default."""
    found = await overrides(db, tenant_id)
    if endpoint in found:
        return found[endpoint]
    return getattr(settings, ENV_DEFAULTS[endpoint])


def invalidate(tenant_id: UUID | None = None) -> None:
    """Drop cached overrides for one tenant, or all of them."""
    if tenant_id is None:
        _cache.clear()
    else:
        _cache.pop(tenant_id, None)
