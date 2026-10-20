"""Data retention and tenant erasure (#85).

Retention
    Each tenant keeps raw ``ai_usage_events`` for ``retention_days`` days,
    read from ``tenants.settings["retention_days"]`` and falling back to
    ``PS_RETENTION_DAYS_DEFAULT`` (365). Assets and their aggregates are kept.
    ``mcp_tool_usage`` rows (MCP tool-call aggregates) not seen within the
    same period are purged with the events: such a row only summarises calls
    older than the cutoff.
    :func:`purge_expired_events` deletes older events in batches, committing
    after each batch so no lock is held for long. Running it twice deletes
    nothing the second time.

Erasure
    :func:`erase_tenant` removes every row that belongs to one tenant, then
    the tenant itself (GDPR right to erasure). It is batched and can be rerun
    after a partial failure.

Every statement is filtered on ``tenant_id``; nothing here can reach another
tenant's rows. Every purge that deletes rows, and every erasure, writes a
``retention_audit_log`` row with counts only.

Read paths can call :func:`retention_cutoff` (or :func:`get_retention_cutoff`)
to clamp a time window to what is still retained.

There is no scheduler in the collector. Run the purge from cron or a
Kubernetes CronJob::

    python -m collector.retention purge [--tenant UUID] [--dry-run]

or call ``POST /api/v1/operator/retention/purge`` (see ``operator_admin``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from db.models import (
    ATTESTATION_ERASE_SETTING,
    AdminToken,
    AIAsset,
    AIUsageEvent,
    AssetAttestation,
    DataFlow,
    McpToolUsage,
    PartnerAuditLog,
    PartnerCredential,
    ProviderCredential,
    ProviderKeyFingerprint,
    RetentionAuditLog,
    RiskMapping,
    TelemetryClientStats,
    Tenant,
    TenantApiKey,
    WebhookDelivery,
    WebhookSubscription,
)
from sqlalchemy import Table, delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from collector.config import settings

logger = logging.getLogger(__name__)

RETENTION_SETTING_KEY = "retention_days"
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 36500

# Tenant-owned tables, children before parents so foreign keys never block a
# delete. The tenants row itself is removed last. A test checks that every
# model with a tenant_id column is listed here, so a new table can't be
# silently left behind by an erasure.
ERASE_ORDER: tuple[type, ...] = (
    AIUsageEvent,
    DataFlow,
    RiskMapping,
    AssetAttestation,
    PartnerAuditLog,
    WebhookDelivery,
    WebhookSubscription,
    PartnerCredential,
    AIAsset,
    TelemetryClientStats,
    ProviderKeyFingerprint,
    ProviderCredential,
    McpToolUsage,
    TenantApiKey,
    AdminToken,
)

# Tables with a tenant_id that erasure keeps on purpose.
ERASE_EXEMPT: frozenset[str] = frozenset({RetentionAuditLog.__tablename__})


@dataclass
class PurgeResult:
    tenant_id: str
    retention_days: int
    cutoff: str
    events_deleted: int
    dry_run: bool = False
    mcp_tool_usage_deleted: int = 0


@dataclass
class EraseResult:
    tenant_id: str
    tenant_existed: bool
    rows_deleted: dict[str, int] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────
# Retention period helpers
# ──────────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _valid_days(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        days = value
    elif isinstance(value, str) and value.strip().isdigit():
        days = int(value)
    else:
        return None  # None, floats, junk
    if not MIN_RETENTION_DAYS <= days <= MAX_RETENTION_DAYS:
        return None
    return days


def default_retention_days() -> int:
    days = _valid_days(settings.retention_days_default)
    if days is None:
        raise ValueError(
            f"PS_RETENTION_DAYS_DEFAULT must be between {MIN_RETENTION_DAYS} "
            f"and {MAX_RETENTION_DAYS}, got {settings.retention_days_default!r}"
        )
    return days


def retention_days_for(tenant: Tenant | Mapping[str, Any] | None) -> int:
    """Retention period in days for a tenant (or its settings dict).

    An absent or invalid ``retention_days`` falls back to the default rather
    than raising, so a bad setting can never widen what gets deleted.
    """
    tenant_settings = tenant.settings if isinstance(tenant, Tenant) else tenant
    if isinstance(tenant_settings, Mapping):
        days = _valid_days(tenant_settings.get(RETENTION_SETTING_KEY))
        if days is not None:
            return days
    return default_retention_days()


def retention_cutoff(
    tenant: Tenant | Mapping[str, Any] | None,
    now: datetime | None = None,
) -> datetime:
    """Oldest timestamp still retained for this tenant (UTC, tz-aware).

    Events strictly older than this are eligible for purge; read paths can
    clamp their window start to ``max(start, retention_cutoff(tenant))``.
    """
    now = now or _utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now - timedelta(days=retention_days_for(tenant))


async def get_retention_cutoff(
    db: AsyncSession, tenant_id: UUID, now: datetime | None = None
) -> datetime | None:
    """Like :func:`retention_cutoff` but looks the tenant up. None if absent."""
    row = (
        await db.execute(select(Tenant.settings).where(Tenant.id == tenant_id))
    ).one_or_none()
    if row is None:
        return None
    return retention_cutoff(row[0] or {}, now)


# ──────────────────────────────────────────────────────────────────────
# Batched delete
# ──────────────────────────────────────────────────────────────────────


def _batch_size(batch_size: int | None) -> int:
    size = batch_size if batch_size is not None else settings.retention_batch_size
    if size < 1:
        raise ValueError("batch size must be at least 1")
    return size


async def _delete_in_batches(
    db: AsyncSession,
    table: Table,
    conditions: list,
    batch_size: int,
    *,
    erasing_tenant: UUID | None = None,
) -> int:
    """Delete rows matching ``conditions`` in batches, committing each one.

    ``DELETE ... WHERE id IN (SELECT id ... LIMIT n)`` keeps each transaction
    short, so ingest and reads are not blocked behind a long delete. Tables
    without an ``id`` column (small per-tenant counter tables) are deleted in
    one statement.

    ``erasing_tenant`` marks each delete transaction as part of that
    tenant's erasure, which the append-only trigger on
    ``asset_attestations`` requires before it allows a DELETE.
    """

    async def _mark_erasure() -> None:
        # The trigger only exists on PostgreSQL (unit tests also use SQLite).
        if erasing_tenant is not None and db.get_bind().dialect.name == "postgresql":
            await db.execute(
                text("SELECT set_config(:name, :tenant, true)"),
                {"name": ATTESTATION_ERASE_SETTING, "tenant": str(erasing_tenant)},
            )

    if "id" not in table.c:
        await _mark_erasure()
        result = await db.execute(delete(table).where(*conditions))
        await db.commit()
        return result.rowcount or 0
    total = 0
    while True:
        ids = select(table.c.id).where(*conditions).limit(batch_size)
        await _mark_erasure()
        result = await db.execute(delete(table).where(table.c.id.in_(ids)))
        await db.commit()
        deleted = result.rowcount or 0
        total += deleted
        if deleted < batch_size:
            return total


async def _count(db: AsyncSession, table: Table, conditions: list) -> int:
    return (
        await db.execute(select(func.count()).select_from(table).where(*conditions))
    ).scalar_one()


async def _audit(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    action: str,
    actor: str,
    details: dict[str, int] | None = None,
    cutoff: datetime | None = None,
) -> None:
    db.add(
        RetentionAuditLog(
            tenant_id=tenant_id,
            action=action,
            actor=actor,
            cutoff=cutoff,
            details=details or {},
        )
    )
    await db.commit()


# ──────────────────────────────────────────────────────────────────────
# Purge
# ──────────────────────────────────────────────────────────────────────


async def purge_tenant_events(
    db: AsyncSession,
    tenant: Tenant | UUID,
    *,
    now: datetime | None = None,
    batch_size: int | None = None,
    dry_run: bool = False,
    actor: str = "system",
) -> PurgeResult | None:
    """Delete one tenant's usage events older than its retention cutoff,
    and its MCP tool usage rows last seen before it.

    Returns None if the tenant does not exist. With ``dry_run`` the matching
    rows are counted, not deleted, and nothing is audited.
    """
    size = _batch_size(batch_size)
    if isinstance(tenant, Tenant):
        tenant_id, tenant_settings = tenant.id, tenant.settings or {}
    else:
        row = (
            await db.execute(
                select(Tenant.id, Tenant.settings).where(Tenant.id == tenant)
            )
        ).one_or_none()
        if row is None:
            return None
        tenant_id, tenant_settings = row[0], row[1] or {}

    days = retention_days_for(tenant_settings)
    cutoff = retention_cutoff(tenant_settings, now)
    table = AIUsageEvent.__table__
    conditions = [table.c.tenant_id == tenant_id, table.c.timestamp < cutoff]

    mcp = McpToolUsage.__table__
    mcp_conditions = [mcp.c.tenant_id == tenant_id, mcp.c.last_seen < cutoff]

    if dry_run:
        deleted = await _count(db, table, conditions)
        mcp_deleted = await _count(db, mcp, mcp_conditions)
    else:
        deleted = await _delete_in_batches(db, table, conditions, size)
        mcp_deleted = await _delete_in_batches(db, mcp, mcp_conditions, size)
        if deleted or mcp_deleted:
            details = {AIUsageEvent.__tablename__: deleted}
            if mcp_deleted:
                details[McpToolUsage.__tablename__] = mcp_deleted
            await _audit(
                db,
                tenant_id=tenant_id,
                action="purge",
                actor=actor,
                cutoff=cutoff,
                details=details,
            )
            logger.info(
                "retention purge tenant=%s cutoff=%s events_deleted=%d "
                "mcp_tool_usage_deleted=%d",
                tenant_id,
                cutoff.isoformat(),
                deleted,
                mcp_deleted,
            )

    return PurgeResult(
        tenant_id=str(tenant_id),
        retention_days=days,
        cutoff=cutoff.isoformat(),
        events_deleted=deleted,
        dry_run=dry_run,
        mcp_tool_usage_deleted=mcp_deleted,
    )


async def purge_expired_events(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    batch_size: int | None = None,
    dry_run: bool = False,
    actor: str = "system",
) -> list[PurgeResult]:
    """Run :func:`purge_tenant_events` for every tenant."""
    now = now or _utcnow()
    tenant_ids = (
        (await db.execute(select(Tenant.id).order_by(Tenant.id))).scalars().all()
    )
    results = []
    for tenant_id in tenant_ids:
        result = await purge_tenant_events(
            db, tenant_id, now=now, batch_size=batch_size, dry_run=dry_run, actor=actor
        )
        if result is not None:
            results.append(result)
    return results


# ──────────────────────────────────────────────────────────────────────
# Erasure
# ──────────────────────────────────────────────────────────────────────


async def erase_tenant(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    batch_size: int | None = None,
    actor: str = "system",
) -> EraseResult:
    """Delete every row owned by ``tenant_id``, then the tenant.

    Idempotent: erasing a tenant that is already gone deletes any stragglers
    (from a partial earlier run) and reports ``tenant_existed=False``. The
    erasure is always audited.
    """
    size = _batch_size(batch_size)
    existed = (
        await db.execute(select(Tenant.id).where(Tenant.id == tenant_id))
    ).scalar_one_or_none() is not None

    rows_deleted: dict[str, int] = {}
    for model in ERASE_ORDER:
        table = model.__table__
        rows_deleted[model.__tablename__] = await _delete_in_batches(
            db, table, [table.c.tenant_id == tenant_id], size, erasing_tenant=tenant_id
        )

    result = await db.execute(delete(Tenant).where(Tenant.id == tenant_id))
    await db.commit()
    rows_deleted[Tenant.__tablename__] = result.rowcount or 0

    await _audit(
        db, tenant_id=tenant_id, action="erase", actor=actor, details=rows_deleted
    )
    logger.warning("tenant erased tenant=%s rows_deleted=%s", tenant_id, rows_deleted)
    return EraseResult(
        tenant_id=str(tenant_id), tenant_existed=existed, rows_deleted=rows_deleted
    )


async def set_tenant_retention(
    db: AsyncSession,
    tenant_id: UUID,
    retention_days: int | None,
    *,
    actor: str = "system",
) -> int | None:
    """Set (or with None, clear) a tenant's retention override.

    Returns the effective retention in days, or None if the tenant is absent.
    """
    if retention_days is not None and _valid_days(retention_days) is None:
        raise ValueError(
            f"retention_days must be between {MIN_RETENTION_DAYS} and {MAX_RETENTION_DAYS}"
        )
    tenant = (
        await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant is None:
        return None
    new_settings = dict(tenant.settings or {})
    if retention_days is None:
        new_settings.pop(RETENTION_SETTING_KEY, None)
    else:
        new_settings[RETENTION_SETTING_KEY] = retention_days
    tenant.settings = new_settings  # reassign so the JSONB change is flushed
    await db.commit()
    effective = retention_days_for(new_settings)
    await _audit(
        db,
        tenant_id=tenant_id,
        action="set_retention",
        actor=actor,
        details={"retention_days": effective},
    )
    return effective


# ──────────────────────────────────────────────────────────────────────
# CLI: python -m collector.retention purge [--tenant UUID] [--dry-run]
# ──────────────────────────────────────────────────────────────────────


async def _run_cli(args: argparse.Namespace) -> list[dict]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from collector import rls

    engine = create_async_engine(settings.database_url)
    try:
        session_factory = async_sessionmaker(
            engine, expire_on_commit=False, info=rls.bypass_info("retention-cli")
        )
        async with session_factory() as db:
            if args.tenant:
                result = await purge_tenant_events(
                    db,
                    UUID(args.tenant),
                    batch_size=args.batch_size,
                    dry_run=args.dry_run,
                    actor="cli",
                )
                if result is None:
                    raise SystemExit(f"tenant {args.tenant} not found")
                results = [result]
            else:
                results = await purge_expired_events(
                    db, batch_size=args.batch_size, dry_run=args.dry_run, actor="cli"
                )
        return [asdict(r) for r in results]
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m collector.retention")
    sub = parser.add_subparsers(dest="command", required=True)
    purge = sub.add_parser("purge", help="delete usage events older than retention")
    purge.add_argument("--tenant", help="only this tenant id")
    purge.add_argument("--dry-run", action="store_true", help="count, don't delete")
    purge.add_argument("--batch-size", type=int, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    results = asyncio.run(_run_cli(args))
    print(json.dumps({"data": results}, indent=2))


if __name__ == "__main__":
    main()
