"""Savings and sovereign-share reports over `ai_usage_events` (#26).

Tenant-wide aggregates for the Partner API (`/partner/v1/usage/savings`,
`/partner/v1/usage/sovereign-share`) and the `sovereign_share` field of
the per-asset usage summary. Aggregates only: no individual event, prompt
or response content ever leaves this module.

Savings
-------
An event counts towards savings only when it reported both ``cost`` and
``counterfactual_cost`` (what the call would have cost on its baseline
model, ``baseline_id``). An event missing either one is still counted in
``total_requests`` and ``estimated_cost_usd`` but adds nothing to
``compared_cost_usd``, ``counterfactual_cost_usd`` or ``savings_usd``: a
missing counterfactual is "not measured", never "saved nothing".
``counterfactual_coverage`` says how many requests that is.

Sovereign share
---------------
A call is sovereign when its ``served_jurisdiction`` is ``EEA`` or an EEA
country code (`SOVEREIGN_JURISDICTIONS`, the same set the SDK route policy
uses). Events that did not report a jurisdiction are excluded from both
the numerator and the denominator, so the shares describe only calls
whose serving location is known. ``served_jurisdiction`` is the
jurisdiction the route policy states for the serving tier, so a
US-headquartered provider's EU region counts when the policy marks it
``EEA``. US CLOUD Act exposure is not stored per event (and
``served_provider`` is a customer-chosen label, not a catalog id), so this
is a jurisdiction share, not a sovereignty guarantee or a legal assessment.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

from db.models import AIAsset, AIUsageEvent
from sqlalchemy import String, and_, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from collector.ingest import OUTCOME_SIGNAL_CLASSES

# Mirrors prompt_shields.route_policy.EEA_COUNTRIES plus the "EEA" region
# label the catalog uses. The collector image does not ship the SDK, so
# the set is repeated here; test_usage_reports checks they stay equal.
# fmt: off
EEA_COUNTRIES = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
    "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
    "SI", "ES", "SE", "IS", "LI", "NO",
})
# fmt: on
SOVEREIGN_JURISDICTIONS: frozenset[str] = frozenset({"EEA"}) | EEA_COUNTRIES

SavingsGroupBy = Literal["none", "day", "week", "month", "asset", "use_case", "business_unit"]
SAVINGS_GROUP_BY: tuple[str, ...] = (
    "none",
    "day",
    "week",
    "month",
    "asset",
    "use_case",
    "business_unit",
)


def _sovereign_case():
    """1 for a sovereign event, NULL otherwise (for COUNT/SUM)."""
    return AIUsageEvent.served_jurisdiction.in_(sorted(SOVEREIGN_JURISDICTIONS))


def sovereign_share_columns() -> list:
    """Labelled aggregates for the usage summary's `sovereign_share`."""
    return [
        func.count(AIUsageEvent.served_jurisdiction).label("jurisdiction_reported"),
        func.count(case((_sovereign_case(), 1))).label("sovereign_requests"),
    ]


def sovereign_share(row: Mapping[str, Any]) -> float | None:
    """Share (0 to 1) of requests with a known jurisdiction served in the EEA."""
    reported = int(row["jurisdiction_reported"] or 0)
    if not reported:
        return None
    return round(int(row["sovereign_requests"] or 0) / reported, 4)


# ──────────────────────────────────────────────────────────────────────
# Outcome signals (#44)
# ──────────────────────────────────────────────────────────────────────


def outcome_signal_columns() -> list:
    """Labelled aggregates for the usage summary's outcome signal counts:
    events reporting ``outcome_signals`` (an empty list counts: measured,
    nothing happened) and, per class, the events that carried it."""
    return [
        func.count(AIUsageEvent.outcome_signals).label("outcome_signals_reported"),
        *(
            func.count(case((AIUsageEvent.outcome_signals.contains([name]), 1))).label(
                f"outcome_{name}"
            )
            for name in OUTCOME_SIGNAL_CLASSES
        ),
    ]


def outcome_signal_counts(row: Mapping[str, Any]) -> dict:
    """``outcome_signals_reported`` and ``outcome_signal_counts`` ({class:
    events}); the counts are null when no event reported outcome signals,
    since zeros would claim a measurement never taken."""
    reported = int(row["outcome_signals_reported"] or 0)
    return {
        "outcome_signals_reported": reported,
        "outcome_signal_counts": (
            {name: int(row[f"outcome_{name}"] or 0) for name in OUTCOME_SIGNAL_CLASSES}
            if reported
            else None
        ),
    }


def _money(value) -> float | None:
    return round(float(value), 6) if value is not None else None


def _ratio(num, den) -> float | None:
    if num is None or den is None or float(den) == 0:
        return None
    return round(float(num) / float(den), 4)


# ──────────────────────────────────────────────────────────────────────
# Savings
# ──────────────────────────────────────────────────────────────────────


def _savings_key(group_by: str):
    """SQL expression for the bucket key, or None for a single total."""
    if group_by in ("day", "week", "month"):
        # Buckets start at 00:00 UTC (weeks on Monday), whatever the
        # session time zone.
        return func.to_char(
            func.date_trunc(group_by, func.timezone("UTC", AIUsageEvent.timestamp)),
            "YYYY-MM-DD",
        )
    if group_by == "asset":
        return cast(AIUsageEvent.asset_id, String)
    if group_by == "use_case":
        return AIAsset.use_case_name
    if group_by == "business_unit":
        return AIAsset.business_unit
    return None


def _savings_columns() -> list:
    both = and_(
        AIUsageEvent.cost.is_not(None), AIUsageEvent.counterfactual_cost.is_not(None)
    )
    return [
        func.count(AIUsageEvent.id).label("total_requests"),
        func.sum(AIUsageEvent.cost).label("estimated_cost_usd"),
        func.count(case((both, 1))).label("requests_with_counterfactual"),
        func.sum(case((both, AIUsageEvent.cost))).label("compared_cost_usd"),
        func.sum(case((both, AIUsageEvent.counterfactual_cost))).label(
            "counterfactual_cost_usd"
        ),
        # NULL unless both costs were reported, so sum() skips it.
        func.sum(AIUsageEvent.counterfactual_cost - AIUsageEvent.cost).label(
            "savings_usd"
        ),
    ]


def _savings_bucket(row: Mapping[str, Any]) -> dict:
    total = int(row["total_requests"] or 0)
    compared = int(row["requests_with_counterfactual"] or 0)
    savings = row["savings_usd"]
    counterfactual = row["counterfactual_cost_usd"]
    return {
        "total_requests": total,
        "estimated_cost_usd": _money(row["estimated_cost_usd"]),
        "requests_with_counterfactual": compared,
        "counterfactual_coverage": round(compared / total, 4) if total else None,
        "compared_cost_usd": _money(row["compared_cost_usd"]),
        "counterfactual_cost_usd": _money(counterfactual),
        "savings_usd": _money(savings),
        "savings_rate": _ratio(savings, counterfactual),
    }


async def savings_report(
    db: AsyncSession,
    tenant_id: UUID,
    since,
    period: str,
    group_by: str,
) -> dict:
    """Realised savings against the counterfactual, total and per bucket."""
    where = [AIUsageEvent.tenant_id == tenant_id, AIUsageEvent.timestamp >= since]
    total_row = (
        (await db.execute(select(*_savings_columns()).where(*where))).mappings().one()
    )

    data: list[dict] = []
    key = _savings_key(group_by)
    if key is not None:
        stmt = select(key.label("key"), *_savings_columns()).where(*where)
        if group_by in ("use_case", "business_unit"):
            stmt = stmt.join(
                AIAsset,
                and_(
                    AIAsset.id == AIUsageEvent.asset_id,
                    AIAsset.tenant_id == AIUsageEvent.tenant_id,
                ),
            )
        stmt = stmt.group_by(key).order_by(key.asc().nulls_last())
        for row in (await db.execute(stmt)).mappings().all():
            data.append({"key": row["key"], **_savings_bucket(row)})

    return {
        "period": period,
        "group_by": group_by,
        "total": _savings_bucket(total_row),
        "data": data,
    }


# ──────────────────────────────────────────────────────────────────────
# Sovereign share
# ──────────────────────────────────────────────────────────────────────


def _sovereign_columns() -> list:
    located = AIUsageEvent.served_jurisdiction.is_not(None)
    sovereign = _sovereign_case()
    return [
        func.count(AIUsageEvent.id).label("total_requests"),
        func.count(AIUsageEvent.served_jurisdiction).label("jurisdiction_reported"),
        func.count(case((sovereign, 1))).label("sovereign_requests"),
        func.sum(case((located, AIUsageEvent.cost))).label("jurisdiction_cost_usd"),
        func.sum(case((sovereign, AIUsageEvent.cost))).label("sovereign_cost_usd"),
    ]


def _sovereign_bucket(row: Mapping[str, Any]) -> dict:
    located_cost = row["jurisdiction_cost_usd"]
    sovereign_cost = row["sovereign_cost_usd"]
    if located_cost is not None and sovereign_cost is None:
        sovereign_cost = 0  # costs were reported, none of them sovereign
    return {
        "total_requests": int(row["total_requests"] or 0),
        "requests_with_jurisdiction": int(row["jurisdiction_reported"] or 0),
        "sovereign_requests": int(row["sovereign_requests"] or 0),
        "request_share": sovereign_share(row),
        "cost_with_jurisdiction_usd": _money(located_cost),
        "sovereign_cost_usd": _money(sovereign_cost),
        "spend_share": _ratio(sovereign_cost, located_cost),
    }


async def sovereign_share_report(
    db: AsyncSession, tenant_id: UUID, since, period: str
) -> dict:
    """Share of calls and spend served in the EEA, total and per sensitivity level."""
    where = [AIUsageEvent.tenant_id == tenant_id, AIUsageEvent.timestamp >= since]
    total_row = (
        (await db.execute(select(*_sovereign_columns()).where(*where))).mappings().one()
    )
    level = AIUsageEvent.sensitivity_level
    rows = (
        (
            await db.execute(
                select(level.label("sensitivity_level"), *_sovereign_columns())
                .where(*where)
                .group_by(level)
                .order_by(level.asc().nulls_last())
            )
        )
        .mappings()
        .all()
    )
    return {
        "period": period,
        "sovereign_jurisdictions": sorted(SOVEREIGN_JURISDICTIONS),
        "total": _sovereign_bucket(total_row),
        "by_sensitivity_level": [
            {"sensitivity_level": r["sensitivity_level"], **_sovereign_bucket(r)}
            for r in rows
        ],
    }


__all__ = [
    "EEA_COUNTRIES",
    "SAVINGS_GROUP_BY",
    "SOVEREIGN_JURISDICTIONS",
    "SavingsGroupBy",
    "savings_report",
    "sovereign_share",
    "sovereign_share_columns",
    "sovereign_share_report",
]
