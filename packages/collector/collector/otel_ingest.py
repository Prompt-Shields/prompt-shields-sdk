"""OTLP/HTTP trace receiver for OTel GenAI spans.

Exporters point ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` (or the base
``OTEL_EXPORTER_OTLP_ENDPOINT`` set to ``<collector>/ingest/otel``) here with
``Authorization: Bearer <tenant ingest key>``. Both OTLP/HTTP encodings are
accepted: JSON (``application/json``) and protobuf (``application/x-protobuf``,
the default of OpenLLMetry and most OTel SDKs; collector/otlp_protobuf.py).
The response uses the request's encoding. Compressed bodies
(``Content-Encoding: gzip``) are not supported.

Limited per tenant (``PS_OTEL_TRACES_RATE_LIMIT_PER_MINUTE``, default 600
requests/minute, or the tenant's ``ingest_rate_limits.otel_traces``; 429 with
``Retry-After``), counted apart from ``/ingest/events``.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Annotated, NoReturn
from uuid import UUID

from db.models import AIUsageEvent
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from collector import metrics, tenant_keys, tenant_limits
from collector.auth import resolve_tenant
from collector.data_flows import record_event_flows
from collector.dedup import AssetKey, find_or_create_asset, lock_asset_keys
from collector.embeddings import build_asset_text, get_embedding
from collector.ingest import _enforce_rate_limit
from collector.loss_accounting import OtelCounts, otel_client_id, record_otel
from collector.mcp_usage import McpCall, record_calls, register_assets
from collector.otel_mapping import (
    GenAISpan,
    count_spans,
    invalid_attributes,
    is_genai_span,
    is_mcp_span,
    iter_spans,
    map_mcp_span,
    map_span,
    safe_attributes,
)
from collector.otlp_protobuf import (
    PROTOBUF_CONTENT_TYPES,
    PROTOBUF_MEDIA_TYPE,
    TooManySpans,
    decode_traces,
    encode_response,
)
from collector.partner_rate_limit import RateLimiter, get_otel_traces_rate_limiter
from collector.sanitize import BodyTooDeep, loads_bounded, too_deep_problem
from collector.webhooks import AssetChanges

logger = logging.getLogger(__name__)
router = APIRouter()

OTEL_SOURCE = "otel"
# Same limit as /ingest/events (ingest.MAX_MODEL_NAME_LENGTH): a longer
# requested/served model attribute is stored as null, not truncated.
_MAX_MODEL_NAME_LENGTH = 256
# ingest.MAX_LABEL_LENGTH, for gen_ai.operation.name stored as api_surface.
_MAX_LABEL_LENGTH = 64


def _bounded_model(value: str | None) -> str | None:
    if value is None or len(value) > _MAX_MODEL_NAME_LENGTH:
        return None
    return value


def _bounded_label(value: str | None) -> str | None:
    if value is None or len(value) > _MAX_LABEL_LENGTH:
        return None
    return value


MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_SPANS = 2000


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


async def _read_bounded_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise _problem(413, "Payload Too Large", f"Body exceeds {MAX_BODY_BYTES} bytes")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise _problem(
                413, "Payload Too Large", f"Body exceeds {MAX_BODY_BYTES} bytes"
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def write_spans(db: AsyncSession, tenant_id: UUID, spans: list[GenAISpan]) -> int:
    """Persist mapped spans as usage events. Returns the number of new assets.

    Mirrors the ``/ingest/events`` write path. No prompt hash is stored because
    content attributes never reach this point.
    """
    assets_created = 0
    changes = AssetChanges()
    await lock_asset_keys(
        db,
        tenant_id,
        (AssetKey(s.vendor, s.model, environment=s.environment) for s in spans),
    )
    for span in spans:
        asset, is_new = await find_or_create_asset(
            db=db,
            tenant_id=tenant_id,
            vendor=span.vendor,
            model=span.model,
            use_case_name=None,
            business_unit=None,
            calling_service=span.calling_service,
            source=OTEL_SOURCE,
            owner_email=None,
            environment=span.environment,
            data_classification=None,
        )
        changes.record(asset, is_new)
        if is_new:
            assets_created += 1
            await db.flush()
            if asset.embedding is None:
                embedding = await get_embedding(
                    build_asset_text(
                        vendor=span.vendor,
                        model=span.model,
                        use_case_name=None,
                        business_unit=None,
                        data_classification=None,
                    )
                )
                if embedding:
                    asset.embedding = embedding

        usage_event = AIUsageEvent(
            tenant_id=tenant_id,
            asset_id=asset.id,
            tokens_in=span.tokens_in,
            tokens_out=span.tokens_out,
            latency_ms=span.latency_ms,
            source=OTEL_SOURCE,
            session_id=span.session_id,
            requested_model=_bounded_model(span.requested_model),
            served_model=_bounded_model(span.served_model),
            api_surface=_bounded_label(span.api_surface),
        )
        db.add(usage_event)
        await record_event_flows(
            db, tenant_id, asset, usage_event, calling_service=span.calling_service
        )
    await changes.enqueue(db, tenant_id)
    await db.commit()
    return assets_created


async def _reject_too_many_spans(
    db: AsyncSession, tenant_id: UUID, genai_clients: list[str]
) -> NoReturn:
    """413 for a request over the span limit. Exporters do not retry a 413:
    the GenAI spans in it (one client id each) are lost."""
    counts: dict[str, OtelCounts] = {}
    for client in genai_clients:
        counts.setdefault(client, OtelCounts()).reject("too_large")
    await record_otel(db, tenant_id, counts)
    raise _problem(
        413, "Payload Too Large", f"More than {MAX_SPANS} spans in one request"
    )


@router.post("/ingest/otel/v1/traces")
async def ingest_otel_traces(
    request: Request,
    response: Response,
    limiter: Annotated[RateLimiter, Depends(get_otel_traces_rate_limiter)],
):
    tenant_id = await resolve_tenant(request, tenant_keys.INGEST_WRITE)
    # Per-tenant request limit (PS_OTEL_TRACES_RATE_LIMIT_PER_MINUTE or the
    # tenant's ingest_rate_limits.otel_traces), counted apart from
    # /ingest/events; 429 with Retry-After, which OTLP exporters retry.
    await _enforce_rate_limit(
        limiter,
        tenant_id,
        response,
        await tenant_limits.limit_for(request.state.db, tenant_id, "otel_traces"),
    )

    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    is_protobuf = content_type in PROTOBUF_CONTENT_TYPES
    if content_type != "application/json" and not is_protobuf:
        raise _problem(
            415,
            "Unsupported Media Type",
            "Use OTLP/HTTP JSON (application/json) or protobuf"
            " (application/x-protobuf)",
        )

    raw = await _read_bounded_body(request)
    db = request.state.db
    try:
        payload = decode_traces(raw, MAX_SPANS) if is_protobuf else loads_bounded(raw)
    except TooManySpans as exc:
        await _reject_too_many_spans(db, tenant_id, exc.genai_clients)
    except BodyTooDeep:
        raise too_deep_problem() from None
    except ValueError:  # UnicodeDecodeError included
        what = "an OTLP protobuf message" if is_protobuf else "valid JSON"
        raise _problem(400, "Bad Request", f"Body is not {what}") from None
    if not isinstance(payload, dict):
        raise _problem(
            400, "Bad Request", "Expected an ExportTraceServiceRequest object"
        )

    if count_spans(payload) > MAX_SPANS:
        await _reject_too_many_spans(
            db,
            tenant_id,
            [
                otel_client_id(resource_attrs)
                for span, resource_attrs in iter_spans(payload)
                if is_genai_span(safe_attributes(span.get("attributes")))
            ],
        )

    mcp_calls: list[McpCall] = []
    assets_created = 0
    mapped, counts = classify_spans(payload, mcp_calls=mcp_calls)
    if mapped or mcp_calls:
        try:
            if mcp_calls:
                # Assets are registered after the commit, below.
                await record_calls(db, tenant_id, mcp_calls, register=False)
            if mapped:
                assets_created = await write_spans(db, tenant_id, mapped)  # commits
            else:
                await db.commit()
        except Exception:
            metrics.record_ingest("otel", "write_failed", len(mapped))
            with contextlib.suppress(Exception):
                await db.rollback()
            for c in counts.values():
                c.reject("write_failed", c.received)
                c.received = 0
            await record_otel(db, tenant_id, counts)
            raise
    if mcp_calls:
        await _register_mcp_assets(db, tenant_id, mcp_calls)
    await record_otel(db, tenant_id, counts)

    # ExportTraceServiceResponse: an empty object means full success. Non-GenAI
    # spans are ignored, not rejected; GenAI spans that cannot be mapped are
    # rejected and reported in partialSuccess (OTLP/HTTP spec).
    rejected = sum(c.rejected.get(NO_VENDOR, 0) for c in counts.values())
    if mapped:
        metrics.record_ingest("otel", "accepted", len(mapped))
        metrics.record_assets_created("otel", assets_created)
    metrics.record_ingest("otel", "rejected", rejected)
    error_message = (
        "GenAI spans without gen_ai.provider.name or gen_ai.system"
        if rejected
        else ""
    )
    if is_protobuf:
        return Response(
            content=encode_response(rejected, error_message),
            media_type=PROTOBUF_MEDIA_TYPE,
            headers={
                k: v for k, v in response.headers.items() if k.startswith("x-ratelimit-")
            },
        )
    if rejected:
        return {
            "partialSuccess": {
                "rejectedSpans": rejected,
                "errorMessage": error_message,
            }
        }
    return {}


async def _register_mcp_assets(db, tenant_id, calls: list[McpCall]) -> None:
    """MCP servers as assets (#77), in their own transaction after the spans
    are stored: the span write locks LLM asset keys, and taking MCP asset
    locks in the same transaction could deadlock with another ingest. The
    calls are already counted, so a failure here is logged, not raised; the
    next batch from the same server registers it."""
    try:
        await register_assets(db, tenant_id, calls)
        await db.commit()
    except Exception:
        logger.warning("MCP asset registration failed", exc_info=True)
        with contextlib.suppress(Exception):
            await db.rollback()


NO_VENDOR = "no_vendor"


def classify_spans(
    payload: dict,
    *,
    mcp_calls: list[McpCall] | None = None,
) -> tuple[list[GenAISpan], dict[str, OtelCounts]]:
    """Map every span and count, per OTLP client (service), what is stored,
    rejected (GenAI span without a vendor), ignored (non-GenAI span) and
    which values were dropped as invalid (#87).

    MCP ``tools/call`` spans are tool calls, not usage events: they are
    appended to ``mcp_calls`` (when given) and left out of the usage-event
    counts. Other MCP spans are classified like any span."""
    mapped: list[GenAISpan] = []
    counts: dict[str, OtelCounts] = {}
    for span, resource_attrs in iter_spans(payload):
        client_id = otel_client_id(resource_attrs)
        c = counts.setdefault(client_id, OtelCounts())
        attrs = safe_attributes(span.get("attributes"))
        if is_mcp_span(attrs):
            call = map_mcp_span(span, resource_attrs)
            if call is not None:
                if mcp_calls is not None:
                    mcp_calls.append(call)
                continue
        if not is_genai_span(attrs):
            c.ignore({"span:non_genai": 1})
            continue
        result = map_span(span, resource_attrs)
        if result is None:
            c.reject(NO_VENDOR)
            continue
        invalid = invalid_attributes(span)
        for key, value in (
            ("gen_ai.request.model", result.requested_model),
            ("gen_ai.response.model", result.served_model),
        ):
            if value is not None and _bounded_model(value) is None:
                invalid[f"{key}:invalid"] = 1
        c.ignore(invalid)
        c.received += 1
        mapped.append(result)
    return mapped, counts
