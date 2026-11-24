"""Agent traces: the span store behind ``/ingest/otel/v1/traces`` and the
trace query API (#385).

Storing
    The OTLP trace receiver (collector.otel_ingest) hands every accepted
    request to :func:`store_trace_spans`, which keeps, per tenant, one
    ``trace_spans`` row per span of a GenAI trace:

    * every span that is a GenAI span (``gen_ai.operation.name``,
      ``gen_ai.provider.name`` or ``gen_ai.system``) or an MCP span
      (``mcp.method.name``): model calls, ``execute_tool`` and
      ``invoke_agent`` spans, MCP calls;
    * every other span of the same request whose trace has such a span
      (an HTTP call made inside a tool, the app's own root span);
    * every other span whose trace is already stored for the tenant (spans
      exported in a later batch than the trace's first GenAI span);
    * every other span whose trace gains a GenAI span within
      ``PS_TRACE_PENDING_SECONDS`` (default 300) of its arrival. Exporters
      batch independently and a child span ends (and is exported) before
      its parent, so an HTTP span made inside a tool, or the SDK's spans and
      the gateway's, can arrive before the trace's first GenAI span. Such a
      span is stored as *pending* (``pending_until`` set, never returned by
      the API) and becomes part of the trace when a GenAI span of the trace
      arrives; pending spans of traces that never gain one are deleted once
      the window has passed (at the tenant's next ingest, and by the
      retention purge). At most ``MAX_PENDING_PER_REQUEST`` pending spans
      are kept per request; 0 seconds turns pending spans off.

    Spans of traces with no GenAI span are not kept: pointing a service's
    whole OTel export here does not fill the database with its HTTP spans
    (they are held for the pending window only).

    Stored per span: trace id, span id, parent span id (lowercase hex; the
    protobuf encoding's base64 ids are converted), name, kind, status,
    ``error.type``, start and end time, duration, ``service.name``,
    ``deployment.environment(.name)`` and an allowlist of GenAI attributes:
    ``gen_ai.operation.name``, ``gen_ai.provider.name`` (or
    ``gen_ai.system``), ``gen_ai.request.model``, ``gen_ai.response.model``,
    ``gen_ai.tool.name``, input and output token counts,
    ``gen_ai.conversation.id`` (as ``session_id``) and the user id
    (``ps.user.hash``, ``user.id`` or ``enduser.id``) hashed with the
    tenant's salt as ``user_id_hash`` (collector.user_identity; the id as
    sent is never stored). Content attributes are dropped before anything is
    read (collector.otel_mapping), and span events, links and status
    messages are never read, so no prompt, completion, tool argument or
    result text can reach the table. One row per (tenant, trace id, span
    id): a resent span is stored once.

    Operators turn the store off with ``PS_TRACE_STORE_ENABLED=false``.

Retention and erasure
    Spans are purged with the tenant's usage events (collector.retention:
    start or receipt before the cutoff), removed by tenant erasure, and user
    erasure (collector.user_erasure) clears ``user_id_hash`` and
    ``session_id`` on the user's spans.

Query (Partner API, scope ``traces:read``)::

    GET /partner/v1/traces?since=&until=&session_id=&user_id_hash=&status=&cursor=&limit=
    GET /partner/v1/traces/{trace_id}

The list returns one summary per trace that has a span starting in
``[since, until)`` (default: the last 24 hours) matching the filters, newest
first, paged by an opaque cursor. ``status=error`` keeps traces with at least
one errored span, ``status=ok`` the others. The detail returns every stored
span of the trace in start order (404 when the tenant has none). Every read
is written to the partner audit log.
"""

from __future__ import annotations

import base64
import binascii
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from db.models import TraceSpan, uuid7
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from collector import user_identity
from collector.otel_mapping import (
    _count,
    _duration_ms,
    _int,
    _str,
    is_genai_span,
    is_mcp_span,
    iter_spans,
    normalize_id,
    safe_attributes,
)
from collector.partner_auth import PartnerPrincipal, require_scope
from collector.partner_registry import _audit, _enforce_rate_limit

logger = logging.getLogger(__name__)

SCOPE = "traces:read"
# Longer names are truncated; longer label values are stored as null.
MAX_NAME_LENGTH = 256
MAX_LABEL_LENGTH = 256
# A start time further ahead of receipt than this is not a real span.
MAX_CLOCK_SKEW = timedelta(hours=1)
INSERT_CHUNK = 500
# Pending spans (traces with no GenAI span yet) kept from one request, and
# expired pending spans deleted per ingest request.
MAX_PENDING_PER_REQUEST = 500
PENDING_GC_BATCH = 5000

USER_ID_ATTRIBUTES = ("ps.user.hash", "user.id", "enduser.id")

_KINDS = {
    1: "internal",
    2: "server",
    3: "client",
    4: "producer",
    5: "consumer",
    "SPAN_KIND_INTERNAL": "internal",
    "SPAN_KIND_SERVER": "server",
    "SPAN_KIND_CLIENT": "client",
    "SPAN_KIND_PRODUCER": "producer",
    "SPAN_KIND_CONSUMER": "consumer",
}
_STATUSES = {
    1: "ok",
    2: "error",
    "STATUS_CODE_OK": "ok",
    "STATUS_CODE_ERROR": "error",
}


# ──────────────────────────────────────────────────────────────────────
# Mapping
# ──────────────────────────────────────────────────────────────────────


def _label(value: Any) -> str | None:
    text = _str(value)
    if text is None or len(text) > MAX_LABEL_LENGTH:
        return None
    return text


def _kind(value: Any) -> str | None:
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    return _KINDS.get(value)


def _status(span: dict) -> str:
    status = span.get("status") if isinstance(span.get("status"), dict) else {}
    code = status.get("code")
    if isinstance(code, str) and code.isdigit():
        code = int(code)
    return _STATUSES.get(code, "unset")


def _time(nanos: Any) -> datetime | None:
    n = _int(nanos)
    if n is None or n <= 0:
        return None
    try:
        return datetime.fromtimestamp(n / 1e9, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def map_trace_span(
    span: dict, resource_attrs: dict[str, Any], now: datetime
) -> tuple[dict, bool] | None:
    """``(row, is_genai)`` for one OTLP span, or None when it has no valid
    trace id, span id or start time. ``row`` holds ``user_id`` (the id as
    sent) until :func:`store_trace_spans` replaces it with the salted hash."""
    trace_id = normalize_id(span.get("traceId"), 16)
    span_id = normalize_id(span.get("spanId"), 8)
    start = _time(span.get("startTimeUnixNano"))
    if trace_id is None or span_id is None or start is None:
        return None
    if start > now + MAX_CLOCK_SKEW:
        return None
    end = _time(span.get("endTimeUnixNano"))
    if end is not None and end < start:
        end = None
    attrs = safe_attributes(span.get("attributes"))
    name = _str(span.get("name")) or "unknown"
    tokens_in = _count(attrs.get("gen_ai.usage.input_tokens"))
    if tokens_in is None:
        tokens_in = _count(attrs.get("gen_ai.usage.prompt_tokens"))
    tokens_out = _count(attrs.get("gen_ai.usage.output_tokens"))
    if tokens_out is None:
        tokens_out = _count(attrs.get("gen_ai.usage.completion_tokens"))
    # A gateway attempt span (#390) names its provider and model in
    # ps.attempt.*: it has no gen_ai.* attribute, so it is never billed.
    provider = (
        _label(attrs.get("gen_ai.provider.name"))
        or _label(attrs.get("gen_ai.system"))
        or _label(attrs.get("ps.attempt.provider"))
    )
    user_id = next(
        (v for v in (_str(attrs.get(k)) for k in USER_ID_ATTRIBUTES) if v is not None),
        None,
    )
    environment = _label(resource_attrs.get("deployment.environment.name")) or _label(
        resource_attrs.get("deployment.environment")
    )
    row = {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": normalize_id(span.get("parentSpanId"), 8),
        "name": name[:MAX_NAME_LENGTH],
        "kind": _kind(span.get("kind")),
        "status": _status(span),
        "error_type": _label(attrs.get("error.type")),
        "start_time": start,
        "end_time": end,
        "duration_ms": _duration_ms(span) if end is not None else None,
        "service_name": _label(resource_attrs.get("service.name")),
        "environment": environment,
        "operation": _label(attrs.get("gen_ai.operation.name")),
        "provider": provider.lower() if provider else None,
        "request_model": _label(attrs.get("gen_ai.request.model"))
        or _label(attrs.get("ps.attempt.model")),
        "response_model": _label(attrs.get("gen_ai.response.model")),
        "tool_name": _label(attrs.get("gen_ai.tool.name")),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "session_id": _label(attrs.get("gen_ai.conversation.id")),
        "user_id": user_id if user_id is None or len(user_id) <= 512 else None,
    }
    return row, is_genai_span(attrs) or is_mcp_span(attrs)


def collect_spans(payload: dict, now: datetime) -> tuple[list[dict], set[str]]:
    """Every mappable span of a request, and the trace ids that have a GenAI
    or MCP span in it. A span id repeated within a trace keeps its first
    occurrence."""
    rows: dict[tuple[str, str], dict] = {}
    genai_traces: set[str] = set()
    for span, resource_attrs in iter_spans(payload):
        mapped = map_trace_span(span, resource_attrs, now)
        if mapped is None:
            continue
        row, is_genai = mapped
        if is_genai:
            genai_traces.add(row["trace_id"])
        rows.setdefault((row["trace_id"], row["span_id"]), row)
    return list(rows.values()), genai_traces


def _pending_window() -> timedelta:
    from collector.config import settings

    return timedelta(seconds=max(0, settings.trace_pending_seconds))


async def store_trace_spans(
    db: AsyncSession, tenant_id: UUID, payload: dict, *, now: datetime | None = None
) -> int:
    """Keep the spans of the request's GenAI traces, hold the others as
    pending (module docstring). Commits; returns the number of new rows,
    pending ones included."""
    now = now or datetime.now(UTC)
    t = TraceSpan
    window = _pending_window()
    if window:
        await _delete_expired_pending(db, tenant_id, now)
    candidates, genai = collect_spans(payload, now)
    if not candidates:
        if window:
            await db.commit()
        return 0
    keep = set(genai)
    others = {r["trace_id"] for r in candidates} - keep
    if others:
        keep |= set(
            (
                await db.execute(
                    select(t.trace_id)
                    .where(
                        t.tenant_id == tenant_id,
                        t.trace_id.in_(others),
                        t.pending_until.is_(None),
                    )
                    .distinct()
                )
            ).scalars()
        )
    rows = []
    pending = 0
    for r in candidates:
        if r["trace_id"] in keep:
            r["pending_until"] = None
        elif window and pending < MAX_PENDING_PER_REQUEST:
            r["pending_until"] = now + window
            pending += 1
        else:
            continue
        rows.append(r)
    if genai and window:
        # Spans held for these traces become part of them.
        await db.execute(
            update(t)
            .where(
                t.tenant_id == tenant_id,
                t.trace_id.in_(genai),
                t.pending_until.is_not(None),
            )
            .values(pending_until=None)
            .execution_options(synchronize_session=False)
        )
    if not rows:
        await db.commit()
        return 0
    salt = None
    if any(r["user_id"] for r in rows):
        salt = await user_identity.tenant_salt(db, tenant_id)
    for r in rows:
        user_id = r.pop("user_id")
        r["user_id_hash"] = (
            user_identity.hash_user_id(salt, user_id) if salt and user_id else None
        )
    table = TraceSpan.__table__
    inserted = 0
    for i in range(0, len(rows), INSERT_CHUNK):
        chunk = rows[i : i + INSERT_CHUNK]
        stmt = (
            pg_insert(table)
            .values(
                [
                    {"id": uuid7(), "tenant_id": tenant_id, "received_at": now, **r}
                    for r in chunk
                ]
            )
            .on_conflict_do_nothing(constraint="ux_trace_spans_span")
            .returning(table.c.id)
        )
        inserted += len((await db.execute(stmt)).all())
    await db.commit()
    return inserted


async def _delete_expired_pending(db: AsyncSession, tenant_id: UUID, now: datetime) -> None:
    """Delete (a bounded batch of) the tenant's pending spans whose window
    has passed: their trace never gained a GenAI span."""
    t = TraceSpan
    expired = (
        select(t.id)
        .where(
            t.tenant_id == tenant_id,
            t.pending_until.is_not(None),
            t.pending_until < now,
        )
        .limit(PENDING_GC_BATCH)
    )
    await db.execute(
        delete(t)
        .where(t.id.in_(expired))
        .execution_options(synchronize_session=False)
    )


# ──────────────────────────────────────────────────────────────────────
# Reading
# ──────────────────────────────────────────────────────────────────────


def _ts(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


SPAN_FIELDS = (
    "span_id",
    "parent_span_id",
    "name",
    "kind",
    "status",
    "error_type",
    "duration_ms",
    "service_name",
    "environment",
    "operation",
    "provider",
    "request_model",
    "response_model",
    "tool_name",
    "tokens_in",
    "tokens_out",
    "session_id",
    "user_id_hash",
)


def span_payload(row: TraceSpan) -> dict:
    out: dict[str, Any] = {f: getattr(row, f) for f in SPAN_FIELDS}
    out["start_time"] = _ts(row.start_time)
    out["end_time"] = _ts(row.end_time)
    return out


async def load_trace(db: AsyncSession, tenant_id: UUID, trace_id: str) -> list[TraceSpan]:
    t = TraceSpan
    return list(
        (
            await db.execute(
                select(t)
                .where(
                    t.tenant_id == tenant_id,
                    t.trace_id == trace_id,
                    t.pending_until.is_(None),
                )
                .order_by(t.start_time, t.span_id)
            )
        ).scalars()
    )


def trace_payload(trace_id: str, rows: list[TraceSpan]) -> dict:
    summary = summarize(trace_id, rows)
    return {**summary, "spans": [span_payload(r) for r in rows]}


def _root(rows: list[TraceSpan]) -> TraceSpan | None:
    """The trace's root: the earliest span without a parent, else (its
    parent was never exported or not kept) the earliest span whose parent
    is not stored."""
    ids = {r.span_id for r in rows}
    for r in rows:
        if r.parent_span_id is None:
            return r
    for r in rows:
        if r.parent_span_id not in ids:
            return r
    return rows[0] if rows else None


def summarize(trace_id: str, rows: list[TraceSpan]) -> dict:
    """The list endpoint's summary of one trace (rows in start order)."""
    root = _root(rows)
    ends = [r.end_time for r in rows if r.end_time is not None]
    start = rows[0].start_time if rows else None
    end = max(ends) if ends else None
    errors = sum(1 for r in rows if r.status == "error")

    def total(field: str) -> int | None:
        values = [getattr(r, field) for r in rows if getattr(r, field) is not None]
        return sum(values) if values else None

    def first(field: str) -> str | None:
        return next((getattr(r, field) for r in rows if getattr(r, field)), None)

    duration = None
    if start is not None and end is not None:
        duration = int((end - start).total_seconds() * 1000)
    return {
        "trace_id": trace_id,
        "root_name": root.name if root is not None else None,
        "root_service_name": root.service_name if root is not None else None,
        "start_time": _ts(start),
        "end_time": _ts(end),
        "duration_ms": duration,
        "span_count": len(rows),
        "error_count": errors,
        "status": "error" if errors else "ok",
        "tokens_in": total("tokens_in"),
        "tokens_out": total("tokens_out"),
        "session_id": first("session_id"),
        "user_id_hash": first("user_id_hash"),
    }


def encode_cursor(start: datetime, trace_id: str) -> str:
    raw = f"{_ts(start)}|{trace_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Raises ValueError on anything but a cursor this module made."""
    try:
        if len(cursor) > 200:
            raise ValueError
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        ts, _, trace_id = raw.partition("|")
        parsed = datetime.fromisoformat(ts)
        if parsed.tzinfo is None or normalize_id(trace_id, 16) != trace_id:
            raise ValueError
        return parsed, trace_id
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("invalid cursor") from exc


Status = Literal["ok", "error"]


async def list_trace_ids(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    since: datetime,
    until: datetime,
    limit: int,
    after: tuple[datetime, str] | None = None,
    session_id: str | None = None,
    user_id_hash: str | None = None,
    status: Status | None = None,
) -> list[tuple[str, datetime]]:
    """``(trace_id, first span start)`` of the traces with a span starting
    in ``[since, until)`` that matches the filters, newest first, after the
    cursor. ``status`` looks at every stored span of the trace."""
    t = TraceSpan
    conds = [
        t.tenant_id == tenant_id,
        t.start_time >= since,
        t.start_time < until,
        t.pending_until.is_(None),
    ]
    if session_id is not None:
        conds.append(t.session_id == session_id)
    if user_id_hash is not None:
        conds.append(t.user_id_hash == user_id_hash)
    candidates = select(t.trace_id).where(*conds).distinct()

    start = func.min(t.start_time)
    errors = func.sum(case((t.status == "error", 1), else_=0))
    stmt = (
        select(t.trace_id, start.label("start"))
        .where(
            t.tenant_id == tenant_id,
            t.trace_id.in_(candidates),
            t.pending_until.is_(None),
        )
        .group_by(t.trace_id)
    )
    having = []
    if after is not None:
        ts, last_id = after
        having.append(or_(start < ts, and_(start == ts, t.trace_id < last_id)))
    if status == "error":
        having.append(errors > 0)
    elif status == "ok":
        having.append(errors == 0)
    if having:
        stmt = stmt.having(*having)
    stmt = stmt.order_by(start.desc(), t.trace_id.desc()).limit(limit)
    return [(row[0], row[1]) for row in (await db.execute(stmt)).all()]


async def load_traces(
    db: AsyncSession, tenant_id: UUID, trace_ids: list[str]
) -> dict[str, list[TraceSpan]]:
    if not trace_ids:
        return {}
    t = TraceSpan
    out: dict[str, list[TraceSpan]] = {tid: [] for tid in trace_ids}
    rows = (
        await db.execute(
            select(t)
            .where(
                t.tenant_id == tenant_id,
                t.trace_id.in_(trace_ids),
                t.pending_until.is_(None),
            )
            .order_by(t.start_time, t.span_id)
        )
    ).scalars()
    for row in rows:
        out[row.trace_id].append(row)
    return out


# ──────────────────────────────────────────────────────────────────────
# Partner query endpoints
# ──────────────────────────────────────────────────────────────────────

LIST_ENDPOINT = "/partner/v1/traces"
MAX_LIMIT = 200
DEFAULT_LIMIT = 50
DEFAULT_WINDOW = timedelta(hours=24)

partner_router = APIRouter(prefix="/partner/v1", tags=["Partner API"])

_READ = Depends(require_scope(SCOPE))
_CURSOR = Query(default=None, max_length=200, description="meta.next_cursor of the previous page")
_SINCE = Query(default=None, description="ISO 8601 span start, inclusive (default: 24 hours ago)")
_UNTIL = Query(default=None, description="ISO 8601 span start, exclusive (default: now)")
_SESSION = Query(default=None, min_length=1, max_length=MAX_LABEL_LENGTH)
_USER = Query(default=None, pattern="^[0-9a-f]{64}$")
_STATUS = Query(default=None)
_LIMIT = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)


def _problem(status: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@partner_router.get("/traces", summary="List the tenant's agent traces")
async def list_traces(
    request: Request,
    response: Response,
    cursor: str | None = _CURSOR,
    since: datetime | None = _SINCE,
    until: datetime | None = _UNTIL,
    session_id: str | None = _SESSION,
    user_id_hash: str | None = _USER,
    status: Status | None = _STATUS,
    limit: int = _LIMIT,
    principal: PartnerPrincipal = _READ,
) -> dict:
    await _enforce_rate_limit(request, response, principal)
    try:
        after = decode_cursor(cursor) if cursor else None
    except ValueError:
        raise _problem(400, "Bad Request", "Invalid `cursor`") from None
    until_ = _aware(until) or datetime.now(UTC)
    since_ = _aware(since) or until_ - DEFAULT_WINDOW
    if since_ >= until_:
        raise _problem(400, "Bad Request", "`since` must be before `until`")
    db = request.state.db
    ids = await list_trace_ids(
        db,
        principal.tenant_id,
        since=since_,
        until=until_,
        limit=limit + 1,
        after=after,
        session_id=session_id,
        user_id_hash=user_id_hash,
        status=status,
    )
    has_more = len(ids) > limit
    ids = ids[:limit]
    spans = await load_traces(db, principal.tenant_id, [tid for tid, _ in ids])
    next_cursor = encode_cursor(ids[-1][1], ids[-1][0]) if ids else cursor
    # Serialized before the read audit commits (which expires the rows).
    body = {
        "data": [summarize(tid, spans[tid]) for tid, _ in ids],
        "meta": {"has_more": has_more, "next_cursor": next_cursor},
    }
    await _audit(request, principal, LIST_ENDPOINT, "GET", 200, len(ids))
    return body


@partner_router.get("/traces/{trace_id}", summary="Get one agent trace with its spans")
async def get_trace(
    trace_id: str,
    request: Request,
    response: Response,
    principal: PartnerPrincipal = _READ,
) -> dict:
    await _enforce_rate_limit(request, response, principal)
    normalized = normalize_id(trace_id, 16)
    if normalized is None or normalized != trace_id.lower():
        raise _problem(
            400, "Bad Request", "trace_id must be 32 hex characters (not all zero)"
        )
    rows = await load_trace(request.state.db, principal.tenant_id, normalized)
    endpoint = f"{LIST_ENDPOINT}/{normalized}"
    if not rows:
        await _audit(request, principal, endpoint, "GET", 404)
        raise _problem(404, "Not Found", "No spans stored for this trace")
    body = trace_payload(normalized, rows)
    await _audit(request, principal, endpoint, "GET", 200, len(rows))
    return body
