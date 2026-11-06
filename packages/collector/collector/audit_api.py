"""Tenant audit trail read API (#353).

    GET /partner/v1/audit   scope audit:read

The tenant's hash-chained audit trail (collector.audit_trail) as JSON pages,
in chain order (``seq`` ascending), each row in the same form as the
``audit.*`` webhook events (``audit_trail.audit_payload``) with its
``prev_hash`` and ``row_hash``, so a reader can re-verify the chain. Filters:
``since`` / ``until`` (``created_at``, ``since`` ignored with a cursor) and
``action`` (exact, e.g. ``user.erase``). ``meta.next_cursor`` is the ``seq``
of the last row; pass it back as ``cursor``. Only the caller's tenant chain
is read (its RLS context and an explicit ``tenant_id`` filter).

The NDJSON form for log forwarders is ``/partner/v1/siem/audit``
(collector.sentinel_export); both read through ``load_audit``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from collector import audit_trail
from collector.partner_auth import PartnerPrincipal, require_scope
from collector.partner_registry import _audit, _enforce_rate_limit
from collector.sentinel_export import load_audit

ENDPOINT = "/partner/v1/audit"
MAX_LIMIT = 1000
DEFAULT_LIMIT = 200

partner_router = APIRouter(prefix="/partner/v1", tags=["Partner API"])

_CURSOR = Query(default=None, description="meta.next_cursor of the previous page")
_SINCE = Query(default=None, description="ISO 8601; ignored with a cursor")
_UNTIL = Query(default=None, description="ISO 8601, exclusive")
_ACTION = Query(default=None, min_length=1, max_length=200)
_LIMIT = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
_READ = Depends(require_scope("audit:read"))


def _problem(status: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@partner_router.get("/audit", summary="List the tenant's audit trail")
async def list_audit(
    request: Request,
    response: Response,
    cursor: str | None = _CURSOR,
    since: datetime | None = _SINCE,
    until: datetime | None = _UNTIL,
    action: str | None = _ACTION,
    limit: int = _LIMIT,
    principal: PartnerPrincipal = _READ,
) -> dict:
    await _enforce_rate_limit(request, response, principal)
    after_seq = None
    if cursor is not None:
        # isascii: str.isdigit also takes other scripts' digits. The seq is
        # a bigint, so a larger value would fail in the database (500).
        if (
            len(cursor) > 19
            or not (cursor.isascii() and cursor.isdigit())
            or int(cursor) > 2**63 - 1
        ):
            raise _problem(400, "Bad Request", "Invalid `cursor`")
        after_seq = int(cursor)
    rows = await load_audit(
        request.state.db,
        principal.tenant_id,
        after_seq=after_seq,
        since=_aware(since),
        until=_aware(until),
        action=action,
        limit=limit + 1,
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = str(rows[-1].seq) if rows else cursor
    # Serialized before the read audit commits (which expires the rows).
    body = {
        "data": [audit_trail.audit_payload(r) for r in rows],
        "meta": {"has_more": has_more, "next_cursor": next_cursor},
    }
    await _audit(request, principal, ENDPOINT, "GET", 200, len(rows))
    return body
