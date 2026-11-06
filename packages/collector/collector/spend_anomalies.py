"""Spend anomaly alerts (#30).

A tenant's daily spend (``ai_usage_events.cost``, summed per UTC day) is
tracked per use case (the asset's ``use_case_name``, ``(none)`` when it has
none) or per asset. For each day the job compares that day's spend with an
exponentially weighted moving average (EWMA) of the days before it, and the
EWMA standard deviation (σ). A day whose spend is at least ``sigma`` σ above
the baseline, and at least ``min_delta_usd`` above it in dollars, becomes a
``spend_alerts`` row and a signed ``spend.anomaly`` webhook event
(collector.webhooks) to every subscription that names it.

Detector
    Over the history window (the ``baseline_days`` days before the day,
    starting at the group's first day with spend in that window; days
    without spend count as 0)::

        m = x0, v = 0
        for x in later days:  d = x - m;  m += α·d;  v = (1 - α)·(v + α·d²)
        σ = √v

    σ is floored at ``min_sigma_fraction`` · m (and one cent), so a flat
    series still needs a real jump to alert. z = (spend - m) / σ. Only
    spikes alert (z ≥ ``sigma``), never drops. z ≥ 2·``sigma`` is
    ``critical``, otherwise ``warning``. A group with fewer than
    ``min_history_days`` days of history is not judged. Day-of-week
    seasonality is not modelled.

Thresholds
    ``tenants.settings["spend_anomaly"]`` (set with the operator tenant API)
    overrides any of `DEFAULTS`' fields; a missing or invalid value falls
    back to the default. ``{"enabled": false}`` turns the job off for the
    tenant.

Running it (there is no scheduler in the collector)::

    python -m collector.spend_anomalies --all-tenants          # yesterday (UTC)
    python -m collector.spend_anomalies --all-tenants --days 3 # last 3 days
    python -m collector.spend_anomalies --tenant <uuid> --day 2026-11-05

Run it daily from cron or a Kubernetes CronJob, after the day has ended.
Re-running a day is safe: alerts are unique per (tenant, group, day), and
only a newly created alert sends a webhook. The all-tenants run lists
tenants through a logged RLS bypass, then runs each tenant in its own
session under that tenant's RLS context.

Partner API
    GET  /partner/v1/spend-alerts                         spend-alerts:read
    GET  /partner/v1/spend-alerts/{alert_id}              spend-alerts:read
    POST /partner/v1/spend-alerts/{alert_id}/acknowledge  spend-alerts:acknowledge

Alerts hold numbers only: no prompt content and no personal data.
``acknowledged_by`` is the credential (``partner:<id>``), never a person.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

from db.models import AIAsset, AIUsageEvent, SpendAlert, Tenant
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import String, cast, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from collector import rls, webhooks
from collector.partner_auth import PartnerPrincipal, require_scope
from collector.partner_registry import _audit, _enforce_rate_limit
from collector.sanitize import BoundedJsonRoute

logger = logging.getLogger(__name__)

SETTINGS_KEY = "spend_anomaly"
GROUP_BYS: tuple[str, ...] = ("use_case", "asset")
NO_USE_CASE = "(none)"
STATUSES: tuple[str, ...] = ("open", "acknowledged")
READ_SCOPE = webhooks.SPEND_ALERTS_SCOPE
ACK_SCOPE = "spend-alerts:acknowledge"
LIST_BYPASS_REASON = "spend-anomaly-job"
# Absolute σ floor in USD, so z is always finite.
MIN_SIGMA_USD = 0.01
MAX_BACKFILL_DAYS = 31


@dataclass(frozen=True)
class SpendAnomalyConfig:
    enabled: bool = True
    group_by: str = "use_case"
    # EWMA smoothing factor: weight of the newest day.
    alpha: float = 0.3
    # Alert when spend is at least this many σ above the baseline.
    sigma: float = 3.0
    baseline_days: int = 28
    min_history_days: int = 7
    # ... and at least this many USD above it.
    min_delta_usd: float = 1.0
    # σ is at least this fraction of the baseline.
    min_sigma_fraction: float = 0.1


DEFAULTS = SpendAnomalyConfig()

# Field -> (type, min, max) for numeric fields; bounds inclusive.
_BOUNDS: dict[str, tuple[type, float, float]] = {
    "alpha": (float, 0.01, 1.0),
    "sigma": (float, 0.5, 20.0),
    "baseline_days": (int, 3, 90),
    "min_history_days": (int, 2, 90),
    "min_delta_usd": (float, 0.0, 1_000_000.0),
    "min_sigma_fraction": (float, 0.0, 10.0),
}


def _field_error(name: str, value: Any) -> str | None:
    if name == "enabled":
        return None if isinstance(value, bool) else "must be true or false"
    if name == "group_by":
        return None if value in GROUP_BYS else f"must be one of {list(GROUP_BYS)}"
    kind, lo, hi = _BOUNDS[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "must be a number"
    if kind is int and not isinstance(value, int):
        return "must be an integer"
    if not (lo <= value <= hi) or (isinstance(value, float) and math.isnan(value)):
        return f"must be between {lo:g} and {hi:g}"
    return None


def settings_errors(value: Any) -> list[str]:
    """Problems with a ``settings.spend_anomaly`` value (None clears it)."""
    if value is None:
        return []
    if not isinstance(value, Mapping):
        return [f"settings.{SETTINGS_KEY} must be an object"]
    known = {f.name for f in fields(SpendAnomalyConfig)}
    errors = [f"settings.{SETTINGS_KEY}.{k} is not a known field" for k in value if k not in known]
    for key in sorted(known & set(value)):
        problem = _field_error(key, value[key])
        if problem:
            errors.append(f"settings.{SETTINGS_KEY}.{key} {problem}")
    if not errors:
        window = value.get("baseline_days", DEFAULTS.baseline_days)
        if value.get("min_history_days", DEFAULTS.min_history_days) > window:
            errors.append(
                f"settings.{SETTINGS_KEY}.min_history_days must not exceed baseline_days"
            )
    return errors


def config_for(tenant_settings: Mapping[str, Any] | None) -> SpendAnomalyConfig:
    """The tenant's thresholds: its overrides on top of `DEFAULTS`. An
    invalid field falls back to its default rather than raising."""
    raw = (tenant_settings or {}).get(SETTINGS_KEY) if isinstance(tenant_settings, Mapping) else None
    if not isinstance(raw, Mapping):
        return DEFAULTS
    overrides = {}
    for f in fields(SpendAnomalyConfig):
        if f.name in raw and _field_error(f.name, raw[f.name]) is None:
            value = raw[f.name]
            overrides[f.name] = float(value) if _BOUNDS.get(f.name, (None,))[0] is float else value
    cfg = replace(DEFAULTS, **overrides)
    if cfg.min_history_days > cfg.baseline_days:
        cfg = replace(cfg, min_history_days=min(DEFAULTS.min_history_days, cfg.baseline_days))
    return cfg


# ──────────────────────────────────────────────────────────────────────
# Detector (pure)
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Baseline:
    mean: float
    sigma: float
    history_days: int


@dataclass(frozen=True)
class Detection:
    observed: float
    baseline: float
    sigma: float
    z_score: float
    history_days: int
    severity: Literal["warning", "critical"]


def ewma_baseline(history: Sequence[float], alpha: float) -> Baseline:
    """EWMA mean and standard deviation of ``history`` (oldest first)."""
    if not history:
        return Baseline(0.0, 0.0, 0)
    mean, var = float(history[0]), 0.0
    for x in history[1:]:
        diff = float(x) - mean
        mean += alpha * diff
        var = (1 - alpha) * (var + alpha * diff * diff)
    return Baseline(mean, math.sqrt(var), len(history))


def trim_leading_zeros(history: Sequence[float]) -> list[float]:
    """Drop the days before the group's first spend (a new use case has no
    history yet, not a history of zero)."""
    for i, x in enumerate(history):
        if x > 0:
            return [float(v) for v in history[i:]]
    return []


def detect(
    history: Sequence[float], observed: float, cfg: SpendAnomalyConfig = DEFAULTS
) -> Detection | None:
    """Judge ``observed`` (one day's spend) against ``history`` (the days
    before it, oldest first, missing days as 0). None means no alert."""
    series = trim_leading_zeros(history)
    if len(series) < cfg.min_history_days:
        return None
    base = ewma_baseline(series, cfg.alpha)
    sigma = max(base.sigma, cfg.min_sigma_fraction * base.mean, MIN_SIGMA_USD)
    delta = observed - base.mean
    z = delta / sigma
    if z < cfg.sigma or delta < cfg.min_delta_usd:
        return None
    return Detection(
        observed=observed,
        baseline=base.mean,
        sigma=sigma,
        z_score=z,
        history_days=base.history_days,
        severity="critical" if z >= 2 * cfg.sigma else "warning",
    )


# ──────────────────────────────────────────────────────────────────────
# Job
# ──────────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _day_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


async def daily_spend(
    db: AsyncSession, tenant_id: UUID, group_by: str, start: date, end: date
) -> dict[str, dict[date, float]]:
    """group key -> {UTC day: summed cost} for days in [start, end]."""
    if group_by == "asset":
        key = cast(AIUsageEvent.asset_id, String)
    else:
        key = func.coalesce(AIAsset.use_case_name, NO_USE_CASE)
    day = func.date(func.timezone("UTC", AIUsageEvent.timestamp))
    stmt = (
        select(key.label("k"), day.label("d"), func.sum(AIUsageEvent.cost).label("c"))
        .select_from(AIUsageEvent)
        .join(AIAsset, AIAsset.id == AIUsageEvent.asset_id)
        .where(
            AIUsageEvent.tenant_id == tenant_id,
            AIAsset.tenant_id == tenant_id,
            AIUsageEvent.cost.is_not(None),
            AIUsageEvent.timestamp >= _day_start(start),
            AIUsageEvent.timestamp < _day_start(end + timedelta(days=1)),
        )
        .group_by("k", "d")
    )
    out: dict[str, dict[date, float]] = {}
    for k, d, c in (await db.execute(stmt)).all():
        out.setdefault(k, {})[d] = float(c or 0)
    return out


def serialize_alert(row: SpendAlert) -> dict:
    def ts(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    return {
        "id": str(row.id),
        "group_by": row.group_by,
        "group_key": row.group_key,
        "day": row.day.isoformat(),
        "observed_cost": float(row.observed_cost),
        "baseline_cost": row.baseline_cost,
        "baseline_sigma": row.baseline_sigma,
        "z_score": row.z_score,
        "threshold_sigma": row.threshold_sigma,
        "history_days": row.history_days,
        "severity": row.severity,
        "status": row.status,
        "acknowledged_at": ts(row.acknowledged_at),
        "acknowledged_by": row.acknowledged_by,
        "created_at": ts(row.created_at),
    }


@dataclass
class TenantRun:
    tenant_id: str
    day: str
    group_by: str
    groups_checked: int = 0
    alerts_created: int = 0
    webhooks_queued: int = 0
    skipped: str | None = None


async def run_tenant(
    db: AsyncSession,
    tenant_id: UUID,
    day: date,
    *,
    cfg: SpendAnomalyConfig | None = None,
    now: datetime | None = None,
) -> TenantRun:
    """Judge one tenant's spend on ``day`` and record new alerts. Commits.

    The session must be scoped to the tenant (rls.set_tenant) or bypass.
    """
    now = now or _utcnow()
    if cfg is None:
        tenant_settings = (
            await db.execute(select(Tenant.settings).where(Tenant.id == tenant_id))
        ).scalar_one_or_none()
        cfg = config_for(tenant_settings)
    result = TenantRun(tenant_id=str(tenant_id), day=day.isoformat(), group_by=cfg.group_by)
    if not cfg.enabled:
        result.skipped = "disabled"
        return result
    start = day - timedelta(days=cfg.baseline_days)
    spend = await daily_spend(db, tenant_id, cfg.group_by, start, day)
    for key in sorted(spend):
        by_day = spend[key]
        observed = by_day.get(day, 0.0)
        if observed <= 0:
            continue
        result.groups_checked += 1
        history = [
            by_day.get(start + timedelta(days=i), 0.0) for i in range(cfg.baseline_days)
        ]
        found = detect(history, observed, cfg)
        if found is None:
            continue
        values = {
            "tenant_id": tenant_id,
            "group_by": cfg.group_by,
            "group_key": key,
            "day": day,
            "observed_cost": Decimal(str(round(observed, 6))),
            "baseline_cost": round(found.baseline, 6),
            "baseline_sigma": round(found.sigma, 6),
            "z_score": round(found.z_score, 4),
            "threshold_sigma": cfg.sigma,
            "history_days": found.history_days,
            "severity": found.severity,
            "status": "open",
            "created_at": now,
            "updated_at": now,
        }
        alert_id = (
            await db.execute(
                pg_insert(SpendAlert)
                .values(id=uuid4(), **values)
                .on_conflict_do_nothing(constraint="ux_spend_alerts_group_day")
                .returning(SpendAlert.id)
            )
        ).scalar_one_or_none()
        if alert_id is None:
            continue  # already recorded by an earlier run
        result.alerts_created += 1
        row = await db.get(SpendAlert, alert_id)
        result.webhooks_queued += await webhooks.enqueue_spend_anomaly_event(
            db, tenant_id, alert_id, serialize_alert(row), now
        )
    await db.commit()
    return result


def days_to_check(now: datetime, days: int = 1, day: date | None = None) -> list[date]:
    """``day`` alone, or the last ``days`` complete UTC days, oldest first."""
    if day is not None:
        return [day]
    if not 1 <= days <= MAX_BACKFILL_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_BACKFILL_DAYS}")
    today = now.astimezone(UTC).date()
    return [today - timedelta(days=i) for i in range(days, 0, -1)]


def list_session_factory(session_factory):
    """Sessions that bypass RLS, used only to list tenants. Logged once."""
    info = rls.bypass_info(LIST_BYPASS_REASON)
    return lambda: session_factory(info=info)


async def run_all_tenants(
    session_factory,
    *,
    days: int = 1,
    day: date | None = None,
    list_factory=None,
    now: datetime | None = None,
) -> dict:
    """Run every enabled tenant, each in its own session under its own RLS
    context. A tenant that fails is reported and does not stop the others."""
    now = now or _utcnow()
    check = days_to_check(now, days, day)
    list_factory = list_factory or list_session_factory(session_factory)
    async with list_factory() as db:
        tenants = (
            await db.execute(
                select(Tenant.id, Tenant.settings).where(Tenant.disabled_at.is_(None))
            )
        ).all()
    results: dict[str, list[dict]] = {}
    for tenant_id, tenant_settings in sorted(tenants, key=lambda t: str(t[0])):
        cfg = config_for(tenant_settings)
        runs: list[dict] = []
        try:
            async with session_factory() as db:
                await rls.set_tenant(db, tenant_id)
                for d in check:
                    runs.append(asdict(await run_tenant(db, tenant_id, d, cfg=cfg, now=now)))
                    if not cfg.enabled:
                        break
        except Exception as exc:  # noqa: BLE001 - one tenant must not stop the rest
            logger.error(
                "spend anomaly run failed for tenant %s: %s", tenant_id, type(exc).__name__
            )
            runs.append({"tenant_id": str(tenant_id), "error": type(exc).__name__})
        results[str(tenant_id)] = runs
    return {"days": [d.isoformat() for d in check], "tenants": results}


# ──────────────────────────────────────────────────────────────────────
# Partner API
# ──────────────────────────────────────────────────────────────────────

partner_router = APIRouter(
    prefix="/partner/v1", tags=["Partner API"], route_class=BoundedJsonRoute
)
_READ = Depends(require_scope(READ_SCOPE))
_ACK = Depends(require_scope(ACK_SCOPE))
_SINCE = Query(default=None, description="First day to include (UTC)")
_UNTIL = Query(default=None, description="Last day to include (UTC)")


def _problem(status: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


async def _load(db: AsyncSession, tenant_id: UUID, alert_id: UUID) -> SpendAlert | None:
    return (
        await db.execute(
            select(SpendAlert).where(SpendAlert.id == alert_id, SpendAlert.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()


@partner_router.get("/spend-alerts", summary="List spend anomaly alerts")
async def list_spend_alerts(
    request: Request,
    response: Response,
    status: Literal["open", "acknowledged"] | None = None,
    severity: Literal["warning", "critical"] | None = None,
    since: date | None = _SINCE,
    until: date | None = _UNTIL,
    limit: int = Query(default=100, ge=1, le=500),
    principal: PartnerPrincipal = _READ,
):
    await _enforce_rate_limit(request, response, principal)
    db = request.state.db
    stmt = select(SpendAlert).where(SpendAlert.tenant_id == principal.tenant_id)
    if status:
        stmt = stmt.where(SpendAlert.status == status)
    if severity:
        stmt = stmt.where(SpendAlert.severity == severity)
    if since:
        stmt = stmt.where(SpendAlert.day >= since)
    if until:
        stmt = stmt.where(SpendAlert.day <= until)
    rows = (
        await db.execute(
            stmt.order_by(SpendAlert.day.desc(), SpendAlert.created_at.desc(), SpendAlert.id)
            .limit(limit)
        )
    ).scalars().all()
    body = {"data": [serialize_alert(r) for r in rows], "meta": {"total": len(rows)}}
    await _audit(request, principal, "/partner/v1/spend-alerts", "GET", 200, len(rows))
    return body


@partner_router.get("/spend-alerts/{alert_id}", summary="Get a spend anomaly alert")
async def get_spend_alert(
    alert_id: UUID,
    request: Request,
    response: Response,
    principal: PartnerPrincipal = _READ,
):
    await _enforce_rate_limit(request, response, principal)
    endpoint = f"/partner/v1/spend-alerts/{alert_id}"
    row = await _load(request.state.db, principal.tenant_id, alert_id)
    if row is None:
        await _audit(request, principal, endpoint, "GET", 404)
        raise _problem(404, "Not Found", "Spend alert not found")
    body = {"data": serialize_alert(row)}
    await _audit(request, principal, endpoint, "GET", 200, 1)
    return body


@partner_router.post(
    "/spend-alerts/{alert_id}/acknowledge", summary="Acknowledge a spend anomaly alert"
)
async def acknowledge_spend_alert(
    alert_id: UUID,
    request: Request,
    response: Response,
    principal: PartnerPrincipal = _ACK,
):
    """Mark the alert acknowledged. Acknowledging it again changes nothing
    and returns it as it is."""
    await _enforce_rate_limit(request, response, principal)
    db = request.state.db
    endpoint = f"/partner/v1/spend-alerts/{alert_id}/acknowledge"
    row = await _load(db, principal.tenant_id, alert_id)
    if row is None:
        await _audit(request, principal, endpoint, "POST", 404)
        raise _problem(404, "Not Found", "Spend alert not found")
    if row.status != "acknowledged":
        now = _utcnow()
        row.status = "acknowledged"
        row.acknowledged_at = now
        row.acknowledged_by = f"partner:{principal.partner_id}"
        row.updated_at = now
        await db.flush()
    body = {"data": serialize_alert(row)}
    await db.commit()
    await _audit(request, principal, endpoint, "POST", 200, 1)
    return body


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────


async def _main(tenant: UUID | None, days: int, day: date | None) -> dict:
    from collector.app import SessionLocal, engine

    try:
        if tenant is None:
            return await run_all_tenants(SessionLocal, days=days, day=day)
        now = _utcnow()
        async with SessionLocal() as db:
            await rls.set_tenant(db, tenant)
            return {
                "days": [d.isoformat() for d in days_to_check(now, days, day)],
                "tenants": {
                    str(tenant): [
                        asdict(await run_tenant(db, tenant, d, now=now))
                        for d in days_to_check(now, days, day)
                    ]
                },
            }
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--tenant", type=UUID)
    target.add_argument("--all-tenants", action="store_true")
    when = parser.add_mutually_exclusive_group()
    when.add_argument(
        "--days",
        type=int,
        default=1,
        help=f"check the last N complete UTC days (1-{MAX_BACKFILL_DAYS}, default 1)",
    )
    when.add_argument("--day", type=date.fromisoformat, help="check one UTC day (YYYY-MM-DD)")
    args = parser.parse_args(argv)
    if args.day is None and not 1 <= args.days <= MAX_BACKFILL_DAYS:
        parser.error(f"--days must be between 1 and {MAX_BACKFILL_DAYS}")
    logging.basicConfig(level=logging.INFO)
    result = asyncio.run(_main(args.tenant, args.days, args.day))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
