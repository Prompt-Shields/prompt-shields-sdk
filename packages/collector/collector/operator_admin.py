"""Operator endpoints for data retention and tenant erasure (#85).

Mounted at /api/v1/operator. These act across tenants, so they are not
behind a tenant API key: they take ``Authorization: Bearer $PS_ADMIN_TOKEN``.
With ``PS_ADMIN_TOKEN`` unset every route answers 404, as if absent.
"""

from __future__ import annotations

import hmac
from dataclasses import asdict
from uuid import UUID

from db.models import Tenant
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from collector import retention
from collector.config import settings

router = APIRouter(prefix="/api/v1/operator", tags=["Operator"])


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


async def require_operator(request: Request) -> str:
    """Check the operator token. Returns the actor name used in the audit log."""
    token = settings.admin_token
    if not token:
        raise _problem(404, "Not Found", "Not Found")
    auth = request.headers.get("Authorization", "")
    presented = (
        auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    )
    if not presented or not hmac.compare_digest(presented.encode(), token.encode()):
        raise _problem(401, "Unauthorized", "Invalid operator token")
    return "operator-api"


class PurgeBody(BaseModel):
    tenant_id: UUID | None = None
    dry_run: bool = False


@router.post("/retention/purge", summary="Delete usage events past retention")
async def purge(
    body: PurgeBody, request: Request, actor: str = Depends(require_operator)
):
    db = request.state.db
    if body.tenant_id is not None:
        result = await retention.purge_tenant_events(
            db, body.tenant_id, dry_run=body.dry_run, actor=actor
        )
        if result is None:
            raise _problem(404, "Not Found", "Tenant not found")
        results = [result]
    else:
        results = await retention.purge_expired_events(
            db, dry_run=body.dry_run, actor=actor
        )
    return {"data": [asdict(r) for r in results]}


class RetentionBody(BaseModel):
    # None clears the override, falling back to PS_RETENTION_DAYS_DEFAULT.
    retention_days: int | None = Field(
        None, ge=retention.MIN_RETENTION_DAYS, le=retention.MAX_RETENTION_DAYS
    )


@router.get("/tenants/{tenant_id}/retention", summary="Show a tenant's retention")
async def get_retention(
    tenant_id: UUID, request: Request, actor: str = Depends(require_operator)
):
    row = (
        await request.state.db.execute(
            select(Tenant.settings).where(Tenant.id == tenant_id)
        )
    ).one_or_none()
    if row is None:
        raise _problem(404, "Not Found", "Tenant not found")
    tenant_settings = row[0] or {}
    return {
        "tenant_id": str(tenant_id),
        "retention_days": retention.retention_days_for(tenant_settings),
        "cutoff": retention.retention_cutoff(tenant_settings).isoformat(),
    }


@router.put("/tenants/{tenant_id}/retention", summary="Set a tenant's retention")
async def put_retention(
    tenant_id: UUID,
    body: RetentionBody,
    request: Request,
    actor: str = Depends(require_operator),
):
    days = await retention.set_tenant_retention(
        request.state.db, tenant_id, body.retention_days, actor=actor
    )
    if days is None:
        raise _problem(404, "Not Found", "Tenant not found")
    return {"tenant_id": str(tenant_id), "retention_days": days}


@router.delete("/tenants/{tenant_id}", summary="Erase a tenant and all its data")
async def erase(
    tenant_id: UUID,
    request: Request,
    confirm: str = Query(..., description="Must repeat the tenant id"),
    actor: str = Depends(require_operator),
):
    if confirm.strip().lower() != str(tenant_id):
        raise _problem(400, "Bad Request", "confirm must equal the tenant id")
    result = await retention.erase_tenant(request.state.db, tenant_id, actor=actor)
    return asdict(result)
