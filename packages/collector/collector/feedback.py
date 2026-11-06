"""Explicit call feedback (#43): ``POST /ingest/feedback``.

The routing learner's strongest signal (PRD 0001 §7.13) is a person saying
whether a call's answer was any good. The SDK (``client.feedback``) and the
gateway (``POST /v1/feedback``) send it here, naming the call by the
``call_id`` they put on its usage event.

Metadata only. A feedback is a score in [0, 1], an optional reason from a
fixed set of labels (``REASONS``), the caller's user id as a hex hash
(never a raw identifier) and the source; there is no field for free text, and unknown fields are ignored.

* Tenant-scoped: the same ingest-key auth as ``/ingest/events``; one row per
  (tenant, call_id).
* Idempotent per call: a resend stores nothing new, and a later feedback for
  the same call replaces the earlier one (the user changed their mind). The
  feedback is kept even when the call's event has not arrived yet; the
  response says whether it has (``matched``).
* Bounded: ``MAX_BODY_BYTES`` per request, one feedback per request.
* A string the database cannot store (NUL, lone surrogate; see
  collector/sanitize.py) is refused with a 422, like any invalid field.
* Rate-limited per tenant (``PS_FEEDBACK_RATE_LIMIT_PER_MINUTE``) and, when
  a ``user_id`` is sent, per user within the tenant
  (``PS_FEEDBACK_USER_RATE_LIMIT_PER_MINUTE``), so one user cannot flood
  the learner with votes.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from db.models import AIUsageEvent, CallFeedback, uuid7
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from collector import tenant_keys
from collector.auth import resolve_tenant
from collector.config import settings
from collector.ingest import CALL_ID_RE, _enforce_rate_limit
from collector.partner_rate_limit import (
    FixedWindowRateLimiter,
    get_feedback_rate_limiter,
)
from collector.sanitize import BodyTooDeep, is_storable, loads_bounded, too_deep_problem

router = APIRouter()

MAX_BODY_BYTES = 4096
# A user id is kept only as a hex hash, as on /ingest/mcp (the gateway's
# OIDC subject hash; the SDK hashes the caller's user id before sending), so
# a raw identifier (an e-mail, a username) is never stored.
USER_HASH_RE = re.compile(r"[0-9a-f]{16,64}")
SOURCES = frozenset({"sdk", "gateway"})
# Why a call was rated as it was: labels, never text. Kept in step with
# prompt_shields.feedback.FEEDBACK_REASONS and the gateway's FEEDBACK_REASONS.
REASONS = frozenset(
    {
        "correct",
        "helpful",
        "incorrect",
        "incomplete",
        "hallucination",
        "off_topic",
        "refusal",
        "format_error",
        "unsafe",
        "too_slow",
        "too_long",
        "other",
    }
)


def _problem(status: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


def _invalid(detail: str) -> HTTPException:
    return _problem(422, "Unprocessable Entity", detail)


def parse_feedback(body: Any) -> dict:
    """The stored fields of a feedback body, or a 422 naming what is wrong."""
    if not isinstance(body, dict):
        raise _invalid("Expected a JSON object")
    if not is_storable(body):
        raise _invalid("Body must not contain NUL characters or unpaired surrogates")

    call_id = body.get("call_id")
    if not isinstance(call_id, str) or not CALL_ID_RE.fullmatch(call_id):
        raise _invalid("call_id must be 1-128 characters of A-Z a-z 0-9 . _ : -")

    score = body.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise _invalid("score must be a number from 0 to 1")
    if isinstance(score, int) and score not in (0, 1):
        raise _invalid("score must be a number from 0 to 1")
    score = float(score)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise _invalid("score must be a number from 0 to 1")

    reason = body.get("reason")
    if reason is not None and reason not in REASONS:
        raise _invalid(f"reason must be one of: {', '.join(sorted(REASONS))}")

    user_id = body.get("user_id")
    if user_id is not None and (
        not isinstance(user_id, str) or not USER_HASH_RE.fullmatch(user_id)
    ):
        raise _invalid("user_id must be a hex hash (16-64 lowercase hex characters)")

    source = body.get("source", "sdk")
    if source not in SOURCES:
        raise _invalid(f"source must be one of: {', '.join(sorted(SOURCES))}")

    return {
        "call_id": call_id,
        "score": score,
        "reason": reason,
        "user_id": user_id,
        "source": source,
    }


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


async def store_feedback(
    db: AsyncSession, tenant_id: UUID, fields: dict, now: datetime | None = None
) -> bool:
    """Insert or replace the tenant's feedback for ``fields["call_id"]`` (no
    commit). Returns True when it was new."""
    now = now or datetime.now(UTC)
    table = CallFeedback.__table__
    stmt = pg_insert(table).values(
        id=uuid7(), tenant_id=tenant_id, created_at=now, updated_at=now, **fields
    )
    ex = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        constraint="ux_call_feedback_call",
        set_={
            "score": ex.score,
            "reason": ex.reason,
            "user_id": ex.user_id,
            "source": ex.source,
            "updated_at": ex.updated_at,
        },
    ).returning(literal_column("(xmax = 0)").label("inserted"))
    return bool((await db.execute(stmt)).scalar_one())


async def call_matched(db: AsyncSession, tenant_id: UUID, call_id: str) -> bool:
    """Whether a usage event with this call_id has arrived for the tenant."""
    row = await db.execute(
        select(AIUsageEvent.id)
        .where(AIUsageEvent.tenant_id == tenant_id, AIUsageEvent.call_id == call_id)
        .limit(1)
    )
    return row.first() is not None


def _enforce_limits(
    limiter: FixedWindowRateLimiter,
    tenant_id: UUID,
    user_id: str | None,
    response: Response,
) -> None:
    _enforce_rate_limit(
        limiter, ("tenant", tenant_id), response, settings.feedback_rate_limit_per_minute
    )
    if user_id is not None:
        # The user's own budget; its headers replace the tenant's.
        _enforce_rate_limit(
            limiter,
            ("user", tenant_id, user_id),
            response,
            settings.feedback_user_rate_limit_per_minute,
        )


@router.post("/ingest/feedback")
async def ingest_feedback(
    request: Request,
    response: Response,
    limiter: Annotated[FixedWindowRateLimiter, Depends(get_feedback_rate_limiter)],
):
    """Record feedback on one call: ``{"call_id", "score", "reason",
    "user_id", "source"}``. See the module docstring."""
    tenant_id = await resolve_tenant(request, tenant_keys.INGEST_WRITE)
    body = await _read_body(request)
    fields = parse_feedback(body)
    _enforce_limits(limiter, tenant_id, fields["user_id"], response)

    db = request.state.db
    try:
        created = await store_feedback(db, tenant_id, fields)
        matched = await call_matched(db, tenant_id, fields["call_id"])
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return {"call_id": fields["call_id"], "created": created, "matched": matched}
