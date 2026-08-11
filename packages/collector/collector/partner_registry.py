"""Partner API router — /partner/v1/*.

Read-only registry surface for EA tool integrations (Ardoq, LeanIX,
ServiceNow). Mirrors `/api/v1/registry/*` but with:

  - OAuth-or-API-key authentication via `partner_auth.resolve_partner`
  - Per-partner rate limiting (default 1000 req/min)
  - Audit logging on every request
  - Two new endpoints not in the internal API: /changes and /export

Response envelopes match the internal registry for consistency:
  { "data": [...], "meta": {"total", "has_more", "next_cursor"} }

Errors follow RFC 7807 Problem Details:
  { "type", "title", "status", "detail" }
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import and_, desc, func, select

from collector.partner_auth import (
    PartnerPrincipal,
    require_scope,
)
from collector.partner_rate_limit import get_rate_limiter
from db.models import AIAsset, DataFlow, PartnerAuditLog, PartnerCredential, RiskMapping

router = APIRouter(prefix="/partner/v1", tags=["Partner API"])


# ──────────────────────────────────────────────────────────────────────
# Cross-cutting helpers
# ──────────────────────────────────────────────────────────────────────


async def _audit(
    request: Request,
    principal: PartnerPrincipal,
    endpoint: str,
    method: str,
    status_code: int,
    response_count: int | None = None,
) -> None:
    """Write one row to partner_audit_log. Best-effort — never raises."""
    try:
        db = request.state.db
        ip = request.client.host if request.client else None
        ua = request.headers.get("User-Agent")
        db.add(
            PartnerAuditLog(
                tenant_id=principal.tenant_id,
                partner_id=principal.partner_id,
                endpoint=endpoint,
                method=method,
                status_code=status_code,
                response_count=response_count,
                ip_address=ip,
                user_agent=ua,
            )
        )
        await db.commit()
    except Exception:
        # Audit failures must not break the request. They go to the
        # observability layer in a real deployment.
        try:
            await request.state.db.rollback()
        except Exception:
            pass


async def _enforce_rate_limit(
    request: Request,
    response: Response,
    principal: PartnerPrincipal,
) -> None:
    """Enforce per-partner rate limit. Sets X-RateLimit-* headers on the
    response. Raises 429 on exceed."""
    # Load the partner's configured rate limit
    db = request.state.db
    stmt = select(PartnerCredential.rate_limit).where(
        PartnerCredential.id == principal.partner_id
    )
    limit = (await db.execute(stmt)).scalar() or 1000

    result = get_rate_limiter().check(principal.partner_id, limit)
    response.headers["X-RateLimit-Limit"] = str(result.limit)
    response.headers["X-RateLimit-Remaining"] = str(result.remaining)
    response.headers["X-RateLimit-Reset"] = str(result.reset_at)

    if not result.allowed:
        await _audit(
            request,
            principal,
            endpoint=str(request.url.path),
            method=request.method,
            status_code=429,
        )
        raise HTTPException(
            status_code=429,
            detail={
                "type": "about:blank",
                "title": "Too Many Requests",
                "status": 429,
                "detail": f"Rate limit exceeded. Retry after {result.retry_after} seconds.",
            },
            headers={"Retry-After": str(result.retry_after)},
        )


# ──────────────────────────────────────────────────────────────────────
# Serializers
# ──────────────────────────────────────────────────────────────────────


def _asset_dict(a: AIAsset) -> dict:
    return {
        "id": str(a.id),
        "vendor": a.vendor,
        "model": a.model,
        "use_case_name": a.use_case_name,
        "business_unit": a.business_unit,
        "owner_email": a.owner_email,
        "environment": a.environment,
        "status": a.status,
        "data_classification": a.data_classification,
        "discovery_source": a.discovery_source,
        "confidence": a.confidence,
        "first_seen": a.first_seen.isoformat() if a.first_seen else None,
        "last_seen": a.last_seen.isoformat() if a.last_seen else None,
    }


def _flow_dict(f: DataFlow) -> dict:
    return {
        "id": str(f.id),
        "source_system": f.source_system,
        "destination_system": f.destination_system,
        "data_classification": f.data_classification,
        "direction": f.direction,
        "detected_pii_types": f.detected_pii_types,
    }


def _risk_dict(r: RiskMapping) -> dict:
    return {
        "id": str(r.id),
        "risk_category": r.risk_category,
        "risk_level": r.risk_level,
        "framework": r.framework,
    }


# ──────────────────────────────────────────────────────────────────────
# Health (no auth)
# ──────────────────────────────────────────────────────────────────────


@router.get("/health", summary="Health check (no auth)")
async def health() -> dict:
    return {"status": "ok", "version": "1.0.0"}


# ──────────────────────────────────────────────────────────────────────
# Assets
# ──────────────────────────────────────────────────────────────────────


@router.get("/assets", summary="List AI assets")
async def list_assets(
    request: Request,
    response: Response,
    principal: PartnerPrincipal = Depends(require_scope("registry:read")),
    business_unit: str | None = None,
    status: str | None = None,
    discovery_source: str | None = None,
    since: str | None = None,
    after: str | None = None,
    vendor: str | None = None,
    confidence: str | None = None,
    data_classification: str | None = None,
    limit: int = Query(default=50, le=200, ge=1),
):
    await _enforce_rate_limit(request, response, principal)
    db = request.state.db

    conditions = [AIAsset.tenant_id == principal.tenant_id]
    if business_unit:
        conditions.append(AIAsset.business_unit == business_unit)
    if status:
        conditions.append(AIAsset.status == status)
    if discovery_source:
        conditions.append(AIAsset.discovery_source.any(discovery_source))
    if since:
        conditions.append(AIAsset.first_seen >= datetime.fromisoformat(since))
    if after:
        conditions.append(AIAsset.id > UUID(after))
    if vendor:
        conditions.append(AIAsset.vendor == vendor)
    if confidence:
        # Filter for assets at or above the requested confidence level.
        order = ["low", "medium", "high", "verified"]
        if confidence in order:
            threshold = order.index(confidence)
            conditions.append(
                AIAsset.confidence.in_(order[threshold:])
            )
    if data_classification:
        conditions.append(AIAsset.data_classification == data_classification)

    total = (await db.execute(
        select(func.count(AIAsset.id)).where(and_(*conditions))
    )).scalar() or 0

    rows = (
        await db.execute(
            select(AIAsset)
            .where(and_(*conditions))
            .order_by(AIAsset.id)
            .limit(limit)
        )
    ).scalars().all()

    data = [_asset_dict(a) for a in rows]
    has_more = len(data) == limit and total > limit
    next_cursor = data[-1]["id"] if has_more and data else None

    await _audit(
        request,
        principal,
        endpoint="/partner/v1/assets",
        method="GET",
        status_code=200,
        response_count=len(data),
    )

    return {
        "data": data,
        "meta": {"total": total, "has_more": has_more, "next_cursor": next_cursor},
    }


@router.get("/assets/{asset_id}", summary="Get one asset")
async def get_asset(
    asset_id: UUID,
    request: Request,
    response: Response,
    principal: PartnerPrincipal = Depends(require_scope("registry:read")),
):
    await _enforce_rate_limit(request, response, principal)
    db = request.state.db
    row = (
        await db.execute(
            select(AIAsset).where(
                AIAsset.id == asset_id,
                AIAsset.tenant_id == principal.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        await _audit(request, principal, f"/partner/v1/assets/{asset_id}", "GET", 404)
        raise HTTPException(
            status_code=404,
            detail={
                "type": "about:blank",
                "title": "Not Found",
                "status": 404,
                "detail": "Asset not found",
            },
        )
    await _audit(request, principal, f"/partner/v1/assets/{asset_id}", "GET", 200, 1)
    return _asset_dict(row)


@router.get("/assets/{asset_id}/data-flows", summary="Get data flows for an asset")
async def get_data_flows(
    asset_id: UUID,
    request: Request,
    response: Response,
    principal: PartnerPrincipal = Depends(require_scope("registry:read")),
):
    await _enforce_rate_limit(request, response, principal)
    db = request.state.db
    rows = (
        await db.execute(
            select(DataFlow).where(
                DataFlow.asset_id == asset_id,
                DataFlow.tenant_id == principal.tenant_id,
            )
        )
    ).scalars().all()
    await _audit(
        request, principal, f"/partner/v1/assets/{asset_id}/data-flows", "GET", 200, len(rows)
    )
    return {"data": [_flow_dict(f) for f in rows]}


@router.get("/assets/{asset_id}/risks", summary="Get risk mappings for an asset")
async def get_risks(
    asset_id: UUID,
    request: Request,
    response: Response,
    principal: PartnerPrincipal = Depends(require_scope("registry:read")),
):
    await _enforce_rate_limit(request, response, principal)
    db = request.state.db
    rows = (
        await db.execute(
            select(RiskMapping).where(
                RiskMapping.asset_id == asset_id,
                RiskMapping.tenant_id == principal.tenant_id,
            )
        )
    ).scalars().all()
    await _audit(
        request, principal, f"/partner/v1/assets/{asset_id}/risks", "GET", 200, len(rows)
    )
    return {"data": [_risk_dict(r) for r in rows]}


# ──────────────────────────────────────────────────────────────────────
# Taxonomy
# ──────────────────────────────────────────────────────────────────────


@router.get("/vendors", summary="List AI vendors in use")
async def list_vendors(
    request: Request,
    response: Response,
    principal: PartnerPrincipal = Depends(require_scope("registry:read")),
):
    await _enforce_rate_limit(request, response, principal)
    db = request.state.db
    rows = (
        await db.execute(
            select(AIAsset.vendor)
            .where(AIAsset.tenant_id == principal.tenant_id)
            .distinct()
        )
    ).all()
    data = sorted({row[0] for row in rows if row[0]})
    await _audit(request, principal, "/partner/v1/vendors", "GET", 200, len(data))
    return {"data": data}


@router.get("/models", summary="List AI models in use")
async def list_models(
    request: Request,
    response: Response,
    principal: PartnerPrincipal = Depends(require_scope("registry:read")),
):
    await _enforce_rate_limit(request, response, principal)
    db = request.state.db
    rows = (
        await db.execute(
            select(AIAsset.model)
            .where(
                AIAsset.tenant_id == principal.tenant_id,
                AIAsset.model.is_not(None),
            )
            .distinct()
        )
    ).all()
    data = sorted({row[0] for row in rows if row[0]})
    await _audit(request, principal, "/partner/v1/models", "GET", 200, len(data))
    return {"data": data}


@router.get("/business-units", summary="List business units with AI usage")
async def list_business_units(
    request: Request,
    response: Response,
    principal: PartnerPrincipal = Depends(require_scope("registry:read")),
):
    await _enforce_rate_limit(request, response, principal)
    db = request.state.db
    rows = (
        await db.execute(
            select(AIAsset.business_unit)
            .where(
                AIAsset.tenant_id == principal.tenant_id,
                AIAsset.business_unit.is_not(None),
            )
            .distinct()
        )
    ).all()
    data = sorted({row[0] for row in rows if row[0]})
    await _audit(
        request, principal, "/partner/v1/business-units", "GET", 200, len(data)
    )
    return {"data": data}


# ──────────────────────────────────────────────────────────────────────
# Sync — changes + export
# ──────────────────────────────────────────────────────────────────────


@router.get("/changes", summary="Delta feed — assets changed since timestamp")
async def get_changes(
    request: Request,
    response: Response,
    since: str = Query(..., description="ISO 8601 timestamp"),
    after: str | None = None,
    limit: int = Query(default=50, le=200, ge=1),
    principal: PartnerPrincipal = Depends(require_scope("registry:read")),
):
    await _enforce_rate_limit(request, response, principal)
    db = request.state.db

    try:
        since_dt = datetime.fromisoformat(since)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "type": "about:blank",
                "title": "Bad Request",
                "status": 400,
                "detail": f"Invalid `since` timestamp: {exc}",
            },
        ) from exc

    conditions = [
        AIAsset.tenant_id == principal.tenant_id,
        AIAsset.updated_at >= since_dt,
    ]
    if after:
        conditions.append(AIAsset.id > UUID(after))

    total = (await db.execute(
        select(func.count(AIAsset.id)).where(and_(*conditions))
    )).scalar() or 0

    rows = (
        await db.execute(
            select(AIAsset)
            .where(and_(*conditions))
            .order_by(AIAsset.id)
            .limit(limit)
        )
    ).scalars().all()

    data = []
    latest_change = since_dt
    for a in rows:
        change_type = "created" if a.created_at >= since_dt else "updated"
        if a.status == "deprecated" and a.updated_at >= since_dt:
            change_type = "deactivated"
        if a.last_seen and a.last_seen > latest_change:
            latest_change = a.last_seen
        data.append({
            "change_type": change_type,
            "asset": _asset_dict(a),
            "changed_at": a.updated_at.isoformat() if a.updated_at else None,
            # `changed_fields` requires field-level audit; deferred to Phase 2.
            "changed_fields": None,
        })

    has_more = len(data) == limit and total > limit
    next_cursor = data[-1]["asset"]["id"] if has_more and data else None

    await _audit(
        request, principal, "/partner/v1/changes", "GET", 200, len(data)
    )

    return {
        "data": data,
        "meta": {
            "total": total,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "sync_token": latest_change.isoformat(),
        },
    }


@router.get("/export", summary="Bulk export of the full registry")
async def export_registry(
    request: Request,
    response: Response,
    format: str = Query(default="json"),
    principal: PartnerPrincipal = Depends(require_scope("registry:read")),
):
    if format != "json":
        raise HTTPException(
            status_code=400,
            detail={
                "type": "about:blank",
                "title": "Bad Request",
                "status": 400,
                "detail": "Only format=json is supported",
            },
        )

    await _enforce_rate_limit(request, response, principal)
    db = request.state.db

    rows = (
        await db.execute(
            select(AIAsset)
            .where(AIAsset.tenant_id == principal.tenant_id)
            .order_by(AIAsset.id)
        )
    ).scalars().all()

    now = datetime.utcnow()
    await _audit(request, principal, "/partner/v1/export", "GET", 200, len(rows))

    return {
        "data": [_asset_dict(a) for a in rows],
        "meta": {
            "total": len(rows),
            "exported_at": now.isoformat() + "Z",
            "sync_token": now.isoformat() + "Z",
        },
    }
