import hashlib
import math
import re
from typing import Annotated, Any

from db.models import AIUsageEvent
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, PrivateAttr, field_validator, model_validator

from collector import loss_accounting, metrics
from collector.auth import resolve_tenant
from collector.config import settings
from collector.data_flows import record_event_flows
from collector.dedup import AssetKey, find_or_create_asset, lock_asset_keys
from collector.embeddings import build_asset_text, get_embedding
from collector.partner_rate_limit import FixedWindowRateLimiter, get_ingest_rate_limiter
from collector.sanitize import BoundedJsonRoute, is_storable, storable_text
from collector.webhooks import AssetChanges

router = APIRouter(route_class=BoundedJsonRoute)


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
    # SDK's sha256(api key)[:16] (#93). Anything else (a raw or redacted
    # key) is dropped, so a key can never be stored here.
    api_key_fingerprint: str | None = None
    # Routing, sovereignty and savings metadata (PRD 0001 §7.12, #25).
    # Lenient like the extended usage fields above.
    api_surface: str | None = None
    route_mode: str | None = None
    route_group: str | None = None
    route_reason: str | None = None
    route_est_cost: float | None = None
    sensitivity_level: str | None = None
    sensitivity_source: str | None = None
    served_provider: str | None = None
    served_jurisdiction: str | None = None
    policy_version: str | None = None
    fallback_chain: list[str] | None = None
    counterfactual_cost: float | None = None
    baseline_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _drop_unstorable(cls, data: Any) -> Any:
        # A value the database cannot store (NUL, lone surrogate; see
        # collector/sanitize.py) is dropped as if not sent, so it costs that
        # field rather than the batch. It is counted as "<field>:invalid".
        if not isinstance(data, dict):
            return data
        bad = [k for k in SANITISED_FIELDS if k in data and not is_storable(data[k])]
        if not bad:
            return data
        return {k: v for k, v in data.items() if k not in bad}

    @field_validator("api_key_fingerprint", mode="before")
    @classmethod
    def _fingerprint(cls, value: Any) -> str | None:
        # Hex case and stray whitespace don't change the key it identifies.
        if not isinstance(value, str):
            return None
        value = value.strip().lower()
        return value if FINGERPRINT_RE.fullmatch(value) else None

    @field_validator("requested_model", "served_model", mode="before")
    @classmethod
    def _model_name(cls, value: Any) -> str | None:
        return _lenient_str(value, MAX_MODEL_NAME_LENGTH)

    @field_validator(
        "cost_source",
        "service_tier",
        "framework",
        "usage_unit_type",
        "api_surface",
        "route_group",
        "sensitivity_level",
        "served_provider",
        "served_jurisdiction",
        mode="before",
    )
    @classmethod
    def _label(cls, value: Any) -> str | None:
        return _lenient_str(value, MAX_LABEL_LENGTH)

    @field_validator("route_reason", mode="before")
    @classmethod
    def _reason(cls, value: Any) -> str | None:
        return _lenient_str(value, MAX_REASON_LENGTH)

    @field_validator("policy_version", "baseline_id", mode="before")
    @classmethod
    def _identifier(cls, value: Any) -> str | None:
        return _lenient_str(value, MAX_IDENTIFIER_LENGTH)

    @field_validator("route_mode", mode="before")
    @classmethod
    def _route_mode(cls, value: Any) -> str | None:
        return _lenient_choice(value, ROUTE_MODES)

    @field_validator("sensitivity_source", mode="before")
    @classmethod
    def _sensitivity_source(cls, value: Any) -> str | None:
        return _lenient_choice(value, SENSITIVITY_SOURCES)

    @field_validator("fallback_chain", mode="before")
    @classmethod
    def _fallback_chain(cls, value: Any) -> list[str] | None:
        # Error classes only (rate_limited, server_error, ...): anything
        # that is not a short snake_case token, such as an error message,
        # drops the whole field so content can never be stored here.
        if not isinstance(value, list) or len(value) > MAX_FALLBACK_CHAIN:
            return None
        if not all(isinstance(v, str) and ERROR_CLASS_RE.fullmatch(v) for v in value):
            return None
        return value

    @field_validator("route_est_cost", "counterfactual_cost", mode="before")
    @classmethod
    def _route_cost(cls, value: Any) -> float | None:
        return _lenient_amount(value, MAX_COST)

    @field_validator("modality", mode="before")
    @classmethod
    def _modality(cls, value: Any) -> str | None:
        value = _lenient_str(value, MAX_LABEL_LENGTH)
        if value is None:
            return None
        value = value.lower()
        # An unknown value would register the asset under a modality nothing
        # filters on; it is dropped (counted as modality:invalid) instead.
        return value if value in MODALITIES else None

    @field_validator(
        "tokens_in",
        "tokens_out",
        "latency_ms",
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
        return _lenient_amount(value, MAX_USAGE_UNITS)

    @field_validator("cost", mode="before")
    @classmethod
    def _cost(cls, value: Any) -> float | None:
        return _lenient_amount(value, MAX_COST)


# Limits for the lenient fields (extended usage, cost and the core counts).
# Longer or out-of-range values are dropped (stored as null), not truncated:
# half a model name is wrong data.
MAX_MODEL_NAME_LENGTH = 256
MAX_LABEL_LENGTH = 64
MAX_TOKEN_COUNT = 2**31 - 1  # Integer column
MAX_USAGE_UNITS = 1e12  # fits Numeric(20, 6)
MAX_COST = 9999.999999  # largest Numeric(10, 6) value
FINGERPRINT_RE = re.compile(r"[0-9a-f]{16}")
MAX_REASON_LENGTH = 256
MAX_IDENTIFIER_LENGTH = 128
# The gateway keeps at most MAX_ATTEMPT_RECORDS (32) attempts per request.
MAX_FALLBACK_CHAIN = 32
ERROR_CLASS_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
# PRD 0001 §7.12. Matched case-insensitively, stored lower-case.
ROUTE_MODES = frozenset({"gateway", "local", "none"})
SENSITIVITY_SOURCES = frozenset({"declared", "pii_escalated", "default"})

# The modalities telemetry reports, as the gateway's modalityOf() sets them
# from the endpoint: chat, completions, messages, responses, embeddings and
# rerank are text; /v1/images is image; /v1/audio is audio; /v1/ocr is
# document. Matched case-insensitively, stored lower-case.
MODALITIES = frozenset({"text", "image", "audio", "document"})

# PRD 0001 §7.12 routing metadata (#25), stored as sent once validated.
ROUTE_FIELDS = (
    "api_surface",
    "route_mode",
    "route_group",
    "route_reason",
    "route_est_cost",
    "sensitivity_level",
    "sensitivity_source",
    "served_provider",
    "served_jurisdiction",
    "policy_version",
    "fallback_chain",
    "counterfactual_cost",
    "baseline_id",
)

# Lenient fields whose dropped values are reported in ignored_fields as
# "<field>:invalid", so a client sending bad values shows up in
# /telemetry/loss instead of losing the data silently.
LENIENT_FIELDS = (
    "tokens_in",
    "tokens_out",
    "cost",
    "latency_ms",
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
    "api_key_fingerprint",
) + ROUTE_FIELDS


def _lenient_str(value: Any, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > max_length:
        return None
    return value


def _lenient_choice(value: Any, choices: frozenset[str]) -> str | None:
    value = _lenient_str(value, MAX_LABEL_LENGTH)
    if value is None:
        return None
    value = value.lower()
    return value if value in choices else None


def _lenient_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or value < 0 or value > MAX_TOKEN_COUNT:
        return None
    return value


def _lenient_amount(value: Any, maximum: float) -> float | None:
    """A finite number in [0, maximum] once rounded to the column's 6 decimal
    places (9999.9999999 rounds to 10000 and overflows Numeric(10, 6))."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # Check ints before float(): a huge JSON integer overflows float()
    # with OverflowError, which pydantic does not catch (500, batch lost).
    if isinstance(value, int) and not 0 <= value <= maximum:
        return None
    value = float(value)
    if not math.isfinite(value) or value < 0 or round(value, 6) > maximum:
        return None
    return value


# Fields dropped when they hold a value the database cannot store. The
# vendor is required, so an event whose vendor cannot be stored is skipped
# instead (unstorable_event). The prompt text is only hashed, never stored.
SANITISED_FIELDS = tuple(
    name for name in IngestEvent.model_fields if name not in ("vendor", "prompt_text")
)
# Fields whose dropped values are reported as "<field>:invalid": the lenient
# ones plus every sanitised one that has no default to fall back to.
REPORTED_FIELDS = tuple(
    dict.fromkeys(
        LENIENT_FIELDS
        + tuple(n for n in SANITISED_FIELDS if IngestEvent.model_fields[n].default is None)
    )
)
UNSTORABLE = "unstorable"


def unstorable_event(raw: Any) -> bool:
    """An event that cannot be stored at all: its vendor is a string the
    database cannot hold."""
    if not isinstance(raw, dict):
        return False
    vendor = raw.get("vendor")
    return isinstance(vendor, str) and storable_text(vendor) is None


def storable_events(raw_events: Any) -> list:
    """The raw events that are not skipped by ``unstorable_event``."""
    if not isinstance(raw_events, list):
        return []
    return [e for e in raw_events if not unstorable_event(e)]


class IngestRequest(BaseModel):
    events: list[IngestEvent]
    # SDK delivery accounting (#87); parsed leniently by loss_accounting.
    metadata: dict | None = None
    # Events skipped by unstorable_event; never read from the body.
    _unstorable: int = PrivateAttr(default=0)

    @model_validator(mode="wrap")
    @classmethod
    def _skip_unstorable_events(cls, data: Any, handler):
        skipped = 0
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            kept = storable_events(data["events"])
            skipped = len(data["events"]) - len(kept)
            if skipped:
                data = {**data, "events": kept}
        model = handler(data)
        model._unstorable = skipped
        return model


def _enforce_rate_limit(
    limiter: FixedWindowRateLimiter, tenant_id, response: Response, limit: int | None = None
) -> None:
    """Per-tenant request limit on ingest. Sets X-RateLimit-* headers and
    raises 429 with Retry-After once the tenant exceeds the window. ``limit``
    defaults to PS_RATE_LIMIT_PER_MINUTE."""
    if limit is None:
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
            metrics.record_ingest("events", "rate_limited", len(body.events))
            await loss_accounting.record_rejected(
                db, tenant_id, meta, len(body.events), "rate_limited"
            )
        raise

    try:
        assets_created = await _store_events(db, tenant_id, body.events)
        if body.events:
            raw_events = storable_events((await request.json()).get("events"))
            ignored = loss_accounting.ignored_fields(
                raw_events, set(IngestEvent.model_fields)
            )
            for key, n in loss_accounting.invalid_fields(
                raw_events, body.events, REPORTED_FIELDS
            ).items():
                ignored[key] = ignored.get(key, 0) + n
            await loss_accounting.record_received(
                db, tenant_id, meta, len(body.events), ignored
            )
        await db.commit()
    except Exception:
        await db.rollback()
        metrics.record_ingest("events", "write_failed", len(body.events))
        await loss_accounting.record_rejected(
            db, tenant_id, meta, len(body.events), "write_failed"
        )
        raise

    metrics.record_ingest("events", "accepted", len(body.events))
    metrics.record_assets_created("events", assets_created)
    if body._unstorable:
        # Answered 200, so the client will not resend them.
        metrics.record_ingest("events", "rejected", body._unstorable)
        await loss_accounting.record_rejected(
            db, tenant_id, meta, body._unstorable, UNSTORABLE
        )
    return {
        "ingested": len(body.events),
        "assets_created": assets_created,
        "rejected": body._unstorable,
    }


async def _store_events(db, tenant_id, events: list[IngestEvent]) -> int:
    assets_created = 0
    changes = AssetChanges()
    await lock_asset_keys(
        db,
        tenant_id,
        (
            AssetKey(e.vendor, e.model, e.use_case_name, e.business_unit, e.environment)
            for e in events
        ),
    )
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
            # surrogatepass: a lone surrogate (never stored) must not fail
            # the batch's write; valid text hashes as before.
            prompt_hash = hashlib.sha256(
                event.prompt_text.encode("utf-8", "surrogatepass")
            ).hexdigest()

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
            api_key_fingerprint=event.api_key_fingerprint,
            **{name: getattr(event, name) for name in ROUTE_FIELDS},
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
