"""Partner API authentication.

Supports two flows:

  1. OAuth 2.0 Client Credentials. Partners POST to /oauth/token with
     client_id + client_secret and receive a short-lived JWT (1 hour).
  2. Partner API Key (fallback). Partners send the raw key as a Bearer
     token. We compare the SHA-256 hash.

`resolve_partner(request)` returns a PartnerPrincipal regardless of which
flow was used. Tenant boundary is implicit: the credentials row owns its
tenant_id and that's what every Partner API query is scoped against.

Scopes and rate-limit tiers are defined in `collector.partner_scopes`.
Guard a route with `require_scope("<scope>")`; it returns 403 unless the
credential holds that scope.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import bcrypt
from db.models import PartnerCredential, Tenant
from fastapi import HTTPException, Request
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from collector import audit_trail, rls
from collector.audit import record_read
from collector.partner_scopes import SCOPES, effective_rate_limit, expand_scopes

# ──────────────────────────────────────────────────────────────────────
# Constants and JWT config
# ──────────────────────────────────────────────────────────────────────

JWT_ALGORITHM = "HS256"
JWT_TTL_SECONDS = 3600  # 1 hour

# Secret must be set via env var in production. Default is for dev/test only.
JWT_SECRET = os.environ.get(
    "PS_PARTNER_JWT_SECRET",
    "dev-only-secret-do-not-use-in-production-min-32-chars",
)


# ──────────────────────────────────────────────────────────────────────
# Hashing helpers
# ──────────────────────────────────────────────────────────────────────


def hash_secret(plaintext: str) -> str:
    """Bcrypt hash for client_secret and api_key plaintexts."""
    return bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt(rounds=10)).decode()


def verify_secret(plaintext: str, hashed: str) -> bool:
    """Constant-time bcrypt comparison."""
    try:
        return bcrypt.checkpw(plaintext.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


def generate_client_id(partner_name: str) -> str:
    """ps-partner-<name>-<random>. Random suffix is URL-safe base64."""
    safe_name = "".join(c if c.isalnum() else "-" for c in partner_name.lower())[:20]
    return f"ps-partner-{safe_name}-{secrets.token_urlsafe(8)}"


def generate_secret(prefix: str = "sk-partner-secret") -> str:
    """Plaintext secret; bcrypt-hash before storing."""
    return f"{prefix}-{secrets.token_urlsafe(24)}"


def generate_api_key(partner_name: str) -> str:
    """Plaintext API key for the fallback flow; bcrypt-hash before storing."""
    safe_name = "".join(c if c.isalnum() else "-" for c in partner_name.lower())[:20]
    return f"ps-pk-{safe_name}-{secrets.token_urlsafe(24)}"


# ──────────────────────────────────────────────────────────────────────
# JWT issue / verify
# ──────────────────────────────────────────────────────────────────────


def issue_access_token(
    tenant_id: UUID,
    partner_id: UUID,
    client_id: str,
    scopes: list[str],
) -> tuple[str, int]:
    """Returns (token, expires_in_seconds)."""
    now = int(time.time())
    payload = {
        "iss": "prompt-shields",
        "aud": "partner-api",
        "sub": client_id,
        "tenant_id": str(tenant_id),
        "partner_id": str(partner_id),
        "scope": " ".join(scopes),
        "iat": now,
        "exp": now + JWT_TTL_SECONDS,
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return token, JWT_TTL_SECONDS


def verify_access_token(token: str) -> dict:
    """Decode and validate. Raises HTTPException(401) on failure."""
    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            audience="partner-api",
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=401,
            detail={
                "type": "about:blank",
                "title": "Unauthorized",
                "status": 401,
                "detail": f"Invalid or expired access token: {exc}",
            },
        ) from exc
    return payload


# ──────────────────────────────────────────────────────────────────────
# Partner principal — resolves either flow
# ──────────────────────────────────────────────────────────────────────


@dataclass
class PartnerPrincipal:
    tenant_id: UUID
    partner_id: UUID
    client_id: str
    scopes: set[str]
    # Requests per minute, resolved from the credential's tier or custom
    # limit. None when the principal was built without a credential row.
    rate_limit: int | None = None

    def has_scope(self, required: str) -> bool:
        """Deny by default: true only if `required` is among the expanded
        scopes (so a Phase 1 `registry:read` still covers the read scopes)."""
        return required in expand_scopes(self.scopes)


async def _lookup_by_api_key(db: AsyncSession, plaintext_key: str) -> PartnerCredential | None:
    """Find a partner credential whose api_key_hash matches `plaintext_key`.

    bcrypt doesn't support indexed lookup, so we scan active rows. At
    Phase-1 scale (dozens of partners per tenant, low hundreds total) this
    is fine. Phase-2: store a SHA-256 prefix in an indexed column for
    O(1) filtering before the bcrypt verify.
    """
    stmt = select(PartnerCredential).where(
        PartnerCredential.is_active == "true",
        PartnerCredential.api_key_hash.is_not(None),
    )
    result = await db.execute(stmt)
    for row in result.scalars():
        if verify_secret(plaintext_key, row.api_key_hash):
            return row
    return None


async def resolve_partner(request: Request) -> PartnerPrincipal:
    """Resolve the partner principal from the request (`_resolve_credential`),
    refusing a tenant the operator has disabled (403)."""
    principal = await _resolve_credential(request)
    disabled_at = (
        await request.state.db.execute(
            select(Tenant.disabled_at).where(Tenant.id == principal.tenant_id)
        )
    ).scalar_one_or_none()
    if disabled_at is not None:
        raise HTTPException(
            status_code=403,
            detail={
                "type": "about:blank",
                "title": "Forbidden",
                "status": 403,
                "detail": "Tenant is disabled",
            },
        )
    return principal


async def _resolve_credential(request: Request) -> PartnerPrincipal:
    """Resolve the partner principal from the request.

    Looks at the Authorization header. If the token is a JWT (3 dot-
    separated parts), verifies it. Otherwise treats it as an API key
    and bcrypt-checks against the partner_credentials table.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={
                "type": "about:blank",
                "title": "Unauthorized",
                "status": 401,
                "detail": "Missing or invalid Authorization header",
            },
        )
    token = auth.removeprefix("Bearer ").strip()
    db: AsyncSession = request.state.db

    # JWT format check — 3 dot-separated base64url segments
    if token.count(".") == 2 and not token.startswith("ps-pk-"):
        payload = verify_access_token(token)
        # The signed token names the tenant, so the revocation check below
        # already runs under row-level security for that tenant.
        await rls.set_tenant(db, UUID(payload["tenant_id"]))
        # Confirm the partner still exists and is active. Cheap, prevents
        # using a token issued before the partner was revoked.
        partner_id = UUID(payload["partner_id"])
        stmt = select(PartnerCredential).where(
            PartnerCredential.id == partner_id,
            PartnerCredential.is_active == "true",
        )
        result = await db.execute(stmt)
        partner = result.scalar_one_or_none()
        if partner is None:
            raise HTTPException(
                status_code=401,
                detail={
                    "type": "about:blank",
                    "title": "Unauthorized",
                    "status": 401,
                    "detail": "Partner credential revoked",
                },
            )
        if partner.expires_at and partner.expires_at < datetime.now(UTC):
            raise HTTPException(
                status_code=401,
                detail={
                    "type": "about:blank",
                    "title": "Unauthorized",
                    "status": 401,
                    "detail": "Partner credential expired",
                },
            )
        # A token never grants more than the credential holds now, so a
        # scope removed by the admin stops working before the token expires.
        token_scopes = expand_scopes((payload.get("scope") or "").split())
        return PartnerPrincipal(
            tenant_id=UUID(payload["tenant_id"]),
            partner_id=partner_id,
            client_id=payload["sub"],
            scopes=set(token_scopes & expand_scopes(partner.scopes)),
            rate_limit=_rate_limit_for(partner),
        )

    # API key fallback
    async with rls.auth_lookup(db):
        partner = await _lookup_by_api_key(db, token)
    if partner is None:
        raise HTTPException(
            status_code=401,
            detail={
                "type": "about:blank",
                "title": "Unauthorized",
                "status": 401,
                "detail": "Invalid API key",
            },
        )
    if partner.expires_at and partner.expires_at < datetime.now(UTC):
        raise HTTPException(
            status_code=401,
            detail={
                "type": "about:blank",
                "title": "Unauthorized",
                "status": 401,
                "detail": "Partner credential expired",
            },
        )
    await rls.set_tenant(db, partner.tenant_id)
    return PartnerPrincipal(
        tenant_id=partner.tenant_id,
        partner_id=partner.id,
        client_id=partner.client_id,
        scopes=set(expand_scopes(partner.scopes)),
        rate_limit=_rate_limit_for(partner),
    )


def _rate_limit_for(partner: PartnerCredential) -> int:
    return effective_rate_limit(partner.rate_limit_tier, partner.rate_limit)


# Scopes whose routes change tenant configuration or data. A partner
# refused one of them is an auth.failure in the tenant's audit trail
# (collector.audit_trail); read-scope refusals stay in partner_audit_log.
ADMIN_SCOPES = frozenset(
    {"webhooks:manage", "attestations:write", "provider-credentials:manage"}
)


def require_scope(scope: str):
    """Dependency factory: ensures the principal has the named scope.

    `scope` must be defined in `partner_scopes.SCOPES`; a typo fails at
    import time instead of silently locking every partner out. A missing
    scope is a 403, recorded in the audit log.
    """
    if scope not in SCOPES:
        raise ValueError(f"Unknown partner scope: {scope!r}")

    async def _dep(request: Request) -> PartnerPrincipal:
        principal = await resolve_partner(request)
        if not principal.has_scope(scope):
            if scope in ADMIN_SCOPES:
                await audit_trail.record_auth_failure(
                    request,
                    realm="partner",
                    reason="insufficient_scope",
                    status_code=403,
                    tenant_id=principal.tenant_id,
                    actor=audit_trail.partner_actor(principal.partner_id),
                    db=request.state.db,
                )
            await record_read(
                request,
                tenant_id=principal.tenant_id,
                partner_id=principal.partner_id,
                endpoint=str(request.url.path),
                method=request.method,
                status_code=403,
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "type": "about:blank",
                    "title": "Forbidden",
                    "status": 403,
                    "detail": f"Token lacks required scope: {scope}",
                },
            )
        return principal

    return _dep


# Convenience helper for the SHA-256 prefix optimization mentioned above
def api_key_prefix_sha256(plaintext_key: str) -> str:
    return hashlib.sha256(plaintext_key.encode()).hexdigest()[:16]
