"""Audit log for registry reads.

One mechanism for both read surfaces:

  - `/partner/v1/*`       : who = partner_id (via `partner_registry._audit`)
  - `/api/v1/registry/*`  : who = SHA-256 prefix of the tenant API key

Each read writes one `partner_audit_log` row with tenant, caller, route,
asset ids, redacted query parameters, status, result count and time.

Write path: a single INSERT + COMMIT on the request's own session, after
the data has been read and serialized. That reuses the connection the
request already holds, so it costs one short round trip and never competes
with requests for pool connections the way a fire-and-forget task with its
own session would. It also means the row is durable before the response
leaves, which is what an audit trail wants. Failures are swallowed: an
audit error never fails a read.

Retention: `purge_expired` deletes rows older than
`settings.audit_retention_days`. Run it from a scheduled job with
`python -m collector.audit purge`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from db.models import PartnerAuditLog
from fastapi import Request
from sqlalchemy import delete

from collector.partner_auth import api_key_prefix_sha256

logger = logging.getLogger(__name__)

# Query parameters whose value is never stored.
SECRET_PARAM_MARKERS = (
    "key",
    "token",
    "secret",
    "password",
    "authorization",
    "credential",
)
# Free-text parameters: stored as a hash so equal searches can be correlated
# without keeping the text, which may name people or projects.
FREE_TEXT_PARAMS = frozenset({"q", "query", "search", "text"})
REDACTED = "[REDACTED]"
# Upper bound on asset ids stored per row. response_count still holds the
# true count, so a row with fewer ids than response_count was truncated.
MAX_ASSET_IDS = 200


def hash_free_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:16]


def redact_query_params(
    params: Iterable[tuple[str, str]],
) -> dict[str, str | list[str]]:
    """Return query params safe to store: secrets redacted, free text hashed.

    Repeated keys become a list, matching how they were sent.
    """
    out: dict[str, str | list[str]] = {}
    for key, value in params:
        lowered = key.lower()
        if any(marker in lowered for marker in SECRET_PARAM_MARKERS):
            value = REDACTED
        elif lowered in FREE_TEXT_PARAMS:
            value = hash_free_text(value)
        if key in out:
            existing = out[key]
            out[key] = (
                [*existing, value] if isinstance(existing, list) else [existing, value]
            )
        else:
            out[key] = value
    return out


def bearer_key_prefix(request: Request) -> str | None:
    """SHA-256 lookup prefix of the Bearer token, or None if absent."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth.removeprefix("Bearer ").strip()
    return api_key_prefix_sha256(token) if token else None


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", None) or request.url.path


def _collect_asset_ids(
    request: Request, asset_ids: Iterable[UUID | str] | None
) -> list[UUID] | None:
    ids: list[UUID] = []
    path_id = request.path_params.get("asset_id")
    candidates = [path_id] if path_id is not None else []
    candidates.extend(asset_ids or [])
    for raw in candidates:
        if len(ids) >= MAX_ASSET_IDS:
            break
        try:
            value = raw if isinstance(raw, UUID) else UUID(str(raw))
        except ValueError:
            continue
        if value not in ids:
            ids.append(value)
    return ids or None


def build_audit_row(
    request: Request,
    *,
    tenant_id: UUID,
    status_code: int,
    response_count: int | None = None,
    partner_id: UUID | None = None,
    endpoint: str | None = None,
    method: str | None = None,
    asset_ids: Iterable[UUID | str] | None = None,
) -> PartnerAuditLog:
    """Build (but do not write) one audit row for this request."""
    query_params = redact_query_params(request.query_params.multi_items())
    return PartnerAuditLog(
        tenant_id=tenant_id,
        partner_id=partner_id,
        # Partner rows are identified by partner_id; the key prefix only
        # identifies tenant callers.
        api_key_prefix=None if partner_id else bearer_key_prefix(request),
        endpoint=endpoint or _route_template(request),
        method=method or request.method,
        status_code=status_code,
        response_count=response_count,
        asset_ids=_collect_asset_ids(request, asset_ids),
        query_params=query_params or None,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("User-Agent"),
        timestamp=datetime.now(UTC),
    )


async def record_read(
    request: Request,
    *,
    tenant_id: UUID,
    status_code: int,
    response_count: int | None = None,
    partner_id: UUID | None = None,
    endpoint: str | None = None,
    method: str | None = None,
    asset_ids: Iterable[UUID | str] | None = None,
) -> None:
    """Write one audit row for a registry read. Best effort, never raises."""
    db = request.state.db
    try:
        db.add(
            build_audit_row(
                request,
                tenant_id=tenant_id,
                status_code=status_code,
                response_count=response_count,
                partner_id=partner_id,
                endpoint=endpoint,
                method=method,
                asset_ids=asset_ids,
            )
        )
        await db.commit()
    except Exception:
        logger.warning("registry read audit write failed", exc_info=True)
        try:
            await db.rollback()
        except Exception:
            logger.debug("rollback after audit failure also failed", exc_info=True)


async def purge_expired(db, retention_days: int, now: datetime | None = None) -> int:
    """Delete audit rows older than `retention_days`. Returns rows deleted."""
    if retention_days <= 0:
        raise ValueError("retention_days must be positive")
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    result = await db.execute(
        delete(PartnerAuditLog).where(PartnerAuditLog.timestamp < cutoff)
    )
    await db.commit()
    return result.rowcount or 0


async def _purge_main() -> int:
    from collector.app import SessionLocal, engine
    from collector.config import settings

    try:
        async with SessionLocal() as db:
            return await purge_expired(db, settings.audit_retention_days)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    import sys

    args = sys.argv[1:] if argv is None else argv
    if args != ["purge"]:
        print("usage: python -m collector.audit purge", file=sys.stderr)
        return 2
    deleted = asyncio.run(_purge_main())
    print(f"purged {deleted} audit rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MAX_ASSET_IDS",
    "build_audit_row",
    "hash_free_text",
    "purge_expired",
    "record_read",
    "redact_query_params",
]
