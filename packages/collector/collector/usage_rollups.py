"""Usage rollups and the tenant-wide usage time series (#346).

Raw ``ai_usage_events`` are purged at the tenant's retention period, and
reading months of them per request does not scale. Two rollup tables keep
the aggregates instead, for PS_USAGE_ROLLUP_RETENTION_DAYS (default 760,
about 25 months), separately from the raw events:

* ``usage_rollup_hourly``: one row per (tenant, UTC hour, asset, model,
  provider, API surface, user id hash) with request, token, cost, cache and
  error counts, computed from the raw events of that hour.
* ``usage_rollup_daily``: the same per UTC day, the sum of the day's hourly
  rows.

Maintenance is incremental and exact. Each run, per tenant and under that
tenant's RLS context, finds the hours that hold events ingested since its
last run (``ai_usage_events.ingested_at`` > watermark - overlap), deletes
those hours' rollup rows and recomputes them from every raw event of the
hour, then does the same for the days those hours fall in from the hourly
rows. Recomputing a bucket is idempotent, so the overlap (which covers
ingest transactions still open at the last run) and overlapping runs cost
work but never double counts; a per-tenant advisory lock makes a second
concurrent run (another replica) skip the tenant. The work is committed
BATCH_DAYS days of touched hours at a time, re-taking the lock for each
step, so a large catch-up (the first run after the migration) holds no long
transaction; the watermark only moves once every step is done. An ingest
transaction open longer than the overlap (5 minutes) when a run lists the
touched hours can be missed until a later event lands in the same hour or a
``rebuild``. A bucket is only recomputed while every
event of it is still in raw retention (it starts at or after the cutoff),
so a purge never shrinks a rollup: rollups survive the raw purge. For days
still in raw retention the daily rollup equals the raw events to the token.

What the rollups do not follow on their own: deleting raw events other
than by the retention purge (an asset merge repoints the rollups itself;
erasing an end user nulls their ``user_id_hash`` here too; erasing a tenant
deletes them). ``python -m collector.usage_rollups
rebuild --tenant <id>`` recomputes every bucket still in raw retention.

The loop runs in the collector's worker set every
PS_USAGE_ROLLUP_INTERVAL_SECONDS (default 30; 0 turns it off, then run
``python -m collector.usage_rollups refresh --all-tenants`` from cron).

Group by user uses ``user_id_hash`` only: the rollups never hold a raw user
id, and the tenant's salt never leaves ``tenant_secrets``. Group by team is
not available yet: it needs principals (#338).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from db.models import (
    AIAsset,
    Tenant,
    UsageRollupDaily,
    UsageRollupHourly,
    UsageRollupState,
)
from sqlalchemy import Text, bindparam, delete, func, literal, select, text
from sqlalchemy.dialects.postgresql import ARRAY, TIMESTAMP
from sqlalchemy.ext.asyncio import AsyncSession

from collector import rls
from collector.ingest import CACHE_HIT_STATUSES
from collector.retention import retention_cutoff, rollup_cutoff

logger = logging.getLogger(__name__)

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
# Events ingested this long before the last run's start are looked at
# again: an ingest transaction open at that moment commits rows stamped
# before it. Recomputing is idempotent, so this only costs work.
OVERLAP = timedelta(minutes=5)
# Buckets recomputed per statement.
CHUNK = 168
LIST_BYPASS_REASON = "usage-rollup-job"
LOCK_KEY = "ps_usage_rollup"
# Days of touched hours recomputed per transaction. A large catch-up (the
# first run after the migration backfills all raw events still kept) is
# committed in these steps, so no transaction holds the lock for long.
BATCH_DAYS = 7
# Key of the rollup rows in a user erasure's rows_erased.
ROWS_KEY = "usage_rollups"

GroupBy = Literal["none", "user", "project", "model", "provider", "api_surface", "asset"]
Granularity = Literal["hour", "day", "week"]
ExportFormat = Literal["csv", "jsonl"]

# Longest window one request may span, per granularity.
MAX_RANGE_DAYS: dict[str, int] = {"hour": 93, "day": 800, "week": 800}
DEFAULT_RANGE_DAYS: dict[str, int] = {"hour": 2, "day": 30, "week": 182}
MAX_PAGE = 10000
EXPORT_MAX_ROWS = 100000

COUNT_COLUMNS = (
    "requests",
    "tokens_in",
    "tokens_out",
    "tokens_cached",
    "tokens_cache_write",
    "tokens_reasoning",
    "cost_requests",
    "cache_lookups",
    "cache_hits",
    "attempts_reported",
    "error_requests",
)
SUM_COLUMNS = (*COUNT_COLUMNS, "cost", "cache_saved_cost")
DIMENSIONS = ("asset_id", "model", "provider", "api_surface", "user_id_hash")


def _ceil_hour(moment: datetime) -> datetime:
    floor = moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return floor if floor == moment else floor + HOUR


def _day_of(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _chunks(items: Sequence[datetime]) -> Iterable[list[datetime]]:
    for i in range(0, len(items), CHUNK):
        yield list(items[i : i + CHUNK])


# ──────────────────────────────────────────────────────────────────────
# Maintenance
# ──────────────────────────────────────────────────────────────────────

_DIMS_SQL = ", ".join(DIMENSIONS)
_COUNTS_SQL = ", ".join(SUM_COLUMNS)

# Every raw event of the given hours, aggregated. The error side counts
# events whose last attempt reported an HTTP status of 400 or more; the
# jsonb_typeof guards keep a malformed value from failing the statement.
_HOURLY_INSERT = text(
    f"""
    INSERT INTO usage_rollup_hourly (tenant_id, bucket_start, {_DIMS_SQL}, {_COUNTS_SQL})
    SELECT e.tenant_id, h.bucket, e.asset_id,
           coalesce(e.served_model, e.requested_model), e.served_provider,
           e.api_surface, e.user_id_hash,
           count(*),
           coalesce(sum(e.tokens_in), 0),
           coalesce(sum(e.tokens_out), 0),
           coalesce(sum(e.tokens_cached), 0),
           coalesce(sum(e.tokens_cache_write), 0),
           coalesce(sum(e.tokens_reasoning), 0),
           count(e.cost),
           count(e.cache_status),
           count(*) FILTER (WHERE e.cache_status = ANY(:hits)),
           count(*) FILTER (
               WHERE jsonb_typeof(e.attempts) = 'array' AND e.attempts <> '[]'::jsonb
           ),
           count(*) FILTER (
               WHERE (CASE WHEN jsonb_typeof(e.attempts) = 'array'
                           AND jsonb_typeof(e.attempts -> -1 -> 'status') = 'number'
                      THEN (e.attempts -> -1 ->> 'status')::numeric END) >= 400
           ),
           coalesce(sum(e.cost), 0),
           coalesce(sum(e.cache_saved_cost), 0)
    FROM unnest(:buckets) AS h(bucket)
    JOIN ai_usage_events e
      ON e.tenant_id = :tenant
     AND e.timestamp >= h.bucket AND e.timestamp < h.bucket + interval '1 hour'
    GROUP BY e.tenant_id, h.bucket, e.asset_id,
             coalesce(e.served_model, e.requested_model), e.served_provider,
             e.api_surface, e.user_id_hash
    """
).bindparams(
    bindparam("buckets", type_=ARRAY(TIMESTAMP(timezone=True))),
    bindparam("hits", type_=ARRAY(Text)),
)

_DAILY_INSERT = text(
    f"""
    INSERT INTO usage_rollup_daily (tenant_id, bucket_start, {_DIMS_SQL}, {_COUNTS_SQL})
    SELECT r.tenant_id, d.bucket, {", ".join("r." + c for c in DIMENSIONS)},
           {", ".join(f"sum(r.{c})" for c in SUM_COLUMNS)}
    FROM unnest(:buckets) AS d(bucket)
    JOIN usage_rollup_hourly r
      ON r.tenant_id = :tenant
     AND r.bucket_start >= d.bucket AND r.bucket_start < d.bucket + interval '1 day'
    GROUP BY r.tenant_id, d.bucket, {", ".join("r." + c for c in DIMENSIONS)}
    """
).bindparams(bindparam("buckets", type_=ARRAY(TIMESTAMP(timezone=True))))


@dataclass
class RefreshResult:
    tenant_id: str
    skipped: bool = False  # another run held the tenant's lock
    hours: int = 0
    days: int = 0
    watermark: str | None = None


async def _try_lock(db: AsyncSession, tenant_id: UUID) -> bool:
    return bool(
        (
            await db.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:k), hashtext(:t))"),
                {"k": LOCK_KEY, "t": str(tenant_id)},
            )
        ).scalar()
    )


async def lock_tenant(db: AsyncSession, tenant_id: UUID) -> None:
    """Wait for the tenant's rollup lock, held to the end of the transaction.
    A writer that changes rollup rows outside the job (user erasure) takes
    it, so a job transaction that read raw events before that writer's
    change cannot commit rows that undo it. No-op off PostgreSQL."""
    if db.get_bind().dialect.name != "postgresql":
        return
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k), hashtext(:t))"),
        {"k": LOCK_KEY, "t": str(tenant_id)},
    )


async def erase_user_hash(
    db: AsyncSession, tenant_id: UUID, user_id_hash: str, batch_size: int
) -> int:
    """Set ``user_id_hash`` to NULL on the tenant's hourly and daily rollup
    rows of one user (erasure by end user, #353), in batches, each under
    the tenant's rollup lock; commits. The totals stay; the rows no longer
    say whose they were. Call it after the user's raw events are erased."""
    total = 0
    for model in (UsageRollupHourly, UsageRollupDaily):
        t = model.__table__
        match = (t.c.tenant_id == tenant_id, t.c.user_id_hash == user_id_hash)
        while True:
            await lock_tenant(db, tenant_id)
            ids = select(t.c.id).where(*match).limit(batch_size)
            changed = (
                await db.execute(t.update().where(t.c.id.in_(ids)).values(user_id_hash=None))
            ).rowcount or 0
            await db.commit()
            total += changed
            if changed < batch_size:
                break
    return total


async def _touched_hours(
    db: AsyncSession, tenant_id: UUID, since: datetime | None, floor: datetime
) -> list[datetime]:
    sql = (
        "SELECT DISTINCT date_trunc('hour', timestamp, 'UTC') AS h "
        "FROM ai_usage_events WHERE tenant_id = :tenant AND timestamp >= :floor"
    )
    params: dict[str, Any] = {"tenant": tenant_id, "floor": floor}
    if since is not None:
        sql += " AND ingested_at > :since"
        params["since"] = since
    rows = (await db.execute(text(sql + " ORDER BY h"), params)).scalars().all()
    return list(rows)


def _day_batches(hours: Sequence[datetime]) -> list[list[datetime]]:
    """`hours` split into lists of whole UTC days, BATCH_DAYS days each."""
    by_day: dict[datetime, list[datetime]] = {}
    for h in sorted(set(hours)):
        by_day.setdefault(_day_of(h), []).append(h)
    days = list(by_day.values())
    return [
        [h for day in days[i : i + BATCH_DAYS] for h in day]
        for i in range(0, len(days), BATCH_DAYS)
    ]


async def recompute(
    db: AsyncSession, tenant_id: UUID, hours: Sequence[datetime], extra_days: Iterable[datetime] = ()
) -> tuple[int, int]:
    """Recompute the hourly rows of `hours` from raw events, then the daily
    rows of their days (and `extra_days`) from the hourly rows."""
    hours = sorted(set(hours))
    hourly = UsageRollupHourly.__table__
    daily = UsageRollupDaily.__table__
    for chunk in _chunks(hours):
        await db.execute(
            delete(hourly).where(hourly.c.tenant_id == tenant_id, hourly.c.bucket_start.in_(chunk))
        )
        await db.execute(
            _HOURLY_INSERT,
            {"tenant": tenant_id, "buckets": chunk, "hits": list(CACHE_HIT_STATUSES)},
        )
    days = sorted({_day_of(h) for h in hours} | {_day_of(d) for d in extra_days})
    for chunk in _chunks(days):
        await db.execute(
            delete(daily).where(daily.c.tenant_id == tenant_id, daily.c.bucket_start.in_(chunk))
        )
        await db.execute(_DAILY_INSERT, {"tenant": tenant_id, "buckets": chunk})
    return len(hours), len(days)


async def _tenant_settings(db: AsyncSession, tenant_id: UUID) -> dict | None:
    row = (
        await db.execute(select(Tenant.settings).where(Tenant.id == tenant_id))
    ).one_or_none()
    return None if row is None else (row[0] or {})


async def _save_watermark(db: AsyncSession, tenant_id: UUID, watermark: datetime) -> None:
    await db.execute(
        text(
            "INSERT INTO usage_rollup_state (tenant_id, watermark, updated_at) "
            "VALUES (:t, :w, now()) ON CONFLICT (tenant_id) "
            "DO UPDATE SET watermark = greatest(usage_rollup_state.watermark, "
            "excluded.watermark), updated_at = now()"
        ),
        {"t": tenant_id, "w": watermark},
    )


def _floor(tenant_settings: dict, now: datetime) -> datetime:
    """Start of the oldest hour whose events are all still in raw retention.
    (Rollup retention is never shorter than raw retention.)"""
    return _ceil_hour(retention_cutoff(tenant_settings, now))


async def refresh_tenant(db: AsyncSession, tenant_id: UUID) -> RefreshResult:
    """One incremental run for one tenant; commits. The session must be
    scoped to the tenant (rls.set_tenant) or bypass RLS."""
    result = RefreshResult(tenant_id=str(tenant_id))
    if not await _try_lock(db, tenant_id):
        await db.rollback()
        result.skipped = True
        return result
    tenant_settings = await _tenant_settings(db, tenant_id)
    if tenant_settings is None:
        await db.rollback()
        result.skipped = True
        return result
    # The database's clock, so app and database clocks need not agree.
    now = (await db.execute(text("SELECT now()"))).scalar_one()
    previous = (
        await db.execute(
            select(UsageRollupState.watermark).where(UsageRollupState.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    since = previous - OVERLAP if previous is not None else None
    hours = await _touched_hours(db, tenant_id, since, _floor(tenant_settings, now))
    batches = _day_batches(hours)
    for i, batch in enumerate(batches):
        if i:
            # Commit each step; take the lock again for the next one. If
            # another run holds it now, stop: that run starts from the
            # saved watermark, which this one has not moved.
            await db.commit()
            if not await _try_lock(db, tenant_id):
                await db.rollback()
                result.skipped = True
                return result
        h, d = await recompute(db, tenant_id, batch)
        result.hours += h
        result.days += d
    await _save_watermark(db, tenant_id, now)
    await db.commit()
    result.watermark = now.isoformat()
    return result


async def rebuild_tenant(db: AsyncSession, tenant_id: UUID) -> RefreshResult:
    """Recompute every rollup bucket still in raw retention; commits."""
    result = RefreshResult(tenant_id=str(tenant_id))
    if not await _try_lock(db, tenant_id):
        await db.rollback()
        result.skipped = True
        return result
    tenant_settings = await _tenant_settings(db, tenant_id)
    if tenant_settings is None:
        await db.rollback()
        result.skipped = True
        return result
    now = (await db.execute(text("SELECT now()"))).scalar_one()
    floor = _floor(tenant_settings, now)
    hourly = UsageRollupHourly.__table__
    dropped = (
        await db.execute(
            delete(hourly)
            .where(hourly.c.tenant_id == tenant_id, hourly.c.bucket_start >= floor)
            .returning(hourly.c.bucket_start)
        )
    ).scalars().all()
    hours = await _touched_hours(db, tenant_id, None, floor)
    result.hours, result.days = await recompute(db, tenant_id, hours, extra_days=dropped)
    await _save_watermark(db, tenant_id, now)
    await db.commit()
    result.watermark = now.isoformat()
    return result


def list_session_factory(session_factory):
    """Sessions that bypass RLS, used only to list tenants. Logged once."""
    info = rls.bypass_info(LIST_BYPASS_REASON)
    return lambda: session_factory(info=info)


async def refresh_all_tenants(session_factory, *, list_factory=None, rebuild: bool = False) -> list[dict]:
    """Run every enabled tenant, each in its own session under its own RLS
    context. A tenant that fails is reported and does not stop the others."""
    list_factory = list_factory or list_session_factory(session_factory)
    async with list_factory() as db:
        tenants = (
            await db.execute(select(Tenant.id).where(Tenant.disabled_at.is_(None)))
        ).scalars().all()
    run = rebuild_tenant if rebuild else refresh_tenant
    results: list[dict] = []
    for tenant_id in sorted(tenants, key=str):
        try:
            async with session_factory() as db:
                await rls.set_tenant(db, tenant_id)
                results.append(asdict(await run(db, tenant_id)))
        except Exception as exc:  # noqa: BLE001 - one tenant must not stop the rest
            logger.error("usage rollup failed for tenant %s: %s", tenant_id, type(exc).__name__)
            results.append({"tenant_id": str(tenant_id), "error": type(exc).__name__})
    return results


async def run_worker(session_factory, interval: float) -> None:
    """Refresh every tenant's rollups forever, every `interval` seconds."""
    list_factory = list_session_factory(session_factory)
    while True:
        try:
            await refresh_all_tenants(session_factory, list_factory=list_factory)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("usage rollup run failed")
        await asyncio.sleep(interval)


# ──────────────────────────────────────────────────────────────────────
# Time series
# ──────────────────────────────────────────────────────────────────────


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"o:{offset}".encode()).decode().rstrip("=")


def decode_cursor(raw: str | None) -> int:
    """Offset from a cursor; ValueError if it is not one of ours."""
    if not raw:
        return 0
    try:
        text_ = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("invalid cursor") from exc
    if not text_.startswith("o:") or not text_[2:].isdigit() or len(text_) > 12:
        raise ValueError("invalid cursor")
    return int(text_[2:])


def floor_bucket(moment: datetime, granularity: str) -> datetime:
    moment = moment.astimezone(UTC)
    if granularity == "hour":
        return moment.replace(minute=0, second=0, microsecond=0)
    day = _day_of(moment)
    if granularity == "week":
        return day - timedelta(days=day.weekday())  # ISO week, Monday
    return day


def ceil_bucket(moment: datetime, granularity: str) -> datetime:
    floor = floor_bucket(moment, granularity)
    if floor == moment:
        return floor
    width = {"hour": HOUR, "day": DAY, "week": timedelta(days=7)}[granularity]
    return floor + width


def series_query(
    tenant_id: UUID, group_by: str, granularity: str, start: datetime, end: datetime
):
    """Aggregate rollup rows into (bucket_start, key) buckets."""
    model = UsageRollupHourly if granularity == "hour" else UsageRollupDaily
    t = model.__table__
    if granularity == "week":
        bucket = func.date_trunc("week", t.c.bucket_start, "UTC")
    else:
        bucket = t.c.bucket_start
    joins = t
    if group_by == "project":
        a = AIAsset.__table__
        joins = t.outerjoin(a, (a.c.id == t.c.asset_id) & (a.c.tenant_id == t.c.tenant_id))
        key = a.c.use_case_name
    elif group_by == "user":
        key = t.c.user_id_hash
    elif group_by == "asset":
        key = t.c.asset_id.cast(Text)
    elif group_by in ("model", "provider", "api_surface"):
        key = t.c[group_by]
    else:
        key = literal(None, Text)
    bucket = bucket.label("bucket_start")
    key = key.label("key")
    return (
        select(bucket, key, *(func.sum(t.c[c]).label(c) for c in SUM_COLUMNS))
        .select_from(joins)
        .where(t.c.tenant_id == tenant_id, t.c.bucket_start >= start, t.c.bucket_start < end)
        .group_by(bucket, key)
        .order_by(bucket, key.asc().nulls_first())
    )


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def series_point(row: Any) -> dict:
    """One time-series bucket. Cost is null when no event in it reported a
    cost; cache_saved_usd and cache_hit_rate when none reported a cache
    lookup; error_rate when none reported per-attempt metadata."""
    count = {c: int(row[c] or 0) for c in COUNT_COLUMNS}
    return {
        "bucket_start": row["bucket_start"].astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "key": row["key"],
        "requests": count["requests"],
        "tokens_in": count["tokens_in"],
        "tokens_out": count["tokens_out"],
        "tokens_cached": count["tokens_cached"],
        "tokens_cache_write": count["tokens_cache_write"],
        "tokens_reasoning": count["tokens_reasoning"],
        "cost_usd": round(float(row["cost"]), 6) if count["cost_requests"] else None,
        "cache_saved_usd": (
            round(float(row["cache_saved_cost"]), 6) if count["cache_lookups"] else None
        ),
        "cache_hit_rate": _ratio(count["cache_hits"], count["cache_lookups"]),
        "error_rate": _ratio(count["error_requests"], count["attempts_reported"]),
    }


SERIES_COLUMNS = (
    "bucket_start",
    "key",
    "requests",
    "tokens_in",
    "tokens_out",
    "tokens_cached",
    "tokens_cache_write",
    "tokens_reasoning",
    "cost_usd",
    "cache_saved_usd",
    "cache_hit_rate",
    "error_rate",
)


@dataclass
class Window:
    start: datetime
    end: datetime


def resolve_window(
    granularity: str,
    start: datetime | None,
    end: datetime | None,
    tenant_settings: Any,
    now: datetime,
) -> Window:
    """The window to read: defaults, rounded out to whole buckets, clamped
    to rollup retention. ValueError for an empty or too long window."""
    end_at = end or now
    start_at = start or end_at - timedelta(days=DEFAULT_RANGE_DAYS[granularity])
    if start_at >= end_at:
        raise ValueError("`from` must be before `to`")
    if end_at - start_at > timedelta(days=MAX_RANGE_DAYS[granularity]):
        raise ValueError(
            f"With granularity={granularity} the window may span at most "
            f"{MAX_RANGE_DAYS[granularity]} days"
        )
    start_at = floor_bucket(max(start_at, rollup_cutoff(tenant_settings, now)), granularity)
    end_at = ceil_bucket(end_at, granularity)
    return Window(start_at, max(start_at, end_at))


async def watermark(db: AsyncSession, tenant_id: UUID) -> datetime | None:
    return (
        await db.execute(
            select(UsageRollupState.watermark).where(UsageRollupState.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()


async def series_page(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    group_by: str,
    granularity: str,
    window: Window,
    limit: int,
    offset: int,
) -> dict:
    stmt = series_query(tenant_id, group_by, granularity, window.start, window.end)
    rows = (await db.execute(stmt.limit(limit + 1).offset(offset))).mappings().all()
    has_more = len(rows) > limit
    data = [series_point(r) for r in rows[:limit]]
    mark = await watermark(db, tenant_id)
    return {
        "data": data,
        "meta": {
            "group_by": group_by,
            "granularity": granularity,
            "from": window.start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": window.end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "rolled_up_at": mark.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if mark else None,
            "has_more": has_more,
            "next_cursor": encode_cursor(offset + limit) if has_more else None,
        },
    }


async def series_rows(
    db: AsyncSession, tenant_id: UUID, *, group_by: str, granularity: str, window: Window
) -> list[dict] | None:
    """Every bucket of the window for an export; None when there are more
    than EXPORT_MAX_ROWS."""
    stmt = series_query(tenant_id, group_by, granularity, window.start, window.end)
    rows = (await db.execute(stmt.limit(EXPORT_MAX_ROWS + 1))).mappings().all()
    if len(rows) > EXPORT_MAX_ROWS:
        return None
    return [series_point(r) for r in rows]


def serialize(rows: list[dict], fmt: str) -> str:
    """CSV (header row, formula-safe, null is an empty cell) or JSON Lines."""
    if fmt == "jsonl":
        return "".join(
            json.dumps({c: r[c] for c in SERIES_COLUMNS}, ensure_ascii=False) + "\n" for r in rows
        )
    import csv
    import io

    from collector.focus_export import csv_safe

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(SERIES_COLUMNS)
    for r in rows:
        writer.writerow(["" if r[c] is None else csv_safe(r[c]) for c in SERIES_COLUMNS])
    return buf.getvalue()


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────


async def _main(command: str, tenant: UUID | None) -> list[dict]:
    from collector.app import SessionLocal, engine

    try:
        if tenant is None:
            return await refresh_all_tenants(SessionLocal, rebuild=command == "rebuild")
        async with SessionLocal() as db:
            await rls.set_tenant(db, tenant)
            run = rebuild_tenant if command == "rebuild" else refresh_tenant
            return [asdict(await run(db, tenant))]
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command",
        choices=["refresh", "rebuild"],
        help="refresh: incremental run; rebuild: recompute every bucket in raw retention",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--tenant", type=UUID)
    target.add_argument("--all-tenants", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(asyncio.run(_main(args.command, args.tenant)), indent=2))


if __name__ == "__main__":
    main()
