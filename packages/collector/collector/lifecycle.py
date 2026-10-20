"""Asset lifecycle: stale and retired detection.

An asset's `lifecycle_state` is derived from how long ago it was last
sighted by any discovery source (`last_seen`):

  - active  : seen within the last N days
  - stale   : not seen for N days
  - retired : not seen for M days, or retired by its owner (see below)

Thresholds per discovery source
  Sources report on different cadences, so N and M are set per source
  (DEFAULT_SOURCE_THRESHOLDS, stale / retire days):

    platform_signal                7 / 30   cloud inventory and provider
                                            usage refresh daily
    code_scan                     90 / 180  only runs on CI; repos can sit
                                            unchanged for months
    survey                       180 / 365  self-reported, rarely repeated

  Every other source, including sdk, gateway, otel, browser_extension and
  macos_app, uses PS_ASSET_STALE_AFTER_DAYS / PS_ASSET_RETIRE_AFTER_DAYS
  (30 / 90), as before per-source thresholds existed: a deployment upgrading
  sees no wave of runtime assets going stale on its first sweep, and an
  operator who tuned those two settings keeps them for runtime sources.
  PS_ASSET_LIFECYCLE_SOURCE_THRESHOLDS ("code_scan=60:120,sdk=14:60,...")
  overrides single sources. Explicit `stale_after_days`/`retire_after_days`
  on the sweep (endpoint or CLI) apply one threshold to every source.

  An asset seen by several sources is judged by its most recent sighting
  across all of them (`last_seen`) against the most lenient (longest)
  threshold among its sources. `last_seen` does not say which source saw
  the asset last, so the strictest threshold would flag an asset stale
  between two on-schedule runs of its slowest source (an asset a weekly CI
  scan finds would go stale whenever the SDK that also saw it went quiet
  for two weeks). The lenient rule never marks an asset stale while one of
  its sources could still be on schedule; the cost is that it goes stale
  later when only its fast source stops seeing it.

Telemetry arriving again moves a stale or retired asset back to `active`
(see `reactivate`, called from `dedup.find_or_create_asset`, the cloud
inventory and, through `record_sighting`, the provider usage pull, which
only counts usage from after the asset's last lifecycle change).

Owner-initiated retirement
  An `asset` attestation with state `retired` (collector.attestation) sets
  `lifecycle_state=retired`, `retired_by` (the attestation's attested_by)
  and `lifecycle_changed_at` (when). The sweep never touches an
  owner-retired asset and new telemetry does not reactivate it: the first
  sighting afterwards sets `retired_in_use_at` instead ("retired asset still
  in use"), which emits an `asset.changed` webhook (from ingest, OTel
  ingest, the cloud inventory and the provider usage pull) and shows in
  the delta feed as `lifecycle_change: retired_in_use`. Later sightings do
  not raise it again. The owner then either retires the asset again
  (acknowledging the use; this clears `retired_in_use_at`, so the next
  sighting raises it again) or un-retires it with any other `asset`
  attestation in state `attested` or `unreviewed`, which clears
  `retired_by` and puts the asset back in the state its `last_seen` calls
  for. `disputed` (the record is wrong) leaves the retirement in place, as
  do attestations of other fields.

  Two concurrent sightings of an owner-retired asset raise the signal once:
  `lock_for_sighting` takes the row lock first, so the second sees the
  first's `retired_in_use_at`.

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
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from db.models import AIAsset
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import TIMESTAMP, and_, case, func, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from collector.admin_auth import AdminPrincipal, require_admin
from collector.config import parse_source_thresholds, settings
from collector.webhooks import enqueue_lifecycle_events

ACTIVE = "active"
STALE = "stale"
RETIRED = "retired"
LIFECYCLE_STATES = (ACTIVE, STALE, RETIRED)
LifecycleState = Literal["active", "stale", "retired"]


# Stale / retire days per discovery source; see the module docstring.
DEFAULT_SOURCE_THRESHOLDS: dict[str, tuple[int, int]] = {
    "platform_signal": (7, 30),
    "code_scan": (90, 180),
    "survey": (180, 365),
}


@dataclass(frozen=True)
class Thresholds:
    """Stale / retire days: `default` for sources not in `by_source`."""

    default: tuple[int, int]
    by_source: dict[str, tuple[int, int]] = field(default_factory=dict)

    def for_sources(self, sources: Iterable[str] | None) -> tuple[int, int]:
        """Thresholds for an asset seen by `sources`: the most lenient."""
        known = [self.by_source.get(s, self.default) for s in sources or ()]
        if not known:
            return self.default
        return max(t[0] for t in known), max(t[1] for t in known)


@dataclass(frozen=True)
class SweepResult:
    # The default thresholds (sources without their own, or every source
    # when explicit thresholds were given).
    stale_after_days: int
    retire_after_days: int
    marked_stale: int
    marked_retired: int
    reactivated: int
    swept_at: str
    # {source: {"stale_after_days": n, "retire_after_days": m}}; empty when
    # explicit thresholds applied one value to every source.
    source_thresholds: dict[str, dict[str, int]] = field(default_factory=dict)


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


def resolve_source_thresholds(
    stale_after_days: int | None = None,
    retire_after_days: int | None = None,
) -> Thresholds:
    """Thresholds for a sweep. Explicit days apply to every source;
    otherwise the per-source defaults, overridden by
    PS_ASSET_LIFECYCLE_SOURCE_THRESHOLDS. Raises ValueError."""
    default = resolve_thresholds(stale_after_days, retire_after_days)
    if stale_after_days is not None or retire_after_days is not None:
        return Thresholds(default=default)
    by_source = dict(DEFAULT_SOURCE_THRESHOLDS)
    by_source.update(parse_source_thresholds(settings.asset_lifecycle_source_thresholds))
    return Thresholds(default=default, by_source=by_source)


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
    if its state changed.

    An owner-retired asset stays retired; its first sighting since the
    owner retired it sets `retired_in_use_at` instead.
    """
    if asset.retired_by is not None:
        if asset.retired_in_use_at is None:
            asset.retired_in_use_at = now
        return False
    if asset.lifecycle_state in (None, ACTIVE):
        return False
    asset.lifecycle_state = ACTIVE
    asset.lifecycle_changed_at = now
    return True


def record_sighting(asset: AIAsset, seen_at: datetime, now: datetime) -> bool:
    """A source reports that `asset` was in use at `seen_at`. Ingest and the
    cloud inventory see assets live (`seen_at` is now); the provider usage
    pull reads past usage buckets, so `seen_at` can lie well before now and
    a re-pull reports the same buckets again.

    Applies `reactivate`'s rules, but only to use that happened after the
    asset's last lifecycle change (`lifecycle_changed_at`): usage from
    before an owner retired the asset is not "still in use", and usage
    from before the sweep marked it stale or retired was already counted
    by that sweep. Returns True if `lifecycle_state` changed. Does not
    touch `last_seen`; the caller keeps that.
    """
    changed_at = asset.lifecycle_changed_at
    if changed_at is not None and seen_at <= changed_at:
        return False
    return reactivate(asset, now)


async def lock_assets(db: AsyncSession, asset_ids: Iterable[UUID]) -> None:
    """Lock the rows of `asset_ids` in id order and reload them.

    Id order is the one order every writer that updates several assets in
    a transaction takes their row locks in (ingest via
    `dedup.lock_asset_keys`, the cloud inventory and the provider usage
    pull), so two of them touching the same assets wait for each other
    instead of deadlocking. populate_existing: updates build on the locked
    row, not on a version read before the lock (this also does what
    `lock_for_sighting` does for each of them).
    """
    ids = sorted(set(asset_ids))
    if not ids:
        return
    await db.execute(
        select(AIAsset)
        .where(AIAsset.id.in_(ids))
        .order_by(AIAsset.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def lock_for_sighting(db: AsyncSession, asset: AIAsset) -> None:
    """Before recording a sighting of a loaded asset: if it is owner-retired
    and the "still in use" signal is not raised yet, lock its row and reload
    its lifecycle columns, so concurrent sightings raise it once (the second
    waits, then sees `retired_in_use_at` set). Other assets take no lock."""
    if asset.retired_by is None or asset.retired_in_use_at is not None:
        return
    await db.refresh(
        asset,
        attribute_names=[
            "lifecycle_state",
            "lifecycle_changed_at",
            "retired_by",
            "retired_in_use_at",
        ],
        with_for_update=True,
    )


def owner_retire(asset: AIAsset, retired_by: str, now: datetime) -> bool:
    """Retire an asset on its owner's word (an `asset` attestation with
    state `retired`). Returns True if it was not owner-retired before.

    Retiring it again acknowledges a "still in use" signal: it clears
    `retired_in_use_at`, so the next sighting raises it again.
    """
    newly = asset.retired_by is None
    asset.lifecycle_state = RETIRED
    asset.lifecycle_changed_at = now
    asset.retired_by = retired_by
    asset.retired_in_use_at = None
    return newly


def owner_unretire(
    asset: AIAsset, now: datetime, thresholds: Thresholds | None = None
) -> bool:
    """Undo an owner retirement: the asset goes back under the sweep's
    rules, in the state its `last_seen` calls for. Returns True if
    `lifecycle_state` changed. No-op unless the asset is owner-retired."""
    if asset.retired_by is None:
        return False
    asset.retired_by = None
    asset.retired_in_use_at = None
    stale, retire = (thresholds or resolve_source_thresholds()).for_sources(
        asset.discovery_source
    )
    state = compute_lifecycle_state(asset.last_seen, now, stale, retire)
    if state == asset.lifecycle_state:
        return False
    asset.lifecycle_state = state
    asset.lifecycle_changed_at = now
    return True


def lifecycle_change_for(asset: AIAsset, since: datetime) -> str | None:
    """Lifecycle transition to report in the delta feed for `asset`, or
    None if its lifecycle state has not changed since `since`.

    `retired_in_use` (an owner-retired asset was sighted again) wins over
    the retirement itself when both fall in the window.
    """
    in_use_at = asset.retired_in_use_at
    if asset.retired_by is not None and in_use_at is not None and in_use_at >= since:
        return "retired_in_use"
    changed_at = asset.lifecycle_changed_at
    if changed_at is None or changed_at < since:
        return None
    if asset.lifecycle_state == ACTIVE:
        return "reactivated"
    return asset.lifecycle_state


def _cutoff(thresholds: Thresholds, now: datetime, index: int):
    """`last_seen` cutoff for stale (index 0) or retired (index 1): a
    datetime when every source shares one threshold, else a per-row SQL
    expression (the most lenient threshold among the row's sources)."""
    default = thresholds.default[index]
    if not thresholds.by_source:
        return now - timedelta(days=default)
    source = func.unnest(AIAsset.discovery_source).column_valued("source")
    per_source = case(
        {s: days[index] for s, days in thresholds.by_source.items()},
        value=source,
        else_=default,
    )
    days = func.coalesce(select(func.max(per_source)).scalar_subquery(), default)
    return literal(now, TIMESTAMP(timezone=True)) - func.make_interval(0, 0, 0, days)


async def sweep_asset_lifecycle(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    stale_after_days: int | None = None,
    retire_after_days: int | None = None,
    tenant_id: UUID | None = None,
) -> SweepResult:
    """Move assets between active, stale and retired based on `last_seen`
    and the thresholds of their discovery sources.

    Owner-retired assets are left alone. Scoped to one tenant when
    `tenant_id` is given, otherwise all tenants. Does not commit; the
    caller owns the transaction.
    """
    thresholds = resolve_source_thresholds(stale_after_days, retire_after_days)
    now = now or datetime.now(UTC)
    stale_cutoff = _cutoff(thresholds, now, 0)
    retire_cutoff = _cutoff(thresholds, now, 1)

    scope = [AIAsset.tenant_id == tenant_id] if tenant_id is not None else []
    scope.append(AIAsset.retired_by.is_(None))

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
        stale_after_days=thresholds.default[0],
        retire_after_days=thresholds.default[1],
        marked_stale=stale,
        marked_retired=retired,
        reactivated=reactivated,
        swept_at=now.isoformat(),
        source_thresholds={
            s: {"stale_after_days": d[0], "retire_after_days": d[1]}
            for s, d in sorted(thresholds.by_source.items())
        },
    )


# ──────────────────────────────────────────────────────────────────────
# Admin endpoint
# ──────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/v1/admin", tags=["Admin"])

_REGISTRY = Depends(require_admin("admin:registry"))


@router.post(
    "/asset-lifecycle/sweep",
    summary="Mark the tenant's assets stale or retired based on last telemetry",
    description=(
        "Thresholds are per discovery source unless stale_after_days or "
        "retire_after_days is given, which applies one threshold to every "
        "source. Owner-retired assets are not changed."
    ),
)
async def run_sweep(
    request: Request,
    stale_after_days: int | None = Query(default=None, ge=1),
    retire_after_days: int | None = Query(default=None, ge=2),
    principal: AdminPrincipal = _REGISTRY,
) -> dict:
    tenant_id = principal.tenant_id
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

    from collector import rls

    engine = create_async_engine(settings.database_url)
    try:
        session_factory = async_sessionmaker(
            engine, class_=AsyncSession, info=rls.bypass_info("lifecycle-cli")
        )
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
        resolve_source_thresholds(args.stale_after_days, args.retire_after_days)
    except ValueError as exc:
        parser.error(str(exc))
    result = asyncio.run(_run_cli(args.stale_after_days, args.retire_after_days))
    print(
        f"lifecycle sweep: {result.marked_stale} stale, {result.marked_retired} retired, "
        f"{result.reactivated} reactivated ("
        + ("per-source thresholds, default " if result.source_thresholds else "")
        + f"stale after {result.stale_after_days}d, "
        f"retired after {result.retire_after_days}d)"
    )


if __name__ == "__main__":
    main()
