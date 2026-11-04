"""Human review of risk mappings: decision, comment and history (#98).

A rule-based risk suggestion (collector.risk_suggestions) is not a
determination until a person confirms or rejects it. A review records the
decision on the mapping (`status`, `reviewed_at`, `reviewed_by`,
`review_comment`) and appends one row to `risk_mapping_reviews`, the
append-only history: who (`reviewed_by`, plus the tenant key, admin token or
partner that called), when, the new and previous status, the comment and a
snapshot of the mapping as reviewed. Triggers reject UPDATE, and DELETE
outside tenant erasure, so the history cannot be rewritten. A mapping can be
reviewed again (e.g. confirmed, then rejected after an assessment); each
decision is a new row. A review never changes the mapping's `confidence`.

Each review also bumps the asset's `updated_at`, so the asset shows up in
the Partner API delta feed (`/partner/v1/changes`), and writes a
`registry.risk_review` row to the admin audit trail (ids and statuses only,
no comment or name).

Routes:

  Tenant admin (tenant API key or admin token, scope admin:registry):
    POST /api/v1/admin/risk-mappings/{risk_id}/review
    GET  /api/v1/admin/risk-mappings/{risk_id}/reviews

  Partner API:
    GET  /partner/v1/assets/{asset_id}/risks/{risk_id}/reviews  scope risks:read
    POST /partner/v1/assets/{asset_id}/risks/{risk_id}/review   scope risks:review

A mapping in another tenant, or (Partner API) on another asset, is a 404,
the same as one that does not exist.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from db.models import AIAsset, RiskMapping, RiskMappingReview
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from collector import audit_trail
from collector.admin_auth import AdminPrincipal, require_admin
from collector.audit import record_read
from collector.partner_auth import PartnerPrincipal, require_scope
from collector.partner_registry import _audit, _enforce_rate_limit
from collector.risk_suggestions import CONFIRMED, REJECTED, risk_dict
from collector.sanitize import BoundedJsonRoute, StorableStr

WRITE_SCOPE = "risks:review"
ACTOR_PARTNER = "partner"

ReviewStatus = Literal["confirmed", "rejected"]


class ReviewBody(BaseModel):
    status: ReviewStatus
    # A tenant API key is a machine credential, so the human who decided is
    # named here (as for attestations); the key prefix is stored next to it.
    reviewed_by: StorableStr = Field(
        ..., min_length=1, max_length=320, description="The person deciding, e.g. an email"
    )
    comment: StorableStr | None = Field(
        default=None, max_length=4000, description="Why the mapping was confirmed or rejected"
    )


def _snapshot(row: RiskMapping) -> dict:
    return {
        "risk_category": row.risk_category,
        "risk_level": row.risk_level,
        "framework": row.framework,
        "rule_id": row.rule_id,
        "ruleset_version": row.ruleset_version,
        "framework_ref": row.framework_ref,
    }


def serialize_review(r: RiskMappingReview) -> dict:
    return {
        "id": str(r.id),
        "risk_mapping_id": str(r.risk_mapping_id),
        "asset_id": str(r.asset_id),
        "status": r.status,
        "previous_status": r.previous_status,
        "comment": r.comment,
        "reviewed_by": r.reviewed_by,
        "reviewed_at": r.created_at.isoformat() if r.created_at else None,
        "actor_type": r.actor_type,
        "partner_id": str(r.partner_id) if r.partner_id else None,
        "mapping": r.snapshot,
    }


async def record_review(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    mapping: RiskMapping,
    status: str,
    reviewed_by: str,
    actor: audit_trail.Actor,
    comment: str | None = None,
    partner_id: UUID | None = None,
    now: datetime | None = None,
) -> RiskMappingReview:
    """Apply one review to `mapping` and append it to the history. Flushes,
    does not commit. Raises ValueError for a bad status or another tenant's
    mapping."""
    if status not in (CONFIRMED, REJECTED):
        raise ValueError(f"Unknown status {status!r}. Allowed: confirmed, rejected")
    if mapping.tenant_id != tenant_id:
        raise ValueError("Risk mapping does not belong to this tenant")
    now = now or datetime.now(UTC)
    reviewed_by = reviewed_by.strip()
    comment = comment.strip() if comment and comment.strip() else None
    key_prefix = actor.id if actor.type != ACTOR_PARTNER else None
    review = RiskMappingReview(
        tenant_id=tenant_id,
        risk_mapping_id=mapping.id,
        asset_id=mapping.asset_id,
        status=status,
        previous_status=mapping.status,
        comment=comment,
        reviewed_by=reviewed_by,
        actor_type=actor.type,
        partner_id=partner_id,
        api_key_prefix=key_prefix,
        snapshot=_snapshot(mapping),
        created_at=now,
    )
    db.add(review)
    mapping.status = status
    mapping.reviewed_at = now
    mapping.reviewed_by = reviewed_by
    mapping.reviewed_key_prefix = key_prefix
    mapping.review_comment = comment
    mapping.updated_at = now
    # Surface the change in the delta feed. `confidence` is left untouched.
    await db.execute(
        update(AIAsset)
        .where(AIAsset.id == mapping.asset_id, AIAsset.tenant_id == tenant_id)
        .values(updated_at=now)
        .execution_options(synchronize_session=False)
    )
    await db.flush()
    await audit_trail.record(
        db,
        tenant_id=tenant_id,
        action="registry.risk_review",
        actor=actor,
        target_type="risk_mapping",
        target_id=mapping.id,
        now=now,
        details={
            "review_id": review.id,
            "asset_id": mapping.asset_id,
            "status": status,
            "previous_status": review.previous_status,
        },
    )
    return review


async def review_history(
    db: AsyncSession, tenant_id: UUID, risk_id: UUID, *, limit: int = 100
) -> list[RiskMappingReview]:
    """Reviews of one mapping, newest first."""
    return list(
        (
            await db.execute(
                select(RiskMappingReview)
                .where(
                    RiskMappingReview.tenant_id == tenant_id,
                    RiskMappingReview.risk_mapping_id == risk_id,
                )
                .order_by(RiskMappingReview.created_at.desc(), RiskMappingReview.id.desc())
                .limit(limit)
            )
        ).scalars()
    )


def _problem(status: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


async def _load_mapping(
    db, tenant_id: UUID, risk_id: UUID, asset_id: UUID | None = None
) -> RiskMapping | None:
    conds = [RiskMapping.id == risk_id, RiskMapping.tenant_id == tenant_id]
    if asset_id is not None:
        conds.append(RiskMapping.asset_id == asset_id)
    return (await db.execute(select(RiskMapping).where(*conds))).scalar_one_or_none()


def _history_body(rows: list[RiskMappingReview], mapping: RiskMapping | None) -> dict:
    return {
        "data": [serialize_review(r) for r in rows],
        "meta": {
            "total": len(rows),
            "current": risk_dict(mapping) if mapping is not None else None,
        },
    }


async def _review(
    db,
    tenant_id: UUID,
    mapping: RiskMapping,
    body: ReviewBody,
    *,
    actor: audit_trail.Actor,
    partner_id: UUID | None = None,
) -> tuple[dict, dict]:
    """Record a review, commit, and return (review, mapping) as dicts."""
    review = await record_review(
        db,
        tenant_id=tenant_id,
        mapping=mapping,
        status=body.status,
        reviewed_by=body.reviewed_by,
        comment=body.comment,
        actor=actor,
        partner_id=partner_id,
    )
    # Serialize before the commit expires the loaded objects.
    out = serialize_review(review), risk_dict(mapping)
    await db.commit()
    return out


# ──────────────────────────────────────────────────────────────────────
# Tenant admin
# ──────────────────────────────────────────────────────────────────────

admin_router = APIRouter(prefix="/api/v1/admin", tags=["Admin"], route_class=BoundedJsonRoute)

_REGISTRY = Depends(require_admin("admin:registry"))


@admin_router.post(
    "/risk-mappings/{risk_id}/review",
    summary="Confirm or reject a risk mapping (a human decision the engine keeps)",
)
async def review_risk(
    risk_id: UUID, body: ReviewBody, request: Request, principal: AdminPrincipal = _REGISTRY
) -> dict:
    tenant_id = principal.tenant_id
    db = request.state.db
    row = await _load_mapping(db, tenant_id, risk_id)
    if row is None:
        await record_read(request, tenant_id=tenant_id, status_code=404, response_count=0)
        raise _problem(404, "Not Found", "Risk mapping not found")
    _, out = await _review(db, tenant_id, row, body, actor=audit_trail.tenant_key_actor(request))
    await record_read(request, tenant_id=tenant_id, status_code=200, response_count=1)
    return out


@admin_router.get(
    "/risk-mappings/{risk_id}/reviews",
    summary="Review history of a risk mapping (append-only)",
)
async def list_risk_reviews(
    risk_id: UUID,
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    principal: AdminPrincipal = _REGISTRY,
) -> dict:
    tenant_id = principal.tenant_id
    db = request.state.db
    mapping = await _load_mapping(db, tenant_id, risk_id)
    rows = await review_history(db, tenant_id, risk_id, limit=limit)
    # History outlives a mapping a duplicate-asset merge dropped.
    if mapping is None and not rows:
        await record_read(request, tenant_id=tenant_id, status_code=404, response_count=0)
        raise _problem(404, "Not Found", "Risk mapping not found")
    body = _history_body(rows, mapping)
    await record_read(request, tenant_id=tenant_id, status_code=200, response_count=len(rows))
    return body


# ──────────────────────────────────────────────────────────────────────
# Partner API
# ──────────────────────────────────────────────────────────────────────

partner_router = APIRouter(
    prefix="/partner/v1", tags=["Partner API"], route_class=BoundedJsonRoute
)

_READ_SCOPE = Depends(require_scope("risks:read"))
_WRITE_SCOPE = Depends(require_scope(WRITE_SCOPE))


@partner_router.get(
    "/assets/{asset_id}/risks/{risk_id}/reviews",
    summary="Review history of a risk mapping",
)
async def partner_list_risk_reviews(
    asset_id: UUID,
    risk_id: UUID,
    request: Request,
    response: Response,
    limit: int = Query(default=100, ge=1, le=500),
    principal: PartnerPrincipal = _READ_SCOPE,
):
    await _enforce_rate_limit(request, response, principal)
    db = request.state.db
    endpoint = f"/partner/v1/assets/{asset_id}/risks/{risk_id}/reviews"
    mapping = await _load_mapping(db, principal.tenant_id, risk_id, asset_id)
    if mapping is None:
        await _audit(request, principal, endpoint, "GET", 404)
        raise _problem(404, "Not Found", "Risk mapping not found")
    rows = await review_history(db, principal.tenant_id, risk_id, limit=limit)
    body = _history_body(rows, mapping)
    await _audit(request, principal, endpoint, "GET", 200, len(rows), asset_ids=[asset_id])
    return body


@partner_router.post(
    "/assets/{asset_id}/risks/{risk_id}/review",
    status_code=201,
    summary="Confirm or reject a risk mapping",
)
async def partner_review_risk(
    asset_id: UUID,
    risk_id: UUID,
    body: ReviewBody,
    request: Request,
    response: Response,
    principal: PartnerPrincipal = _WRITE_SCOPE,
):
    await _enforce_rate_limit(request, response, principal)
    db = request.state.db
    endpoint = f"/partner/v1/assets/{asset_id}/risks/{risk_id}/review"
    mapping = await _load_mapping(db, principal.tenant_id, risk_id, asset_id)
    if mapping is None:
        await _audit(request, principal, endpoint, "POST", 404)
        raise _problem(404, "Not Found", "Risk mapping not found")
    review, current = await _review(
        db,
        principal.tenant_id,
        mapping,
        body,
        actor=audit_trail.partner_actor(principal.partner_id),
        partner_id=principal.partner_id,
    )
    await _audit(request, principal, endpoint, "POST", 201, 1, asset_ids=[asset_id])
    return {"data": review, "meta": {"current": current}}
