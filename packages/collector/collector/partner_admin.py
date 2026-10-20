"""Internal admin endpoint to issue Partner API credentials.

Mounted at /api/v1/admin/partner-credentials and /partner-scopes. Uses
the existing internal PS API key auth (resolve_tenant) — only tenant
admins can call this. Returns the plaintext client_secret and api_key
ONCE on creation; subsequent reads expose only the hashes. Scopes and
rate-limit tiers come from `collector.partner_scopes`.

This is the dashboard's hook for the "Partners > Add Integration" UX.

Issuing, changing (scopes, rate limit) and revoking a credential each
write an `audit_events` row (collector.audit_trail) with the change: scope
names and tier, never the secret or the API key.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from db.models import PartnerCredential
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from collector import audit_trail
from collector.auth import resolve_tenant
from collector.partner_auth import (
    generate_api_key,
    generate_client_id,
    generate_secret,
    hash_secret,
)
from collector.partner_scopes import (
    DEFAULT_SCOPES,
    GRANTABLE_SCOPES,
    RATE_LIMIT_TIERS,
    SCOPE_ALIASES,
    SCOPES,
    effective_rate_limit,
    unknown_scopes,
)

router = APIRouter(prefix="/api/v1/admin", tags=["Admin"])


# Kept for importers; the source of truth is collector.partner_scopes.
ALLOWED_SCOPES = GRANTABLE_SCOPES


class IssuePartnerCredentialBody(BaseModel):
    partner_name: str = Field(..., min_length=1, max_length=80)
    # Deny by default: read scopes only unless the admin asks for more.
    scopes: list[str] = Field(default_factory=lambda: list(DEFAULT_SCOPES))
    issue_api_key: bool = True
    # Either a named tier or a custom limit; neither means "standard".
    rate_limit_tier: str | None = None
    rate_limit: int | None = Field(default=None, ge=1)
    expires_at: datetime | None = None
    metadata: dict | None = None


class UpdatePartnerCredentialBody(BaseModel):
    """Fields left out are unchanged. Setting `rate_limit` clears the tier."""

    scopes: list[str] | None = None
    rate_limit_tier: str | None = None
    rate_limit: int | None = Field(default=None, ge=1)


class IssuePartnerCredentialResponse(BaseModel):
    id: str
    partner_name: str
    client_id: str
    client_secret: str  # plaintext, shown ONCE
    api_key: str | None  # plaintext, shown ONCE
    scopes: list[str]
    rate_limit: int
    rate_limit_tier: str | None
    created_at: datetime
    expires_at: datetime | None
    warning: Literal["Save the client_secret and api_key now — they will never be shown again."]


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={
            "type": "about:blank",
            "title": "Bad Request",
            "status": 400,
            "detail": detail,
        },
    )


def _validated_scopes(scopes: list[str]) -> list[str]:
    bad = unknown_scopes(scopes)
    if bad:
        raise _bad_request(
            f"Unknown scopes: {bad}. Allowed: {sorted(GRANTABLE_SCOPES)}"
        )
    return list(dict.fromkeys(scopes))  # de-duplicate, keep order


def _resolve_rate_limit(
    tier: str | None, custom: int | None, *, default_tier: str | None
) -> tuple[str | None, int | None]:
    """(tier, rate_limit) to store. A tier stores its limit too, so the
    column stays meaningful if the tier is later removed."""
    if tier is not None and custom is not None:
        raise _bad_request("Set either rate_limit_tier or rate_limit, not both")
    if custom is not None:
        return None, custom
    tier = tier or default_tier
    if tier is None:
        return None, None
    if tier not in RATE_LIMIT_TIERS:
        raise _bad_request(
            f"Unknown rate_limit_tier: {tier!r}. Allowed: {sorted(RATE_LIMIT_TIERS)}"
        )
    return tier, RATE_LIMIT_TIERS[tier]


def _credential_dict(r: PartnerCredential) -> dict:
    return {
        "id": str(r.id),
        "partner_name": r.partner_name,
        "client_id": r.client_id,
        "scopes": r.scopes,
        "rate_limit": effective_rate_limit(r.rate_limit_tier, r.rate_limit),
        "rate_limit_tier": r.rate_limit_tier,
        "is_active": r.is_active,
        "has_api_key": r.api_key_hash is not None,
        "expires_at": r.expires_at.isoformat() if r.expires_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


async def _get_credential(request: Request, credential_id: UUID) -> PartnerCredential:
    tenant_id = await resolve_tenant(request)
    row = (
        await request.state.db.execute(
            select(PartnerCredential).where(
                PartnerCredential.id == credential_id,
                PartnerCredential.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "type": "about:blank",
                "title": "Not Found",
                "status": 404,
                "detail": "Partner credential not found",
            },
        )
    return row


@router.get("/partner-scopes", summary="List partner scopes and rate-limit tiers")
async def list_partner_scopes(request: Request):
    await resolve_tenant(request)
    return {
        "scopes": [{"name": k, "description": v} for k, v in SCOPES.items()],
        "aliases": {k: sorted(v) for k, v in SCOPE_ALIASES.items()},
        "default_scopes": list(DEFAULT_SCOPES),
        "rate_limit_tiers": [
            {"name": k, "requests_per_minute": v} for k, v in RATE_LIMIT_TIERS.items()
        ],
    }


@router.post(
    "/partner-credentials",
    response_model=IssuePartnerCredentialResponse,
    status_code=201,
    summary="Issue a new Partner API credential",
)
async def issue_partner_credential(
    body: IssuePartnerCredentialBody,
    request: Request,
) -> IssuePartnerCredentialResponse:
    tenant_id = await resolve_tenant(request)

    scopes = _validated_scopes(body.scopes)
    tier, rate_limit = _resolve_rate_limit(
        body.rate_limit_tier, body.rate_limit, default_tier="standard"
    )

    client_id = generate_client_id(body.partner_name)
    client_secret = generate_secret()
    api_key = generate_api_key(body.partner_name) if body.issue_api_key else None

    db = request.state.db
    partner = PartnerCredential(
        tenant_id=tenant_id,
        partner_name=body.partner_name,
        client_id=client_id,
        client_secret_hash=hash_secret(client_secret),
        api_key_hash=hash_secret(api_key) if api_key else None,
        scopes=scopes,
        rate_limit=rate_limit,
        rate_limit_tier=tier,
        is_active="true",
        partner_metadata=body.metadata or {},
        expires_at=body.expires_at,
    )
    db.add(partner)
    await db.flush()
    await audit_trail.record(
        db,
        tenant_id=tenant_id,
        action="partner_credential.create",
        actor=audit_trail.tenant_key_actor(request),
        target_type="partner_credential",
        target_id=partner.id,
        status_code=201,
        details={
            "scopes": scopes,
            "rate_limit_tier": tier,
            "rate_limit": rate_limit,
            "issued_partner_api_key": api_key is not None,
            "expires_at": body.expires_at,
        },
    )
    await db.commit()
    await db.refresh(partner)

    return IssuePartnerCredentialResponse(
        id=str(partner.id),
        partner_name=partner.partner_name,
        client_id=partner.client_id,
        client_secret=client_secret,
        api_key=api_key,
        scopes=partner.scopes,
        rate_limit=effective_rate_limit(partner.rate_limit_tier, partner.rate_limit),
        rate_limit_tier=partner.rate_limit_tier,
        created_at=partner.created_at,
        expires_at=partner.expires_at,
        warning="Save the client_secret and api_key now — they will never be shown again.",
    )


@router.get(
    "/partner-credentials",
    summary="List partner credentials for this tenant",
)
async def list_partner_credentials(request: Request):
    tenant_id = await resolve_tenant(request)
    db = request.state.db
    rows = (
        await db.execute(
            select(PartnerCredential).where(
                PartnerCredential.tenant_id == tenant_id,
            )
        )
    ).scalars().all()

    return {"data": [_credential_dict(r) for r in rows]}


@router.patch(
    "/partner-credentials/{credential_id}",
    summary="Change a partner credential's scopes or rate-limit tier",
)
async def update_partner_credential(
    credential_id: UUID,
    body: UpdatePartnerCredentialBody,
    request: Request,
):
    """Takes effect on the partner's next request, including requests made
    with access tokens issued before the change."""
    row = await _get_credential(request, credential_id)
    tenant_id = row.tenant_id
    before_scopes = list(row.scopes or [])
    before_limit = (row.rate_limit_tier, row.rate_limit)
    try:
        if body.scopes is not None:
            row.scopes = _validated_scopes(body.scopes)
        if body.rate_limit_tier is not None or body.rate_limit is not None:
            row.rate_limit_tier, row.rate_limit = _resolve_rate_limit(
                body.rate_limit_tier, body.rate_limit, default_tier=None
            )
    except HTTPException as exc:
        await request.state.db.rollback()
        await audit_trail.record_committed(
            request.state.db,
            tenant_id=tenant_id,
            action="partner_credential.update",
            outcome=audit_trail.FAILURE,
            actor=audit_trail.tenant_key_actor(request),
            target_type="partner_credential",
            target_id=credential_id,
            status_code=exc.status_code,
        )
        raise
    after_scopes = list(row.scopes or [])
    await audit_trail.record(
        request.state.db,
        tenant_id=row.tenant_id,
        action="partner_credential.update",
        actor=audit_trail.tenant_key_actor(request),
        target_type="partner_credential",
        target_id=row.id,
        status_code=200,
        details={
            "scopes": after_scopes,
            "scopes_added": sorted(set(after_scopes) - set(before_scopes)),
            "scopes_removed": sorted(set(before_scopes) - set(after_scopes)),
            "rate_limit_tier": row.rate_limit_tier,
            "rate_limit": row.rate_limit,
            "rate_limit_changed": (row.rate_limit_tier, row.rate_limit) != before_limit,
        },
    )
    await request.state.db.commit()
    await request.state.db.refresh(row)
    return _credential_dict(row)


@router.delete(
    "/partner-credentials/{credential_id}",
    status_code=204,
    summary="Revoke a partner credential",
)
async def revoke_partner_credential(
    credential_id: UUID,
    request: Request,
) -> None:
    row = await _get_credential(request, credential_id)
    row.is_active = "false"
    await audit_trail.record(
        request.state.db,
        tenant_id=row.tenant_id,
        action="partner_credential.revoke",
        actor=audit_trail.tenant_key_actor(request),
        target_type="partner_credential",
        target_id=row.id,
        status_code=204,
    )
    await request.state.db.commit()
