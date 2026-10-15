"""Partner API router — /partner/v1/*.

Read-only registry surface for EA tool integrations (Ardoq, LeanIX,
ServiceNow). Mirrors `/api/v1/registry/*` but with:

  - OAuth-or-API-key authentication via `partner_auth.resolve_partner`
  - One scope per route (`partner_scopes.SCOPES`), 403 without it
  - Per-partner rate limiting by tier (default 1000 req/min)
  - Audit logging on every request
  - Two new endpoints not in the internal API: /changes and /export

Response envelopes match the internal registry for consistency:
  { "data": [...], "meta": {"total", "has_more", "next_cursor"} }

Errors follow RFC 7807 Problem Details:
  { "type", "title", "status", "detail" }
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import and_, desc, func, select

from collector import aibom
from collector.audit import record_read
from collector.lifecycle import LifecycleState, lifecycle_change_for
from collector.partner_auth import (
    PartnerPrincipal,
    require_scope,
)
from collector.partner_rate_limit import get_rate_limiter
from collector.partner_scopes import DEFAULT_RATE_LIMIT
from collector.retention import get_retention_cutoff
from db.models import (
    AIAsset,
    AIUsageEvent,
    DataFlow,
    PartnerCredential,
    RiskMapping,
)

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
    asset_ids: list[UUID | str] | None = None,
) -> None:
    """Write one row to partner_audit_log. Best-effort — never raises.

    Thin wrapper over `collector.audit.record_read`, the same writer the
    tenant registry uses, so every partner endpoint that calls `_audit`
    also records path asset ids and redacted query params. List-style
    endpoints pass the returned `asset_ids`.
    """
    await record_read(
        request,
        tenant_id=principal.tenant_id,
        partner_id=principal.partner_id,
        endpoint=endpoint,
        method=method,
        status_code=status_code,
        response_count=response_count,
        asset_ids=asset_ids,
    )


async def _enforce_rate_limit(
    request: Request,
    response: Response,
    principal: PartnerPrincipal,
) -> None:
    """Enforce per-partner rate limit. Sets X-RateLimit-* headers on the
    response. Raises 429 on exceed.

    The limit comes from the credential's tier or custom limit, resolved
    with the principal; the lookup is only a fallback for principals built
    without a credential row."""
    limit = principal.rate_limit
    if limit is None:
        db = request.state.db
        stmt = select(PartnerCredential.rate_limit).where(
            PartnerCredential.id == principal.partner_id
        )
        limit = (await db.execute(stmt)).scalar() or DEFAULT_RATE_LIMIT

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
        "lifecycle_state": a.lifecycle_state or "active",
        "lifecycle_changed_at": (
            a.lifecycle_changed_at.isoformat() if a.lifecycle_changed_at else None
        ),
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
    principal: PartnerPrincipal = Depends(require_scope("assets:read")),
    business_unit: str | None = None,
    status: str | None = None,
    lifecycle_state: LifecycleState | None = None,
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
    if lifecycle_state:
        conditions.append(AIAsset.lifecycle_state == lifecycle_state)
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
        asset_ids=[a["id"] for a in data],
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
    principal: PartnerPrincipal = Depends(require_scope("assets:read")),
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
    principal: PartnerPrincipal = Depends(require_scope("data-flows:read")),
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
    principal: PartnerPrincipal = Depends(require_scope("risks:read")),
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


# Supported `period` values for /usage-summary, in days.
USAGE_PERIODS: dict[str, int] = {"7d": 7, "30d": 30, "90d": 90}


@router.get(
    "/assets/{asset_id}/usage-summary", summary="Get usage summary for an asset"
)
async def get_usage_summary(
    asset_id: UUID,
    request: Request,
    response: Response,
    period: Literal["7d", "30d", "90d"] = Query(
        default="30d", description="Time period for aggregation"
    ),
    principal: PartnerPrincipal = Depends(require_scope("usage:read")),
):
    """Aggregated usage for one asset over the trailing `period`.

    Aggregates `ai_usage_events` only; never returns individual events or
    prompt content. An asset outside the caller's tenant is a 404, the same
    as an asset that does not exist, so existence does not leak.
    """
    await _enforce_rate_limit(request, response, principal)
    db = request.state.db
    endpoint = f"/partner/v1/assets/{asset_id}/usage-summary"

    owned = (
        await db.execute(
            select(AIAsset.id).where(
                AIAsset.id == asset_id,
                AIAsset.tenant_id == principal.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if owned is None:
        await _audit(request, principal, endpoint, "GET", 404)
        raise HTTPException(
            status_code=404,
            detail={
                "type": "about:blank",
                "title": "Not Found",
                "status": 404,
                "detail": "Asset not found",
            },
        )

    now = datetime.now(UTC)
    since = now - timedelta(days=USAGE_PERIODS[period])
    # Never report events past the tenant's retention period, even if the
    # purge has not run yet (#85).
    cutoff = await get_retention_cutoff(db, principal.tenant_id, now)
    if cutoff is not None and cutoff > since:
        since = cutoff
    row = (
        (
            await db.execute(
                select(
                    func.count(AIUsageEvent.id).label("total_requests"),
                    func.sum(AIUsageEvent.tokens_in).label("total_tokens_in"),
                    func.sum(AIUsageEvent.tokens_out).label("total_tokens_out"),
                    func.sum(AIUsageEvent.cost).label("estimated_cost_usd"),
                    func.count(func.distinct(AIUsageEvent.session_id)).label(
                        "unique_sessions"
                    ),
                    func.avg(AIUsageEvent.latency_ms).label("avg_latency_ms"),
                ).where(
                    AIUsageEvent.asset_id == asset_id,
                    AIUsageEvent.tenant_id == principal.tenant_id,
                    AIUsageEvent.timestamp >= since,
                )
            )
        )
        .mappings()
        .one()
    )

    await _audit(request, principal, endpoint, "GET", 200, 1)
    return _usage_summary_dict(row, period)


def _usage_summary_dict(row, period: str) -> dict:
    """Shape an aggregate row into the documented `UsageSummary` schema.

    Counts and token totals are 0 when there were no events. Cost and
    latency are null when no event in the window reported them, because
    0 would claim a measurement that was never taken.
    """
    cost = row["estimated_cost_usd"]
    latency = row["avg_latency_ms"]
    return {
        "total_requests": int(row["total_requests"] or 0),
        "total_tokens_in": int(row["total_tokens_in"] or 0),
        "total_tokens_out": int(row["total_tokens_out"] or 0),
        "estimated_cost_usd": round(float(cost), 6) if cost is not None else None,
        "unique_sessions": int(row["unique_sessions"] or 0),
        "avg_latency_ms": round(float(latency)) if latency is not None else None,
        "period": period,
    }


# ──────────────────────────────────────────────────────────────────────
# Taxonomy
# ──────────────────────────────────────────────────────────────────────


@router.get("/vendors", summary="List AI vendors in use")
async def list_vendors(
    request: Request,
    response: Response,
    principal: PartnerPrincipal = Depends(require_scope("assets:read")),
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
    principal: PartnerPrincipal = Depends(require_scope("assets:read")),
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
    principal: PartnerPrincipal = Depends(require_scope("assets:read")),
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
    principal: PartnerPrincipal = Depends(require_scope("changes:read")),
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
            # Lifecycle transition since `since`: stale, retired, reactivated, or null.
            "lifecycle_change": lifecycle_change_for(a, since_dt),
            # `changed_fields` requires field-level audit; deferred to Phase 2.
            "changed_fields": None,
        })

    has_more = len(data) == limit and total > limit
    next_cursor = data[-1]["asset"]["id"] if has_more and data else None

    await _audit(
        request,
        principal,
        "/partner/v1/changes",
        "GET",
        200,
        len(data),
        asset_ids=[d["asset"]["id"] for d in data],
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
    format: str = Query(
        default="json",
        description="`json` (registry envelope) or `cyclonedx` (CycloneDX 1.6 AI-BOM)",
    ),
    principal: PartnerPrincipal = Depends(require_scope("export:read")),
):
    if format not in ("json", "cyclonedx"):
        raise HTTPException(
            status_code=400,
            detail={
                "type": "about:blank",
                "title": "Bad Request",
                "status": 400,
                "detail": "Supported formats: json, cyclonedx",
            },
        )

    await _enforce_rate_limit(request, response, principal)
    db = request.state.db

    if format == "cyclonedx":
        assets, flows, risks = await aibom.load_registry(db, principal.tenant_id)
        bom = aibom.build_bom(principal.tenant_id, assets, flows, risks)
        await _audit(
            request,
            principal,
            "/partner/v1/export",
            "GET",
            200,
            len(assets),
            asset_ids=[a.id for a in assets],
        )
        # A returned Response does not inherit headers set on `response`, so
        # carry the X-RateLimit-* headers over explicitly.
        rate_headers = {
            k: v for k, v in response.headers.items() if k.startswith("x-ratelimit-")
        }
        return JSONResponse(
            content=bom, media_type=aibom.MEDIA_TYPE, headers=rate_headers
        )

    rows = (
        await db.execute(
            select(AIAsset)
            .where(AIAsset.tenant_id == principal.tenant_id)
            .order_by(AIAsset.id)
        )
    ).scalars().all()

    now = datetime.utcnow()
    await _audit(
        request,
        principal,
        "/partner/v1/export",
        "GET",
        200,
        len(rows),
        asset_ids=[a.id for a in rows],
    )

    return {
        "data": [_asset_dict(a) for a in rows],
        "meta": {
            "total": len(rows),
            "exported_at": now.isoformat() + "Z",
            "sync_token": now.isoformat() + "Z",
        },
    }
