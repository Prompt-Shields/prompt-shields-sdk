"""Scoped automation tokens for the tenant admin API (/api/v1/admin).

Who may call an admin route
---------------------------
Every tenant admin route declares one scope from `ADMIN_SCOPES` with
``Depends(require_admin("<scope>"))``. GET routes also accept
``admin:read``. Two credentials reach them:

  - an admin token (``ps-at-...``, table ``admin_tokens``): tenant-scoped,
    with explicit scopes, an optional expiry, revocable. Meant for
    Terraform, CI and other automation.
  - a tenant ingest key (the key the SDK sends): full access to every admin
    route, as before admin tokens existed. This is the backwards-compatible
    default.

``PS_ADMIN_REQUIRE_SCOPED_TOKENS=true`` refuses tenant ingest keys on every
admin route (403), so only admin tokens with the right scope get in. It is
off by default so nothing breaks on upgrade. Turning it on is how an
operator closes the open question of whether any ingest key (including one
deployed with an SDK) should be able to rotate provider admin keys, issue
partner credentials or mint more keys. While it is off, ``admin:keys`` is
effectively full admin, because an ingest key it issues reaches every admin
route.

Tokens are stored like ingest keys (SHA-256 lookup prefix + bcrypt hash)
and returned once. A token can only issue tokens with scopes it holds.
Denied calls (403) are written to the audit log.
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

from collector import rls
from collector.audit import bearer_key_prefix, build_audit_row, record_read
from collector.auth import _DUMMY_HASH, bearer_token, resolve_tenant, tenant_disabled
from collector.config import settings
from collector.tenant_keys import DuplicateName, key_status

TOKEN_PREFIX = "ps-at-"
TOKEN_WARNING = "Save the token now. It will not be shown again."

# Scope -> what it grants. Order is the order shown by GET /token-scopes.
ADMIN_SCOPES: dict[str, str] = {
    "admin:read": "Read (GET) every tenant admin resource; changes nothing",
    "admin:keys": (
        "Issue, rotate and revoke tenant ingest keys. While "
        "PS_ADMIN_REQUIRE_SCOPED_TOKENS is off an ingest key reaches every "
        "admin route, so this scope is then effectively full admin"
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
                    Tenant.disabled_at,
                )
                .join(Tenant, Tenant.id == AdminToken.tenant_id)
                .where(AdminToken.token_prefix == api_key_lookup_prefix(token))
            )
        ).one_or_none()
    stored = row.token_hash if row is not None else _DUMMY_HASH
    valid = await asyncio.to_thread(verify_api_key, token, stored)
    if row is None or not valid:
        raise _problem(401, "Unauthorized", "Invalid admin token")
    if key_status(row) != "active":
        raise _problem(401, "Unauthorized", "Admin token revoked or expired")
    if row.disabled_at is not None:
        raise tenant_disabled()
    await rls.set_tenant(db, row.tenant_id)
    return AdminPrincipal(
        tenant_id=row.tenant_id,
        kind="admin_token",
        scopes=frozenset(row.scopes or ()),
        token_id=row.id,
    )


async def resolve_admin(request: Request) -> AdminPrincipal:
    """Authenticate an admin route caller (admin token or tenant key)."""
    token = bearer_token(request)
    if token.startswith(TOKEN_PREFIX):
        return await _resolve_admin_token(request, token)
    tenant_id = await resolve_tenant(request)
    if settings.admin_require_scoped_tokens:
        await record_read(request, tenant_id=tenant_id, status_code=403)
        raise _problem(
            403,
            "Forbidden",
            "This collector requires a scoped admin token on /api/v1/admin "
            "(PS_ADMIN_REQUIRE_SCOPED_TOKENS); tenant ingest keys are refused",
        )
    return AdminPrincipal(
        tenant_id=tenant_id, kind="tenant_key", scopes=frozenset(ADMIN_SCOPES)
    )


def require_admin(scope: str):
    """Dependency factory for a tenant admin route that needs ``scope``
    (GET routes also take ``admin:read``). A typo fails at import time."""
    if scope not in ADMIN_SCOPES or scope == READ_SCOPE:
        raise ValueError(f"Unknown admin scope: {scope!r}")

    async def _dep(request: Request) -> AdminPrincipal:
        principal = await resolve_admin(request)
        if not principal.allows(scope, request.method):
            await record_read(request, tenant_id=principal.tenant_id, status_code=403)
            raise _problem(403, "Forbidden", f"Token lacks required scope: {scope}")
        return principal

    # Read by the spec contract test (tests/test_openapi_spec.py).
    _dep.admin_scope = scope
    return _dep


def add_audit(
    request: Request,
    db: AsyncSession,
    *,
    tenant_id: UUID,
    status_code: int,
    action: str,
    operator: bool = False,
    **details,
) -> None:
    """Stage a partner_audit_log row for an admin change, committed with
    the change itself. ``details`` must be JSON-safe and never secrets
    (ids, names, scopes). Operator rows carry no key prefix."""
    row = build_audit_row(request, tenant_id=tenant_id, status_code=status_code)
    if operator:
        row.api_key_prefix = None
    row.query_params = {
        **(row.query_params or {}),
        "action": action,
        "actor": "operator" if operator else "tenant",
        **details,
    }
    db.add(row)


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
        "status": key_status(row),
        "created_by": row.created_by,
        "created_at": iso(row.created_at),
        "expires_at": iso(row.expires_at),
        "revoked_at": iso(row.revoked_at),
    }


def created_by(request: Request, principal: AdminPrincipal | None) -> str:
    """Who issued a token: 'operator', or the caller's key prefix."""
    if principal is None:
        return "operator"
    prefix = bearer_key_prefix(request) or "unknown"
    kind = "token" if principal.kind == "admin_token" else "key"
    return f"{kind}:{prefix}"


async def issue_token(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    name: str,
    scopes: list[str],
    expires_at: datetime | None,
    created_by: str,
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
    )
    db.add(row)
    await db.flush()
    return row, plaintext
