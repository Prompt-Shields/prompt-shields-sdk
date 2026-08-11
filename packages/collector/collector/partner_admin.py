"""Internal admin endpoint to issue Partner API credentials.

Mounted at /api/v1/admin/partner-credentials. Uses the existing
internal PS API key auth (resolve_tenant) — only tenant admins can call
this. Returns the plaintext client_secret and api_key ONCE on creation;
subsequent reads expose only the hashes.

This is the dashboard's hook for the "Partners > Add Integration" UX.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from collector.auth import resolve_tenant
from collector.partner_auth import (
    generate_api_key,
    generate_client_id,
    generate_secret,
    hash_secret,
)
from db.models import PartnerCredential

router = APIRouter(prefix="/api/v1/admin", tags=["Admin"])


ALLOWED_SCOPES = {
    "registry:read",
    "data-flows:read",
    "risks:read",
    "usage:read",
    "export:read",
    "changes:read",
}


class IssuePartnerCredentialBody(BaseModel):
    partner_name: str = Field(..., min_length=1, max_length=80)
    scopes: list[str] = Field(default_factory=lambda: list(ALLOWED_SCOPES))
    issue_api_key: bool = True
    rate_limit: int = 1000
    expires_at: datetime | None = None
    metadata: dict | None = None


class IssuePartnerCredentialResponse(BaseModel):
    id: str
    partner_name: str
    client_id: str
    client_secret: str  # plaintext, shown ONCE
    api_key: str | None  # plaintext, shown ONCE
    scopes: list[str]
    rate_limit: int
    created_at: datetime
    expires_at: datetime | None
    warning: Literal["Save the client_secret and api_key now — they will never be shown again."]


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

    bad = set(body.scopes) - ALLOWED_SCOPES
    if bad:
        raise HTTPException(
            status_code=400,
            detail={
                "type": "about:blank",
                "title": "Bad Request",
                "status": 400,
                "detail": f"Unknown scopes: {sorted(bad)}. Allowed: {sorted(ALLOWED_SCOPES)}",
            },
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
        scopes=body.scopes,
        rate_limit=body.rate_limit,
        is_active="true",
        partner_metadata=body.metadata or {},
        expires_at=body.expires_at,
    )
    db.add(partner)
    await db.commit()
    await db.refresh(partner)

    return IssuePartnerCredentialResponse(
        id=str(partner.id),
        partner_name=partner.partner_name,
        client_id=partner.client_id,
        client_secret=client_secret,
        api_key=api_key,
        scopes=partner.scopes,
        rate_limit=partner.rate_limit,
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

    return {
        "data": [
            {
                "id": str(r.id),
                "partner_name": r.partner_name,
                "client_id": r.client_id,
                "scopes": r.scopes,
                "rate_limit": r.rate_limit,
                "is_active": r.is_active,
                "has_api_key": r.api_key_hash is not None,
                "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.delete(
    "/partner-credentials/{credential_id}",
    status_code=204,
    summary="Revoke a partner credential",
)
async def revoke_partner_credential(
    credential_id: str,
    request: Request,
) -> None:
    from uuid import UUID

    tenant_id = await resolve_tenant(request)
    db = request.state.db
    row = (
        await db.execute(
            select(PartnerCredential).where(
                PartnerCredential.id == UUID(credential_id),
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
    row.is_active = "false"
    await db.commit()
