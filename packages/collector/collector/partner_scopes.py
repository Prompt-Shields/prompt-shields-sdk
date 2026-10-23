"""Partner API scopes and rate-limit tiers (#106).

One place that defines what a partner credential may do and how fast.

Scopes
------
Every Partner API route requires exactly one scope from `SCOPES`, checked
with `partner_auth.require_scope(<scope>)`. Access is deny by default: a
credential gets a route only when its stored scopes (after alias
expansion) contain that route's scope. Unknown scope strings grant
nothing.

Phase 1 credentials were issued with ``registry:read`` and reached every
route with it. ``registry:read`` is kept as an alias for the read scopes
it used to cover (`LEGACY_REGISTRY_READ`) so those credentials, and JWTs
already issued for them, keep working unchanged. Scopes added later, such
as ``webhooks:manage``, are not part of the alias: a partner must be
granted them explicitly.

To add a scope, add it to `SCOPES` with a description and guard the route
with ``require_scope("<scope>")``. Do not add it to `LEGACY_REGISTRY_READ`.

Rate-limit tiers
----------------
`RATE_LIMIT_TIERS` maps a tier name to requests per minute. A credential
with a tier uses the tier's limit; one without (every Phase 1 credential)
uses its own ``rate_limit`` column, so nothing changes for them.
"""

from __future__ import annotations

from collections.abc import Iterable

# Scope -> what it grants. Order is the order shown in the admin API.
SCOPES: dict[str, str] = {
    "assets:read": "List and get AI assets, vendors, models and business units",
    "data-flows:read": "Read an asset's data flows",
    "risks:read": "Read an asset's risk mappings and their review history",
    "usage:read": "Read an asset's aggregated usage summary",
    "changes:read": "Read the /changes delta feed",
    "export:read": "Bulk export the registry (JSON or CycloneDX)",
    "webhooks:manage": "Create, list and delete webhook subscriptions",
    "attestations:write": (
        "Record human attestations of asset fields, including retiring an "
        "asset (an `asset` attestation with state `retired`) and un-retiring it"
    ),
    "risks:review": (
        "Confirm or reject an asset's risk mappings (rule-based suggestions "
        "included), with the reviewer's name and comment"
    ),
    "provider-credentials:manage": (
        "Store, rotate and delete provider admin keys (write-only) and "
        "key fingerprint links for the org usage pull"
    ),
    "audit:read": (
        "Receive the tenant's admin audit trail as `audit.*` webhook events "
        "(credential, scope, webhook, retention and registry changes)"
    ),
}

# What `registry:read` meant in Phase 1: every read route. Frozen on
# purpose; new scopes never join it.
LEGACY_REGISTRY_READ: frozenset[str] = frozenset(
    {
        "assets:read",
        "data-flows:read",
        "risks:read",
        "usage:read",
        "changes:read",
        "export:read",
    }
)

SCOPE_ALIASES: dict[str, frozenset[str]] = {
    "registry:read": LEGACY_REGISTRY_READ,
}

# Scopes the admin API accepts: real scopes plus aliases.
GRANTABLE_SCOPES: frozenset[str] = frozenset(SCOPES) | frozenset(SCOPE_ALIASES)

# Default for a new credential when the admin names no scopes: read-only.
DEFAULT_SCOPES: tuple[str, ...] = tuple(s for s in SCOPES if s in LEGACY_REGISTRY_READ)


def expand_scopes(granted: Iterable[str] | None) -> frozenset[str]:
    """Granted scope strings -> the effective set of real scopes.

    Aliases expand, unknown strings are dropped (deny by default).
    """
    out: set[str] = set()
    for scope in granted or ():
        if scope in SCOPE_ALIASES:
            out |= SCOPE_ALIASES[scope]
        elif scope in SCOPES:
            out.add(scope)
    return frozenset(out)


def unknown_scopes(requested: Iterable[str]) -> list[str]:
    """Requested scopes that cannot be granted, sorted."""
    return sorted(set(requested) - GRANTABLE_SCOPES)


# ──────────────────────────────────────────────────────────────────────
# Rate-limit tiers
# ──────────────────────────────────────────────────────────────────────

# Tier -> requests per minute per credential.
RATE_LIMIT_TIERS: dict[str, int] = {
    "basic": 120,
    "standard": 1000,
    "premium": 5000,
    "enterprise": 20000,
}

DEFAULT_RATE_LIMIT = RATE_LIMIT_TIERS["standard"]


def effective_rate_limit(tier: str | None, rate_limit: int | None) -> int:
    """Requests per minute for a credential.

    A known tier wins; otherwise the credential's own ``rate_limit``
    (custom or Phase 1), then the default.
    """
    if tier and tier in RATE_LIMIT_TIERS:
        return RATE_LIMIT_TIERS[tier]
    return rate_limit or DEFAULT_RATE_LIMIT
