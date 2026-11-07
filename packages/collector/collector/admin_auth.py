"""Scoped automation tokens for the tenant admin API (/api/v1/admin).

Who may call an admin route
---------------------------
Every tenant admin route declares one scope from `ADMIN_SCOPES` with
``Depends(require_admin("<scope>"))``. GET routes also accept
``admin:read``. Two credentials reach them:

  - an admin token (``ps-at-...``, table ``admin_tokens``): tenant-scoped,
    with explicit scopes, an optional expiry, revocable. Meant for
    Terraform, CI and other automation.
  - a tenant ingest key (the key the SDK sends), only while
    ``PS_ADMIN_REQUIRE_SCOPED_TOKENS=false``: the scopes in
    ``TENANT_KEY_SCOPES``, i.e. every admin scope but member management
    (#338) and the inference credential vault (#342): ``admin:members`` and
    ``admin:inference-credentials`` are never reachable with an ingest key. Only a key holding every ingest key scope (tenant_keys
    ALL_KEY_SCOPES: every key from before scopes existed, #341) gets in; a
    narrowed key, such as a new ``ingest:write``-only key, is refused (403).

``PS_ADMIN_REQUIRE_SCOPED_TOKENS`` defaults to true since roles shipped
(#338): ingest keys get 403 on every admin route, so only admin tokens with
the right scope get in, and an ingest key deployed with an SDK cannot rotate
provider admin keys, issue partner credentials or mint more keys. Setting it
to false restores the old behaviour. While it is off, an ingest key reaches
every route in ``TENANT_KEY_SCOPES``, so issuing or rotating one (which
returns a key) needs all of those scopes, not just ``admin:keys``;
otherwise an ``admin:keys`` token could mint itself full admin
(``require_full_admin``).

A token issued for a principal (``admin_tokens.principal_id``) reaches at
most what the principal's tenant-wide roles grant at request time
(collector/rbac.py); a disabled or deleted principal's tokens get 401.

Tokens are stored like ingest keys (SHA-256 lookup prefix + bcrypt hash)
and returned once. A token can only issue tokens with scopes it holds.

Audit: changes are written to the hash-chained audit trail
(collector.audit_trail, ``audit_events``) through ``add_audit``, in the same
transaction as the change. Refused calls (bad token 401, missing scope or
ingest key refused 403) are ``auth.failure`` rows (rate-limited). The actor
is the credential's SHA-256 lookup prefix (``admin_token`` or
``tenant_key``) or ``operator``; never a secret.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from db.api_keys import api_key_lookup_prefix, hash_api_key, verify_api_key
from db.models import AdminToken, Tenant
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from collector import audit_trail, operator_auth, rbac, rls
from collector.audit import bearer_key_prefix
from collector.auth import (
    _DUMMY_HASH,
    bearer_token,
    check_failed_auth_limit,
    failed_auth_client,
    record_failed_auth,
    resolve_tenant,
    tenant_disabled,
)
from collector.config import settings
from collector.tenant_keys import ALL_KEY_SCOPES, DuplicateName, key_status

TOKEN_PREFIX = audit_trail.ADMIN_TOKEN_PREFIX  # "ps-at-"
TOKEN_WARNING = "Save the token now. It will not be shown again."

# Scope -> what it grants. Order is the order shown by GET /token-scopes.
ADMIN_SCOPES: dict[str, str] = {
    "admin:read": "Read (GET) every tenant admin resource; changes nothing",
    "admin:keys": (
        "Issue, rotate and revoke tenant ingest keys. While "
        "PS_ADMIN_REQUIRE_SCOPED_TOKENS is off an ingest key reaches every "
        "admin route, so issuing or rotating one also needs every other scope"
    ),
    "admin:tokens": "Issue and revoke admin tokens (only with scopes the caller holds)",
    "admin:provider-credentials": (
        "Store, rotate and delete provider admin keys and key fingerprint links"
    ),
    "admin:webhooks": "Manage webhook subscriptions and redeliver webhook deliveries",
    "admin:partners": "Issue, change and revoke Partner API credentials",
    "admin:registry": (
        "Run the asset lifecycle sweep and risk suggestions, and review risk mappings"
    ),
    "admin:privacy": (
        "Erase an end user's identifiers from usage events, call feedback and "
        "MCP usage (erasure by user)"
    ),
    "admin:members": (
        "Manage principals, teams, projects and role memberships (granting a "
        "role needs every admin scope it carries; the owner role needs an owner)"
    ),
    "admin:inference-credentials": (
        "Store, rotate, disable and delete inference API keys in the credential "
        "vault (write-only: no route returns a key)"
    ),
}

# What a full-scope tenant ingest key reaches while
# PS_ADMIN_REQUIRE_SCOPED_TOKENS is off: every admin scope but member
# management (#338), which only admin tokens get.
TENANT_KEY_SCOPES: frozenset[str] = frozenset(ADMIN_SCOPES) - {
    "admin:members",
    # Inference keys (#342) are new and only for admin tokens, too.
    "admin:inference-credentials",
}

READ_SCOPE = "admin:read"
_READ_METHODS = frozenset({"GET", "HEAD"})


def _problem(status: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={
            "type": "about:blank",
            "title": title,
            "status": status,
            "detail": detail,
        },
    )


@dataclass(frozen=True)
class AdminPrincipal:
    tenant_id: UUID
    # "admin_token" or "tenant_key"
    kind: str
    scopes: frozenset[str]
    token_id: UUID | None = None
    # Set for a token issued for a principal: its id and tenant-wide roles.
    principal_id: UUID | None = None
    roles: frozenset[str] = frozenset()

    def allows(self, scope: str, method: str) -> bool:
        if scope in self.scopes:
            return True
        return method in _READ_METHODS and READ_SCOPE in self.scopes


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def unknown_scopes(requested) -> list[str]:
    return sorted(set(requested) - set(ADMIN_SCOPES))


async def _resolve_admin_token(request: Request, token: str) -> AdminPrincipal:
    db = request.state.db
    async with rls.auth_lookup(db):
        row = (
            await db.execute(
                select(
                    AdminToken.id,
                    AdminToken.tenant_id,
                    AdminToken.scopes,
                    AdminToken.token_hash,
                    AdminToken.expires_at,
                    AdminToken.revoked_at,
                    AdminToken.principal_id,
                    Tenant.disabled_at,
                )
                .join(Tenant, Tenant.id == AdminToken.tenant_id)
                .where(AdminToken.token_prefix == api_key_lookup_prefix(token))
            )
        ).one_or_none()
    # Too many failed checks from this client: 429 without bcrypt, unless
    # the token is found and active (only its holder can present it; see
    # collector/auth.py::resolve_tenant).
    if row is None or key_status(row) != "active":
        client = check_failed_auth_limit(request)
    else:
        client = failed_auth_client(request)
    stored = row.token_hash if row is not None else _DUMMY_HASH
    valid = await asyncio.to_thread(verify_api_key, token, stored)
    if row is None or not valid:
        record_failed_auth(client)
        await _audit_failure(request, "invalid_credentials", 401)
        raise _problem(401, "Unauthorized", "Invalid admin token")
    if key_status(row) != "active":
        record_failed_auth(client)
        await _audit_failure(request, "invalid_credentials", 401)
        raise _problem(401, "Unauthorized", "Admin token revoked or expired")
    if row.disabled_at is not None:
        raise tenant_disabled()
    await rls.set_tenant(db, row.tenant_id)
    scopes = frozenset(row.scopes or ())
    roles: frozenset[str] = frozenset()
    if row.principal_id is not None:
        try:
            roles = await rbac.tenant_roles(db, row.tenant_id, row.principal_id)
        except rbac.PrincipalDisabled:
            record_failed_auth(client)
            await _audit_failure(request, "principal_disabled", 401, row.tenant_id)
            raise _problem(
                401, "Unauthorized", "The token's principal is disabled or deleted"
            ) from None
        # Never more than the principal's roles grant now (collector/rbac.py).
        scopes &= rbac.role_scopes(roles)
    return AdminPrincipal(
        tenant_id=row.tenant_id,
        kind="admin_token",
        scopes=scopes,
        token_id=row.id,
        principal_id=row.principal_id,
        roles=roles,
    )


async def _audit_failure(
    request: Request, reason: str, status_code: int, tenant_id: UUID | None = None
) -> None:
    """auth.failure on the audit trail: the tenant's chain when the caller
    is known, else the system chain (rate-limited, best effort)."""
    await audit_trail.record_auth_failure(
        request,
        realm="tenant_admin",
        reason=reason,
        status_code=status_code,
        tenant_id=tenant_id,
        actor=audit_trail.tenant_key_actor(request) if tenant_id else None,
        db=request.state.db if tenant_id else None,
    )


async def resolve_admin(request: Request) -> AdminPrincipal:
    """Authenticate an admin route caller (admin token or tenant key)."""
    token = await bearer_token(request)
    if token.startswith(TOKEN_PREFIX):
        return await _resolve_admin_token(request, token)
    # Only a full-scope key: what every ingest key could do before #341.
    tenant_id = await resolve_tenant(request, *sorted(ALL_KEY_SCOPES))
    if settings.admin_require_scoped_tokens:
        await _audit_failure(request, "insufficient_scope", 403, tenant_id)
        raise _problem(
            403,
            "Forbidden",
            "This collector requires a scoped admin token on /api/v1/admin "
            "(PS_ADMIN_REQUIRE_SCOPED_TOKENS); tenant ingest keys are refused",
        )
    return AdminPrincipal(tenant_id=tenant_id, kind="tenant_key", scopes=TENANT_KEY_SCOPES)


async def require_full_admin(
    request: Request, principal: AdminPrincipal, key_scopes=ALL_KEY_SCOPES
) -> None:
    """403 unless the caller could already do anything a tenant ingest key
    can. Used before handing out an ingest key (issue, rotate): while
    PS_ADMIN_REQUIRE_SCOPED_TOKENS is off a full-scope ingest key reaches
    every admin route, so an ``admin:keys`` token must not be able to mint
    one. A narrowed key (``key_scopes`` short of every ingest key scope)
    cannot reach admin routes, so ``admin:keys`` is enough for it."""
    if settings.admin_require_scoped_tokens:
        return
    if not ALL_KEY_SCOPES <= set(key_scopes):
        return
    missing = sorted(TENANT_KEY_SCOPES - principal.scopes)
    if missing:
        await _audit_failure(request, "insufficient_scope", 403, principal.tenant_id)
        raise _problem(
            403,
            "Forbidden",
            "Issuing or rotating an ingest key needs every admin scope while "
            "PS_ADMIN_REQUIRE_SCOPED_TOKENS is off (an ingest key then has "
            f"full admin access); missing: {missing}",
        )


def can_manage_owner(principal: AdminPrincipal) -> bool:
    """Whether the caller may grant, change or remove an ``owner``
    membership: it holds every admin scope and, for a token issued for a
    principal, that principal is a tenant-wide owner (collector/rbac.py)."""
    if not set(ADMIN_SCOPES) <= principal.scopes:
        return False
    return principal.principal_id is None or rbac.OWNER in principal.roles


def require_admin(scope: str):
    """Dependency factory for a tenant admin route that needs ``scope``
    (GET routes also take ``admin:read``). A typo fails at import time."""
    if scope not in ADMIN_SCOPES or scope == READ_SCOPE:
        raise ValueError(f"Unknown admin scope: {scope!r}")

    async def _dep(request: Request) -> AdminPrincipal:
        principal = await resolve_admin(request)
        if not principal.allows(scope, request.method):
            await _audit_failure(request, "insufficient_scope", 403, principal.tenant_id)
            raise _problem(403, "Forbidden", f"Token lacks required scope: {scope}")
        return principal

    # Read by the spec contract test (tests/test_openapi_spec.py).
    _dep.admin_scope = scope
    return _dep


def admin_actor(request: Request, principal: AdminPrincipal | None) -> audit_trail.Actor:
    """The audit trail actor: the operator (a named operator token by name,
    or ``operator`` for PS_ADMIN_TOKEN), or the caller's credential (admin
    token or tenant key) by its SHA-256 lookup prefix."""
    if principal is None:
        return operator_auth.current_actor(request)
    return audit_trail.tenant_key_actor(request)


async def add_audit(
    request: Request,
    db: AsyncSession,
    *,
    tenant_id: UUID,
    status_code: int,
    action: str,
    principal: AdminPrincipal | None,
    target_type: str | None = None,
    target_id: str | UUID | None = None,
    **details,
) -> None:
    """Append an audit trail row (collector.audit_trail) for an admin change
    to ``tenant_id``'s chain. Flushes, does not commit: it commits with the
    change itself. ``principal=None`` means the operator. ``details`` are
    ids, names and scopes, never secrets (audit_trail redacts secret-looking
    keys anyway)."""
    await audit_trail.record(
        db,
        tenant_id=tenant_id,
        action=action,
        actor=admin_actor(request, principal),
        target_type=target_type,
        target_id=target_id,
        status_code=status_code,
        details=details,
    )


# ──────────────────────────────────────────────────────────────────────
# Token records
# ──────────────────────────────────────────────────────────────────────


def serialize_token(row: AdminToken) -> dict:
    """Metadata only: never the token or its hash."""

    def iso(v):
        return v.isoformat() if v else None

    return {
        "id": str(row.id),
        "name": row.name,
        "scopes": list(row.scopes or ()),
        "token_hint": row.token_hint,
        "principal_id": str(row.principal_id) if row.principal_id else None,
        "status": key_status(row),
        "created_by": row.created_by,
        "created_at": iso(row.created_at),
        "expires_at": iso(row.expires_at),
        "revoked_at": iso(row.revoked_at),
    }


def created_by(request: Request, principal: AdminPrincipal | None) -> str:
    """Who issued a token: 'operator', or the caller's key prefix
    (``token:<token_prefix>`` for a token made by another token)."""
    if principal is None:
        return "operator"
    prefix = bearer_key_prefix(request) or "unknown"
    kind = "token" if principal.kind == "admin_token" else "key"
    return f"{kind}:{prefix}"


async def cap_child_expiry(
    db: AsyncSession, principal: AdminPrincipal, expires_at: datetime | None
) -> datetime | None:
    """A token made by another token expires no later than its parent, so
    a short-lived token cannot mint a longer-lived (or never-expiring) one.
    Tokens made by a tenant key or the operator are not capped."""
    if principal.kind != "admin_token" or principal.token_id is None:
        return expires_at
    parent_expiry = (
        await db.execute(
            select(AdminToken.expires_at).where(AdminToken.id == principal.token_id)
        )
    ).scalar_one_or_none()
    if parent_expiry is None:
        return expires_at
    if expires_at is None or expires_at > parent_expiry:
        return parent_expiry
    return expires_at


async def revoke_descendants(
    db: AsyncSession, parent: AdminToken, now: datetime
) -> list[AdminToken]:
    """Revoke every live token that ``parent`` created, and the tokens those
    created, and so on (linked by ``created_by = token:<token_prefix>``).
    Returns the tokens revoked here. Flushes nothing; the caller audits and
    commits."""
    revoked: list[AdminToken] = []
    seen = {parent.id}
    frontier = [parent.token_prefix]
    while frontier:
        children = (
            (
                await db.execute(
                    select(AdminToken).where(
                        AdminToken.tenant_id == parent.tenant_id,
                        AdminToken.created_by.in_([f"token:{p}" for p in frontier]),
                    )
                )
            )
            .scalars()
            .all()
        )
        frontier = []
        for child in children:
            if child.id in seen:
                continue
            seen.add(child.id)
            # Walk through already revoked tokens too: their children may
            # still be live.
            frontier.append(child.token_prefix)
            if child.revoked_at is None:
                child.revoked_at = now
                revoked.append(child)
    return revoked


async def issue_token(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    name: str,
    scopes: list[str],
    expires_at: datetime | None,
    created_by: str,
    principal_id: UUID | None = None,
) -> tuple[AdminToken, str]:
    """Create a token. Returns (row, plaintext). Flushes; the caller commits."""
    taken = (
        await db.execute(
            select(AdminToken.id).where(
                AdminToken.tenant_id == tenant_id,
                AdminToken.name == name,
                AdminToken.revoked_at.is_(None),
            )
        )
    ).first()
    if taken is not None:
        raise DuplicateName(name)
    plaintext = generate_token()
    row = AdminToken(
        tenant_id=tenant_id,
        name=name,
        scopes=list(dict.fromkeys(scopes)),
        token_prefix=api_key_lookup_prefix(plaintext),
        token_hash=hash_api_key(plaintext),
        token_hint=plaintext[-4:],
        created_by=created_by,
        created_at=datetime.now(UTC),
        expires_at=expires_at,
        principal_id=principal_id,
    )
    db.add(row)
    await db.flush()
    return row, plaintext
