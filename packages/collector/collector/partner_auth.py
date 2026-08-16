"""Partner API authentication.

Supports two flows:

  1. OAuth 2.0 Client Credentials. Partners POST to /oauth/token with
     client_id + client_secret and receive a short-lived JWT (1 hour).
  2. Partner API Key (fallback). Partners send the raw key as a Bearer
     token. We compare the SHA-256 hash.

`resolve_partner(request)` returns a PartnerPrincipal regardless of which
flow was used. Tenant boundary is implicit: the credentials row owns its
tenant_id and that's what every Partner API query is scoped against.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import bcrypt
from fastapi import HTTPException, Request
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import PartnerCredential


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

    def has_scope(self, required: str) -> bool:
        return required in self.scopes


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
        if partner.expires_at and partner.expires_at < datetime.now(timezone.utc):
            raise HTTPException(
                status_code=401,
                detail={
                    "type": "about:blank",
                    "title": "Unauthorized",
                    "status": 401,
                    "detail": "Partner credential expired",
                },
            )
        return PartnerPrincipal(
            tenant_id=UUID(payload["tenant_id"]),
            partner_id=partner_id,
            client_id=payload["sub"],
            scopes=set((payload.get("scope") or "").split()),
        )

    # API key fallback
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
    if partner.expires_at and partner.expires_at < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=401,
            detail={
                "type": "about:blank",
                "title": "Unauthorized",
                "status": 401,
                "detail": "Partner credential expired",
            },
        )
    return PartnerPrincipal(
        tenant_id=partner.tenant_id,
        partner_id=partner.id,
        client_id=partner.client_id,
        scopes=set(partner.scopes or []),
    )


def require_scope(scope: str):
    """Dependency factory: ensures the principal has the named scope."""

    async def _dep(request: Request) -> PartnerPrincipal:
        principal = await resolve_partner(request)
        if not principal.has_scope(scope):
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
