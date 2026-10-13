from uuid import UUID
from datetime import datetime
from fastapi import APIRouter, Request, Query, HTTPException
from sqlalchemy import select, func, and_
from collector.audit import record_read
from collector.auth import resolve_tenant
from collector.embeddings import get_embedding, build_asset_text, mock_embedding
from db.models import AIAsset, DataFlow, RiskMapping

router = APIRouter(prefix="/api/v1/registry")


@router.get("/assets")
async def list_assets(
    request: Request,
    business_unit: str | None = None,
    status: str | None = None,
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
async def get_risks(asset_id: UUID, request: Request):
    tenant_id = await resolve_tenant(request)
    db = request.state.db

    stmt = select(RiskMapping).where(RiskMapping.asset_id == asset_id, RiskMapping.tenant_id == tenant_id)
    result = await db.execute(stmt)
    risks = result.scalars().all()
    data = [_serialize_risk(r) for r in risks]
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


@router.get("/search")
async def semantic_search(
    request: Request,
    q: str = Query(..., description="Natural language search query"),
    limit: int = Query(default=10, le=50),
    use_mock: bool = Query(default=False, description="Use mock embeddings for testing"),
):
    """Semantic search over AI assets using pgvector cosine similarity."""
    tenant_id = await resolve_tenant(request)
    db = request.state.db

    # Generate embedding for query
    if use_mock:
        query_embedding = mock_embedding(q)
    else:
        query_embedding = await get_embedding(q)
        if query_embedding is None:
            # Fallback to mock if no API key
            query_embedding = mock_embedding(q)

    # Cosine similarity search using pgvector
    stmt = (
        select(AIAsset)
        .where(
            AIAsset.tenant_id == tenant_id,
            AIAsset.embedding.is_not(None),
        )
        .order_by(AIAsset.embedding.cosine_distance(query_embedding))
        .limit(limit)
    )
    result = await db.execute(stmt)
    assets = result.scalars().all()

    data = []
    for a in assets:
        serialized = _serialize_asset(a)
        # Add similarity score
        if a.embedding is not None:
            serialized["similarity"] = round(1.0 - float(
                sum((x - y) ** 2 for x, y in zip(a.embedding, query_embedding)) ** 0.5
            ), 4)
        data.append(serialized)

    # `q` is stored hashed, never as text (see collector.audit).
    await record_read(
        request, tenant_id=tenant_id, status_code=200, response_count=len(data),
        asset_ids=[a["id"] for a in data],
    )
    return {"data": data, "meta": {"total": len(data), "query": q}}


def _serialize_asset(a: AIAsset) -> dict:
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


def _serialize_flow(f: DataFlow) -> dict:
    return {
        "id": str(f.id),
        "source_system": f.source_system,
        "destination_system": f.destination_system,
        "data_classification": f.data_classification,
        "direction": f.direction,
        "detected_pii_types": f.detected_pii_types,
    }


def _serialize_risk(r: RiskMapping) -> dict:
    return {
        "id": str(r.id),
        "risk_category": r.risk_category,
        "risk_level": r.risk_level,
        "framework": r.framework,
    }
