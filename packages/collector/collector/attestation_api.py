"""HTTP routes for human attestation (#98). Logic: collector.attestation.

Tenant registry (tenant API key):
  GET  /api/v1/registry/assets/{asset_id}/attestations
  POST /api/v1/registry/assets/{asset_id}/attestations

Partner API:
  GET  /partner/v1/assets/{asset_id}/attestations   scope assets:read
  POST /partner/v1/assets/{asset_id}/attestations   scope attestations:write

Every call, reads and writes, writes a `partner_audit_log` row through
`collector.audit.record_read`. A tenant API key is a machine credential, so
writes name the human in `attested_by`; the key prefix or partner id is
stored next to it. An asset in another tenant is a 404, the same as one
that does not exist.
"""

from __future__ import annotations

from uuid import UUID

from db.models import AIAsset
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from collector import attestation
from collector.attestation import AttestationState
from collector.audit import bearer_key_prefix, record_read
from collector.auth import resolve_tenant
from collector.partner_auth import PartnerPrincipal, require_scope
from collector.partner_registry import _audit, _enforce_rate_limit
from collector.sanitize import BoundedJsonRoute, StorableStr

WRITE_SCOPE = "attestations:write"
_READ_SCOPE = Depends(require_scope("assets:read"))
_WRITE_SCOPE = Depends(require_scope(WRITE_SCOPE))

tenant_router = APIRouter(
    prefix="/api/v1/registry", tags=["Registry"], route_class=BoundedJsonRoute
)
partner_router = APIRouter(
    prefix="/partner/v1", tags=["Partner API"], route_class=BoundedJsonRoute
)


class AttestationBody(BaseModel):
    field: StorableStr = Field(..., min_length=1, max_length=64)
    state: AttestationState
    attested_by: StorableStr = Field(
        ..., min_length=1, max_length=320, description="The person signing off, e.g. an email"
    )
    comment: StorableStr | None = Field(default=None, max_length=4000)


def _problem(status: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


async def _load_asset(db, tenant_id: UUID, asset_id: UUID) -> AIAsset | None:
    return (
        await db.execute(
            select(AIAsset).where(AIAsset.id == asset_id, AIAsset.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()


async def _history_body(db, tenant_id: UUID, asset: AIAsset, field: str | None, limit: int) -> dict:
    rows = await attestation.attestation_history(db, tenant_id, asset.id, field=field, limit=limit)
    latest = await attestation.latest_attestations(db, tenant_id, [asset.id])
    return {
        "data": [attestation.serialize_attestation(r) for r in rows],
        "meta": {
            "total": len(rows),
            "confidence": asset.confidence,
            "current": attestation.summarize(asset, latest.get(asset.id, {})),
        },
    }


async def _attest(
    db,
    tenant_id: UUID,
    asset: AIAsset,
    body: AttestationBody,
    *,
    actor_type: str,
    partner_id: UUID | None = None,
    api_key_prefix: str | None = None,
) -> dict:
    """Record one attestation and return the response body. Commits."""
    row = await attestation.record_attestation(
        db,
        tenant_id=tenant_id,
        asset=asset,
        field=body.field,
        state=body.state,
        attested_by=body.attested_by,
        comment=body.comment,
        actor_type=actor_type,
        partner_id=partner_id,
        api_key_prefix=api_key_prefix,
    )
    latest = await attestation.latest_attestations(db, tenant_id, [asset.id])
    # Serialize before the commit expires the loaded objects.
    out = {
        "data": attestation.serialize_attestation(row),
        "meta": {
            "confidence": asset.confidence,
            "current": attestation.summarize(asset, latest.get(asset.id, {})),
        },
    }
    await db.commit()
    return out


# ──────────────────────────────────────────────────────────────────────
# Tenant registry
# ──────────────────────────────────────────────────────────────────────


@tenant_router.get("/assets/{asset_id}/attestations")
async def list_attestations(
    asset_id: UUID,
    request: Request,
    field: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    tenant_id = await resolve_tenant(request)
    db = request.state.db
    asset = await _load_asset(db, tenant_id, asset_id)
    if asset is None:
        await record_read(request, tenant_id=tenant_id, status_code=404, response_count=0)
        raise _problem(404, "Not Found", "Asset not found")
    body = await _history_body(db, tenant_id, asset, field, limit)
    await record_read(request, tenant_id=tenant_id, status_code=200, response_count=len(body["data"]))
    return body


@tenant_router.post("/assets/{asset_id}/attestations", status_code=201)
async def create_attestation(asset_id: UUID, body: AttestationBody, request: Request):
    tenant_id = await resolve_tenant(request)
    db = request.state.db
    asset = await _load_asset(db, tenant_id, asset_id)
    if asset is None:
        await record_read(request, tenant_id=tenant_id, status_code=404, response_count=0)
        raise _problem(404, "Not Found", "Asset not found")
    try:
        out = await _attest(
            db,
            tenant_id,
            asset,
            body,
            actor_type=attestation.ACTOR_TENANT,
            api_key_prefix=bearer_key_prefix(request),
        )
    except ValueError as exc:
        await db.rollback()
        await record_read(request, tenant_id=tenant_id, status_code=400, response_count=0)
        raise _problem(400, "Bad Request", str(exc)) from exc
    await record_read(request, tenant_id=tenant_id, status_code=201, response_count=1)
    return out


# ──────────────────────────────────────────────────────────────────────
# Partner API
# ──────────────────────────────────────────────────────────────────────


@partner_router.get(
    "/assets/{asset_id}/attestations", summary="Attestation history for an asset"
)
async def partner_list_attestations(
    asset_id: UUID,
    request: Request,
    response: Response,
    field: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    principal: PartnerPrincipal = _READ_SCOPE,
):
    await _enforce_rate_limit(request, response, principal)
    db = request.state.db
    endpoint = f"/partner/v1/assets/{asset_id}/attestations"
    asset = await _load_asset(db, principal.tenant_id, asset_id)
    if asset is None:
        await _audit(request, principal, endpoint, "GET", 404)
        raise _problem(404, "Not Found", "Asset not found")
    body = await _history_body(db, principal.tenant_id, asset, field, limit)
    await _audit(request, principal, endpoint, "GET", 200, len(body["data"]))
    return body


@partner_router.post(
    "/assets/{asset_id}/attestations",
    status_code=201,
    summary="Record a human attestation for an asset field",
)
async def partner_create_attestation(
    asset_id: UUID,
    body: AttestationBody,
    request: Request,
    response: Response,
    principal: PartnerPrincipal = _WRITE_SCOPE,
):
    await _enforce_rate_limit(request, response, principal)
    db = request.state.db
    endpoint = f"/partner/v1/assets/{asset_id}/attestations"
    asset = await _load_asset(db, principal.tenant_id, asset_id)
    if asset is None:
        await _audit(request, principal, endpoint, "POST", 404)
        raise _problem(404, "Not Found", "Asset not found")
    try:
        out = await _attest(
            db,
            principal.tenant_id,
            asset,
            body,
            actor_type=attestation.ACTOR_PARTNER,
            partner_id=principal.partner_id,
        )
    except ValueError as exc:
        await db.rollback()
        await _audit(request, principal, endpoint, "POST", 400)
        raise _problem(400, "Bad Request", str(exc)) from exc
    await _audit(request, principal, endpoint, "POST", 201, 1)
    return out
