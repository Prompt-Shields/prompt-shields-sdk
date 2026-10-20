"""MCP tool usage: observe-only discovery of MCP servers and tools (#63).

Phase 0 of the MCP plan (gateway/docs/mcp-tool-call-discovery.md). Two
writers feed one aggregate table, ``mcp_tool_usage``:

  - ``POST /ingest/mcp``: one event per tool call, sent by the SDK's
    ``mcp.ClientSession.call_tool`` instrumentation (or any client with an
    ingest key);
  - MCP ``tools/call`` spans on ``/ingest/otel/v1/traces`` (otel_ingest).

and one reader, ``GET /api/v1/registry/mcp-tools`` (tenant ingest key).

Names only. Arguments, results, tool descriptions, server URLs, commands
and headers are never accepted or stored: an event has no field for them,
and unknown fields are ignored. Every string comes from an MCP server or an
app, so it is treated as attacker-controlled data: control characters are
removed, and a value that is too long is refused rather than cut (half a
name is wrong data). A URL-shaped server identity is reduced to its host.

An invalid event is skipped and counted in the response (``skipped``), so
one bad event never costs the batch, and the SDK does not retry it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import UUID

from db.models import McpToolUsage, uuid7
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from collector.audit import record_read
from collector.auth import resolve_tenant
from collector.config import settings
from collector.ingest import _enforce_rate_limit
from collector.partner_rate_limit import FixedWindowRateLimiter, get_mcp_rate_limiter

router = APIRouter()

MAX_BODY_BYTES = 1024 * 1024
MAX_EVENTS = 1000
MAX_NAME_LENGTH = 200
MAX_LABEL_LENGTH = 64
MAX_LATENCY_MS = 24 * 3600 * 1000  # a day; longer is not a latency

UNKNOWN_SERVER = "unknown"
OUTCOMES = frozenset({"ok", "error"})
# Sources a client may claim on /ingest/mcp. "otel" is set by the OTLP
# receiver only.
INGEST_SOURCES = frozenset({"sdk", "gateway", "scan"})
OTEL_SOURCE = "otel"

_KEY = (
    "tenant_id",
    "source",
    "server_name",
    "tool_name",
    "calling_service",
    "environment",
)
UPSERT_CHUNK = 500


def _problem(status: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


# ──────────────────────────────────────────────────────────────────────
# Validation
# ──────────────────────────────────────────────────────────────────────


def clean_name(value: Any, max_length: int = MAX_NAME_LENGTH) -> str | None:
    """A printable, stripped, non-empty string of at most ``max_length``
    characters, else None. Control characters are removed first."""
    if not isinstance(value, str):
        return None
    value = "".join(ch for ch in value if ch.isprintable()).strip()
    if not value or len(value) > max_length:
        return None
    return value


def server_identity(value: Any) -> str | None:
    """A server identity safe to store. A URL is reduced to host[:port]:
    never credentials, path or query."""
    if isinstance(value, str) and "://" in value:
        try:
            parts = urlsplit(value.strip())
            host, port = parts.hostname, parts.port
        except ValueError:
            return None
        if not host:
            return None
        value = f"{host}:{port}" if port else host
    return clean_name(value)


@dataclass(frozen=True)
class McpCall:
    """One observed tool call, validated."""

    source: str
    server_name: str
    tool_name: str
    is_error: bool
    latency_ms: int | None = None
    calling_service: str = ""
    environment: str = ""
    transport: str | None = None


def _latency(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and 0 <= value <= MAX_LATENCY_MS:
        return value
    return None


def parse_event(raw: Any) -> McpCall | None:
    """An /ingest/mcp event, or None when it cannot be stored. Optional
    labels that are invalid are dropped; a bad required field (tool name,
    outcome, source) skips the event."""
    if not isinstance(raw, dict):
        return None
    tool = clean_name(raw.get("tool_name"))
    if tool is None:
        return None
    server = raw.get("server_name")
    server = UNKNOWN_SERVER if server is None else server_identity(server)
    if server is None:
        return None
    outcome = raw.get("outcome", "ok")
    if outcome not in OUTCOMES:
        return None
    source = raw.get("source", "sdk")
    if source not in INGEST_SOURCES:
        return None
    return McpCall(
        source=source,
        server_name=server,
        tool_name=tool,
        is_error=outcome == "error",
        latency_ms=_latency(raw.get("latency_ms")),
        calling_service=clean_name(raw.get("calling_service"), MAX_NAME_LENGTH) or "",
        environment=clean_name(raw.get("environment"), MAX_LABEL_LENGTH) or "",
        transport=clean_name(raw.get("transport"), MAX_LABEL_LENGTH),
    )


# ──────────────────────────────────────────────────────────────────────
# Storage
# ──────────────────────────────────────────────────────────────────────


def aggregate(tenant_id: UUID, calls: Iterable[McpCall], now: datetime) -> list[dict]:
    """One upsert row per key, sorted so concurrent upserts lock rows in the
    same order."""
    rows: dict[tuple, dict] = {}
    for c in calls:
        key = (
            tenant_id,
            c.source,
            c.server_name,
            c.tool_name,
            c.calling_service,
            c.environment,
        )
        row = rows.get(key)
        if row is None:
            row = rows[key] = {
                **dict(zip(_KEY, key, strict=True)),
                "transport": None,
                "call_count": 0,
                "error_count": 0,
                "latency_ms_total": 0,
                "latency_count": 0,
                "first_seen": now,
                "last_seen": now,
            }
        row["call_count"] += 1
        row["error_count"] += int(c.is_error)
        if c.latency_ms is not None:
            row["latency_ms_total"] += c.latency_ms
            row["latency_count"] += 1
        if c.transport is not None:
            row["transport"] = c.transport
    return [rows[k] for k in sorted(rows, key=str)]


async def record_calls(
    db: AsyncSession,
    tenant_id: UUID,
    calls: Iterable[McpCall],
    now: datetime | None = None,
) -> int:
    """Add calls to the tenant's aggregates (no commit). Returns the number
    of rows upserted."""
    rows = aggregate(tenant_id, calls, now or datetime.now(UTC))
    table = McpToolUsage.__table__
    for i in range(0, len(rows), UPSERT_CHUNK):
        chunk = rows[i : i + UPSERT_CHUNK]
        for row in chunk:
            row["id"] = uuid7()
        stmt = pg_insert(table).values(chunk)
        ex = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=list(_KEY),
            set_={
                "call_count": table.c.call_count + ex.call_count,
                "error_count": table.c.error_count + ex.error_count,
                "latency_ms_total": table.c.latency_ms_total + ex.latency_ms_total,
                "latency_count": table.c.latency_count + ex.latency_count,
                "transport": func.coalesce(ex.transport, table.c.transport),
                "first_seen": literal_column(
                    "LEAST(mcp_tool_usage.first_seen, excluded.first_seen)"
                ),
                "last_seen": literal_column(
                    "GREATEST(mcp_tool_usage.last_seen, excluded.last_seen)"
                ),
            },
        )
        await db.execute(stmt)
    return len(rows)


# ──────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────


async def _read_body(request: Request) -> Any:
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
    try:
        return json.loads(b"".join(chunks))
    except (ValueError, UnicodeDecodeError):
        raise _problem(400, "Bad Request", "Body is not valid JSON") from None


@router.post("/ingest/mcp")
async def ingest_mcp(
    request: Request,
    response: Response,
    limiter: Annotated[FixedWindowRateLimiter, Depends(get_mcp_rate_limiter)],
):
    """Ingest observed MCP tool calls: ``{"events": [{server_name,
    tool_name, outcome, latency_ms, calling_service, environment,
    transport, source}]}``. Same ingest-key auth as ``/ingest/events``,
    with its own per-tenant rate limit (``PS_MCP_RATE_LIMIT_PER_MINUTE``);
    at most ``MAX_EVENTS`` events and ``MAX_BODY_BYTES`` bytes per request."""
    tenant_id = await resolve_tenant(request)
    _enforce_rate_limit(limiter, tenant_id, response, settings.mcp_rate_limit_per_minute)
    body = await _read_body(request)
    events = body.get("events") if isinstance(body, dict) else None
    if not isinstance(events, list):
        raise _problem(422, "Unprocessable Entity", "Expected {\"events\": [...]}")
    if len(events) > MAX_EVENTS:
        raise _problem(
            413, "Payload Too Large", f"More than {MAX_EVENTS} events in one request"
        )

    calls = [c for c in (parse_event(e) for e in events) if c is not None]
    db = request.state.db
    if calls:
        try:
            await record_calls(db, tenant_id, calls)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return {"ingested": len(calls), "skipped": len(events) - len(calls)}


def _usage_dict(row: McpToolUsage) -> dict:
    return {
        "server_name": row.server_name,
        "tool_name": row.tool_name,
        "source": row.source,
        "calling_service": row.calling_service or None,
        "environment": row.environment or None,
        "transport": row.transport,
        "call_count": row.call_count,
        "error_count": row.error_count,
        "avg_latency_ms": (
            round(row.latency_ms_total / row.latency_count)
            if row.latency_count
            else None
        ),
        "first_seen": row.first_seen.isoformat() if row.first_seen else None,
        "last_seen": row.last_seen.isoformat() if row.last_seen else None,
    }


@router.get("/api/v1/registry/mcp-tools")
async def list_mcp_tools(
    request: Request,
    server_name: str | None = None,
    source: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
):
    """The tenant's observed MCP servers and tools, most recently seen
    first. Names are data reported by MCP servers and apps: escape them
    when rendering and never put them in an LLM prompt."""
    tenant_id = await resolve_tenant(request)
    db = request.state.db
    conditions = [McpToolUsage.tenant_id == tenant_id]
    if server_name:
        conditions.append(McpToolUsage.server_name == server_name)
    if source:
        conditions.append(McpToolUsage.source == source)
    rows = (
        (
            await db.execute(
                select(McpToolUsage)
                .where(*conditions)
                .order_by(McpToolUsage.last_seen.desc(), McpToolUsage.id)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    data = [_usage_dict(r) for r in rows]
    await record_read(request, tenant_id=tenant_id, status_code=200, response_count=len(data))
    return {"data": data}
