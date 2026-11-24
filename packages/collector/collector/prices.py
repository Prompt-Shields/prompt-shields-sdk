"""Negotiated model prices per tenant (#348).

The tenant's negotiated prices live in the ``prices`` section of its
tenant-scope central policy (collector/policy_doc.py, ps-policy.v1), so they
are versioned, audited and pulled by the gateway with the rest of the
policy. No other table holds them. Format::

    "prices": {
      "version": 1,
      "currency": "USD",          # ISO 4217; default USD
      "usd_per_unit": 1.0,        # USD per 1 unit of currency (non-USD only)
      "overrides": [
        {"provider": "openai", "model": "gpt-4o",
         "valid_from": "2026-11-01", "valid_to": "2027-11-01",
         "input_per_1k": 0.002, "output_per_1k": 0.008,
         "cached_input_per_1k": 0.001},
        {"provider": "anthropic", "model": "*",
         "valid_from": "2026-11-01", "multiplier": 0.85}
      ]
    }

An override applies to calls made at or after ``valid_from`` and before
``valid_to`` (exclusive; open-ended when left out). Dates are midnight UTC.
``model`` ``"*"`` covers every model of the provider; an exact model wins
over ``"*"``. Overlapping windows for one (provider, model) are refused.

Two kinds:

* ``multiplier``: the list price (the SDK's and gateway's catalog price,
  ``cost_source`` ``pricing_table``) times the multiplier: a contract
  discount. Works for every list-priced call, per-unit ones included.
* per-1k rates (``input_per_1k``, ``output_per_1k``, optional
  ``cached_input_per_1k``, ``cache_write_per_1k``): the call's token cost at
  these rates, in the section's currency, converted at ``usd_per_unit``.
  Cache rates left out follow the provider's list ratios (cache reads at
  0.5x input for OpenAI, 0.1x Anthropic, 0.25x Google; Anthropic writes
  1.25x, 1-hour writes 2x); non-standard service tiers (batch, flex) get the
  provider's tier multiplier, an unknown tier is left at list. Token-billed
  calls only (per-unit calls keep their list price), and server tool fees
  (web search) are not included. Rates also price calls of a model the
  catalog has no list price for.

Matching uses the event's vendor (``azure-openai`` and ``azure_openai`` are
the same) and, as the SDK prices a call, its served model, the served model
without an OpenAI-style date suffix, then the requested model, then the
asset's model.

Where it applies
----------------
* Gateway (policyPull/prices.ts): prices its telemetry ``cost`` and the
  ``ps_gateway_cost_usd_total`` metric at the override, with
  ``cost_source: negotiated``.
* Collector ingest (`reprice`): an event whose cost is a list price
  (``cost_source`` ``pricing_table*`` or none) or that has no cost is priced
  at the override in force when it is stored, and marked ``negotiated``.
  Provider-reported, cache and already negotiated costs are kept. So SDK
  events (whose ``pricing_table=`` is per process) end up at the tenant's
  negotiated price too, and so do gateway events sent before the gateway
  pulled the latest policy.
* Rollups, showback and FOCUS read the stored cost, so they follow.

History: overrides apply going forward, at ingest. Events already stored
keep the cost they were stored with; a contract signed retroactively is
applied by `reprice_stored` (``python -m collector.prices reprice``), which
re-prices the stored list-priced raw events in a window and then rebuilds
the tenant's rollups (buckets still in raw retention only). It runs under
the tenant's RLS context, commits every batch under the tenant's rollup
lock (usage_rollups.lock_tenant, so no rollup run reads a half-repriced
batch), and waits for that lock to rebuild. Re-running it is safe: events
already ``negotiated`` are never re-priced, and the rollups are rebuilt on
every run that has prices (so a run interrupted before its rebuild is
completed by the next one).

Arithmetic is Decimal, rounded half-up to 6 places (the ``cost`` column's
scale); the result is handed back as the float of that exact 6-place value,
like the SDK's and gateway's catalog costs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from db.models import AIAsset, AIUsageEvent, Policy, PolicyVersion
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from collector import policy_doc

SECTION = "prices"
COST_SOURCE_NEGOTIATED = "negotiated"
# Costs a negotiated price never replaces.
KEPT_COST_SOURCES = frozenset({"provider_reported", "cache", COST_SOURCE_NEGOTIATED})

# psPricing.ts / pricing.py, kept in step by hand (test_prices.py).
CACHE_READ_MULTIPLIER = {"openai": 0.5, "anthropic": 0.1, "google": 0.25}
CACHE_WRITE_MULTIPLIER = {"anthropic": 1.25}
CACHE_WRITE_1H_MULTIPLIER = {"anthropic": 2.0}
STANDARD_SERVICE_TIERS = frozenset({"default", "standard", "auto"})
SERVICE_TIER_MULTIPLIER = {
    "openai": {"batch": 0.5, "flex": 0.5},
    "anthropic": {"batch": 0.5},
    "google": {"batch": 0.5},
}
_DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")
# Bedrock cross-region inference profile prefix (`us.anthropic.claude-...`),
# as psPricing.ts BEDROCK_PROFILE_PREFIX: a profile matches its base model.
_BEDROCK_PROFILE_PREFIX = re.compile(r"^(?:us|us-gov|eu|apac|au|ca|jp|in|global)\.(?=[a-z0-9-]+\.)")
_SIX = Decimal("0.000001")


def _d(x: float | int | Decimal | None) -> Decimal:
    """Decimal of a JSON number as written (str(), not the binary float)."""
    return x if isinstance(x, Decimal) else Decimal(str(x or 0))


def _r6(x: Decimal) -> float:
    return float(x.quantize(_SIX, rounding=ROUND_HALF_UP))


def normalize_provider(value: str | None) -> str:
    return (value or "").strip().lower().replace("-", "_")


@dataclass(frozen=True)
class Override:
    provider: str
    model: str
    valid_from: datetime
    valid_to: datetime | None
    multiplier: Decimal | None = None
    # USD per 1k tokens (already converted from the section's currency).
    input_per_1k: Decimal | None = None
    output_per_1k: Decimal | None = None
    cached_input_per_1k: Decimal | None = None
    cache_write_per_1k: Decimal | None = None

    def active_at(self, at: datetime) -> bool:
        return self.valid_from <= at and (self.valid_to is None or at < self.valid_to)


@dataclass(frozen=True)
class PriceBook:
    overrides: tuple[Override, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.overrides)

    def find(
        self, provider: str | None, models: Iterable[str | None], at: datetime
    ) -> Override | None:
        """The override for the first of ``models`` that has one at ``at``
        (exact model first, then the provider's ``*``)."""
        vendor = normalize_provider(provider)
        live = [o for o in self.overrides if o.provider == vendor and o.active_at(at)]
        if not live:
            return None
        exact = {o.model: o for o in live if o.model != "*"}
        for m in models:
            if m and m in exact:
                return exact[m]
        return next((o for o in live if o.model == "*"), None)


def parse_section(section: Mapping[str, Any] | None) -> PriceBook:
    """A PriceBook from a validated ``prices`` v1 section (empty if None)."""
    if not section:
        return PriceBook()
    fx = _d(section.get("usd_per_unit", 1) or 1)
    out = []
    for o in section.get("overrides") or []:

        def rate(key: str, o=o) -> Decimal | None:
            v = o.get(key)
            return None if v is None else _d(v) * fx

        out.append(
            Override(
                provider=normalize_provider(o["provider"]),
                model=o["model"],
                valid_from=policy_doc.parse_instant(o["valid_from"]),
                valid_to=policy_doc.parse_instant(o["valid_to"]) if o.get("valid_to") else None,
                multiplier=_d(o["multiplier"]) if "multiplier" in o else None,
                input_per_1k=rate("input_per_1k"),
                output_per_1k=rate("output_per_1k"),
                cached_input_per_1k=rate("cached_input_per_1k"),
                cache_write_per_1k=rate("cache_write_per_1k"),
            )
        )
    return PriceBook(tuple(out))


async def load_price_book(db: AsyncSession, tenant_id: UUID) -> PriceBook:
    """The ``prices`` section of the tenant's active tenant-scope policy."""
    document = (
        await db.execute(
            select(PolicyVersion.document)
            .join(
                Policy,
                and_(
                    PolicyVersion.policy_id == Policy.id,
                    PolicyVersion.version == Policy.active_version,
                ),
            )
            .where(Policy.tenant_id == tenant_id, Policy.scope == "tenant")
        )
    ).scalar_one_or_none()
    return parse_section(((document or {}).get("sections") or {}).get(SECTION))


def pricing_candidates(*models: str | None, vendor: str | None = None) -> list[str]:
    """Model ids to match, best first: each given id, the first one also
    without an OpenAI-style date suffix (``gpt-4o-2024-08-06`` -> ``gpt-4o``);
    for ``bedrock``, then each without its cross-region profile prefix
    (psPricing.ts pricingCandidates + withBedrockBaseModels)."""
    out: list[str] = []
    for i, m in enumerate(models):
        if not m:
            continue
        for name in (m, _DATE_SUFFIX.sub("", m)) if i == 0 else (m,):
            if name not in out:
                out.append(name)
    if normalize_provider(vendor) == "bedrock":
        for c in list(out):
            base = _BEDROCK_PROFILE_PREFIX.sub("", c)
            if base != c and base not in out:
                out.append(base)
    return out


def token_cost(o: Override, vendor: str, usage: Mapping[str, Any]) -> float | None:
    """USD token cost of ``usage`` at a rate override, or None when it does
    not apply (no token counts, per-unit billing, an unknown service tier)."""
    tokens_in, tokens_out = usage.get("tokens_in"), usage.get("tokens_out")
    if tokens_in is None or tokens_out is None or usage.get("usage_unit_type"):
        return None
    tier = usage.get("service_tier")
    scale = Decimal(1)
    if tier is not None and tier not in STANDARD_SERVICE_TIERS:
        found = SERVICE_TIER_MULTIPLIER.get(vendor, {}).get(tier)
        if found is None:
            return None
        scale = _d(found)
    in_rate, out_rate = _d(o.input_per_1k), _d(o.output_per_1k)
    cached_rate = (
        o.cached_input_per_1k
        if o.cached_input_per_1k is not None
        else in_rate * _d(CACHE_READ_MULTIPLIER.get(vendor, 1.0))
    )
    write_mult = _d(CACHE_WRITE_MULTIPLIER.get(vendor, 1.0))
    write_rate = o.cache_write_per_1k if o.cache_write_per_1k is not None else in_rate * write_mult
    write_1h_rate = (
        o.cache_write_per_1k
        if o.cache_write_per_1k is not None
        else in_rate * _d(CACHE_WRITE_1H_MULTIPLIER.get(vendor, write_mult))
    )
    cached = usage.get("tokens_cached") or 0
    write = usage.get("tokens_cache_write") or 0
    write_1h = min(usage.get("tokens_cache_write_1h") or 0, write)
    uncached = max(tokens_in - cached - write, 0)
    cost = (
        uncached * in_rate
        + cached * cached_rate
        + (write - write_1h) * write_rate
        + write_1h * write_1h_rate
        + tokens_out * out_rate
    ) / 1000
    return _r6(cost * scale)


def negotiated_cost(
    book: PriceBook,
    vendor: str | None,
    models: Iterable[str | None],
    usage: Mapping[str, Any],
    cost: float | Decimal | None,
    cost_source: str | None,
    at: datetime,
) -> float | None:
    """The negotiated cost of a call, or None when no override changes it."""
    if not book or cost_source in KEPT_COST_SOURCES:
        return None
    list_priced = cost is not None and (
        cost_source is None or cost_source.startswith("pricing_table")
    )
    if cost is not None and not list_priced:
        # A framework's own cost: not a catalog list price to discount.
        return None
    o = book.find(vendor, models, at)
    if o is None:
        return None
    if o.multiplier is not None:
        return _r6(_d(cost) * o.multiplier) if list_priced else None
    return token_cost(o, normalize_provider(vendor), usage)


_USAGE_FIELDS = (
    "tokens_in",
    "tokens_out",
    "tokens_cached",
    "tokens_cache_write",
    "tokens_cache_write_1h",
    "service_tier",
    "usage_unit_type",
)


def reprice(
    book: PriceBook, event: Any, vendor: str | None, asset_model: str | None, at: datetime
) -> bool:
    """Price an ingest event (or a stored AIUsageEvent) at the negotiated
    price in force at ``at``. Changes ``cost`` and ``cost_source`` in place;
    True if it did."""
    usage = {k: getattr(event, k, None) for k in _USAGE_FIELDS}
    models = pricing_candidates(
        getattr(event, "served_model", None),
        getattr(event, "requested_model", None),
        asset_model,
        vendor=vendor,
    )
    cost = event.cost
    new = negotiated_cost(
        book,
        vendor,
        models,
        usage,
        cost,
        event.cost_source,
        at,
    )
    if new is None:
        return False
    event.cost = new
    event.cost_source = COST_SOURCE_NEGOTIATED
    return True


# ──────────────────────────────────────────────────────────────────────
# Re-pricing stored events (retroactive contracts)
# ──────────────────────────────────────────────────────────────────────


async def reprice_stored(
    db: AsyncSession, tenant_id: UUID, start: datetime, end: datetime, *, batch: int = 1000
) -> int:
    """Re-price the tenant's stored list-priced (or unpriced) events with
    ``start <= timestamp < end`` at the negotiated prices of its active
    policy. Works in batches of ``batch`` events by id, each committed in
    its own transaction under the tenant's rollup lock (a rollup run never
    reads a half-repriced batch; no long transaction). Events already
    ``negotiated`` keep their cost (their list price is not stored, so it
    cannot be re-discounted), which also makes a re-run, or a run resumed
    after a failure, change nothing twice. The session must be scoped to
    the tenant (rls.set_tenant). Returns the number of events changed."""
    from collector import usage_rollups

    book = await load_price_book(db, tenant_id)
    await db.rollback()
    if not book:
        return 0
    providers = {o.provider for o in book.overrides}
    changed = 0
    last_id = None
    while True:
        await usage_rollups.lock_tenant(db, tenant_id)
        stmt = (
            select(AIUsageEvent, AIAsset.vendor, AIAsset.model)
            .join(AIAsset, AIAsset.id == AIUsageEvent.asset_id)
            .where(
                AIUsageEvent.tenant_id == tenant_id,
                AIAsset.tenant_id == tenant_id,
                AIUsageEvent.timestamp >= start,
                AIUsageEvent.timestamp < end,
                or_(
                    AIUsageEvent.cost_source.is_(None),
                    AIUsageEvent.cost_source.notin_(sorted(KEPT_COST_SOURCES)),
                ),
            )
            .order_by(AIUsageEvent.id)
            .limit(batch)
        )
        if last_id is not None:
            stmt = stmt.where(AIUsageEvent.id > last_id)
        rows = (await db.execute(stmt)).all()
        if not rows:
            await db.commit()
            return changed
        for event, vendor, model in rows:
            last_id = event.id
            if normalize_provider(vendor) not in providers:
                continue
            ts = event.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if reprice(book, event, vendor, model, ts):
                changed += 1
        # Releases the rollup lock; the next batch takes it again.
        await db.commit()
        if len(rows) < batch:
            return changed


async def reprice_and_rebuild(
    session_factory, tenant: UUID, start: datetime, end: datetime, *, batch: int = 1000
) -> dict:
    """`reprice_stored`, then a rollup rebuild that waits for the tenant's
    rollup lock (rollups only follow new ingest on their own, so a skipped
    rebuild would leave them stale). The rebuild runs whenever the tenant
    has prices, even when nothing changed this time, so a run interrupted
    after re-pricing is completed by the next one."""
    from collector import rls, usage_rollups

    async with session_factory() as db:
        await rls.set_tenant(db, tenant)
        has_prices = bool(await load_price_book(db, tenant))
        await db.rollback()
    if not has_prices:
        return {"tenant_id": str(tenant), "events_repriced": 0, "rollups": None}
    async with session_factory() as db:
        await rls.set_tenant(db, tenant)
        changed = await reprice_stored(db, tenant, start, end, batch=batch)
    async with session_factory() as db:
        await rls.set_tenant(db, tenant)
        rebuilt = asdict(await usage_rollups.rebuild_tenant(db, tenant, wait=True))
    return {"tenant_id": str(tenant), "events_repriced": changed, "rollups": rebuilt}


async def _main_reprice(tenant: UUID, start: datetime, end: datetime) -> dict:
    from collector.app import SessionLocal, engine

    try:
        return await reprice_and_rebuild(SessionLocal, tenant, start, end)
    finally:
        await engine.dispose()


def _instant(value: str) -> datetime:
    parsed = policy_doc.parse_instant(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"not a date or date-time with offset: {value!r}")
    return parsed


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m collector.prices",
        description="Re-price stored usage events at the tenant's negotiated prices.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    rp = sub.add_parser("reprice", help="re-price stored events in a window, then rebuild rollups")
    rp.add_argument("--tenant", type=UUID, required=True)
    rp.add_argument("--since", type=_instant, required=True, help="YYYY-MM-DD (UTC) or ISO date-time")
    rp.add_argument("--until", type=_instant, default=None, help="exclusive; default now")
    args = parser.parse_args(argv)
    until = args.until or datetime.now(UTC)
    print(json.dumps(asyncio.run(_main_reprice(args.tenant, args.since, until)), indent=2))


if __name__ == "__main__":
    main()
