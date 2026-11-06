"""Routing learner snapshots and recommendation review (#47, #48).

The routing learner runs in the customer's environment (the SDK's offline
``ps-learn``, PRD 0001 §7.13, ``recommend`` mode). It uploads each snapshot
here; a person then accepts or rejects each recommended route change. The
collector never routes traffic and accepting a recommendation changes no
routing: the reviewer edits the route policy themselves.

Snapshots
    ``POST /ingest/learner-snapshots`` (tenant ingest key) stores one
    snapshot per (tenant, ``learner_version``). The version is the SHA-256
    of the snapshot's canonical JSON (same rule as
    ``prompt_shields.learner.snapshot_digest``) and is checked here, so a
    stored snapshot is exactly what the learner produced and can be
    reproduced from the same input. A resend is a no-op. Only ``recommend``
    mode is accepted. A snapshot holds aggregates over local prompt
    features, never prompt text or embeddings.

Recommendations
    Each snapshot's recommendations become rows with ``status`` pending.
    A recommended model must be one of the snapshot's policy-compliant
    candidates for its group. A newer snapshot supersedes pending rows of the
    same (use case, sensitivity). A person accepts or rejects a pending row
    once, naming themselves (``reviewed_by``) with an optional comment; the
    decision is also written to the admin audit trail
    (``learner.recommendation_review``, ids and decision only).

Realised vs. projected
    For an accepted recommendation, reads add ``realised``: usage events of
    that use case and sensitivity since the decision (cost per call, the
    share served by the recommended model, explicit feedback on those calls)
    next to the projection, with the gap in percentage points.

Routes (Partner API):
    GET  /partner/v1/learner/snapshots                         learner:read
    GET  /partner/v1/learner/snapshots/{learner_version}       learner:read
    GET  /partner/v1/learner/recommendations                   learner:read
    GET  /partner/v1/learner/recommendations/{rec_id}          learner:read
    POST /partner/v1/learner/recommendations/{rec_id}/review   learner:review
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from db.models import (
    AIAsset,
    AIUsageEvent,
    CallFeedback,
    LearnerRecommendation,
    LearnerSnapshot,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from collector import audit_trail, tenant_keys
from collector.auth import resolve_tenant
from collector.partner_auth import PartnerPrincipal, require_scope
from collector.partner_registry import _audit, _enforce_rate_limit
from collector.sanitize import (
    BodyTooDeep,
    BoundedJsonRoute,
    StorableStr,
    is_storable,
    loads_bounded,
    too_deep_problem,
)

SNAPSHOT_SCHEMA = "ps-learner-snapshot.v1"
MODES = frozenset({"recommend"})
VERSION_RE = re.compile(r"lv1-[0-9a-f]{16}")
HEX64_RE = re.compile(r"[0-9a-f]{64}")
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_RECOMMENDATIONS = 500
MAX_GROUPS = 2000
MAX_LABEL = 200
MAX_CANDIDATES = 100

PENDING, ACCEPTED, REJECTED, SUPERSEDED = "pending", "accepted", "rejected", "superseded"
READ_SCOPE = "learner:read"
REVIEW_SCOPE = "learner:review"


def _problem(status: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


def _invalid(detail: str) -> HTTPException:
    return _problem(422, "Unprocessable Entity", detail)


def canonical_json(obj: Any) -> str:
    """Same rule as prompt_shields.learner.canonical_json."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def expected_version(snapshot: dict) -> str:
    body = {k: v for k, v in snapshot.items() if k != "learner_version"}
    return "lv1-" + hashlib.sha256(canonical_json(body).encode()).hexdigest()[:16]


def _label(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not 0 < len(value) <= MAX_LABEL:
        raise _invalid(f"{name} must be a string of 1-{MAX_LABEL} characters")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(
        value
    ):
        raise _invalid(f"{name} must be a finite number")
    return float(value)


def _count(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid(f"{name} must be a non-negative integer")
    return value


def _parse_recommendation(r: Any, i: int) -> dict:
    where = f"recommendations[{i}]"
    if not isinstance(r, dict):
        raise _invalid(f"{where} must be an object")
    candidates = r.get("candidates")
    if (
        not isinstance(candidates, list)
        or not 0 < len(candidates) <= MAX_CANDIDATES
        or not all(isinstance(c, str) and 0 < len(c) <= MAX_LABEL for c in candidates)
    ):
        raise _invalid(f"{where}.candidates must be a list of 1-{MAX_CANDIDATES} model labels")
    current = _label(r.get("current_model"), f"{where}.current_model")
    recommended = _label(r.get("recommended_model"), f"{where}.recommended_model")
    if recommended not in candidates:
        raise _invalid(f"{where}.recommended_model is not one of its policy candidates")
    if recommended == current:
        raise _invalid(f"{where}.recommended_model equals current_model")
    projection = r.get("projection")
    if not isinstance(projection, dict):
        raise _invalid(f"{where}.projection must be an object")
    for key in ("savings_per_call", "savings_pct", "quality_delta", "prob_best"):
        _number(projection.get(key), f"{where}.projection.{key}")
    _count(projection.get("calls"), f"{where}.projection.calls")
    for side in ("current", "recommended"):
        part = projection.get(side)
        if not isinstance(part, dict):
            raise _invalid(f"{where}.projection.{side} must be an object")
        for key in ("quality", "cost_per_call"):
            _number(part.get(key), f"{where}.projection.{side}.{key}")
    return {
        "use_case": _label(r.get("use_case"), f"{where}.use_case", nullable=True),
        "sensitivity_level": _label(r.get("sensitivity"), f"{where}.sensitivity"),
        "current_model": current,
        "recommended_model": recommended,
        "candidates": candidates,
        "projection": projection,
    }


def parse_snapshot(body: Any) -> tuple[dict, list[dict]]:
    """(snapshot columns, recommendation rows) of an upload, or a 422."""
    if not isinstance(body, dict):
        raise _invalid("Expected a JSON object")
    if not is_storable(body):
        raise _invalid("Body must not contain NUL characters or unpaired surrogates")
    if body.get("schema") != SNAPSHOT_SCHEMA:
        raise _invalid(f"schema must be {SNAPSHOT_SCHEMA}")
    if body.get("mode") not in MODES:
        raise _invalid("mode must be 'recommend': the learner may only recommend")
    version = body.get("learner_version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise _invalid("learner_version must look like lv1-<16 hex characters>")
    try:
        if expected_version(body) != version:
            raise _invalid("learner_version does not match the snapshot content")
    except ValueError:  # NaN / Infinity cannot be canonicalised
        raise _invalid("snapshot must not contain NaN or Infinity") from None
    parent = body.get("parent_version")
    if parent is not None and (not isinstance(parent, str) or not VERSION_RE.fullmatch(parent)):
        raise _invalid("parent_version must be null or a learner_version")
    data = body.get("data")
    if not isinstance(data, dict):
        raise _invalid("data must be an object")
    fingerprint = data.get("fingerprint")
    if not isinstance(fingerprint, str) or not HEX64_RE.fullmatch(fingerprint):
        raise _invalid("data.fingerprint must be a SHA-256 hex digest")
    groups = body.get("groups")
    if not isinstance(groups, list) or len(groups) > MAX_GROUPS:
        raise _invalid(f"groups must be a list of at most {MAX_GROUPS}")
    recs = body.get("recommendations")
    if not isinstance(recs, list) or len(recs) > MAX_RECOMMENDATIONS:
        raise _invalid(f"recommendations must be a list of at most {MAX_RECOMMENDATIONS}")
    parsed = [_parse_recommendation(r, i) for i, r in enumerate(recs)]
    keys = [(r["use_case"], r["sensitivity_level"]) for r in parsed]
    if len(set(keys)) != len(keys):
        raise _invalid("at most one recommendation per (use_case, sensitivity)")
    columns = {
        "learner_version": version,
        "parent_version": parent,
        "mode": body["mode"],
        "algorithm": _label(body.get("algorithm"), "algorithm"),
        "feature_schema": _label(body.get("feature_schema"), "feature_schema", nullable=True),
        "policy_version": _label(body.get("policy_version"), "policy_version", nullable=True),
        "data_fingerprint": fingerprint,
        "training_rows": _count(data.get("rows"), "data.rows"),
        "scored_rows": _count(data.get("scored_rows"), "data.scored_rows"),
        "snapshot": body,
    }
    return columns, parsed


async def _read_body(request: Request) -> Any:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise _problem(413, "Payload Too Large", f"Body exceeds {MAX_BODY_BYTES} bytes")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise _problem(413, "Payload Too Large", f"Body exceeds {MAX_BODY_BYTES} bytes")
        chunks.append(chunk)
    try:
        return loads_bounded(b"".join(chunks))
    except BodyTooDeep:
        raise too_deep_problem() from None
    except ValueError:
        raise _problem(400, "Bad Request", "Body is not valid JSON") from None


async def store_snapshot(
    db: AsyncSession,
    tenant_id: UUID,
    columns: dict,
    recs: list[dict],
    *,
    now: datetime | None = None,
) -> tuple[LearnerSnapshot, bool, int]:
    """Store a parsed snapshot and its recommendations (flushes, no commit).

    Returns (snapshot row, created, superseded count). A snapshot already
    stored for the tenant is returned unchanged.
    """
    existing = (
        await db.execute(
            select(LearnerSnapshot).where(
                LearnerSnapshot.tenant_id == tenant_id,
                LearnerSnapshot.learner_version == columns["learner_version"],
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False, 0
    now = now or datetime.now(UTC)
    snap = LearnerSnapshot(tenant_id=tenant_id, created_at=now, **columns)
    db.add(snap)
    await db.flush()
    superseded = 0
    for r in recs:
        result = await db.execute(
            update(LearnerRecommendation)
            .where(
                LearnerRecommendation.tenant_id == tenant_id,
                LearnerRecommendation.status == PENDING,
                LearnerRecommendation.use_case.is_not_distinct_from(r["use_case"]),
                LearnerRecommendation.sensitivity_level == r["sensitivity_level"],
            )
            .values(status=SUPERSEDED)
            .execution_options(synchronize_session=False)
        )
        superseded += result.rowcount or 0
        db.add(
            LearnerRecommendation(
                tenant_id=tenant_id,
                snapshot_id=snap.id,
                learner_version=snap.learner_version,
                status=PENDING,
                created_at=now,
                **r,
            )
        )
    await db.flush()
    return snap, True, superseded


ingest_router = APIRouter()


@ingest_router.post("/ingest/learner-snapshots")
async def ingest_learner_snapshot(request: Request):
    """Store one learner snapshot (``ps-learn --upload``). See the module
    docstring."""
    tenant_id = await resolve_tenant(request, tenant_keys.INGEST_WRITE)
    body = await _read_body(request)
    columns, recs = parse_snapshot(body)
    db = request.state.db
    try:
        snap, created, superseded = await store_snapshot(db, tenant_id, columns, recs)
        out = {
            "learner_version": snap.learner_version,
            "snapshot_id": str(snap.id),
            "created": created,
            "recommendations": len(recs) if created else 0,
            "superseded": superseded,
        }
        if created:
            await audit_trail.record(
                db,
                tenant_id=tenant_id,
                action="learner.snapshot_upload",
                actor=audit_trail.tenant_key_actor(request),
                target_type="learner_snapshot",
                target_id=snap.id,
                details={
                    "learner_version": snap.learner_version,
                    "recommendations": len(recs),
                    "superseded": superseded,
                },
            )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return out


# ──────────────────────────────────────────────────────────────────────
# Serialisation and realised savings
# ──────────────────────────────────────────────────────────────────────


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


def serialize_snapshot(s: LearnerSnapshot, *, full: bool = False) -> dict:
    doc = s.snapshot or {}
    out = {
        "id": str(s.id),
        "learner_version": s.learner_version,
        "parent_version": s.parent_version,
        "mode": s.mode,
        "algorithm": s.algorithm,
        "feature_schema": s.feature_schema,
        "policy_version": s.policy_version,
        "data_fingerprint": s.data_fingerprint,
        "training_rows": s.training_rows,
        "scored_rows": s.scored_rows,
        "recommendations": len(doc.get("recommendations") or []),
        "created_at": _iso(s.created_at),
    }
    if full:
        out["snapshot"] = doc
    return out


def serialize_recommendation(r: LearnerRecommendation, realised: dict | None = None) -> dict:
    return {
        "id": str(r.id),
        "snapshot_id": str(r.snapshot_id),
        "learner_version": r.learner_version,
        "use_case": r.use_case,
        "sensitivity_level": r.sensitivity_level,
        "current_model": r.current_model,
        "recommended_model": r.recommended_model,
        "candidates": r.candidates,
        "projection": r.projection,
        "status": r.status,
        "reviewed_by": r.reviewed_by,
        "reviewed_at": _iso(r.reviewed_at),
        "review_comment": r.review_comment,
        "reviewer_actor_type": r.reviewer_actor_type,
        "created_at": _iso(r.created_at),
        "realised": realised,
    }


def _ratio(num: float | None, den: float | None) -> float | None:
    if num is None or not den:
        return None
    return round(num / den, 6)


async def realised_savings(
    db: AsyncSession, r: LearnerRecommendation, *, now: datetime | None = None
) -> dict | None:
    """Realised vs. projected for an accepted recommendation, else None."""
    if r.status != ACCEPTED or r.reviewed_at is None:
        return None
    provider, sep, model = r.recommended_model.partition(":")
    if not sep:
        provider, model = "", r.recommended_model
    served_provider = func.coalesce(AIUsageEvent.served_provider, AIAsset.vendor)
    served_model = func.coalesce(AIUsageEvent.served_model, AIAsset.model)
    on_rec = served_model == model
    if provider:
        on_rec = and_(on_rec, served_provider == provider)
    stmt = (
        select(
            func.count(AIUsageEvent.id),
            func.count(AIUsageEvent.cost),
            func.sum(AIUsageEvent.cost),
            func.sum(case((on_rec, 1), else_=0)),
            func.count(CallFeedback.id),
            func.avg(CallFeedback.score),
        )
        .select_from(AIUsageEvent)
        .join(AIAsset, AIAsset.id == AIUsageEvent.asset_id)
        .outerjoin(
            CallFeedback,
            and_(
                CallFeedback.tenant_id == AIUsageEvent.tenant_id,
                CallFeedback.call_id == AIUsageEvent.call_id,
                on_rec,
            ),
        )
        .where(
            AIUsageEvent.tenant_id == r.tenant_id,
            AIUsageEvent.timestamp >= r.reviewed_at,
            AIUsageEvent.sensitivity_level == r.sensitivity_level,
            AIAsset.use_case_name.is_not_distinct_from(r.use_case),
        )
    )
    if now is not None:
        stmt = stmt.where(AIUsageEvent.timestamp <= now)
    calls, priced, cost_sum, on_rec_calls, fb_n, fb_avg = (await db.execute(stmt)).one()
    projection = r.projection or {}
    current_cost = (projection.get("current") or {}).get("cost_per_call")
    projected_pct = projection.get("savings_pct")
    cost_per_call = _ratio(float(cost_sum), priced) if cost_sum is not None else None
    saving = (
        round(current_cost - cost_per_call, 6)
        if cost_per_call is not None and isinstance(current_cost, (int, float))
        else None
    )
    pct = _ratio(saving, current_cost) if saving is not None else None
    return {
        "since": _iso(r.reviewed_at),
        "calls": calls,
        "calls_on_recommended": int(on_rec_calls or 0),
        "adoption": _ratio(float(on_rec_calls or 0), calls),
        "cost_per_call": cost_per_call,
        "savings_per_call": saving,
        "savings_pct": pct,
        "projected_savings_pct": projected_pct,
        "delta_points": (
            round((pct - projected_pct) * 100, 3)
            if pct is not None and isinstance(projected_pct, (int, float))
            else None
        ),
        "feedback_count": fb_n,
        "feedback_quality": round(float(fb_avg), 6) if fb_avg is not None else None,
        "projected_quality": (projection.get("recommended") or {}).get("quality"),
    }


# ──────────────────────────────────────────────────────────────────────
# Review
# ──────────────────────────────────────────────────────────────────────


class LearnerReviewBody(BaseModel):
    decision: Literal["accepted", "rejected"]
    reviewed_by: StorableStr = Field(
        ..., min_length=1, max_length=320, description="The person deciding, e.g. an email"
    )
    comment: StorableStr | None = Field(
        default=None, max_length=4000, description="Why it was accepted or rejected"
    )


async def review_recommendation(
    db: AsyncSession,
    rec: LearnerRecommendation,
    body: LearnerReviewBody,
    *,
    actor: audit_trail.Actor,
    partner_id: UUID | None = None,
    now: datetime | None = None,
) -> None:
    """Record the decision on a pending recommendation (flushes, no commit).
    Raises ValueError when it is not pending."""
    if rec.status != PENDING:
        raise ValueError(f"Recommendation is {rec.status}; only pending ones can be reviewed")
    now = now or datetime.now(UTC)
    reviewed_by = body.reviewed_by.strip()
    comment = body.comment.strip() if body.comment and body.comment.strip() else None
    rec.status = body.decision
    rec.reviewed_by = reviewed_by
    rec.reviewed_at = now
    rec.review_comment = comment
    rec.reviewer_actor_type = actor.type
    rec.reviewer_partner_id = partner_id
    await db.flush()
    await audit_trail.record(
        db,
        tenant_id=rec.tenant_id,
        action="learner.recommendation_review",
        actor=actor,
        target_type="learner_recommendation",
        target_id=rec.id,
        now=now,
        details={"learner_version": rec.learner_version, "decision": body.decision},
    )


# ──────────────────────────────────────────────────────────────────────
# Partner API
# ──────────────────────────────────────────────────────────────────────

partner_router = APIRouter(
    prefix="/partner/v1", tags=["Partner API"], route_class=BoundedJsonRoute
)

_READ = Depends(require_scope(READ_SCOPE))
_REVIEW = Depends(require_scope(REVIEW_SCOPE))


async def _load_rec(
    db, tenant_id: UUID, rec_id: UUID, *, for_update: bool = False
) -> LearnerRecommendation | None:
    stmt = select(LearnerRecommendation).where(
        LearnerRecommendation.id == rec_id,
        LearnerRecommendation.tenant_id == tenant_id,
    )
    if for_update:  # one decision wins when two reviewers race
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


@partner_router.get("/learner/snapshots", summary="List routing learner snapshots")
async def partner_list_snapshots(
    request: Request,
    response: Response,
    limit: int = Query(default=50, ge=1, le=200),
    principal: PartnerPrincipal = _READ,
):
    await _enforce_rate_limit(request, response, principal)
    db = request.state.db
    rows = (
        await db.execute(
            select(LearnerSnapshot)
            .where(LearnerSnapshot.tenant_id == principal.tenant_id)
            .order_by(LearnerSnapshot.created_at.desc(), LearnerSnapshot.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    data = [serialize_snapshot(s) for s in rows]
    await _audit(request, principal, "/partner/v1/learner/snapshots", "GET", 200, len(data))
    return {"data": data, "meta": {"total": len(data)}}


@partner_router.get(
    "/learner/snapshots/{learner_version}",
    summary="Get one learner snapshot, with its full state",
)
async def partner_get_snapshot(
    learner_version: str,
    request: Request,
    response: Response,
    principal: PartnerPrincipal = _READ,
):
    await _enforce_rate_limit(request, response, principal)
    db = request.state.db
    endpoint = f"/partner/v1/learner/snapshots/{learner_version}"
    row = None
    if VERSION_RE.fullmatch(learner_version):
        row = (
            await db.execute(
                select(LearnerSnapshot).where(
                    LearnerSnapshot.tenant_id == principal.tenant_id,
                    LearnerSnapshot.learner_version == learner_version,
                )
            )
        ).scalar_one_or_none()
    if row is None:
        await _audit(request, principal, endpoint, "GET", 404)
        raise _problem(404, "Not Found", "Learner snapshot not found")
    out = serialize_snapshot(row, full=True)  # before the audit commit expires it
    await _audit(request, principal, endpoint, "GET", 200, 1)
    return {"data": out}


@partner_router.get(
    "/learner/recommendations",
    summary="List learner recommendations with projections (and realised savings)",
)
async def partner_list_recommendations(
    request: Request,
    response: Response,
    status: Literal["pending", "accepted", "rejected", "superseded"] | None = None,
    use_case: str | None = Query(default=None, max_length=MAX_LABEL),
    learner_version: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=50, ge=1, le=200),
    principal: PartnerPrincipal = _READ,
):
    await _enforce_rate_limit(request, response, principal)
    db = request.state.db
    conds = [LearnerRecommendation.tenant_id == principal.tenant_id]
    if status is not None:
        conds.append(LearnerRecommendation.status == status)
    if use_case is not None:
        conds.append(LearnerRecommendation.use_case == use_case)
    if learner_version is not None:
        conds.append(LearnerRecommendation.learner_version == learner_version)
    rows = (
        await db.execute(
            select(LearnerRecommendation)
            .where(*conds)
            .order_by(LearnerRecommendation.created_at.desc(), LearnerRecommendation.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    data = [serialize_recommendation(r, await realised_savings(db, r)) for r in rows]
    await _audit(
        request, principal, "/partner/v1/learner/recommendations", "GET", 200, len(data)
    )
    return {"data": data, "meta": {"total": len(data)}}


@partner_router.get(
    "/learner/recommendations/{rec_id}", summary="Get one learner recommendation"
)
async def partner_get_recommendation(
    rec_id: UUID,
    request: Request,
    response: Response,
    principal: PartnerPrincipal = _READ,
):
    await _enforce_rate_limit(request, response, principal)
    db = request.state.db
    endpoint = f"/partner/v1/learner/recommendations/{rec_id}"
    rec = await _load_rec(db, principal.tenant_id, rec_id)
    if rec is None:
        await _audit(request, principal, endpoint, "GET", 404)
        raise _problem(404, "Not Found", "Recommendation not found")
    out = serialize_recommendation(rec, await realised_savings(db, rec))
    await _audit(request, principal, endpoint, "GET", 200, 1)
    return {"data": out}


@partner_router.post(
    "/learner/recommendations/{rec_id}/review",
    summary="Accept or reject a learner recommendation (a human decision)",
)
async def partner_review_recommendation(
    rec_id: UUID,
    body: LearnerReviewBody,
    request: Request,
    response: Response,
    principal: PartnerPrincipal = _REVIEW,
):
    await _enforce_rate_limit(request, response, principal)
    db = request.state.db
    endpoint = f"/partner/v1/learner/recommendations/{rec_id}/review"
    if not body.reviewed_by.strip():
        raise _invalid("reviewed_by must not be blank")
    rec = await _load_rec(db, principal.tenant_id, rec_id, for_update=True)
    if rec is None:
        await _audit(request, principal, endpoint, "POST", 404)
        raise _problem(404, "Not Found", "Recommendation not found")
    try:
        await review_recommendation(
            db,
            rec,
            body,
            actor=audit_trail.partner_actor(principal.partner_id),
            partner_id=principal.partner_id,
        )
    except ValueError as e:
        await db.rollback()
        await _audit(request, principal, endpoint, "POST", 409)
        raise _problem(409, "Conflict", str(e)) from None
    out = serialize_recommendation(rec, await realised_savings(db, rec))
    await db.commit()
    await _audit(request, principal, endpoint, "POST", 200, 1)
    return {"data": out}


__all__ = [
    "LearnerReviewBody",
    "ingest_router",
    "parse_snapshot",
    "partner_router",
    "realised_savings",
    "review_recommendation",
    "serialize_recommendation",
    "serialize_snapshot",
    "store_snapshot",
]
