"""MCP tool usage: observe-only discovery of MCP servers and tools (#63).

Phase 0 of the MCP plan (gateway/docs/mcp-tool-call-discovery.md). Two
writers feed one aggregate table, ``mcp_tool_usage``:

  - ``POST /ingest/mcp``: one event per tool call, sent by the SDK's
    ``mcp.ClientSession.call_tool`` instrumentation (or any client with an
    ingest key);
  - MCP ``tools/call`` spans on ``/ingest/otel/v1/traces`` (otel_ingest).

and readers: ``GET /api/v1/registry/mcp-tools`` (tenant ingest key) and the
Partner API's ``/partner/v1/mcp/tools`` and ``/partner/v1/mcp/usage``
(scope ``mcp:read``, collector/partner_registry.py), all cursor-paginated.

Metering (#77): each call is also added to ``mcp_tool_usage_daily``, one row
per (tenant, UTC day, source, server, tool) with counts, latency and the
request/response bytes the gateway reports.

Assets (#77): every named MCP server is registered in the asset registry
(``ai_assets``, vendor ``mcp``, model = the server name), so it has an
owner, a lifecycle and change webhooks; usage rows link to it by name. The
owner comes from the gateway's server config (``server_owner``) and is only
set while the asset has none, so an owner set in the registry is never
overwritten by telemetry.

Names only. Arguments, results, tool descriptions, server URLs, commands
and headers are never accepted or stored: an event has no field for them,
and unknown fields are ignored. The caller is attributed by a pseudonym
only: ``user_id`` must be a hex hash (the gateway's OIDC subject hash) and
``user_group`` an operator-chosen policy group name, so a raw subject or an
email address is never stored. Every string comes from an MCP server or an
app, so it is treated as attacker-controlled data: control characters are
removed, and a value that is too long is refused rather than cut (half a
name is wrong data). A URL-shaped server identity is reduced to its host.

An invalid event is skipped and counted in the response (``skipped``), so
one bad event never costs the batch, and the SDK does not retry it.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import UUID

from db.models import AIAsset, McpToolUsage, McpToolUsageDaily, uuid7
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import and_, func, literal_column, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from collector import tenant_keys
from collector.audit import record_read
from collector.auth import resolve_tenant
from collector.config import settings
from collector.dedup import AssetKey, find_or_create_asset, lock_asset_keys
from collector.ingest import _enforce_rate_limit
from collector.partner_rate_limit import FixedWindowRateLimiter, get_mcp_rate_limiter
from collector.sanitize import BodyTooDeep, loads_bounded, too_deep_problem
from collector.webhooks import AssetChanges

router = APIRouter()

MAX_BODY_BYTES = 1024 * 1024
MAX_EVENTS = 1000
MAX_NAME_LENGTH = 200
MAX_LABEL_LENGTH = 64
MAX_LATENCY_MS = 24 * 3600 * 1000  # a day; longer is not a latency
MAX_BYTES = 1 << 40  # a terabyte; larger is not one MCP message
# Asset registry vendor for MCP servers (model = server name).
MCP_ASSET_VENDOR = "mcp"
# The owner the gateway's server config names: a plain email address.
OWNER_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,187}\.[A-Za-z]{2,24}")

UNKNOWN_SERVER = "unknown"
# blocked: refused by a guardrail (the gateway's MCP proxy). Stored apart
# from errors: a blocked call is a policy outcome, not a failing tool.
OUTCOMES = frozenset({"ok", "error", "blocked"})
# A pseudonymous user id: a hex hash (the gateway sends 32 characters).
USER_HASH_RE = re.compile(r"[0-9a-f]{16,64}")
# The gateway's policy group names (PS_OIDC_POLICY groups[].name).
USER_GROUP_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
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
    "user_hash",
    "user_group",
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
    characters, else None. Control characters are removed first; NUL and
    lone surrogates are not printable, so what is left can be stored
    (collector/sanitize.py)."""
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
    is_blocked: bool = False
    # Pseudonymous caller (gateway with OIDC); "" when not reported.
    user_hash: str = ""
    user_group: str = ""
    # Metering (gateway): bytes of the request body and of the response.
    request_bytes: int | None = None
    response_bytes: int | None = None
    # The server's owner, from the gateway's server config.
    server_owner: str | None = None


def _bytes(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= MAX_BYTES else None


def _latency(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and 0 <= value <= MAX_LATENCY_MS:
        return value
    return None


def _matching(value: Any, pattern: re.Pattern[str]) -> str:
    return value if isinstance(value, str) and pattern.fullmatch(value) else ""


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
        is_blocked=outcome == "blocked",
        # Anything but a hash (a raw subject, an email) is dropped.
        user_hash=_matching(raw.get("user_id"), USER_HASH_RE),
        user_group=_matching(raw.get("user_group"), USER_GROUP_RE),
        request_bytes=_bytes(raw.get("request_bytes")),
        response_bytes=_bytes(raw.get("response_bytes")),
        server_owner=_matching(raw.get("server_owner"), OWNER_RE).lower() or None,
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
            c.user_hash,
            c.user_group,
        )
        row = rows.get(key)
        if row is None:
            row = rows[key] = {
                **dict(zip(_KEY, key, strict=True)),
                "transport": None,
                "call_count": 0,
                "error_count": 0,
                "blocked_count": 0,
                "latency_ms_total": 0,
                "latency_count": 0,
                "first_seen": now,
                "last_seen": now,
            }
        row["call_count"] += 1
        row["error_count"] += int(c.is_error)
        row["blocked_count"] += int(c.is_blocked)
        if c.latency_ms is not None:
            row["latency_ms_total"] += c.latency_ms
            row["latency_count"] += 1
        if c.transport is not None:
            row["transport"] = c.transport
    return [rows[k] for k in sorted(rows, key=str)]


_DAILY_KEY = ("tenant_id", "day", "source", "server_name", "tool_name")
_DAILY_SUMS = (
    "call_count",
    "error_count",
    "blocked_count",
    "latency_ms_total",
    "latency_count",
    "request_bytes",
    "response_bytes",
    "bytes_count",
)


def aggregate_daily(tenant_id: UUID, calls: Iterable[McpCall], now: datetime) -> list[dict]:
    """Metering rows: one per (day, source, server, tool), sorted."""
    day = now.astimezone(UTC).date()
    rows: dict[tuple, dict] = {}
    for c in calls:
        key = (tenant_id, day, c.source, c.server_name, c.tool_name)
        row = rows.get(key)
        if row is None:
            row = rows[key] = {
                **dict(zip(_DAILY_KEY, key, strict=True)),
                **dict.fromkeys(_DAILY_SUMS, 0),
            }
        row["call_count"] += 1
        row["error_count"] += int(c.is_error)
        row["blocked_count"] += int(c.is_blocked)
        if c.latency_ms is not None:
            row["latency_ms_total"] += c.latency_ms
            row["latency_count"] += 1
        if c.request_bytes is not None or c.response_bytes is not None:
            row["request_bytes"] += c.request_bytes or 0
            row["response_bytes"] += c.response_bytes or 0
            row["bytes_count"] += 1
    return [rows[k] for k in sorted(rows, key=str)]


async def _record_daily(db: AsyncSession, rows: list[dict]) -> None:
    table = McpToolUsageDaily.__table__
    for i in range(0, len(rows), UPSERT_CHUNK):
        chunk = rows[i : i + UPSERT_CHUNK]
        for row in chunk:
            row["id"] = uuid7()
        stmt = pg_insert(table).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=list(_DAILY_KEY),
            set_={name: table.c[name] + stmt.excluded[name] for name in _DAILY_SUMS},
        )
        await db.execute(stmt)


async def register_assets(db: AsyncSession, tenant_id: UUID, calls: list[McpCall]) -> int:
    """Register each named MCP server as an asset (vendor ``mcp``), once
    per source that saw it, and queue the asset webhooks. The owner is set
    only while the asset has none. Returns the number of assets created."""
    owners: dict[str, str] = {}
    services: dict[str, str] = {}
    keys: set[tuple[str, str]] = set()
    for c in calls:
        if c.server_name == UNKNOWN_SERVER:
            continue
        keys.add((c.server_name, c.source))
        if c.server_owner:
            owners.setdefault(c.server_name, c.server_owner)
        if c.calling_service:
            services.setdefault(c.server_name, c.calling_service)
    if not keys:
        return 0
    ordered = sorted(keys)
    await lock_asset_keys(
        db, tenant_id, (AssetKey(MCP_ASSET_VENDOR, server) for server, _ in ordered)
    )
    changes = AssetChanges()
    created = 0
    for server, source in ordered:
        asset, is_new = await find_or_create_asset(
            db=db,
            tenant_id=tenant_id,
            vendor=MCP_ASSET_VENDOR,
            model=server,
            use_case_name=None,
            business_unit=None,
            calling_service=services.get(server),
            source=source,
        )
        owner = owners.get(server)
        if owner and not asset.owner_email:
            asset.owner_email = owner
        changes.record(asset, is_new)
        if is_new:
            created += 1
            await db.flush()
    await changes.enqueue(db, tenant_id)
    return created


async def record_calls(
    db: AsyncSession,
    tenant_id: UUID,
    calls: Iterable[McpCall],
    now: datetime | None = None,
    *,
    register: bool = True,
) -> int:
    """Add calls to the tenant's aggregates and daily metering and, unless
    ``register`` is False, register their servers as assets (no commit).
    A caller that also writes LLM assets in the transaction passes False
    and calls ``register_assets`` in a transaction of its own, so asset
    locks are never taken in two batches within one transaction. Returns
    the number of aggregate rows upserted."""
    calls = list(calls)
    now = now or datetime.now(UTC)
    rows = aggregate(tenant_id, calls, now)
    await _record_daily(db, aggregate_daily(tenant_id, calls, now))
    if register:
        await register_assets(db, tenant_id, calls)
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
                "blocked_count": table.c.blocked_count + ex.blocked_count,
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
        return loads_bounded(b"".join(chunks))
    except BodyTooDeep:
        raise too_deep_problem() from None
    except ValueError:  # UnicodeDecodeError included
        raise _problem(400, "Bad Request", "Body is not valid JSON") from None


@router.post("/ingest/mcp")
async def ingest_mcp(
    request: Request,
    response: Response,
    limiter: Annotated[FixedWindowRateLimiter, Depends(get_mcp_rate_limiter)],
):
    """Ingest observed MCP tool calls: ``{"events": [{server_name,
    tool_name, outcome, latency_ms, calling_service, environment,
    transport, source, user_id, user_group}]}``. ``outcome`` is ok, error
    or blocked; ``user_id`` is kept only when it is a hex hash. Same ingest-key auth as ``/ingest/events``,
    with its own per-tenant rate limit (``PS_MCP_RATE_LIMIT_PER_MINUTE``);
    at most ``MAX_EVENTS`` events and ``MAX_BODY_BYTES`` bytes per request."""
    tenant_id = await resolve_tenant(request, tenant_keys.INGEST_WRITE)
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


# ──────────────────────────────────────────────────────────────────────
# Reads (registry and Partner API)
# ──────────────────────────────────────────────────────────────────────

MAX_PAGE = 1000
DEFAULT_USAGE_DAYS = 30


def encode_cursor(*parts: str) -> str:
    """An opaque keyset cursor."""
    return base64.urlsafe_b64encode("|".join(parts).encode()).decode().rstrip("=")


def decode_cursor(raw: str, n: int) -> list[str]:
    """The ``n`` parts of a cursor from ``encode_cursor``, else a 400."""
    try:
        text = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        text = ""
    parts = text.split("|")
    if len(parts) != n or not all(parts):
        raise _problem(400, "Bad Request", "Invalid cursor")
    return parts


def _cursor_values(raw: str, first: type) -> tuple[Any, UUID]:
    a, b = decode_cursor(raw, 2)
    try:
        value = (
            datetime.fromisoformat(a) if first is datetime else date.fromisoformat(a)
        )
        return value, UUID(b)
    except ValueError:
        raise _problem(400, "Bad Request", "Invalid cursor") from None


async def assets_by_server(
    db: AsyncSession, tenant_id: UUID, servers: Iterable[str]
) -> dict[str, tuple[str, str | None]]:
    """Server name -> (asset id, owner email) of its registry asset. When
    a server has several (one per environment, merged later), the oldest
    wins."""
    names = sorted(set(servers))
    if not names:
        return {}
    rows = (
        await db.execute(
            select(AIAsset.model, AIAsset.id, AIAsset.owner_email)
            .where(
                AIAsset.tenant_id == tenant_id,
                AIAsset.vendor == MCP_ASSET_VENDOR,
                AIAsset.model.in_(names),
            )
            .order_by(AIAsset.first_seen, AIAsset.id)
        )
    ).all()
    out: dict[str, tuple[str, str | None]] = {}
    for model, asset_id, owner in rows:
        out.setdefault(model, (str(asset_id), owner))
    return out


def _with_asset(data: dict, assets: dict[str, tuple[str, str | None]]) -> dict:
    asset_id, owner = assets.get(data["server_name"], (None, None))
    data["asset_id"] = asset_id
    data["owner_email"] = owner
    return data


async def tools_page(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    server_name: str | None,
    source: str | None,
    limit: int,
    cursor: str | None,
) -> dict:
    """One page of observed tools, most recently seen first, with each
    server's asset id and owner. Keyset on (last_seen desc, id): a row seen
    again while a client pages moves to the front and may be skipped or
    repeated, which a registry sync tolerates."""
    conditions = [McpToolUsage.tenant_id == tenant_id]
    if server_name:
        conditions.append(McpToolUsage.server_name == server_name)
    if source:
        conditions.append(McpToolUsage.source == source)
    if cursor:
        last_seen, last_id = _cursor_values(cursor, datetime)
        conditions.append(
            or_(
                McpToolUsage.last_seen < last_seen,
                and_(McpToolUsage.last_seen == last_seen, McpToolUsage.id > last_id),
            )
        )
    rows = (
        (
            await db.execute(
                select(McpToolUsage)
                .where(*conditions)
                .order_by(McpToolUsage.last_seen.desc(), McpToolUsage.id)
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    assets = await assets_by_server(db, tenant_id, (r.server_name for r in rows))
    data = [_with_asset(_usage_dict(r), assets) for r in rows]
    next_cursor = (
        encode_cursor(rows[-1].last_seen.isoformat(), str(rows[-1].id))
        if has_more
        else None
    )
    return {"data": data, "meta": {"has_more": has_more, "next_cursor": next_cursor}}


def _daily_dict(row: McpToolUsageDaily) -> dict:
    return {
        "day": row.day.isoformat(),
        "source": row.source,
        "server_name": row.server_name,
        "tool_name": row.tool_name,
        "call_count": row.call_count,
        "error_count": row.error_count,
        "blocked_count": row.blocked_count,
        "latency_ms_total": row.latency_ms_total,
        "latency_count": row.latency_count,
        "avg_latency_ms": (
            round(row.latency_ms_total / row.latency_count) if row.latency_count else None
        ),
        "request_bytes": row.request_bytes,
        "response_bytes": row.response_bytes,
        "bytes_count": row.bytes_count,
    }


async def usage_page(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    since: date | None,
    until: date | None,
    server_name: str | None,
    tool_name: str | None,
    source: str | None,
    limit: int,
    cursor: str | None,
) -> dict:
    """One page of daily metering rows, newest day first. ``since``
    defaults to ``DEFAULT_USAGE_DAYS`` days ago (UTC); both bounds are
    inclusive."""
    since = since or datetime.now(UTC).date() - timedelta(days=DEFAULT_USAGE_DAYS)
    if until is not None and until < since:
        raise _problem(400, "Bad Request", "until is before since")
    t = McpToolUsageDaily
    conditions = [t.tenant_id == tenant_id, t.day >= since]
    if until is not None:
        conditions.append(t.day <= until)
    if server_name:
        conditions.append(t.server_name == server_name)
    if tool_name:
        conditions.append(t.tool_name == tool_name)
    if source:
        conditions.append(t.source == source)
    if cursor:
        last_day, last_id = _cursor_values(cursor, date)
        conditions.append(or_(t.day < last_day, and_(t.day == last_day, t.id > last_id)))
    rows = (
        (
            await db.execute(
                select(t).where(*conditions).order_by(t.day.desc(), t.id).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    assets = await assets_by_server(db, tenant_id, (r.server_name for r in rows))
    data = [_with_asset(_daily_dict(r), assets) for r in rows]
    next_cursor = (
        encode_cursor(rows[-1].day.isoformat(), str(rows[-1].id)) if has_more else None
    )
    return {
        "data": data,
        "meta": {
            "since": since.isoformat(),
            "until": until.isoformat() if until else None,
            "has_more": has_more,
            "next_cursor": next_cursor,
        },
    }


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
        "blocked_count": row.blocked_count,
        "user_hash": row.user_hash or None,
        "user_group": row.user_group or None,
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
    limit: int = Query(default=200, ge=1, le=MAX_PAGE),
    cursor: str | None = None,
):
    """The tenant's observed MCP servers and tools, most recently seen
    first, with each server's registry asset id and owner. Pages of at most
    ``MAX_PAGE`` rows: pass ``meta.next_cursor`` as ``cursor`` for the
    next. Names are data reported by MCP servers and apps: escape them when
    rendering and never put them in an LLM prompt."""
    tenant_id = await resolve_tenant(request, tenant_keys.REGISTRY_READ)
    db = request.state.db
    body = await tools_page(
        db, tenant_id, server_name=server_name, source=source, limit=limit, cursor=cursor
    )
    await record_read(
        request, tenant_id=tenant_id, status_code=200, response_count=len(body["data"])
    )
    return body
