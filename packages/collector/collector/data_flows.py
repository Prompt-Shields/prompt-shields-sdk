"""Derive ``data_flows`` rows from ingested telemetry.

Each usage event yields one or more flows for its asset:

* ``input``: every entry in ``data_sources`` (or, without any, the calling
  service) -> ``vendor/model[@region]``. These carry the PII categories seen.
* ``output``: ``vendor/model[@region]`` -> ``output_destination``, when set.

A flow is keyed on (tenant, asset, source, destination, direction). Per flow
we keep the event count, first and last seen, the highest data
classification and the union of PII categories.

Two entry points share the same derivation:

* :func:`record_event_flows`, called once per event from ingest: stores the
  event's flow metadata on ``ai_usage_events.flow_context`` and does one
  additive upsert.
* :func:`rebuild_flows`, a batch job per tenant (``python -m
  collector.data_flows --tenant <id>`` or ``--all-tenants``): re-derives every
  flow from stored events. Counts are replaced, the other fields only ever
  widen, so running it any number of times gives the same result. Events
  stored before ``flow_context`` existed fall back to the asset's calling
  service and classification.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from db.models import AIAsset, AIUsageEvent, DataFlow, Tenant, uuid7
from sqlalchemy import case, literal_column, null, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from collector.dedup import CLASSIFICATION_ORDER

INPUT = "input"
OUTPUT = "output"
UNKNOWN_SOURCE = "unknown"

MAX_NAME_LEN = 200
MAX_DATA_SOURCES = 20
MAX_PII_TYPES = 32
UPSERT_CHUNK = 500
STREAM_PARTITION = 1000

_KEY = ("tenant_id", "asset_id", "source_system", "destination_system", "direction")


def _name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:MAX_NAME_LEN] if value else None


def _names(values: Any, limit: int, lower: bool = False) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    out: list[str] = []
    for v in values:
        n = _name(v)
        if n is None:
            continue
        if lower:
            n = n.lower()
        if n not in out:
            out.append(n)
        if len(out) >= limit:
            break
    return out


def build_flow_context(
    *,
    data_sources: Iterable[str] | None = None,
    output_destination: str | None = None,
    pii_types: Iterable[str] | None = None,
    region: str | None = None,
    data_classification: str | None = None,
    calling_service: str | None = None,
) -> dict | None:
    """Normalised, bounded flow metadata for one event (None when empty)."""
    ctx: dict[str, Any] = {}
    sources = _names(list(data_sources or []), MAX_DATA_SOURCES)
    if sources:
        ctx["data_sources"] = sources
    pii = _names(list(pii_types or []), MAX_PII_TYPES, lower=True)
    if pii:
        ctx["pii_types"] = sorted(pii)
    for key, value in (
        ("output_destination", output_destination),
        ("region", region),
        ("service", calling_service),
    ):
        n = _name(value)
        if n:
            ctx[key] = n
    classification = _name(data_classification)
    if classification and classification.lower() in CLASSIFICATION_ORDER:
        ctx["classification"] = classification.lower()
    return ctx or None


@dataclass
class FlowAggregate:
    tenant_id: UUID
    asset_id: UUID
    source_system: str
    destination_system: str
    direction: str
    data_classification: str | None = None
    pii_types: set[str] = field(default_factory=set)
    event_count: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None

    @property
    def key(self) -> tuple:
        return tuple(getattr(self, k) for k in _KEY)

    def add(self, classification: str | None, pii: Iterable[str], ts: datetime) -> None:
        self.event_count += 1
        if _rank(classification) > _rank(self.data_classification):
            self.data_classification = classification
        self.pii_types.update(pii)
        if self.first_seen is None or ts < self.first_seen:
            self.first_seen = ts
        if self.last_seen is None or ts > self.last_seen:
            self.last_seen = ts

    def row(self) -> dict:
        return {
            "id": uuid7(),
            "tenant_id": self.tenant_id,
            "asset_id": self.asset_id,
            "source_system": self.source_system,
            "destination_system": self.destination_system,
            "direction": self.direction,
            "data_classification": self.data_classification,
            # SQL NULL, not a JSON null, when no PII was seen.
            "detected_pii_types": sorted(self.pii_types) or null(),
            "event_count": self.event_count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }


def _rank(classification: str | None) -> int:
    return CLASSIFICATION_ORDER.get(classification or "", -1)


def destination_name(vendor: str, model: str | None, region: str | None) -> str:
    name = f"{vendor}/{model}" if model else vendor
    return f"{name}@{region}" if region else name


def flow_edges(
    *,
    vendor: str,
    model: str | None,
    asset_calling_service: str | None,
    asset_classification: str | None,
    ctx: dict | None,
) -> list[tuple[str, str, str, str | None, list[str]]]:
    """(source, destination, direction, classification, pii) for one event."""
    ctx = ctx or {}
    model_node = destination_name(vendor, model, ctx.get("region"))
    classification = ctx.get("classification") or asset_classification
    pii = list(ctx.get("pii_types") or [])
    sources = ctx.get("data_sources") or [
        ctx.get("service") or asset_calling_service or UNKNOWN_SOURCE
    ]
    edges = [(src, model_node, INPUT, classification, pii) for src in sources]
    if ctx.get("output_destination"):
        edges.append((model_node, ctx["output_destination"], OUTPUT, classification, []))
    return edges


class _Aggregator:
    def __init__(self, tenant_id: UUID):
        self.tenant_id = tenant_id
        self.flows: dict[tuple, FlowAggregate] = {}

    def add_event(
        self,
        *,
        asset_id: UUID,
        vendor: str,
        model: str | None,
        asset_calling_service: str | None,
        asset_classification: str | None,
        ctx: dict | None,
        ts: datetime,
    ) -> None:
        for src, dst, direction, classification, pii in flow_edges(
            vendor=vendor,
            model=model,
            asset_calling_service=asset_calling_service,
            asset_classification=asset_classification,
            ctx=ctx,
        ):
            key = (self.tenant_id, asset_id, src, dst, direction)
            agg = self.flows.get(key)
            if agg is None:
                agg = self.flows[key] = FlowAggregate(*key)
            agg.add(classification, pii, ts)

    def rows(self) -> list[dict]:
        # Sorted so concurrent upserts lock rows in the same order.
        return [self.flows[k].row() for k in sorted(self.flows, key=str)]


def _json_array_sql(col: str) -> str:
    return f"COALESCE(CASE WHEN jsonb_typeof({col}) = 'array' THEN {col} END, '[]'::jsonb)"


_PII_UNION = literal_column(
    "(SELECT jsonb_agg(DISTINCT v ORDER BY v) FROM jsonb_array_elements_text("
    f"{_json_array_sql('data_flows.detected_pii_types')} || "
    f"{_json_array_sql('excluded.detected_pii_types')}) AS v)"
)


def _rank_sql(col):
    return case(CLASSIFICATION_ORDER, value=col, else_=-1)


async def _upsert(db: AsyncSession, rows: list[dict], *, additive: bool) -> None:
    """Upsert derived flows. ``additive`` adds event counts (ingest); otherwise
    they are replaced (rebuild). Seen-times, classification and PII widen."""
    table = DataFlow.__table__
    for i in range(0, len(rows), UPSERT_CHUNK):
        stmt = pg_insert(table).values(rows[i : i + UPSERT_CHUNK])
        ex = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=list(_KEY),
            set_={
                "event_count": (
                    table.c.event_count + ex.event_count if additive else ex.event_count
                ),
                "first_seen": literal_column(
                    "LEAST(data_flows.first_seen, excluded.first_seen)"
                ),
                "last_seen": literal_column(
                    "GREATEST(data_flows.last_seen, excluded.last_seen)"
                ),
                "data_classification": case(
                    (
                        _rank_sql(ex.data_classification)
                        > _rank_sql(table.c.data_classification),
                        ex.data_classification,
                    ),
                    else_=table.c.data_classification,
                ),
                "detected_pii_types": _PII_UNION,
            },
        )
        await db.execute(stmt)


async def record_event_flows(
    db: AsyncSession,
    tenant_id: UUID,
    asset: AIAsset,
    usage_event: AIUsageEvent,
    *,
    data_sources: Iterable[str] | None = None,
    output_destination: str | None = None,
    pii_types: Iterable[str] | None = None,
    region: str | None = None,
    data_classification: str | None = None,
    calling_service: str | None = None,
) -> None:
    """Ingest hook: store the event's flow metadata and upsert its flows.

    ``asset`` must already be flushed (it has an id) and belong to
    ``tenant_id``. The caller commits.
    """
    ctx = build_flow_context(
        data_sources=data_sources,
        output_destination=output_destination,
        pii_types=pii_types,
        region=region,
        data_classification=data_classification,
        calling_service=calling_service,
    )
    ts = usage_event.timestamp or datetime.now(UTC)
    usage_event.timestamp = ts
    usage_event.flow_context = ctx

    agg = _Aggregator(tenant_id)
    agg.add_event(
        asset_id=asset.id,
        vendor=asset.vendor,
        model=asset.model,
        asset_calling_service=asset.calling_service,
        asset_classification=asset.data_classification,
        ctx=ctx,
        ts=ts,
    )
    await _upsert(db, agg.rows(), additive=True)


async def rebuild_flows(db: AsyncSession, tenant_id: UUID) -> int:
    """Re-derive every flow for one tenant from its stored usage events.

    Idempotent. Flows whose events are gone (e.g. purged) are kept with the
    counts they had. Returns the number of flows written. The caller commits.
    """
    stmt = (
        select(
            AIUsageEvent.asset_id,
            AIUsageEvent.timestamp,
            AIUsageEvent.flow_context,
            AIAsset.vendor,
            AIAsset.model,
            AIAsset.calling_service,
            AIAsset.data_classification,
        )
        .join(AIAsset, AIAsset.id == AIUsageEvent.asset_id)
        .where(AIUsageEvent.tenant_id == tenant_id, AIAsset.tenant_id == tenant_id)
    )
    agg = _Aggregator(tenant_id)
    result = await db.stream(stmt)
    async for partition in result.partitions(STREAM_PARTITION):
        for row in partition:
            agg.add_event(
                asset_id=row.asset_id,
                vendor=row.vendor,
                model=row.model,
                asset_calling_service=row.calling_service,
                asset_classification=row.data_classification,
                ctx=row.flow_context if isinstance(row.flow_context, dict) else None,
                ts=row.timestamp,
            )
    rows = agg.rows()
    if rows:
        await _upsert(db, rows, additive=False)
    return len(rows)


async def _run_cli(tenant_ids: list[UUID] | None, database_url: str) -> int:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, class_=AsyncSession)
    try:
        if tenant_ids is None:
            async with session_factory() as db:
                tenant_ids = list((await db.execute(select(Tenant.id))).scalars())
        for tenant_id in tenant_ids:
            async with session_factory() as db:
                n = await rebuild_flows(db, tenant_id)
                await db.commit()
            print(f"{tenant_id}: {n} flows")
    finally:
        await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m collector.data_flows",
        description="Rebuild data_flows from stored telemetry.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--tenant", action="append", type=UUID, help="tenant id (repeatable)")
    group.add_argument("--all-tenants", action="store_true")
    args = parser.parse_args(argv)

    from collector.config import settings

    return asyncio.run(_run_cli(args.tenant, settings.database_url))


if __name__ == "__main__":
    sys.exit(main())
