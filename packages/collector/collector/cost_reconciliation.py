"""Monthly cost reconciliation against provider usage (#348).

Compares, per calendar month (UTC), provider and model, the usage the SDK
and gateway reported (``ai_usage_events`` from every source but the
provider pull) with the usage the provider's own organisation usage API
reports (the ``platform_signal`` events of collector/provider_usage.py:
OpenAI and Anthropic). Each (month, provider, model) becomes a
``cost_reconciliations`` row; a gap over the tenant's threshold
(default 5%) is ``flagged``: the alert.

Valuing
    Both sides are valued at the same prices, day by day: the tenant's
    negotiated price in force that day (collector/prices.py; per-1k rates,
    or the list price times its multiplier), else the catalog list price
    (list_prices.json, generated from the model catalog). Standard input
    and output rates only, on both sides: the provider usage pull folds
    cache reads and writes into input tokens, so pricing cache and service
    tiers on one side only would show as a gap that is not one. The
    variance is therefore the metered-usage gap weighted by price
    (``basis`` ``price``). A model with no price on some day of the month
    is compared in tokens instead (``basis`` ``tokens``: input + output).
    ``estimated_cost`` is the cost the events reported (negotiated or list,
    cache discounts included), for reference.

    ``variance_pct`` = (estimated - provider) / provider x 100: positive
    when the SDK and gateway reported more than the provider bills (e.g.
    calls through another organisation's key), negative when the provider
    bills usage nobody reported (uninstrumented callers, a leaked key). It
    is NULL when the provider side is 0; such a row is flagged when its
    estimated side is over the minimum.

Models are matched by vendor (``openai``, ``anthropic``; other vendors,
Azure OpenAI included, are not on these invoices) and model id without a
``-latest``, ``-YYYY-MM-DD`` or ``-YYYYMMDD`` suffix, so ``gpt-4o`` and
``gpt-4o-2024-08-06`` are one row. A provider with no pulled usage in the
month (no admin key, or the pull did not run) is reported as
``no_provider_data`` and gets no rows, rather than flagging every model.

Settings
    ``tenants.settings["cost_reconciliation"]`` (operator tenant API)
    overrides any of `DEFAULTS`: ``threshold_pct`` (5.0), ``min_usd`` (1.0;
    rows valued below it on both sides are never flagged), ``min_tokens``
    (100000, the same for ``tokens`` rows) and ``enabled`` (true).

Running it (run the provider usage pull for the month first)::

    python -m collector.cost_reconciliation --all-tenants            # last month
    python -m collector.cost_reconciliation --tenant <uuid> --month 2026-10

Re-running a month replaces its rows. It reads raw events, so run it before
the month falls out of the tenant's raw retention.

Partner API
    GET  /partner/v1/cost-reconciliations       usage:read
    POST /partner/v1/cost-reconciliations/run   usage:reconcile
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

from db.models import AIAsset, AIUsageEvent, CostReconciliation, Tenant
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from collector import prices, rls
from collector.partner_auth import PartnerPrincipal, require_scope
from collector.partner_registry import _audit, _enforce_rate_limit
from collector.provider_usage import SOURCE as PROVIDER_SOURCE
from collector.provider_usage import SUPPORTED_PROVIDERS
from collector.sanitize import BoundedJsonRoute

logger = logging.getLogger(__name__)

SETTINGS_KEY = "cost_reconciliation"
READ_SCOPE = "usage:read"
RUN_SCOPE = "usage:reconcile"
LIST_BYPASS_REASON = "cost-reconciliation-job"
LIST_PRICES = Path(__file__).with_name("list_prices.json")
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
_MODEL_SUFFIX = re.compile(r"(-latest|-\d{4}-\d{2}-\d{2}|-\d{8})$")


@dataclass(frozen=True)
class ReconciliationConfig:
    enabled: bool = True
    threshold_pct: float = 5.0
    min_usd: float = 1.0
    min_tokens: int = 100_000


DEFAULTS = ReconciliationConfig()
_BOUNDS: dict[str, tuple[type, float, float]] = {
    "threshold_pct": (float, 0.1, 100.0),
    "min_usd": (float, 0.0, 1_000_000_000.0),
    "min_tokens": (int, 0, 10**15),
}


def config_for(tenant_settings: Mapping[str, Any] | None) -> ReconciliationConfig:
    """The tenant's config; a missing or invalid value is the default."""
    raw = (tenant_settings or {}).get(SETTINGS_KEY)
    if not isinstance(raw, Mapping):
        return DEFAULTS
    values: dict[str, Any] = {}
    for f in fields(ReconciliationConfig):
        if f.name not in raw:
            continue
        v = raw[f.name]
        if f.name == "enabled":
            if isinstance(v, bool):
                values["enabled"] = v
            continue
        typ, lo, hi = _BOUNDS[f.name]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if typ is int and not isinstance(v, int):
            continue
        if lo <= v <= hi:
            values[f.name] = typ(v)
    return ReconciliationConfig(**{**asdict(DEFAULTS), **values})


@lru_cache(maxsize=1)
def list_prices() -> dict[str, tuple[float, float]]:
    """``"<provider>/<model>"`` -> (input, output) USD per 1k tokens."""
    data = json.loads(LIST_PRICES.read_text())
    return {k: (float(v[0]), float(v[1])) for k, v in data["tokenPricing"].items()}


def model_key(model: str | None) -> str:
    return _MODEL_SUFFIX.sub("", (model or "").strip()) or "(unknown)"


def parse_month(value: str) -> date:
    if not re.match(MONTH_PATTERN, value or ""):
        raise ValueError("month must be YYYY-MM")
    return date(int(value[:4]), int(value[5:7]), 1)


def month_bounds(month: date) -> tuple[datetime, datetime]:
    start = datetime(month.year, month.month, 1, tzinfo=UTC)
    nxt = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    return start, datetime(nxt.year, nxt.month, 1, tzinfo=UTC)


def previous_month(now: datetime) -> date:
    first = now.astimezone(UTC).date().replace(day=1)
    return (first - timedelta(days=1)).replace(day=1)


@dataclass
class _Side:
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float | None = None
    # day -> (tokens_in, tokens_out)
    days: dict[date, tuple[int, int]] = field(default_factory=dict)


@dataclass
class _Group:
    provider: str
    model: str
    raw_models: set[str] = field(default_factory=set)
    estimated: _Side = field(default_factory=_Side)
    provider_side: _Side = field(default_factory=_Side)


async def _usage(db: AsyncSession, tenant_id: UUID, start: datetime, end: datetime) -> list:
    vendor = func.lower(AIAsset.vendor)
    model = func.coalesce(AIUsageEvent.served_model, AIAsset.model)
    day = func.date(func.timezone("UTC", AIUsageEvent.timestamp))
    from_provider = AIUsageEvent.source == PROVIDER_SOURCE
    stmt = (
        select(
            vendor.label("vendor"),
            model.label("model"),
            from_provider.label("from_provider"),
            day.label("day"),
            func.coalesce(func.sum(AIUsageEvent.tokens_in), 0).label("tokens_in"),
            func.coalesce(func.sum(AIUsageEvent.tokens_out), 0).label("tokens_out"),
            func.sum(AIUsageEvent.cost).label("cost"),
        )
        .select_from(AIUsageEvent)
        .join(AIAsset, AIAsset.id == AIUsageEvent.asset_id)
        .where(
            AIUsageEvent.tenant_id == tenant_id,
            AIAsset.tenant_id == tenant_id,
            AIUsageEvent.timestamp >= start,
            AIUsageEvent.timestamp < end,
            vendor.in_(SUPPORTED_PROVIDERS),
        )
        .group_by(vendor, model, from_provider, day)
    )
    return (await db.execute(stmt)).all()


def _rates(
    book: prices.PriceBook, provider: str, raw_models: set[str], key: str, at: datetime
) -> tuple[float, float] | None:
    """(input, output) USD per 1k for the model on the day ``at``."""
    candidates = [key, *sorted(raw_models)]
    table = list_prices()
    listed = next(
        (table[f"{provider}/{m}"] for m in candidates if f"{provider}/{m}" in table), None
    )
    o = book.find(provider, candidates, at)
    if o is None:
        return listed
    if o.multiplier is not None:
        return None if listed is None else (listed[0] * o.multiplier, listed[1] * o.multiplier)
    return (o.input_per_1k or 0.0, o.output_per_1k or 0.0)


@dataclass
class TenantRun:
    tenant_id: str
    month: str
    providers: dict[str, str] = field(default_factory=dict)
    rows: int = 0
    flagged: int = 0
    skipped: str | None = None


def _value(side: _Side, day_rates: Mapping[date, tuple[float, float]] | None) -> float:
    if day_rates is None:
        return float(side.tokens_in + side.tokens_out)
    return sum(
        (tin * day_rates[d][0] + tout * day_rates[d][1]) / 1000.0
        for d, (tin, tout) in side.days.items()
    )


async def reconcile_month(
    db: AsyncSession,
    tenant_id: UUID,
    month: date,
    *,
    cfg: ReconciliationConfig | None = None,
    now: datetime | None = None,
) -> TenantRun:
    """Compute and store the month's rows for one tenant (replacing any
    earlier run's). Commits."""
    now = now or datetime.now(UTC)
    run = TenantRun(tenant_id=str(tenant_id), month=month.strftime("%Y-%m"))
    start, end = month_bounds(month)
    if start > now:
        raise ValueError("month is in the future")
    if cfg is None:
        tenant_settings = (
            await db.execute(select(Tenant.settings).where(Tenant.id == tenant_id))
        ).scalar_one_or_none()
        cfg = config_for(tenant_settings)
    if not cfg.enabled:
        run.skipped = "disabled"
        return run
    book = await prices.load_price_book(db, tenant_id)
    groups: dict[tuple[str, str], _Group] = {}
    pulled: set[str] = set()
    for r in await _usage(db, tenant_id, start, end):
        key = (r.vendor, model_key(r.model))
        g = groups.setdefault(key, _Group(provider=key[0], model=key[1]))
        if r.model:
            g.raw_models.add(r.model)
        side = g.provider_side if r.from_provider else g.estimated
        if r.from_provider:
            pulled.add(r.vendor)
        tin, tout = int(r.tokens_in), int(r.tokens_out)
        side.tokens_in += tin
        side.tokens_out += tout
        prev = side.days.get(r.day, (0, 0))
        side.days[r.day] = (prev[0] + tin, prev[1] + tout)
        if r.cost is not None:
            side.cost = (side.cost or 0.0) + float(r.cost)
    for provider in SUPPORTED_PROVIDERS:
        run.providers[provider] = "ok" if provider in pulled else "no_provider_data"

    month_rows: list[CostReconciliation] = []
    for (provider, model), g in sorted(groups.items()):
        if provider not in pulled:
            continue
        day_rates: dict[date, tuple[float, float]] | None = {}
        for d in set(g.estimated.days) | set(g.provider_side.days):
            rates = _rates(
                book, provider, g.raw_models, model, datetime(d.year, d.month, d.day, tzinfo=UTC)
            )
            if rates is None:
                day_rates = None
                break
            day_rates[d] = rates
        basis = "tokens" if day_rates is None else "price"
        est = _value(g.estimated, day_rates)
        prov = _value(g.provider_side, day_rates)
        variance = None if prov == 0 else (est - prov) / prov * 100.0
        minimum = cfg.min_tokens if basis == "tokens" else cfg.min_usd
        material = max(est, prov) >= minimum
        flagged = material and (variance is None or abs(variance) > cfg.threshold_pct)
        month_rows.append(
            CostReconciliation(
                tenant_id=tenant_id,
                month=month,
                provider=provider,
                model=model,
                estimated_tokens_in=g.estimated.tokens_in,
                estimated_tokens_out=g.estimated.tokens_out,
                estimated_cost=None if g.estimated.cost is None else round(g.estimated.cost, 6),
                provider_tokens_in=g.provider_side.tokens_in,
                provider_tokens_out=g.provider_side.tokens_out,
                basis=basis,
                estimated_value=round(est, 6),
                provider_value=round(prov, 6),
                variance_pct=None if variance is None else round(variance, 4),
                threshold_pct=cfg.threshold_pct,
                flagged=flagged,
                created_at=now,
                updated_at=now,
            )
        )
    await db.execute(
        delete(CostReconciliation).where(
            CostReconciliation.tenant_id == tenant_id, CostReconciliation.month == month
        )
    )
    db.add_all(month_rows)
    run.rows = len(month_rows)
    gaps = [(r.provider, r.model, r.variance_pct, r.basis) for r in month_rows if r.flagged]
    run.flagged = len(gaps)
    await db.commit()
    for provider, model, variance, basis in gaps:
        logger.warning(
            "cost reconciliation gap: tenant=%s month=%s %s/%s variance=%s%% basis=%s",
            tenant_id,
            run.month,
            provider,
            model,
            variance,
            basis,
        )
    return run


def list_session_factory(session_factory):
    """Sessions that bypass RLS, used only to list tenants. Logged once."""
    info = rls.bypass_info(LIST_BYPASS_REASON)
    return lambda: session_factory(info=info)


async def run_all_tenants(
    session_factory, *, month: date, list_factory=None, now: datetime | None = None
) -> dict:
    """Run every enabled tenant, each in its own session under its own RLS
    context. A tenant that fails is reported and does not stop the others."""
    list_factory = list_factory or list_session_factory(session_factory)
    async with list_factory() as db:
        tenants = (
            await db.execute(
                select(Tenant.id, Tenant.settings).where(Tenant.disabled_at.is_(None))
            )
        ).all()
    results: dict[str, dict] = {}
    for tenant_id, tenant_settings in sorted(tenants, key=lambda t: str(t[0])):
        try:
            async with session_factory() as db:
                await rls.set_tenant(db, tenant_id)
                results[str(tenant_id)] = asdict(
                    await reconcile_month(
                        db, tenant_id, month, cfg=config_for(tenant_settings), now=now
                    )
                )
        except Exception as exc:  # noqa: BLE001 - one tenant must not stop the rest
            logger.error(
                "cost reconciliation failed for tenant %s: %s", tenant_id, type(exc).__name__
            )
            results[str(tenant_id)] = {"tenant_id": str(tenant_id), "error": type(exc).__name__}
    return {"month": month.strftime("%Y-%m"), "tenants": results}


def serialize(row: CostReconciliation) -> dict:
    return {
        "id": str(row.id),
        "month": row.month.strftime("%Y-%m"),
        "provider": row.provider,
        "model": row.model,
        "estimated_tokens_in": row.estimated_tokens_in,
        "estimated_tokens_out": row.estimated_tokens_out,
        "estimated_cost_usd": None if row.estimated_cost is None else float(row.estimated_cost),
        "provider_tokens_in": row.provider_tokens_in,
        "provider_tokens_out": row.provider_tokens_out,
        "basis": row.basis,
        "estimated_value": float(row.estimated_value),
        "provider_value": float(row.provider_value),
        "variance_pct": row.variance_pct,
        "threshold_pct": row.threshold_pct,
        "flagged": row.flagged,
        "updated_at": row.updated_at.isoformat(),
    }


# ──────────────────────────────────────────────────────────────────────
# Partner API
# ──────────────────────────────────────────────────────────────────────

partner_router = APIRouter(
    prefix="/partner/v1", tags=["Partner API"], route_class=BoundedJsonRoute
)
_READ = Depends(require_scope(READ_SCOPE))
_RUN = Depends(require_scope(RUN_SCOPE))


def _problem(status: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    month: str = Field(..., pattern=MONTH_PATTERN, description="YYYY-MM (UTC)")


@partner_router.get("/cost-reconciliations", summary="List monthly cost reconciliations")
async def list_cost_reconciliations(
    request: Request,
    response: Response,
    month: str | None = Query(default=None, pattern=MONTH_PATTERN, description="YYYY-MM"),
    provider: str | None = Query(default=None, max_length=64),
    flagged: bool | None = None,
    limit: int = Query(default=500, ge=1, le=1000),
    principal: PartnerPrincipal = _READ,
):
    await _enforce_rate_limit(request, response, principal)
    stmt = select(CostReconciliation).where(CostReconciliation.tenant_id == principal.tenant_id)
    if month:
        stmt = stmt.where(CostReconciliation.month == parse_month(month))
    if provider:
        stmt = stmt.where(CostReconciliation.provider == provider)
    if flagged is not None:
        stmt = stmt.where(CostReconciliation.flagged.is_(flagged))
    rows = (
        await request.state.db.execute(
            stmt.order_by(
                CostReconciliation.month.desc(),
                CostReconciliation.provider,
                CostReconciliation.model,
            ).limit(limit)
        )
    ).scalars().all()
    body = {"data": [serialize(r) for r in rows], "meta": {"total": len(rows)}}
    await _audit(request, principal, "/partner/v1/cost-reconciliations", "GET", 200, len(rows))
    return body


@partner_router.post(
    "/cost-reconciliations/run", summary="Run the cost reconciliation for a month"
)
async def run_cost_reconciliation(
    body: RunBody,
    request: Request,
    response: Response,
    principal: PartnerPrincipal = _RUN,
):
    """Recompute the month (replacing its rows) and return them. Run the
    provider usage pull for the month first. The current month can be run
    and is partial until it ends."""
    await _enforce_rate_limit(request, response, principal)
    endpoint = "/partner/v1/cost-reconciliations/run"
    db = request.state.db
    month = parse_month(body.month)
    try:
        run = await reconcile_month(db, principal.tenant_id, month)
    except ValueError as exc:
        await _audit(request, principal, endpoint, "POST", 422)
        raise _problem(422, "Unprocessable Entity", str(exc)) from None
    rows = (
        await db.execute(
            select(CostReconciliation)
            .where(
                CostReconciliation.tenant_id == principal.tenant_id,
                CostReconciliation.month == month,
            )
            .order_by(CostReconciliation.provider, CostReconciliation.model)
        )
    ).scalars().all()
    out = {
        "month": run.month,
        "providers": run.providers,
        "skipped": run.skipped,
        "flagged": run.flagged,
        "data": [serialize(r) for r in rows],
    }
    await _audit(request, principal, endpoint, "POST", 200, len(rows))
    return out


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────


async def _main(tenant: UUID | None, month: date) -> dict:
    from collector.app import SessionLocal, engine

    try:
        if tenant is None:
            return await run_all_tenants(SessionLocal, month=month)
        async with SessionLocal() as db:
            await rls.set_tenant(db, tenant)
            return asdict(await reconcile_month(db, tenant, month))
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--tenant", type=UUID)
    target.add_argument("--all-tenants", action="store_true")
    parser.add_argument("--month", help="YYYY-MM (UTC); default: last month")
    args = parser.parse_args(argv)
    try:
        month = parse_month(args.month) if args.month else previous_month(datetime.now(UTC))
    except ValueError as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(asyncio.run(_main(args.tenant, month)), indent=2))


if __name__ == "__main__":
    main()
