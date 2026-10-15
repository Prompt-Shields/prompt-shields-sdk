"""Asset lifecycle: stale and retired detection.

An asset's `lifecycle_state` is derived from how long ago telemetry last
arrived for it (`last_seen`):

  - active  : seen within the last N days
  - stale   : no telemetry for N days (PS_ASSET_STALE_AFTER_DAYS, default 30)
  - retired : no telemetry for M days (PS_ASSET_RETIRE_AFTER_DAYS, default 90)

Telemetry arriving again moves a stale or retired asset back to `active`
(see `reactivate`, called from `dedup.find_or_create_asset`).

The collector has no scheduler, so the sweep is exposed three ways:

  - `sweep_asset_lifecycle(db, ...)`: call it from any periodic job.
  - `POST /api/v1/admin/asset-lifecycle/sweep`: sweeps the caller's tenant.
  - `python -m collector.lifecycle`: sweeps every tenant; run it from cron or
    a Kubernetes CronJob, e.g. once a day.

The sweep is idempotent: running it twice in a row changes nothing the
second time. Every transition sets `lifecycle_changed_at` and bumps
`updated_at`, so it shows up in the Partner API delta feed (`/changes`).
The endpoint and CLI also queue partner webhooks for the transitions
(`collector.webhooks.enqueue_lifecycle_events`).
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from db.models import AIAsset
from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import and_, update
from sqlalchemy.ext.asyncio import AsyncSession

from collector.auth import resolve_tenant
from collector.config import settings
from collector.webhooks import enqueue_lifecycle_events

ACTIVE = "active"
STALE = "stale"
RETIRED = "retired"
LIFECYCLE_STATES = (ACTIVE, STALE, RETIRED)
LifecycleState = Literal["active", "stale", "retired"]


@dataclass(frozen=True)
class SweepResult:
    stale_after_days: int
    retire_after_days: int
    marked_stale: int
    marked_retired: int
    reactivated: int
    swept_at: str


def resolve_thresholds(
    stale_after_days: int | None = None,
    retire_after_days: int | None = None,
) -> tuple[int, int]:
    """Fill in defaults from settings and validate. Raises ValueError."""
    stale = (
        settings.asset_stale_after_days
        if stale_after_days is None
        else stale_after_days
    )
    retire = (
        settings.asset_retire_after_days
        if retire_after_days is None
        else retire_after_days
    )
    if stale < 1:
        raise ValueError("stale_after_days must be at least 1")
    if retire <= stale:
        raise ValueError("retire_after_days must be greater than stale_after_days")
    return stale, retire


def compute_lifecycle_state(
    last_seen: datetime | None,
    now: datetime,
    stale_after_days: int,
    retire_after_days: int,
) -> str:
    """Lifecycle state an asset should be in, given when it was last seen."""
    if last_seen is None:
        return ACTIVE
    age = now - last_seen
    if age >= timedelta(days=retire_after_days):
        return RETIRED
    if age >= timedelta(days=stale_after_days):
        return STALE
    return ACTIVE


def reactivate(asset: AIAsset, now: datetime) -> bool:
    """Mark an asset active again because telemetry arrived. Returns True
    if its state changed."""
    if asset.lifecycle_state in (None, ACTIVE):
        return False
    asset.lifecycle_state = ACTIVE
    asset.lifecycle_changed_at = now
    return True


def lifecycle_change_for(asset: AIAsset, since: datetime) -> str | None:
    """Lifecycle transition to report in the delta feed for `asset`, or
    None if its lifecycle state has not changed since `since`."""
    changed_at = asset.lifecycle_changed_at
    if changed_at is None or changed_at < since:
        return None
    if asset.lifecycle_state == ACTIVE:
        return "reactivated"
    return asset.lifecycle_state


async def sweep_asset_lifecycle(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    stale_after_days: int | None = None,
    retire_after_days: int | None = None,
    tenant_id: UUID | None = None,
) -> SweepResult:
    """Move assets between active, stale and retired based on `last_seen`.

    Scoped to one tenant when `tenant_id` is given, otherwise all tenants.
    Does not commit; the caller owns the transaction.
    """
    stale_days, retire_days = resolve_thresholds(stale_after_days, retire_after_days)
    now = now or datetime.now(UTC)
    stale_cutoff = now - timedelta(days=stale_days)
    retire_cutoff = now - timedelta(days=retire_days)

    scope = [AIAsset.tenant_id == tenant_id] if tenant_id is not None else []

    async def _move(target: str, *window) -> int:
        stmt = (
            update(AIAsset)
            .where(and_(*scope, *window, AIAsset.lifecycle_state != target))
            .values(lifecycle_state=target, lifecycle_changed_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        return (await db.execute(stmt)).rowcount or 0

    retired = await _move(RETIRED, AIAsset.last_seen <= retire_cutoff)
    stale = await _move(
        STALE, AIAsset.last_seen <= stale_cutoff, AIAsset.last_seen > retire_cutoff
    )
    reactivated = await _move(ACTIVE, AIAsset.last_seen > stale_cutoff)

    return SweepResult(
        stale_after_days=stale_days,
        retire_after_days=retire_days,
        marked_stale=stale,
        marked_retired=retired,
        reactivated=reactivated,
        swept_at=now.isoformat(),
    )


# ──────────────────────────────────────────────────────────────────────
# Admin endpoint
# ──────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/v1/admin", tags=["Admin"])


@router.post(
    "/asset-lifecycle/sweep",
    summary="Mark the tenant's assets stale or retired based on last telemetry",
)
async def run_sweep(
    request: Request,
    stale_after_days: int | None = Query(default=None, ge=1),
    retire_after_days: int | None = Query(default=None, ge=2),
) -> dict:
    tenant_id = await resolve_tenant(request)
    db = request.state.db
    now = datetime.now(UTC)
    try:
        result = await sweep_asset_lifecycle(
            db,
            now=now,
            tenant_id=tenant_id,
            stale_after_days=stale_after_days,
            retire_after_days=retire_after_days,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "type": "about:blank",
                "title": "Bad Request",
                "status": 400,
                "detail": str(exc),
            },
        ) from exc
    await enqueue_lifecycle_events(db, changed_at=now, tenant_id=tenant_id)
    await db.commit()
    return asdict(result)


# ──────────────────────────────────────────────────────────────────────
# CLI: python -m collector.lifecycle
# ──────────────────────────────────────────────────────────────────────


async def _run_cli(
    stale_after_days: int | None, retire_after_days: int | None
) -> SweepResult:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(settings.database_url)
    try:
        session_factory = async_sessionmaker(engine, class_=AsyncSession)
        async with session_factory() as db:
            now = datetime.now(UTC)
            result = await sweep_asset_lifecycle(
                db,
                now=now,
                stale_after_days=stale_after_days,
                retire_after_days=retire_after_days,
            )
            await enqueue_lifecycle_events(db, changed_at=now)
            await db.commit()
            return result
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m collector.lifecycle",
        description="Mark AI assets stale or retired across all tenants.",
    )
    parser.add_argument("--stale-after-days", type=int, default=None)
    parser.add_argument("--retire-after-days", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        resolve_thresholds(args.stale_after_days, args.retire_after_days)
    except ValueError as exc:
        parser.error(str(exc))
    result = asyncio.run(_run_cli(args.stale_after_days, args.retire_after_days))
    print(
        f"lifecycle sweep: {result.marked_stale} stale, {result.marked_retired} retired, "
        f"{result.reactivated} reactivated "
        f"(stale after {result.stale_after_days}d, retired after {result.retire_after_days}d)"
    )


if __name__ == "__main__":
    main()
