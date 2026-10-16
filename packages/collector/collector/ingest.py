import hashlib
import math
from typing import Annotated, Any

from db.models import AIUsageEvent
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, field_validator

from collector import loss_accounting
from collector.auth import resolve_tenant
from collector.config import settings
from collector.data_flows import record_event_flows
from collector.dedup import find_or_create_asset
from collector.embeddings import build_asset_text, get_embedding
from collector.partner_rate_limit import FixedWindowRateLimiter, get_ingest_rate_limiter
from collector.webhooks import AssetChanges

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
    # Data-flow metadata (ps_metadata on the SDK); see collector/data_flows.py.
    # Loosely typed on purpose: these fields used to be ignored, so a malformed
    # value must not 422 the whole batch. build_flow_context drops what it
    # can't use.
    data_sources: Any = None
    output_destination: Any = None
    detected_pii_types: Any = None
    region: Any = None
    # Extended usage (#9, #90, #75). Lenient like the data-flow fields: an
    # SDK or gateway version that sends an unexpected value loses that field
    # (stored as null), never the batch. See _lenient_* below.
    requested_model: str | None = None
    served_model: str | None = None
    tokens_cached: int | None = None
    tokens_cache_write: int | None = None
    tokens_cache_write_1h: int | None = None
    tokens_reasoning: int | None = None
    cost_source: str | None = None
    service_tier: str | None = None
    framework: str | None = None
    modality: str | None = None
    usage_units: float | None = None
    usage_unit_type: str | None = None

    @field_validator("requested_model", "served_model", mode="before")
    @classmethod
    def _model_name(cls, value: Any) -> str | None:
        return _lenient_str(value, MAX_MODEL_NAME_LENGTH)

    @field_validator(
        "cost_source", "service_tier", "framework", "usage_unit_type", mode="before"
    )
    @classmethod
    def _label(cls, value: Any) -> str | None:
        return _lenient_str(value, MAX_LABEL_LENGTH)

    @field_validator("modality", mode="before")
    @classmethod
    def _modality(cls, value: Any) -> str | None:
        value = _lenient_str(value, MAX_LABEL_LENGTH)
        return value.lower() if value is not None else None

    @field_validator(
        "tokens_cached",
        "tokens_cache_write",
        "tokens_cache_write_1h",
        "tokens_reasoning",
        mode="before",
    )
    @classmethod
    def _token_count(cls, value: Any) -> int | None:
        return _lenient_count(value)

    @field_validator("usage_units", mode="before")
    @classmethod
    def _usage_units(cls, value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        # Check ints before float(): a huge JSON integer overflows float()
        # with OverflowError, which pydantic does not catch (500, batch lost).
        if isinstance(value, int) and not 0 <= value <= MAX_USAGE_UNITS:
            return None
        value = float(value)
        if not math.isfinite(value) or value < 0 or value > MAX_USAGE_UNITS:
            return None
        return value


# Limits for the extended usage fields. Longer or out-of-range values are
# dropped (stored as null), not truncated: half a model name is wrong data.
MAX_MODEL_NAME_LENGTH = 256
MAX_LABEL_LENGTH = 64
MAX_TOKEN_COUNT = 2**31 - 1  # Integer column
MAX_USAGE_UNITS = 1e12  # fits Numeric(20, 6)

# Lenient fields whose dropped values are reported in ignored_fields as
# "<field>:invalid", so a client sending bad values shows up in
# /telemetry/loss instead of losing the data silently.
LENIENT_FIELDS = (
    "requested_model",
    "served_model",
    "tokens_cached",
    "tokens_cache_write",
    "tokens_cache_write_1h",
    "tokens_reasoning",
    "cost_source",
    "service_tier",
    "framework",
    "modality",
    "usage_units",
    "usage_unit_type",
)


def _lenient_str(value: Any, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > max_length:
        return None
    return value


def _lenient_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or value < 0 or value > MAX_TOKEN_COUNT:
        return None
    return value


class IngestRequest(BaseModel):
    events: list[IngestEvent]
    # SDK delivery accounting (#87); parsed leniently by loss_accounting.
    metadata: dict | None = None


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
    db = request.state.db
    meta = loss_accounting.parse_metadata(body.metadata)
    try:
        _enforce_rate_limit(limiter, tenant_id, response)
    except HTTPException as exc:
        if exc.status_code == 429:
            await loss_accounting.record_rejected(
                db, tenant_id, meta, len(body.events), "rate_limited"
            )
        raise

    try:
        assets_created = await _store_events(db, tenant_id, body.events)
        if body.events:
            raw_events = (await request.json()).get("events")
            ignored = loss_accounting.ignored_fields(
                raw_events, set(IngestEvent.model_fields)
            )
            for key, n in loss_accounting.invalid_fields(
                raw_events, body.events, LENIENT_FIELDS
            ).items():
                ignored[key] = ignored.get(key, 0) + n
            await loss_accounting.record_received(
                db, tenant_id, meta, len(body.events), ignored
            )
        await db.commit()
    except Exception:
        await db.rollback()
        await loss_accounting.record_rejected(
            db, tenant_id, meta, len(body.events), "write_failed"
        )
        raise

    return {"ingested": len(body.events), "assets_created": assets_created}


async def _store_events(db, tenant_id, events: list[IngestEvent]) -> int:
    assets_created = 0
    changes = AssetChanges()
    for event in events:
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
            modality=event.modality,
        )
        changes.record(asset, is_new)
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
            requested_model=event.requested_model,
            served_model=event.served_model,
            tokens_cached=event.tokens_cached,
            tokens_cache_write=event.tokens_cache_write,
            tokens_cache_write_1h=event.tokens_cache_write_1h,
            tokens_reasoning=event.tokens_reasoning,
            cost_source=event.cost_source,
            service_tier=event.service_tier,
            framework=event.framework,
            modality=event.modality,
            usage_units=event.usage_units,
            usage_unit_type=event.usage_unit_type,
        )
        db.add(usage_event)
        await record_event_flows(
            db,
            tenant_id,
            asset,
            usage_event,
            data_sources=event.data_sources,
            output_destination=event.output_destination,
            pii_types=event.detected_pii_types,
            region=event.region,
            data_classification=event.data_classification,
            calling_service=event.calling_service,
        )

    await changes.enqueue(db, tenant_id)
    return assets_created
