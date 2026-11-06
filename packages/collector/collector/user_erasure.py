"""Erasure by end user (#353, part of #85).

A tenant erases what the collector holds about one of its end users (GDPR
right to erasure) through the tenant admin API, scope ``admin:privacy``:

  POST   /api/v1/admin/users/erase               body ``{"user_id": "..."}``,
                                                 the id as the gateway or
                                                 SDK sent it
  DELETE /api/v1/admin/users/{user_id_hash}/data the stored hash
                                                 (``ai_usage_events.user_id_hash``)

The raw id goes in a POST body, never in a URL, so it is not written to
access logs. The collector hashes it with the tenant's salt
(collector.user_identity) to find the user's events; the salt never leaves
the database.

What is erased, in the caller's tenant only:

* ``ai_usage_events`` of the user (by hash; an event with ``user_id_raw``
  always has the hash too): ``user_id_hash``, ``user_id_raw``,
  ``session_id`` and ``prompt_hash`` (a SHA-256 of the prompt text) are set
  to NULL. The rest of the row (model, tokens, cost, routing metadata) is
  kept, so spend and usage totals do not change; the row no longer says
  whose it was.
* ``call_feedback`` rows with ``user_id`` equal to the id as sent:
  ``user_id`` is set to NULL (score and reason are kept).
* ``mcp_tool_usage`` rows with ``user_hash`` equal to the id as sent:
  merged into the row for the same tool with no user (``user_hash = ''``),
  so the counts stay and the user's key is gone.

The feedback and MCP tables store the id as the gateway sent it (a hex
hash), not the collector's salted hash, so only the POST form, which
carries that id, reaches them.

Every request writes a ``user.erase`` row to the tenant's hash-chained audit
trail (collector.audit_trail), with the user's salted hash as the target
and per-table row counts; it is in the audit export (``audit.user.erase``
webhook events, ``/partner/v1/audit``, ``/partner/v1/siem/audit``) and the
retention audit log. It is idempotent: erasing again changes nothing and
reports zero rows (the request is still audited). Events ingested after the
erasure are stored as usual; stop sending the id first.

Tenant isolation: every statement is filtered on the caller's tenant and
runs in its row-level security context, the hash uses the tenant's own
salt, and the response only counts the tenant's rows, so the result says
nothing about whether another tenant knows the same id. A tenant that has
never ingested a ``user_id`` has no salt; the erasure then creates none
and matches ``user_id_raw`` against the id as sent.

Data already sent elsewhere (webhook receivers, Microsoft Sentinel, a SIEM)
is outside the collector and must be erased there.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from db.models import AIUsageEvent, CallFeedback, McpToolUsage, RetentionAuditLog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from collector import audit_trail, user_identity
from collector.admin_auth import AdminPrincipal, admin_actor, require_admin
from collector.config import settings
from collector.sanitize import BoundedJsonRoute

logger = logging.getLogger(__name__)

ACTION = "user.erase"
RETENTION_ACTION = "erase_user"
SCOPE = "admin:privacy"
USER_ID_HASH_RE = re.compile(r"[0-9a-f]{64}")
MAX_USER_ID_LENGTH = 512

# Columns set to NULL on the user's usage events.
EVENT_COLUMNS = ("user_id_hash", "user_id_raw", "session_id", "prompt_hash")

Input = Literal["user_id", "user_id_hash"]


@dataclass
class UserEraseResult:
    user_id_hash: str | None
    input: Input
    rows_erased: dict[str, int] = field(default_factory=dict)
    audit_event_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "user_id_hash": self.user_id_hash,
            "input": self.input,
            "rows_erased": self.rows_erased,
            "audit_event_id": self.audit_event_id,
        }


def _batch_size(batch_size: int | None) -> int:
    size = batch_size if batch_size is not None else settings.retention_batch_size
    if size < 1:
        raise ValueError("batch size must be at least 1")
    return size


async def _null_in_batches(db: AsyncSession, model, conditions: list, values: dict, size: int) -> int:
    """``UPDATE ... SET <values> WHERE id IN (SELECT id ... LIMIT n)`` until
    nothing matches, committing each batch. ``values`` must make a row stop
    matching ``conditions``, or this would not end."""
    table = model.__table__
    total = 0
    while True:
        ids = select(table.c.id).where(*conditions).limit(size)
        result = await db.execute(
            update(table).where(table.c.id.in_(ids)).values(**values)
        )
        await db.commit()
        changed = result.rowcount or 0
        total += changed
        if changed < size:
            return total


_MERGE_MCP = text(
    """
    WITH moved AS (
        DELETE FROM mcp_tool_usage
        WHERE tenant_id = :tenant_id AND user_hash = :user_hash
        RETURNING tenant_id, source, server_name, tool_name, calling_service,
                  environment, transport, call_count, error_count, blocked_count,
                  user_group, latency_ms_total, latency_count, first_seen, last_seen
    ), summed AS (
        SELECT tenant_id, source, server_name, tool_name, calling_service,
               environment, user_group, max(transport) AS transport,
               sum(call_count) AS call_count, sum(error_count) AS error_count,
               sum(blocked_count) AS blocked_count,
               sum(latency_ms_total) AS latency_ms_total,
               sum(latency_count) AS latency_count,
               min(first_seen) AS first_seen, max(last_seen) AS last_seen
        FROM moved
        GROUP BY tenant_id, source, server_name, tool_name, calling_service,
                 environment, user_group
    ), merged AS (
        INSERT INTO mcp_tool_usage AS u (
            id, tenant_id, source, server_name, tool_name, calling_service,
            environment, transport, call_count, error_count, blocked_count,
            user_hash, user_group, latency_ms_total, latency_count, first_seen,
            last_seen)
        SELECT gen_random_uuid(), tenant_id, source, server_name, tool_name,
               calling_service, environment, transport, call_count, error_count,
               blocked_count, '', user_group, latency_ms_total, latency_count,
               first_seen, last_seen
        FROM summed
        ON CONFLICT (tenant_id, source, server_name, tool_name, calling_service,
                     environment, user_hash, user_group) DO UPDATE SET
            call_count = u.call_count + EXCLUDED.call_count,
            error_count = u.error_count + EXCLUDED.error_count,
            blocked_count = u.blocked_count + EXCLUDED.blocked_count,
            latency_ms_total = u.latency_ms_total + EXCLUDED.latency_ms_total,
            latency_count = u.latency_count + EXCLUDED.latency_count,
            transport = coalesce(u.transport, EXCLUDED.transport),
            first_seen = least(u.first_seen, EXCLUDED.first_seen),
            last_seen = greatest(u.last_seen, EXCLUDED.last_seen)
    )
    SELECT count(*) FROM moved
    """
)


async def _merge_mcp_rows(db: AsyncSession, tenant_id: UUID, user_hash: str) -> int:
    """Fold the user's ``mcp_tool_usage`` rows into the no-user rows of the
    same tools, in one transaction. Returns how many user rows there were."""
    if db.get_bind().dialect.name != "postgresql":
        # SQLite unit tests: the same, row by row.
        return await _merge_mcp_rows_portable(db, tenant_id, user_hash)
    moved = (
        await db.execute(_MERGE_MCP, {"tenant_id": tenant_id, "user_hash": user_hash})
    ).scalar_one()
    await db.commit()
    return moved or 0


async def _merge_mcp_rows_portable(db: AsyncSession, tenant_id: UUID, user_hash: str) -> int:
    rows = (
        await db.execute(
            select(McpToolUsage).where(
                McpToolUsage.tenant_id == tenant_id, McpToolUsage.user_hash == user_hash
            )
        )
    ).scalars().all()
    for row in rows:
        target = (
            await db.execute(
                select(McpToolUsage).where(
                    McpToolUsage.tenant_id == tenant_id,
                    McpToolUsage.source == row.source,
                    McpToolUsage.server_name == row.server_name,
                    McpToolUsage.tool_name == row.tool_name,
                    McpToolUsage.calling_service == row.calling_service,
                    McpToolUsage.environment == row.environment,
                    McpToolUsage.user_group == row.user_group,
                    McpToolUsage.user_hash == "",
                )
            )
        ).scalar_one_or_none()
        if target is None:
            row.user_hash = ""
            continue
        for name in ("call_count", "error_count", "blocked_count", "latency_ms_total", "latency_count"):
            setattr(target, name, (getattr(target, name) or 0) + (getattr(row, name) or 0))
        target.first_seen = min(target.first_seen, row.first_seen)
        target.last_seen = max(target.last_seen, row.last_seen)
        target.transport = target.transport or row.transport
        await db.delete(row)
    await db.commit()
    return len(rows)


async def erase_user(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    user_id: str | None = None,
    user_id_hash: str | None = None,
    actor: audit_trail.Actor,
    status_code: int = 200,
    batch_size: int | None = None,
) -> UserEraseResult:
    """Erase one end user's identifiers from ``tenant_id``'s data. Give
    exactly one of ``user_id`` (as sent) or ``user_id_hash`` (as stored).

    Runs in the caller's session, which must be in ``tenant_id``'s RLS
    context (or a logged bypass). Batched and committed as it goes, so a
    failure part-way leaves less to do on a rerun; always audited.
    """
    if (user_id is None) == (user_id_hash is None):
        raise ValueError("give exactly one of user_id or user_id_hash")
    size = _batch_size(batch_size)
    input_: Input = "user_id" if user_id is not None else "user_id_hash"

    if user_id is not None:
        # Read the salt, never create it: no salt means no hashed rows.
        salt = await user_identity._salt(db, tenant_id)
        user_id_hash = user_identity.hash_user_id(salt, user_id) if salt else None

    # Ingest stores user_id_raw only next to user_id_hash (same salt), so
    # the hash finds every event of the user, through its partial index.
    # Without a salt (no hashed rows), match user_id_raw as sent instead, so
    # a raw id stored any other way is still erased.
    ev = AIUsageEvent
    rows: dict[str, int] = {}
    if user_id_hash is not None:
        match = ev.user_id_hash == user_id_hash
    else:
        match = ev.user_id_raw == user_id
    rows[ev.__tablename__] = await _null_in_batches(
        db, ev, [ev.tenant_id == tenant_id, match], dict.fromkeys(EVENT_COLUMNS), size
    )
    if user_id is not None:
        rows[CallFeedback.__tablename__] = await _null_in_batches(
            db,
            CallFeedback,
            [CallFeedback.tenant_id == tenant_id, CallFeedback.user_id == user_id],
            {"user_id": None},
            size,
        )
        rows[McpToolUsage.__tablename__] = await _merge_mcp_rows(db, tenant_id, user_id)

    result = UserEraseResult(user_id_hash=user_id_hash, input=input_, rows_erased=rows)
    # The data is committed. If the audit write fails now, report the
    # erasure (it happened) and log the missing audit loudly.
    try:
        db.add(
            RetentionAuditLog(
                tenant_id=tenant_id,
                action=RETENTION_ACTION,
                actor=f"{actor.type}:{actor.id}" if actor.id else actor.type,
                details=dict(rows),
            )
        )
        row = await audit_trail.record(
            db,
            tenant_id=tenant_id,
            action=ACTION,
            actor=actor,
            target_type="user",
            target_id=user_id_hash,
            status_code=status_code,
            details={"input": input_, "rows_erased": dict(rows)},
        )
        audit_event_id = str(row.id) if row is not None else None
        await db.commit()
        result.audit_event_id = audit_event_id
    except Exception:
        logger.exception(
            "user erased but its audit record could not be written tenant=%s "
            "rows_erased=%s; record it by hand",
            tenant_id,
            rows,
        )
        try:
            await db.rollback()
        except Exception:
            logger.debug("rollback after the user erase audit failure also failed", exc_info=True)
    logger.info("user erased tenant=%s rows_erased=%s", tenant_id, rows)
    return result


# ──────────────────────────────────────────────────────────────────────
# Tenant admin routes
# ──────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/v1/admin/users", tags=["Admin"], route_class=BoundedJsonRoute)

_PRIVACY = Depends(require_admin(SCOPE))


def _problem(status: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


class EraseUserBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1, max_length=MAX_USER_ID_LENGTH)


@router.post("/erase", summary="Erase an end user's data by the user id as sent")
async def erase_by_user_id(
    body: EraseUserBody, request: Request, principal: AdminPrincipal = _PRIVACY
) -> dict:
    try:
        body.user_id.encode("utf-8")
    except UnicodeEncodeError:
        raise _problem(400, "Bad Request", "user_id must be valid UTF-8") from None
    if "\x00" in body.user_id:
        raise _problem(400, "Bad Request", "user_id must not contain NUL")
    result = await erase_user(
        request.state.db,
        principal.tenant_id,
        user_id=body.user_id,
        actor=admin_actor(request, principal),
    )
    return result.as_dict()


@router.delete("/{user_id_hash}/data", summary="Erase an end user's data by the stored user id hash")
async def erase_by_user_id_hash(
    user_id_hash: str, request: Request, principal: AdminPrincipal = _PRIVACY
) -> dict:
    if not USER_ID_HASH_RE.fullmatch(user_id_hash):
        raise _problem(400, "Bad Request", "user_id_hash must be 64 lowercase hex characters")
    result = await erase_user(
        request.state.db,
        principal.tenant_id,
        user_id_hash=user_id_hash,
        actor=admin_actor(request, principal),
    )
    return result.as_dict()

