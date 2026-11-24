"""FinOps FOCUS cost export over `ai_usage_events` (#31).

Rows follow the Cost and Usage dataset of the FinOps Open Cost and Usage
Specification (FOCUS) **v1.4** (announced June 2026; spec tag ``v1.4``,
commit ``f1eeb30``). The Partner API serves them as CSV or JSON Lines from
``GET /partner/v1/usage/focus-export`` (scope ``usage:export``).

One row is one charge: the cost of one asset's calls to one model at one
provider in one UTC hour or day (``granularity``), for one team, project,
calling service and set of cost tags. Events without a ``cost``
are left out: FOCUS cost columns must not be null, and a 0 would be a
placeholder for a cost that was never measured.

Columns
-------
Every Mandatory and Recommended column of the dataset is present
(`FOCUS_COLUMNS`), plus the Conditional columns whose condition applies to
AI usage telemetry, plus custom ``x_`` columns (`CUSTOM_COLUMNS`). Values:

- ``BilledCost``, ``EffectiveCost``, ``ListCost`` and ``ContractedCost`` are
  the summed estimated ``cost`` (USD) with ``cost_mode=estimated`` (the
  default), and 0 with ``cost_mode=merge_safe``. FOCUS says a data
  generator that neither issues the invoice nor originates the cost data
  must report 0 so its rows can be merged with the provider's own FOCUS
  export without double counting; Prompt Shields is such a generator, so
  ``merge_safe`` is the strictly conformant mode. ``estimated`` deviates on
  purpose (FOCUS also wants BilledCost to be invoiced, not estimated,
  amounts), because a standalone export of zeros is of no use. Both modes
  carry the estimate in ``x_EstimatedCost``.
- ``ListUnitPrice`` and ``ContractedUnitPrice`` are not exported, so FOCUS
  requires ``ListCost`` and ``ContractedCost`` to equal ``BilledCost``.
- ``ServiceProviderName``, ``HostProviderName`` and ``InvoiceIssuerName``
  are the provider that served the call (``served_provider``, else the
  asset's vendor). A reseller or marketplace in between is not known from
  telemetry. ``ProviderName`` and ``PublisherName`` were removed in v1.4.
- ``ServiceName`` is the asset's vendor; ``ServiceCategory`` is
  "AI and Machine Learning" and ``ServiceSubcategory`` "Generative AI".
- ``BillingAccountId`` / ``BillingAccountName`` are the Prompt Shields
  tenant (``BillingAccountType`` "Prompt Shields Tenant"): telemetry does
  not know the provider billing account.
- ``BillingPeriodStart`` / ``End`` are the UTC calendar month of the charge.
- ``ResourceId`` is the registry asset id, ``ResourceName`` its use case
  (null without one: FOCUS forbids repeating the id), ``ResourceType``
  "AI Asset"; ``RegionId`` / ``RegionName`` the asset's region, if known.
- ``SkuId`` is the model that served the call (``served_model``, else
  ``requested_model``, else the asset's model).
- ``PricingQuantity`` / ``ConsumedQuantity`` are input plus output tokens
  in ``Tokens``; null (with their units) when no event reported tokens.
- ``Tags`` holds the asset's ``ps:business_unit``, ``ps:use_case`` and
  ``ps:environment`` (the ``ps:`` prefix marks them as Prompt Shields
  registry tags), keys with no value left out, null when none is set.
- ``SubAccountId`` / ``SubAccountName`` are the team the calls are
  attributed to (#347, collector/cost_attribution.py), ``SubAccountType``
  "Team"; all three null for unattributed spend. A team deleted since keeps
  its id with a null name.
- ``Tags`` also holds the calls' cost tags (``X-PS-Tags``, unprefixed) and
  ``ps:project_id`` / ``ps:project`` for the project they are attributed
  to; ``x_ProjectId``, ``x_ProjectName`` and ``x_CallingService`` repeat
  the project and calling service.
- Always null: ``ChargeClass`` (no corrections to closed periods),
  ``AllocatedMethodDetails`` (no split cost allocation) and
  ``AvailabilityZone`` (not reported).

Not exported (Conditional columns whose condition does not apply, or whose
data the collector does not have): ``SkuPriceId``, ``SkuMeter``,
``SkuPriceDetails``, ``ListUnitPrice``, ``ContractedUnitPrice`` and
``PricingCategory`` (no reference into a provider price list; per-token
prices differ for input, output and cache tokens), the ``PricingCurrency*``
columns (pricing and billing currency are both USD), ``InvoiceId`` and
``InvoiceDetailId`` (no invoices), ``Commitment*``, ``CapacityReservation*``
and ``ContractApplied`` (no commitment or contract data) and the other
``Allocated*`` columns (no shared-cost allocation rules yet).

Known gaps against a strict FOCUS validator: FOCUS ties ``PricingQuantity``
and ``SkuId`` to a ``SkuPriceId`` (and ``ListUnitPrice``) for every Usage
charge, which only the provider can supply. Prompt Shields exports token
quantities and the model as SKU without a price-list reference.

CSV cells that are strings starting with ``=``, ``+``, ``-``, ``@``, tab or
carriage return get a leading ``'`` so spreadsheets do not run them as
formulas (`csv_safe`); numbers are left alone, so a negative cost stays a
number. JSON Lines values are left as they are.

Streaming keeps one transaction open (the cursor's snapshot holds back
vacuum) while the body is sent, so it gets an idle-in-transaction timeout
(`STREAM_IDLE_TIMEOUT`) against clients that stop reading, and
`StreamExportResponse` closes the generator, and with it the session, as
soon as the client disconnects.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from db.models import AIAsset, AIUsageEvent, Project, Team
from sqlalchemy import and_, func, select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from collector import rls

FOCUS_VERSION = "1.4"

Granularity = Literal["hour", "day"]
CostMode = Literal["estimated", "merge_safe"]
ExportFormat = Literal["csv", "jsonl"]

# Longest window one request may ask for.
MAX_RANGE_DAYS = 366
# Rows fetched per round trip from the server-side cursor, and per chunk.
STREAM_BATCH = 1000
# The cursor keeps a transaction (and its snapshot, which holds back vacuum)
# open while the body is sent. A client that stops reading for this long
# gets its transaction ended by Postgres instead of holding it open.
STREAM_IDLE_TIMEOUT = "60s"

# FOCUS v1.4 Cost and Usage columns in this export, in output order.
FOCUS_COLUMNS: tuple[str, ...] = (
    "BilledCost",
    "EffectiveCost",
    "ListCost",
    "ContractedCost",
    "BillingCurrency",
    "BillingAccountId",
    "BillingAccountName",
    "BillingAccountType",
    "SubAccountId",
    "SubAccountName",
    "SubAccountType",
    "BillingPeriodStart",
    "BillingPeriodEnd",
    "ChargePeriodStart",
    "ChargePeriodEnd",
    "ChargeCategory",
    "ChargeClass",
    "ChargeDescription",
    "ChargeFrequency",
    "ServiceProviderName",
    "HostProviderName",
    "InvoiceIssuerName",
    "ServiceName",
    "ServiceCategory",
    "ServiceSubcategory",
    "ResourceId",
    "ResourceName",
    "ResourceType",
    "RegionId",
    "RegionName",
    "AvailabilityZone",
    "SkuId",
    "PricingQuantity",
    "PricingUnit",
    "ConsumedQuantity",
    "ConsumedUnit",
    "AllocatedMethodDetails",
    "Tags",
)

CUSTOM_COLUMNS: tuple[str, ...] = (
    "x_EstimatedCost",
    "x_RequestCount",
    "x_InputTokens",
    "x_OutputTokens",
    "x_ModelName",
    "x_BusinessUnit",
    "x_UseCaseName",
    "x_Environment",
    "x_ProjectId",
    "x_ProjectName",
    "x_CallingService",
)

COLUMNS: tuple[str, ...] = FOCUS_COLUMNS + CUSTOM_COLUMNS

TAG_PREFIX = "ps:"
SUB_ACCOUNT_TYPE = "Team"
SERVICE_CATEGORY = "AI and Machine Learning"
SERVICE_SUBCATEGORY = "Generative AI"
BILLING_ACCOUNT_TYPE = "Prompt Shields Tenant"
RESOURCE_TYPE = "AI Asset"
TOKEN_UNIT = "Tokens"
CURRENCY = "USD"

MEDIA_TYPES: dict[str, str] = {
    "csv": "text/csv; charset=utf-8",
    "jsonl": "application/x-ndjson",
}

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


# ──────────────────────────────────────────────────────────────────────
# Formatting
# ──────────────────────────────────────────────────────────────────────


def focus_datetime(value: datetime) -> str:
    """FOCUS DateTimeFormat: ``YYYY-MM-DDTHH:mm:ssZ`` in UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def csv_safe(value: Any) -> Any:
    """Neutralise spreadsheet formulas in a string cell (OWASP CSV injection)."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _month_bounds(moment: datetime) -> tuple[datetime, datetime]:
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        return start, start.replace(year=start.year + 1, month=1)
    return start, start.replace(month=start.month + 1)


def _bucket_width(granularity: str) -> timedelta:
    return timedelta(hours=1) if granularity == "hour" else timedelta(days=1)


def _tags(row: Mapping[str, Any]) -> dict | None:
    # The event's cost tags (#347) never start with "ps:" (reserved at
    # ingest), so they cannot collide with the registry tags.
    user_tags = row.get("tags")
    tags = dict(user_tags) if isinstance(user_tags, dict) else {}
    project_id = row.get("project_id")
    tags.update(
        {
            f"{TAG_PREFIX}business_unit": row["business_unit"],
            f"{TAG_PREFIX}use_case": row["use_case_name"],
            f"{TAG_PREFIX}environment": row["environment"],
            f"{TAG_PREFIX}project_id": str(project_id) if project_id else None,
            f"{TAG_PREFIX}project": row.get("project_name"),
        }
    )
    tags = {k: v for k, v in tags.items() if v is not None}
    return tags or None


# ──────────────────────────────────────────────────────────────────────
# Rows
# ──────────────────────────────────────────────────────────────────────


def focus_row(
    row: Mapping[str, Any],
    *,
    tenant_id: UUID,
    tenant_name: str,
    window_start: datetime,
    window_end: datetime,
    granularity: str,
    cost_mode: str,
) -> dict[str, Any]:
    """One aggregate row (see `focus_query`) -> {column: value}.

    Values are Python types: Decimal for decimals, int for counts, dict for
    Tags, str for everything else, None for null. `serialize_csv` and
    `serialize_jsonl` turn them into text.
    """
    bucket = row["bucket"]
    if bucket.tzinfo is None:
        bucket = bucket.replace(tzinfo=UTC)
    # A bucket cut by the requested window covers only the part inside it.
    charge_start = max(bucket, window_start)
    charge_end = min(bucket + _bucket_width(granularity), window_end)
    period_start, period_end = _month_bounds(bucket)

    estimated: Decimal = row["cost"]
    billed = estimated if cost_mode == "estimated" else Decimal(0)
    provider = row["provider"]
    model = row["model"]
    requests = int(row["requests"])
    tokens_reported = int(row["tokens_reported"] or 0) > 0
    tokens_in = int(row["tokens_in"] or 0)
    tokens_out = int(row["tokens_out"] or 0)
    quantity = Decimal(tokens_in + tokens_out) if tokens_reported else None
    unit = TOKEN_UNIT if tokens_reported else None
    region = row["region"]
    tags = _tags(row)
    team_id = row.get("team_id")
    project_id = row.get("project_id")

    plural = "" if requests == 1 else "s"
    return {
        "BilledCost": billed,
        "EffectiveCost": billed,
        "ListCost": billed,
        "ContractedCost": billed,
        "BillingCurrency": CURRENCY,
        "BillingAccountId": str(tenant_id),
        "BillingAccountName": tenant_name,
        "BillingAccountType": BILLING_ACCOUNT_TYPE,
        "SubAccountId": str(team_id) if team_id else None,
        "SubAccountName": row.get("team_name") if team_id else None,
        "SubAccountType": SUB_ACCOUNT_TYPE if team_id else None,
        "BillingPeriodStart": focus_datetime(period_start),
        "BillingPeriodEnd": focus_datetime(period_end),
        "ChargePeriodStart": focus_datetime(charge_start),
        "ChargePeriodEnd": focus_datetime(charge_end),
        "ChargeCategory": "Usage",
        "ChargeClass": None,
        "ChargeDescription": f"{model or 'unknown model'} usage ({requests} request{plural})",
        "ChargeFrequency": "Usage-Based",
        "ServiceProviderName": provider,
        "HostProviderName": provider,
        "InvoiceIssuerName": provider,
        "ServiceName": row["vendor"],
        "ServiceCategory": SERVICE_CATEGORY,
        "ServiceSubcategory": SERVICE_SUBCATEGORY,
        "ResourceId": str(row["asset_id"]),
        "ResourceName": row["use_case_name"],
        "ResourceType": RESOURCE_TYPE,
        "RegionId": region,
        "RegionName": region,
        "AvailabilityZone": None,
        "SkuId": model,
        "PricingQuantity": quantity,
        "PricingUnit": unit,
        "ConsumedQuantity": quantity,
        "ConsumedUnit": unit,
        "AllocatedMethodDetails": None,
        "Tags": tags,
        "x_EstimatedCost": estimated,
        "x_RequestCount": requests,
        "x_InputTokens": tokens_in if tokens_reported else None,
        "x_OutputTokens": tokens_out if tokens_reported else None,
        "x_ModelName": model,
        "x_BusinessUnit": row["business_unit"],
        "x_UseCaseName": row["use_case_name"],
        "x_Environment": row["environment"],
        "x_ProjectId": str(project_id) if project_id else None,
        "x_ProjectName": row.get("project_name") if project_id else None,
        "x_CallingService": row.get("calling_service"),
    }


def _text(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def serialize_csv(rows: list[dict[str, Any]], header: bool) -> str:
    """CSV text for `rows` (null is an empty cell), formula-safe."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    if header:
        writer.writerow(COLUMNS)
    for row in rows:
        # Guard the value, not its text: a negative Decimal must stay a number.
        writer.writerow([_text(csv_safe(row[c])) for c in COLUMNS])
    return buf.getvalue()


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    return value


def serialize_jsonl(rows: list[dict[str, Any]]) -> str:
    """One JSON object per line, keys in column order, null for null."""
    return "".join(
        json.dumps({c: _json_value(row[c]) for c in COLUMNS}, ensure_ascii=False) + "\n"
        for row in rows
    )


# ──────────────────────────────────────────────────────────────────────
# Query and stream
# ──────────────────────────────────────────────────────────────────────


def focus_query(tenant_id: UUID, start: datetime, end: datetime, granularity: str):
    """Aggregate ``[start, end)`` per UTC bucket, asset, model and provider."""
    bucket = func.date_trunc(granularity, func.timezone("UTC", AIUsageEvent.timestamp))
    model = func.coalesce(
        AIUsageEvent.served_model, AIUsageEvent.requested_model, AIAsset.model
    )
    provider = func.coalesce(AIUsageEvent.served_provider, AIAsset.vendor)
    keys = [
        bucket.label("bucket"),
        AIUsageEvent.asset_id.label("asset_id"),
        AIAsset.vendor.label("vendor"),
        AIAsset.use_case_name.label("use_case_name"),
        AIAsset.business_unit.label("business_unit"),
        AIAsset.environment.label("environment"),
        AIAsset.region.label("region"),
        model.label("model"),
        provider.label("provider"),
        AIUsageEvent.team_id.label("team_id"),
        Team.name.label("team_name"),
        AIUsageEvent.project_id.label("project_id"),
        Project.name.label("project_name"),
        AIUsageEvent.calling_service.label("calling_service"),
        AIUsageEvent.tags.label("tags"),
    ]
    return (
        select(
            *keys,
            func.count(AIUsageEvent.id).label("requests"),
            func.sum(AIUsageEvent.cost).label("cost"),
            func.sum(AIUsageEvent.tokens_in).label("tokens_in"),
            func.sum(AIUsageEvent.tokens_out).label("tokens_out"),
            (func.count(AIUsageEvent.tokens_in) + func.count(AIUsageEvent.tokens_out)).label(
                "tokens_reported"
            ),
        )
        .join(
            AIAsset,
            and_(
                AIAsset.id == AIUsageEvent.asset_id,
                AIAsset.tenant_id == AIUsageEvent.tenant_id,
            ),
        )
        .outerjoin(
            Team,
            and_(Team.id == AIUsageEvent.team_id, Team.tenant_id == AIUsageEvent.tenant_id),
        )
        .outerjoin(
            Project,
            and_(
                Project.id == AIUsageEvent.project_id,
                Project.tenant_id == AIUsageEvent.tenant_id,
            ),
        )
        .where(
            AIUsageEvent.tenant_id == tenant_id,
            AIUsageEvent.timestamp >= start,
            AIUsageEvent.timestamp < end,
            AIUsageEvent.cost.is_not(None),
        )
        # Labels render as the output column names, so each expression (and
        # its bound parameters) appears once.
        .group_by(*keys)
        .order_by(keys[0], keys[1], keys[7], keys[8])
    )


async def stream_export(
    bind,
    *,
    tenant_id: UUID,
    tenant_name: str,
    start: datetime,
    end: datetime,
    granularity: str,
    cost_mode: str,
    fmt: str,
) -> AsyncIterator[bytes]:
    """Yield the export in chunks of `STREAM_BATCH` rows.

    Runs on its own session, scoped to the tenant for row-level security,
    because the request's session is closed by the DB middleware before a
    streamed body is sent. Rows come from a server-side cursor, so memory
    does not grow with the window.
    """
    if fmt == "csv":
        yield serialize_csv([], header=True).encode()
    async with AsyncSession(bind, expire_on_commit=False) as session:
        await rls.set_tenant(session, tenant_id)
        # Opens the transaction (the RLS context is applied on begin).
        await session.execute(
            sql_text("SELECT set_config('idle_in_transaction_session_timeout', :t, true)"),
            {"t": STREAM_IDLE_TIMEOUT},
        )
        result = await session.stream(
            focus_query(tenant_id, start, end, granularity).execution_options(
                yield_per=STREAM_BATCH
            )
        )
        async for part in result.mappings().partitions(STREAM_BATCH):
            rows = [
                focus_row(
                    r,
                    tenant_id=tenant_id,
                    tenant_name=tenant_name,
                    window_start=start,
                    window_end=end,
                    granularity=granularity,
                    cost_mode=cost_mode,
                )
                for r in part
            ]
            text = serialize_csv(rows, header=False) if fmt == "csv" else serialize_jsonl(rows)
            yield text.encode()



class StreamExportResponse(StreamingResponse):
    """A streamed export that closes its body iterator however it ends.

    Starlette stops iterating when the client disconnects but leaves the
    generator suspended until it is garbage collected, so the export's
    session (connection and open transaction) would stay checked out until
    then. Closing it here returns the connection right away.
    """

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            aclose = getattr(self.body_iterator, "aclose", None)
            if aclose is not None:
                await aclose()


__all__ = [
    "COLUMNS",
    "CUSTOM_COLUMNS",
    "FOCUS_COLUMNS",
    "FOCUS_VERSION",
    "MAX_RANGE_DAYS",
    "MEDIA_TYPES",
    "CostMode",
    "ExportFormat",
    "Granularity",
    "StreamExportResponse",
    "csv_safe",
    "focus_datetime",
    "focus_query",
    "focus_row",
    "serialize_csv",
    "serialize_jsonl",
    "stream_export",
]
