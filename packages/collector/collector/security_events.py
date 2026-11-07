"""Gateway security events in the collector (#353).

The gateway writes security events (schema v1, gateway/src/middlewares/
psSecurityEvents, docs/security-events.schema.json): guardrail verdicts,
PII and injection detections, auth failures, policy denials, rate limits
and payload rejections. Metadata only: never prompt or response content,
PII values, credentials or client IP addresses. This module stores them per
tenant (``gateway_security_events``, row-level security) and serves them
back.

Ingest (tenant ingest key, scope ``ingest:write``; the tenant is the key's,
never a field of the event):

  POST /ingest/otel/v1/logs       OTLP/HTTP JSON logs, as the gateway's
                                  ``PS_SECURITY_EVENTS_OTLP`` exporter sends
                                  them. Point ``PS_SECURITY_EVENTS_OTLP_ENDPOINT``
                                  at ``<collector>/ingest/otel/v1/logs`` with
                                  ``PS_SECURITY_EVENTS_OTLP_HEADERS=
                                  Authorization=Bearer <ingest key>``. Each
                                  log record's body is the event's JSON
                                  (its attributes are used when the body is
                                  not); records whose ``log.logger`` is not
                                  ``ps.security`` are rejected and counted in
                                  ``partialSuccess``.
  POST /ingest/security-events    the stdout JSON lines (``PS_SECURITY_EVENTS_LOG``)
                                  from a log forwarder: ``application/x-ndjson``
                                  (one event per line), or ``application/json``
                                  (an array, or ``{"events": [...]}``).

Each event is validated: ``event.id``, ``event.name``, ``@timestamp`` and
``log.logger: ps.security`` are required; known fields go to columns
(strings over 256 characters are dropped), other ``ps.*``, ``gen_ai.*``
and ``http.*`` fields with scalar values go to ``attributes``. Anything
else (``ps.tenant`` included: the tenant comes from the key) is dropped.
An event already stored for the tenant (same ``event.id``) counts as a
duplicate and is not stored again, so a forwarder may resend.

Query (Partner API, scope ``security-events:read``)::

  GET /partner/v1/security-events

Pages in arrival order (``received_at``, then id) so a reader can tail the
feed with ``meta.next_cursor``; ``since`` / ``until`` filter on the event's
own time, ``name``, ``severity`` and ``action`` on exact values.

Retention: rows older than the tenant's retention period are purged with
its usage events (collector.retention); a tenant erasure deletes them; the
end-user erasure clears ``user_hash`` (collector.user_erasure).
"""

from __future__ import annotations

import base64
import logging
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from db.models import GatewaySecurityEvent, uuid7
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from collector import tenant_keys
from collector.auth import resolve_tenant
from collector.otel_ingest import _read_bounded_body
from collector.partner_auth import PartnerPrincipal, require_scope
from collector.partner_registry import _audit, _enforce_rate_limit
from collector.sanitize import BodyTooDeep, loads_bounded, too_deep_problem

logger = logging.getLogger("prompt_shields.security_events")

LOGGER_NAME = "ps.security"
MAX_EVENTS = 5000
MAX_STRING = 256
MAX_ATTRIBUTES = 64
MAX_ERRORS = 20
NDJSON = "application/x-ndjson"

_EVENT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,30}(?:\.[a-z0-9_]{1,30}){1,3}$")
_ATTR_KEY = re.compile(r"^(?:ps|gen_ai|http)\.[a-z0-9_.]{1,100}$")
_CATEGORY = re.compile(r"^[a-z0-9_]{1,64}$")

# v1 field -> column (strings).
STRING_COLUMNS: dict[str, str] = {
    "event.kind": "kind",
    "event.outcome": "outcome",
    "severity": "severity",
    "action": "action",
    "signature": "signature",
    "ps.request_id": "request_id",
    "http.route": "route",
    "http.request.method": "method",
    "ps.business_unit": "business_unit",
    "ps.environment": "environment",
    "gen_ai.provider.name": "provider",
    "gen_ai.request.model": "model",
    "user.hash": "user_hash",
    "ps.schema_version": "schema_version",
}
# Constants of the gateway's schema and the deployment-wide tenant label:
# not stored.
DROPPED_FIELDS = frozenset(
    {"log.logger", "service.name", "vendor_product", "ps.tenant", "ps.tenant_id"}
)
HANDLED_FIELDS = frozenset(
    {"@timestamp", "event.id", "event.name", "event.category", "http.response.status_code"}
    | set(STRING_COLUMNS)
    | DROPPED_FIELDS
)


class InvalidEvent(ValueError):
    """The event cannot be stored; the message is a short reason (no values)."""


def _problem(status: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise InvalidEvent("@timestamp must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise InvalidEvent("@timestamp must be an ISO 8601 string") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= MAX_STRING else None


def _scalar(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) < 2**53 else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return _string(value)


def parse_event(event: Any) -> dict:
    """A v1 event -> column values for ``GatewaySecurityEvent``.

    Raises InvalidEvent when it is not a storable security event."""
    if not isinstance(event, dict):
        raise InvalidEvent("event must be a JSON object")
    if event.get("log.logger") != LOGGER_NAME:
        raise InvalidEvent("log.logger must be ps.security")
    event_id = event.get("event.id")
    if not isinstance(event_id, str) or not _EVENT_ID.match(event_id):
        raise InvalidEvent("event.id must be a short identifier")
    name = event.get("event.name")
    if not isinstance(name, str) or not _EVENT_NAME.match(name):
        raise InvalidEvent("event.name must be a dotted event name")
    row: dict[str, Any] = {
        "event_id": event_id,
        "name": name,
        "occurred_at": _parse_time(event.get("@timestamp")),
    }
    for key, column in STRING_COLUMNS.items():
        row[column] = _string(event.get(key))
    status = event.get("http.response.status_code")
    row["status_code"] = (
        status
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599
        else None
    )
    attributes: dict[str, Any] = {}
    categories = event.get("event.category")
    if isinstance(categories, list):
        kept = [c for c in categories[:10] if isinstance(c, str) and _CATEGORY.match(c)]
        if kept:
            attributes["event.category"] = kept
    for key in sorted(event):
        if len(attributes) >= MAX_ATTRIBUTES:
            break
        if key in HANDLED_FIELDS or not _ATTR_KEY.match(key):
            continue
        value = _scalar(event[key])
        if value is not None:
            attributes[key] = value
    row["attributes"] = attributes
    return row


@dataclass
class IngestResult:
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    errors: list[dict] = field(default_factory=list)

    def reject(self, index: int, reason: str) -> None:
        self.rejected += 1
        if len(self.errors) < MAX_ERRORS:
            self.errors.append({"index": index, "reason": reason})

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "errors": self.errors,
        }


async def store_events(
    db: AsyncSession, tenant_id: UUID, events: Iterable[Any]
) -> IngestResult:
    """Validate and insert the events for one tenant (one transaction,
    committed). Duplicates of stored events (same ``event.id``) are skipped."""
    result = IngestResult()
    rows: dict[str, dict] = {}
    for index, event in enumerate(events):
        try:
            row = parse_event(event)
        except InvalidEvent as exc:
            result.reject(index, str(exc))
            continue
        if row["event_id"] in rows:
            result.duplicates += 1
            continue
        rows[row["event_id"]] = row
    if rows:
        table = GatewaySecurityEvent.__table__
        now = datetime.now(UTC)
        stmt = (
            pg_insert(table)
            .values(
                [
                    {
                        "id": uuid7(),
                        "tenant_id": tenant_id,
                        "received_at": now,
                        **row,
                    }
                    for row in rows.values()
                ]
            )
            .on_conflict_do_nothing(constraint="ux_gateway_security_events_event")
            .returning(table.c.id)
        )
        inserted = len((await db.execute(stmt)).all())
        result.accepted = inserted
        result.duplicates += len(rows) - inserted
    await db.commit()
    if result.rejected:
        logger.info(
            "security events: tenant=%s accepted=%d rejected=%d",
            tenant_id,
            result.accepted,
            result.rejected,
        )
    return result


# ──────────────────────────────────────────────────────────────────────
# Ingest endpoints
# ──────────────────────────────────────────────────────────────────────

router = APIRouter()


def _content_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";")[0].strip().lower()


def _any_value(value: Any) -> Any:
    """An OTLP AnyValue (JSON encoding) -> a plain value; None for kinds an
    event field never has."""
    if not isinstance(value, dict):
        return None
    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return value["boolValue"]
    if "intValue" in value:
        try:
            return int(value["intValue"])
        except (TypeError, ValueError):
            return None
    if "doubleValue" in value:
        return value["doubleValue"]
    if "arrayValue" in value:
        values = (value["arrayValue"] or {}).get("values")
        if isinstance(values, list):
            return [_any_value(v) for v in values[:10]]
    return None


def _time_from_nanos(value: Any) -> str | None:
    try:
        nanos = int(value)
    except (TypeError, ValueError):
        return None
    if nanos <= 0:
        return None
    return datetime.fromtimestamp(nanos / 1e9, UTC).isoformat()


def otlp_log_events(payload: dict) -> list[Any]:
    """The security events in an ExportLogsServiceRequest (JSON), one per
    log record: the body's JSON object, else the record's attributes."""
    out: list[Any] = []
    for resource_logs in payload.get("resourceLogs") or []:
        if not isinstance(resource_logs, dict):
            out.append(None)
            continue
        for scope_logs in resource_logs.get("scopeLogs") or []:
            records = scope_logs.get("logRecords") if isinstance(scope_logs, dict) else None
            for record in records or []:
                if not isinstance(record, dict):
                    out.append(None)
                    continue
                body = _any_value(record.get("body"))
                event: Any = None
                if isinstance(body, str) and body.startswith("{"):
                    try:
                        event = loads_bounded(body)
                    except ValueError:
                        event = None
                if not isinstance(event, dict):
                    attrs = record.get("attributes")
                    event = {
                        a["key"]: _any_value(a.get("value"))
                        for a in (attrs if isinstance(attrs, list) else [])
                        if isinstance(a, dict) and isinstance(a.get("key"), str)
                    }
                    ts = _time_from_nanos(record.get("timeUnixNano"))
                    if ts and "@timestamp" not in event:
                        event["@timestamp"] = ts
                out.append(event)
    return out


@router.post("/ingest/otel/v1/logs")
async def ingest_otel_logs(request: Request) -> dict:
    """OTLP/HTTP JSON logs carrying gateway security events."""
    tenant_id = await resolve_tenant(request, tenant_keys.INGEST_WRITE)
    if _content_type(request) != "application/json":
        raise _problem(
            415, "Unsupported Media Type", "Use OTLP/HTTP JSON (application/json)"
        )
    raw = await _read_bounded_body(request)
    try:
        payload = loads_bounded(raw)
    except BodyTooDeep:
        raise too_deep_problem() from None
    except ValueError:
        raise _problem(400, "Bad Request", "Body is not valid JSON") from None
    if not isinstance(payload, dict):
        raise _problem(400, "Bad Request", "Expected an ExportLogsServiceRequest object")
    events = otlp_log_events(payload)
    if len(events) > MAX_EVENTS:
        raise _problem(413, "Payload Too Large", f"More than {MAX_EVENTS} log records")
    result = await store_events(request.state.db, tenant_id, events)
    # ExportLogsServiceResponse: empty means full success.
    if result.rejected:
        return {
            "partialSuccess": {
                "rejectedLogRecords": result.rejected,
                "errorMessage": "log records that are not Prompt Shields security events",
            }
        }
    return {}


@router.post("/ingest/security-events")
async def ingest_security_events(request: Request) -> dict:
    """Security events as JSON lines or a JSON array (log forwarders)."""
    tenant_id = await resolve_tenant(request, tenant_keys.INGEST_WRITE)
    content_type = _content_type(request)
    if content_type not in ("application/json", NDJSON):
        raise _problem(
            415,
            "Unsupported Media Type",
            f"Use {NDJSON} (one event per line) or application/json",
        )
    raw = await _read_bounded_body(request)
    try:
        if content_type == NDJSON:
            lines = [ln for ln in raw.decode().splitlines() if ln.strip()]
            if len(lines) > MAX_EVENTS:
                raise _problem(413, "Payload Too Large", f"More than {MAX_EVENTS} events")
            events: list[Any] = []
            for line in lines:
                try:
                    events.append(loads_bounded(line))
                except BodyTooDeep:
                    raise
                except ValueError:
                    events.append(None)
        else:
            payload = loads_bounded(raw)
            events = payload.get("events") if isinstance(payload, dict) else payload
            if not isinstance(events, list):
                raise _problem(
                    400, "Bad Request", 'Expected an array of events or {"events": [...]}'
                )
    except BodyTooDeep:
        raise too_deep_problem() from None
    except ValueError:  # UnicodeDecodeError included
        raise _problem(400, "Bad Request", "Body is not valid JSON") from None
    if len(events) > MAX_EVENTS:
        raise _problem(413, "Payload Too Large", f"More than {MAX_EVENTS} events")
    result = await store_events(request.state.db, tenant_id, events)
    return result.as_dict()


# ──────────────────────────────────────────────────────────────────────
# Reading
# ──────────────────────────────────────────────────────────────────────


def _ts(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def event_fields(row: GatewaySecurityEvent) -> dict:
    """The stored event in the gateway's v1 field names."""
    out: dict[str, Any] = {
        "@timestamp": _ts(row.occurred_at),
        "event.id": row.event_id,
        "event.name": row.name,
    }
    for key, column in STRING_COLUMNS.items():
        value = getattr(row, column)
        if value is not None:
            out[key] = value
    if row.status_code is not None:
        out["http.response.status_code"] = row.status_code
    out.update(row.attributes or {})
    return out


def security_event_payload(row: GatewaySecurityEvent) -> dict:
    """One row as the query API returns it."""
    return {
        "id": str(row.id),
        "received_at": _ts(row.received_at),
        "event": event_fields(row),
    }


def encode_cursor(received_at: datetime, row_id: UUID | str) -> str:
    raw = f"{_ts(received_at)}|{row_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Raises ValueError on anything but a cursor this module made."""
    try:
        if len(cursor) > 200:
            raise ValueError
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        ts, _, row_id = raw.partition("|")
        parsed = datetime.fromisoformat(ts)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed, UUID(row_id)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid cursor") from exc


async def load_security_events(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    after: tuple[datetime, UUID] | None,
    limit: int,
    received_until: datetime | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    name: str | None = None,
    severity: str | None = None,
    action: str | None = None,
) -> list[GatewaySecurityEvent]:
    """Rows after the cursor in ``(received_at, id)`` order. ``since`` /
    ``until`` bound the event time, ``received_until`` the arrival time."""
    t = GatewaySecurityEvent
    conds = [t.tenant_id == tenant_id]
    if after is not None:
        ts, row_id = after
        conds.append(or_(t.received_at > ts, and_(t.received_at == ts, t.id > row_id)))
    if received_until is not None:
        conds.append(t.received_at <= received_until)
    if since is not None:
        conds.append(t.occurred_at >= since)
    if until is not None:
        conds.append(t.occurred_at < until)
    if name is not None:
        conds.append(t.name == name)
    if severity is not None:
        conds.append(t.severity == severity)
    if action is not None:
        conds.append(t.action == action)
    return list(
        (
            await db.execute(
                select(t).where(*conds).order_by(t.received_at, t.id).limit(limit)
            )
        ).scalars()
    )


# ──────────────────────────────────────────────────────────────────────
# Partner query endpoint
# ──────────────────────────────────────────────────────────────────────

ENDPOINT = "/partner/v1/security-events"
MAX_LIMIT = 1000
DEFAULT_LIMIT = 200

partner_router = APIRouter(prefix="/partner/v1", tags=["Partner API"])

_CURSOR = Query(default=None, max_length=200, description="meta.next_cursor of the previous page")
_SINCE = Query(default=None, description="ISO 8601 event time, inclusive")
_UNTIL = Query(default=None, description="ISO 8601 event time, exclusive")
_NAME = Query(default=None, min_length=1, max_length=64)
_SEVERITY = Query(default=None, min_length=1, max_length=32)
_ACTION = Query(default=None, min_length=1, max_length=32)
_LIMIT = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
_READ = Depends(require_scope("security-events:read"))


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@partner_router.get("/security-events", summary="List the tenant's gateway security events")
async def list_security_events(
    request: Request,
    response: Response,
    cursor: str | None = _CURSOR,
    since: datetime | None = _SINCE,
    until: datetime | None = _UNTIL,
    name: str | None = _NAME,
    severity: str | None = _SEVERITY,
    action: str | None = _ACTION,
    limit: int = _LIMIT,
    principal: PartnerPrincipal = _READ,
) -> dict:
    await _enforce_rate_limit(request, response, principal)
    try:
        after = decode_cursor(cursor) if cursor else None
    except ValueError:
        raise _problem(400, "Bad Request", "Invalid `cursor`") from None
    rows = await load_security_events(
        request.state.db,
        principal.tenant_id,
        after=after,
        limit=limit + 1,
        since=_aware(since),
        until=_aware(until),
        name=name,
        severity=severity,
        action=action,
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor(rows[-1].received_at, rows[-1].id) if rows else cursor
    # Serialized before the read audit commits (which expires the rows).
    body = {
        "data": [security_event_payload(r) for r in rows],
        "meta": {"has_more": has_more, "next_cursor": next_cursor},
    }
    await _audit(request, principal, ENDPOINT, "GET", 200, len(rows))
    return body
