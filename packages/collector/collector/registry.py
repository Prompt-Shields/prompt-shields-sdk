import operator
import re
from datetime import datetime
from functools import reduce
from uuid import UUID

from db.models import AIAsset, DataFlow, RiskMapping
from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import and_, case, func, or_, select

from collector.attestation import add_attestation_summaries
from collector.audit import record_read
from collector.auth import resolve_tenant
from collector.embeddings import get_embedding, mock_embedding
from collector.lifecycle import LifecycleState
from collector.loss_accounting import tenant_coverage
from collector.risk_suggestions import RiskStatus, risk_dict, risk_status_filter

router = APIRouter(prefix="/api/v1/registry")


@router.get("/assets")
async def list_assets(
    request: Request,
    business_unit: str | None = None,
    status: str | None = None,
    lifecycle_state: LifecycleState | None = None,
    discovery_source: str | None = None,
    since: str | None = None,
    after: str | None = None,
    limit: int = Query(default=50, le=200),
):
    tenant_id = await resolve_tenant(request)
    db = request.state.db

    conditions = [AIAsset.tenant_id == tenant_id]
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

    count_stmt = select(func.count(AIAsset.id)).where(and_(*conditions))
    total = (await db.execute(count_stmt)).scalar()

    stmt = (
        select(AIAsset)
        .where(and_(*conditions))
        .order_by(AIAsset.id)
        .limit(limit)
    )
    result = await db.execute(stmt)
    assets = result.scalars().all()

    data = [_serialize_asset(a) for a in assets]
    await add_attestation_summaries(db, tenant_id, assets, data)
    has_more = len(data) == limit and total > limit
    next_cursor = str(data[-1]["id"]) if has_more else None

    await record_read(
        request, tenant_id=tenant_id, status_code=200, response_count=len(data),
        asset_ids=[a["id"] for a in data],
    )
    return {"data": data, "meta": {"total": total, "has_more": has_more, "next_cursor": next_cursor}}


@router.get("/assets/{asset_id}")
async def get_asset(asset_id: UUID, request: Request):
    tenant_id = await resolve_tenant(request)
    db = request.state.db

    stmt = select(AIAsset).where(AIAsset.id == asset_id, AIAsset.tenant_id == tenant_id)
    result = await db.execute(stmt)
    asset = result.scalar_one_or_none()
    if asset is None:
        await record_read(request, tenant_id=tenant_id, status_code=404, response_count=0)
        raise HTTPException(status_code=404, detail={
            "type": "about:blank", "title": "Not Found", "status": 404, "detail": "Asset not found"
        })
    # Serialize before the audit commit so no expired attribute is reloaded.
    body = _serialize_asset(asset)
    await add_attestation_summaries(db, tenant_id, [asset], [body])
    await record_read(request, tenant_id=tenant_id, status_code=200, response_count=1)
    return body


@router.get("/assets/{asset_id}/data-flows")
async def get_data_flows(asset_id: UUID, request: Request):
    tenant_id = await resolve_tenant(request)
    db = request.state.db

    stmt = select(DataFlow).where(DataFlow.asset_id == asset_id, DataFlow.tenant_id == tenant_id)
    result = await db.execute(stmt)
    flows = result.scalars().all()
    data = [_serialize_flow(f) for f in flows]
    await record_read(request, tenant_id=tenant_id, status_code=200, response_count=len(data))
    return {"data": data}


@router.get("/assets/{asset_id}/risks")
async def get_risks(
    asset_id: UUID,
    request: Request,
    status: RiskStatus | None = None,
):
    tenant_id = await resolve_tenant(request)
    db = request.state.db

    stmt = (
        select(RiskMapping)
        .where(
            RiskMapping.asset_id == asset_id,
            RiskMapping.tenant_id == tenant_id,
            risk_status_filter(status),
        )
        .order_by(RiskMapping.framework, RiskMapping.risk_category, RiskMapping.id)
    )
    result = await db.execute(stmt)
    risks = result.scalars().all()
    data = [risk_dict(r) for r in risks]
    await record_read(request, tenant_id=tenant_id, status_code=200, response_count=len(data))
    return {"data": data}


@router.get("/vendors")
async def list_vendors(request: Request):
    tenant_id = await resolve_tenant(request)
    db = request.state.db

    stmt = select(AIAsset.vendor).where(AIAsset.tenant_id == tenant_id).distinct()
    result = await db.execute(stmt)
    vendors = [row[0] for row in result.all()]
    await record_read(request, tenant_id=tenant_id, status_code=200, response_count=len(vendors))
    return {"data": vendors}


@router.get("/models")
async def list_models(request: Request):
    tenant_id = await resolve_tenant(request)
    db = request.state.db

    stmt = select(AIAsset.model).where(AIAsset.tenant_id == tenant_id, AIAsset.model.is_not(None)).distinct()
    result = await db.execute(stmt)
    models = [row[0] for row in result.all()]
    await record_read(request, tenant_id=tenant_id, status_code=200, response_count=len(models))
    return {"data": models}


@router.get("/telemetry/coverage")
async def telemetry_coverage(request: Request):
    """Telemetry coverage overall and per source (sdk, gateway, otel), from
    the loss accounting behind /telemetry/loss (#87)."""
    tenant_id = await resolve_tenant(request)
    data = await tenant_coverage(request.state.db, tenant_id)
    await record_read(
        request, tenant_id=tenant_id, status_code=200, response_count=len(data["sources"])
    )
    return data


@router.get("/search")
async def semantic_search(
    request: Request,
    q: str = Query(..., description="Natural language search query"),
    limit: int = Query(default=10, le=50),
    use_mock: bool = Query(default=False, description="Use mock embeddings for testing"),
):
    """Search AI assets: pgvector cosine similarity, or keyword match when
    embeddings are disabled (PS_EMBEDDINGS_BACKEND=none) or unavailable."""
    tenant_id = await resolve_tenant(request)
    db = request.state.db

    if use_mock:
        query_embedding = mock_embedding(q)
    else:
        query_embedding = await get_embedding(q)
    if query_embedding is None:
        data = await _keyword_search(db, tenant_id, q, limit)
        await record_read(
            request, tenant_id=tenant_id, status_code=200, response_count=len(data),
            asset_ids=[a["id"] for a in data],
        )
        return {"data": data, "meta": {"total": len(data), "query": q, "mode": "keyword"}}

    distance = AIAsset.embedding.cosine_distance(query_embedding)
    stmt = (
        select(AIAsset, distance.label("distance"))
        .where(
            AIAsset.tenant_id == tenant_id,
            AIAsset.embedding.is_not(None),
        )
        .order_by(distance)
        .limit(limit)
    )
    result = await db.execute(stmt)

    data = []
    for a, dist in result.all():
        serialized = _serialize_asset(a)
        serialized["similarity"] = round(1.0 - float(dist), 4)
        data.append(serialized)

    # `q` is stored hashed, never as text (see collector.audit).
    await record_read(
        request, tenant_id=tenant_id, status_code=200, response_count=len(data),
        asset_ids=[a["id"] for a in data],
    )
    return {"data": data, "meta": {"total": len(data), "query": q, "mode": "semantic"}}


_KEYWORD_FIELDS = (
    AIAsset.vendor,
    AIAsset.model,
    AIAsset.use_case_name,
    AIAsset.business_unit,
    AIAsset.data_classification,
)


async def _keyword_search(db, tenant_id, q: str, limit: int) -> list[dict]:
    """Rank assets by how many query terms start a word in their metadata.

    Runs entirely in Postgres, so it works with embeddings disabled and sends
    nothing off-box. Prefix-of-word matching keeps "hr" from hitting "anthropic".
    """
    words = re.findall(r"\w[\w.-]*", q.lower())
    terms = list(dict.fromkeys(t for t in words if len(t) > 1))[:10]
    if not terms:
        return []

    hits = [
        case(
            (or_(*(f.regexp_match(r"\m" + re.escape(t), flags="i") for f in _KEYWORD_FIELDS)), 1),
            else_=0,
        )
        for t in terms
    ]
    score = reduce(operator.add, hits).label("score")
    stmt = (
        select(AIAsset, score)
        .where(AIAsset.tenant_id == tenant_id)
        .where(score > 0)
        .order_by(score.desc(), AIAsset.last_seen.desc().nulls_last(), AIAsset.id)
        .limit(limit)
    )
    result = await db.execute(stmt)
    data = []
    for a, matched in result.all():
        serialized = _serialize_asset(a)
        serialized["score"] = round(matched / len(terms), 4)
        data.append(serialized)
    return data


def _serialize_asset(a: AIAsset) -> dict:
    return {
        "id": str(a.id),
        "vendor": a.vendor,
        "model": a.model,
        "use_case_name": a.use_case_name,
        "business_unit": a.business_unit,
        "owner_email": a.owner_email,
        "environment": a.environment,
        "region": a.region,
        "modality": a.modality,
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


def _serialize_flow(f: DataFlow) -> dict:
    return {
        "id": str(f.id),
        "source_system": f.source_system,
        "destination_system": f.destination_system,
        "data_classification": f.data_classification,
        "direction": f.direction,
        "detected_pii_types": f.detected_pii_types,
    }
