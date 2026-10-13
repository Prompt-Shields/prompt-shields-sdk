import hashlib
from typing import Annotated

from db.models import AIUsageEvent
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from collector.auth import resolve_tenant
from collector.config import settings
from collector.dedup import find_or_create_asset
from collector.embeddings import build_asset_text, get_embedding
from collector.partner_rate_limit import FixedWindowRateLimiter, get_ingest_rate_limiter

router = APIRouter()


class IngestEvent(BaseModel):
    vendor: str
    model: str | None = None
    use_case_name: str | None = None
    business_unit: str | None = None
    owner_email: str | None = None
    environment: str | None = None
    data_classification: str | None = None
    calling_service: str | None = None
    source: str = "sdk"
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost: float | None = None
    latency_ms: int | None = None
    session_id: str | None = None
    tool_calls_used: list[dict] | None = None
    prompt_text: str | None = None


class IngestRequest(BaseModel):
    events: list[IngestEvent]


def _enforce_rate_limit(
    limiter: FixedWindowRateLimiter, tenant_id, response: Response
) -> None:
    """Per-tenant request limit on ingest. Sets X-RateLimit-* headers and
    raises 429 with Retry-After once the tenant exceeds the window."""
    limit = settings.rate_limit_per_minute
    if limit <= 0:
        return
    result = limiter.check(tenant_id, limit)
    headers = {
        "X-RateLimit-Limit": str(result.limit),
        "X-RateLimit-Remaining": str(result.remaining),
        "X-RateLimit-Reset": str(result.reset_at),
    }
    if not result.allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "type": "about:blank",
                "title": "Too Many Requests",
                "status": 429,
                "detail": f"Rate limit exceeded. Retry after {result.retry_after} seconds.",
            },
            headers={**headers, "Retry-After": str(result.retry_after)},
        )
    response.headers.update(headers)


@router.post("/ingest/events")
async def ingest_events(
    body: IngestRequest,
    request: Request,
    response: Response,
    limiter: Annotated[FixedWindowRateLimiter, Depends(get_ingest_rate_limiter)],
):
    tenant_id = await resolve_tenant(request)
    _enforce_rate_limit(limiter, tenant_id, response)
    db = request.state.db

    assets_created = 0
    for event in body.events:
        asset, is_new = await find_or_create_asset(
            db=db,
            tenant_id=tenant_id,
            vendor=event.vendor,
            model=event.model,
            use_case_name=event.use_case_name,
            business_unit=event.business_unit,
            calling_service=event.calling_service,
            source=event.source,
            owner_email=event.owner_email,
            environment=event.environment,
            data_classification=event.data_classification,
        )
        if is_new:
            assets_created += 1
            await db.flush()

        # Generate embedding for new assets (async, fail-open)
        if is_new and asset.embedding is None:
            asset_text = build_asset_text(
                vendor=event.vendor,
                model=event.model,
                use_case_name=event.use_case_name,
                business_unit=event.business_unit,
                data_classification=event.data_classification,
            )
            embedding = await get_embedding(asset_text)
            if embedding:
                asset.embedding = embedding

        prompt_hash = None
        if event.prompt_text:
            prompt_hash = hashlib.sha256(event.prompt_text.encode()).hexdigest()

        usage_event = AIUsageEvent(
            tenant_id=tenant_id,
            asset_id=asset.id,
            tokens_in=event.tokens_in,
            tokens_out=event.tokens_out,
            cost=event.cost,
            latency_ms=event.latency_ms,
            source=event.source,
            session_id=event.session_id,
            tool_calls_used=event.tool_calls_used,
            prompt_hash=prompt_hash,
        )
        db.add(usage_event)

    await db.commit()

    return {"ingested": len(body.events), "assets_created": assets_created}
