"""OAuth 2.0 Client Credentials endpoint for the Partner API.

Standards-compliant subset of RFC 6749 Section 4.4:
  - grant_type=client_credentials
  - Form-encoded body
  - Returns JSON with access_token, token_type, expires_in, scope
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Form, HTTPException, Request
from sqlalchemy import select

from collector.partner_auth import (
    JWT_TTL_SECONDS,
    issue_access_token,
    verify_secret,
)
from db.models import PartnerCredential

router = APIRouter(tags=["Partner OAuth"])


@router.post("/oauth/token", summary="Exchange client credentials for an access token")
async def token_endpoint(
    request: Request,
    grant_type: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
):
    """RFC 6749 §4.4 — Client Credentials grant.

    Errors use the OAuth error response format
    (https://www.rfc-editor.org/rfc/rfc6749#section-5.2): JSON body with
    "error" and "error_description" keys.
    """
    if grant_type != "client_credentials":
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unsupported_grant_type",
                "error_description": "Only client_credentials is supported",
            },
        )

    db = request.state.db
    stmt = select(PartnerCredential).where(PartnerCredential.client_id == client_id)
    result = await db.execute(stmt)
    partner = result.scalar_one_or_none()

    if partner is None or partner.is_active != "true":
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_client",
                "error_description": "Unknown or inactive client",
            },
        )

    if partner.expires_at and partner.expires_at < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_client",
                "error_description": "Client credential expired",
            },
        )

    if not verify_secret(client_secret, partner.client_secret_hash):
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_client",
                "error_description": "Invalid client_secret",
            },
        )

    scopes = list(partner.scopes or [])
    token, expires_in = issue_access_token(
        tenant_id=partner.tenant_id,
        partner_id=partner.id,
        client_id=partner.client_id,
        scopes=scopes,
    )

    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "scope": " ".join(scopes),
    }
