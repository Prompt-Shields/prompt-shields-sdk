"""OTLP/HTTP trace receiver for OTel GenAI spans.

Exporters point ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` (or the base
``OTEL_EXPORTER_OTLP_ENDPOINT`` set to ``<collector>/ingest/otel``) here with
``Authorization: Bearer <tenant ingest key>``. Only JSON encoding is accepted
for now; protobuf requests get 415.
"""

from __future__ import annotations

import json
from uuid import UUID

from db.models import AIUsageEvent
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from collector.auth import resolve_tenant
from collector.data_flows import record_event_flows
from collector.dedup import AssetKey, find_or_create_asset, lock_missing_asset_keys
from collector.embeddings import build_asset_text, get_embedding
from collector.otel_mapping import GenAISpan, count_spans, iter_spans, map_span
from collector.webhooks import AssetChanges

router = APIRouter()

OTEL_SOURCE = "otel"
# Same limit as /ingest/events (ingest.MAX_MODEL_NAME_LENGTH): a longer
# requested/served model attribute is stored as null, not truncated.
_MAX_MODEL_NAME_LENGTH = 256


def _bounded_model(value: str | None) -> str | None:
    if value is None or len(value) > _MAX_MODEL_NAME_LENGTH:
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
    await lock_missing_asset_keys(
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
        )
        db.add(usage_event)
        await record_event_flows(
            db, tenant_id, asset, usage_event, calling_service=span.calling_service
        )
    await changes.enqueue(db, tenant_id)
    await db.commit()
    return assets_created


@router.post("/ingest/otel/v1/traces")
async def ingest_otel_traces(request: Request):
    tenant_id = await resolve_tenant(request)

    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise _problem(
            415,
            "Unsupported Media Type",
            "Only OTLP/HTTP JSON (application/json) is supported",
        )

    raw = await _read_bounded_body(request)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise _problem(400, "Bad Request", "Body is not valid JSON") from None
    if not isinstance(payload, dict):
        raise _problem(
            400, "Bad Request", "Expected an ExportTraceServiceRequest object"
        )

    if count_spans(payload) > MAX_SPANS:
        raise _problem(
            413, "Payload Too Large", f"More than {MAX_SPANS} spans in one request"
        )

    mapped: list[GenAISpan] = []
    for span, resource_attrs in iter_spans(payload):
        result = map_span(span, resource_attrs)
        if result is not None:
            mapped.append(result)

    if mapped:
        await write_spans(request.state.db, tenant_id, mapped)

    # ExportTraceServiceResponse: an empty object means full success. Non-GenAI
    # spans are ignored, not rejected.
    return {}
