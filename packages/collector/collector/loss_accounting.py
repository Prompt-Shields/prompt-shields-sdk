"""Telemetry loss accounting (#87).

The SDK sends batch metadata next to its events:

    {"events": [...], "metadata": {"client_id": "...", "seq": 7,
     "enqueued_total": 120, "dropped": {"send_failed": 3}, "sdk": "python"}}

seq is a per-client monotonic batch counter; enqueued_total and dropped are
cumulative since the client started. The collector stores, per tenant and
client, what it received, the latest SDK-reported figures, what it rejected
(rate limited, write failed) and which unknown event fields it ignored.

From those, GET /telemetry/loss reports per client:

    unaccounted = sdk_enqueued_total - received - sdk_dropped_total

events the SDK says it queued that neither arrived nor were reported
dropped. Right after a successful batch this is the silent loss; it also
includes anything still buffered in the SDK. Duplicated deliveries can push
it negative; it is clamped at zero.

Accounting is fail-open: a broken metadata object is ignored, and a failure
to record a rejection never changes the response the caller gets.

Sources. Each client row belongs to one source, from the ``sdk`` name it
reports: "gateway" (the AI gateway's ps-telemetry, which sends the same
metadata as the SDK), "otel" (the OTLP GenAI receiver, one row per
``service.name``), "sdk" for any other identified client, and
"unidentified" for batches without metadata. OTLP exporters send no
counters of their own, so for "otel" what the collector rejected (GenAI
spans it could not map, oversized requests, failed writes) is the loss:
OTLP/HTTP exporters do not retry those responses. For SDK and gateway
clients a rejection is retried by the client and shows up later as
received or as a client-reported drop, so it is not counted twice.
GET /telemetry/loss, /api/v1/registry/telemetry/coverage and
/partner/v1/telemetry/coverage report coverage per source.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from db.models import TelemetryClientStats
from fastapi import APIRouter, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from collector.auth import resolve_tenant

logger = logging.getLogger("collector.loss_accounting")

router = APIRouter()

UNIDENTIFIED_CLIENT = "unidentified"
GATEWAY_SDK = "gateway"
OTEL_SDK = "otel"
OTEL_CLIENT_PREFIX = "otel:"
# Reporting order of the sources.
SOURCES = ("sdk", "gateway", "otel", "unidentified")
_MAX_ID_LEN = 128
_MAX_KEY_LEN = 64
_MAX_KEYS = 32  # per counter map, so a client cannot grow a row without bound
_MAX_COUNT = 2**62


@dataclass
class BatchMetadata:
    client_id: str = UNIDENTIFIED_CLIENT
    seq: int | None = None
    enqueued_total: int | None = None
    dropped: dict[str, int] = field(default_factory=dict)
    sdk: str | None = None


def _count(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= _MAX_COUNT else None


def parse_metadata(raw) -> BatchMetadata:
    """Lenient parse: anything malformed falls back to the unidentified client
    rather than failing the ingest request (the SDK would retry it forever)."""
    if not isinstance(raw, dict):
        return BatchMetadata()
    client_id = raw.get("client_id")
    if not isinstance(client_id, str) or not 0 < len(client_id) <= _MAX_ID_LEN:
        return BatchMetadata()
    dropped: dict[str, int] = {}
    raw_dropped = raw.get("dropped")
    if isinstance(raw_dropped, dict):
        for reason, n in list(raw_dropped.items())[:_MAX_KEYS]:
            n = _count(n)
            if isinstance(reason, str) and 0 < len(reason) <= _MAX_KEY_LEN and n:
                dropped[reason] = n
    sdk = raw.get("sdk")
    return BatchMetadata(
        client_id=client_id,
        seq=_count(raw.get("seq")),
        enqueued_total=_count(raw.get("enqueued_total")),
        dropped=dropped,
        sdk=sdk[:_MAX_KEY_LEN] if isinstance(sdk, str) else None,
    )


def ignored_fields(raw_events, known: set[str]) -> dict[str, int]:
    """Count event keys the ingest model does not know (and so discards)."""
    counts: dict[str, int] = {}
    if not isinstance(raw_events, list):
        return counts
    for event in raw_events:
        if not isinstance(event, dict):
            continue
        for key in event:
            if key not in known and isinstance(key, str):
                counts[key[:_MAX_KEY_LEN]] = counts.get(key[:_MAX_KEY_LEN], 0) + 1
    return counts


def invalid_fields(raw_events, parsed_events, fields) -> dict[str, int]:
    """Count lenient fields that were sent with a value but parsed to None
    (wrong type, out of range, too long), keyed "<field>:invalid"."""
    counts: dict[str, int] = {}
    if not isinstance(raw_events, list):
        return counts
    for raw, parsed in zip(raw_events, parsed_events):
        if not isinstance(raw, dict):
            continue
        for name in fields:
            if raw.get(name) is not None and getattr(parsed, name, None) is None:
                key = f"{name}:invalid"
                counts[key] = counts.get(key, 0) + 1
    return counts


def _merge(base: dict | None, extra: dict[str, int]) -> dict[str, int]:
    merged = dict(base or {})
    for key, n in extra.items():
        if key in merged or len(merged) < _MAX_KEYS:
            merged[key] = merged.get(key, 0) + n
    return merged


async def _locked_row(
    db: AsyncSession, tenant_id: UUID, client_id: str, sdk: str | None = None
) -> TelemetryClientStats:
    await db.execute(
        pg_insert(TelemetryClientStats)
        .values(tenant_id=tenant_id, client_id=client_id, sdk=sdk)
        .on_conflict_do_nothing(index_elements=["tenant_id", "client_id"])
    )
    stmt = (
        select(TelemetryClientStats)
        .where(
            TelemetryClientStats.tenant_id == tenant_id,
            TelemetryClientStats.client_id == client_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await db.execute(stmt)).scalar_one()


async def record_received(
    db: AsyncSession,
    tenant_id: UUID,
    meta: BatchMetadata,
    received: int,
    ignored: dict[str, int],
) -> None:
    """Add a stored batch to the client's counters. Runs in the caller's
    transaction so the counts commit (or roll back) with the events."""
    row = await _locked_row(db, tenant_id, meta.client_id)
    row.received_events += received
    row.received_batches += 1
    if meta.seq is not None and meta.seq > row.last_seq:
        # Only a newer batch may replace the cumulative SDK figures; a
        # late retry of an older batch must not roll them back.
        row.last_seq = meta.seq
        if meta.enqueued_total is not None:
            row.sdk_enqueued_total = meta.enqueued_total
        row.sdk_dropped = meta.dropped
        if meta.sdk:
            row.sdk = meta.sdk
    if ignored:
        row.ignored_fields = _merge(row.ignored_fields, ignored)
    row.last_seen = datetime.now(UTC)


async def record_rejected(
    db: AsyncSession,
    tenant_id: UUID,
    meta: BatchMetadata,
    events: int,
    reason: str,
) -> None:
    """Count a batch the collector refused or failed to store, in its own
    transaction. Never raises."""
    if events <= 0:
        return
    try:
        row = await _locked_row(db, tenant_id, meta.client_id, meta.sdk)
        row.collector_rejected = _merge(row.collector_rejected, {reason: events})
        row.last_seen = datetime.now(UTC)
        await db.commit()
    except Exception:  # accounting must never mask the real response
        logger.exception("could not record %d rejected event(s) (%s)", events, reason)
        with contextlib.suppress(Exception):
            await db.rollback()


# --- OTLP receiver -------------------------------------------------------------


def otel_client_id(resource_attrs: dict) -> str:
    """The loss-accounting client for an OTLP resource: one per service."""
    name = resource_attrs.get("service.name")
    if isinstance(name, str) and name.strip():
        return f"{OTEL_CLIENT_PREFIX}{name.strip()}"[:_MAX_ID_LEN]
    return OTEL_SDK


@dataclass
class OtelCounts:
    """What one OTLP request did with one client's spans."""

    received: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    ignored: dict[str, int] = field(default_factory=dict)

    def reject(self, reason: str, n: int = 1) -> None:
        if n > 0:
            self.rejected[reason] = self.rejected.get(reason, 0) + n

    def ignore(self, counts: dict[str, int]) -> None:
        for key, n in counts.items():
            self.ignored[key] = self.ignored.get(key, 0) + n


async def record_otel(
    db: AsyncSession, tenant_id: UUID, counts: dict[str, OtelCounts]
) -> None:
    """Add one OTLP request's outcome to its clients' counters, in its own
    transaction (the spans are already committed or rolled back). Never
    raises: accounting must not change the receiver's response."""
    if not counts:
        return
    try:
        now = datetime.now(UTC)
        for client_id in sorted(counts):  # stable lock order across requests
            c = counts[client_id]
            row = await _locked_row(db, tenant_id, client_id, OTEL_SDK)
            row.sdk = OTEL_SDK
            if c.received:
                row.received_events += c.received
                row.received_batches += 1
            if c.rejected:
                row.collector_rejected = _merge(row.collector_rejected, c.rejected)
            if c.ignored:
                row.ignored_fields = _merge(row.ignored_fields, c.ignored)
            row.last_seen = now
        await db.commit()
    except Exception:  # accounting must never mask the real response
        logger.exception("could not record OTLP loss accounting")
        with contextlib.suppress(Exception):
            await db.rollback()


# --- reporting -----------------------------------------------------------------


def source_of(client_id: str, sdk: str | None) -> str:
    if client_id == UNIDENTIFIED_CLIENT:
        return "unidentified"
    if sdk == OTEL_SDK:
        return "otel"
    if sdk == GATEWAY_SDK:
        return "gateway"
    return "sdk"


def _ratio(received: int, expected: int) -> float | None:
    return round(received / expected, 4) if expected else None


def _summary(row: TelemetryClientStats) -> dict:
    sdk_dropped = dict(row.sdk_dropped or {})
    dropped_total = sum(sdk_dropped.values())
    source = source_of(row.client_id, row.sdk)
    rejected = dict(row.collector_rejected or {})
    # OTLP rejections are final (see the module docstring).
    lost = dropped_total + (sum(rejected.values()) if source == "otel" else 0)
    identified = row.client_id != UNIDENTIFIED_CLIENT and row.last_seq > 0
    unaccounted = (
        max(0, row.sdk_enqueued_total - row.received_events - dropped_total)
        if identified
        else None
    )
    expected = row.received_events + lost + (unaccounted or 0)
    return {
        "client_id": row.client_id,
        "source": source,
        "sdk": row.sdk,
        "received_events": row.received_events,
        "received_batches": row.received_batches,
        "last_seq": row.last_seq,
        "sdk_enqueued_total": row.sdk_enqueued_total if identified else None,
        "sdk_dropped": sdk_dropped,
        "sdk_dropped_total": dropped_total,
        "unaccounted": unaccounted,
        "collector_rejected": rejected,
        "lost_events": lost,
        "ignored_fields": dict(row.ignored_fields or {}),
        "coverage": _ratio(row.received_events, expected),
        "first_seen": row.first_seen.isoformat() if row.first_seen else None,
        "last_seen": row.last_seen.isoformat() if row.last_seen else None,
    }


def coverage_by_source(clients: list[dict]) -> list[dict]:
    """Per-source totals of client summaries, in SOURCES order.

    dropped_events is what the clients reported dropped plus, for "otel",
    what the collector rejected; unaccounted_events is the silent gap.
    Coverage is null for "unidentified": without client counters the
    collector cannot know what it did not receive.
    """
    out = []
    for source in SOURCES:
        rows = [c for c in clients if c["source"] == source]
        if not rows:
            continue
        received = sum(c["received_events"] for c in rows)
        dropped = sum(c["lost_events"] for c in rows)
        unaccounted = sum(c["unaccounted"] or 0 for c in rows)
        last_seen = max((c["last_seen"] for c in rows if c["last_seen"]), default=None)
        out.append(
            {
                "source": source,
                "clients": len(rows),
                "received_events": received,
                "dropped_events": dropped,
                "unaccounted_events": unaccounted,
                "coverage": None
                if source == "unidentified"
                else _ratio(received, received + dropped + unaccounted),
                "last_seen": last_seen,
            }
        )
    return out


async def _client_summaries(db: AsyncSession, tenant_id: UUID) -> list[dict]:
    rows = (
        (
            await db.execute(
                select(TelemetryClientStats)
                .where(TelemetryClientStats.tenant_id == tenant_id)
                .order_by(TelemetryClientStats.last_seen.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_summary(row) for row in rows]


async def tenant_coverage(db: AsyncSession, tenant_id: UUID) -> dict:
    """The tenant's telemetry coverage, overall and per source (the
    registry and Partner API shape)."""
    sources = coverage_by_source(await _client_summaries(db, tenant_id))
    received = sum(s["received_events"] for s in sources)
    dropped = sum(s["dropped_events"] for s in sources)
    unaccounted = sum(s["unaccounted_events"] for s in sources)
    return {
        "coverage": _ratio(received, received + dropped + unaccounted),
        "received_events": received,
        "dropped_events": dropped,
        "unaccounted_events": unaccounted,
        "sources": sources,
    }


@router.get("/telemetry/loss")
async def telemetry_loss(request: Request):
    """Per-tenant delivery accounting: received vs dropped, per client and
    per source (SDK, gateway, OTLP)."""
    tenant_id = await resolve_tenant(request)
    clients = await _client_summaries(request.state.db, tenant_id)

    sdk_dropped: dict[str, int] = {}
    rejected: dict[str, int] = {}
    for c in clients:
        for k, n in c["sdk_dropped"].items():
            sdk_dropped[k] = sdk_dropped.get(k, 0) + n
        for k, n in c["collector_rejected"].items():
            rejected[k] = rejected.get(k, 0) + n
    received = sum(c["received_events"] for c in clients)
    dropped_total = sum(sdk_dropped.values())
    lost = sum(c["lost_events"] for c in clients)
    unaccounted = sum(c["unaccounted"] or 0 for c in clients)
    return {
        "totals": {
            "clients": len(clients),
            "received_events": received,
            "sdk_dropped": sdk_dropped,
            "sdk_dropped_total": dropped_total,
            "lost_events": lost,
            "unaccounted": unaccounted,
            "collector_rejected": rejected,
            "coverage": _ratio(received, received + lost + unaccounted),
        },
        "sources": coverage_by_source(clients),
        "clients": clients,
    }
