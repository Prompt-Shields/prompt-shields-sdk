"""Human attestation of asset fields (#98).

`confidence` says how many sources agree that an asset exists; it does not
mean a person checked it. Attestation is the separate, human sign-off an
auditor asks for: per asset and per field, someone records that the value
is right (`attested`), wrong (`disputed`), no longer relevant (`retired`), or
resets it to `unreviewed`. Attestation never changes `confidence`; both are
returned side by side.

Storage is `asset_attestations`, append-only: each decision is a new row
with who (`attested_by` plus the authenticated tenant key prefix or
partner), when, the state, a comment and a snapshot of the field's value at
that moment. The current state of a field is its newest row. A database
trigger rejects UPDATE, so history cannot be rewritten.

Fields are generic. `register_attestable_field(name, getter)` adds one; the
getter returns the field's current value from an `AIAsset` and is used to
snapshot the value on attestation and to flag `value_changed` when the
registry value has since moved away from what was signed off. Built in:

  - asset               : the asset record itself (vendor, model, use case, ...)
  - owner               : owner_email
  - data_classification : data_classification

Risk classification can register its own field the same way.

Recording an attestation bumps the asset's `updated_at` so it shows up in
the Partner API delta feed (`/partner/v1/changes`). HTTP routes live in
`collector.attestation_api`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from db.models import AIAsset, AssetAttestation
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

UNREVIEWED = "unreviewed"
ATTESTED = "attested"
DISPUTED = "disputed"
RETIRED = "retired"
ATTESTATION_STATES = (UNREVIEWED, ATTESTED, DISPUTED, RETIRED)
AttestationState = Literal["unreviewed", "attested", "disputed", "retired"]

ACTOR_TENANT = "tenant"
ACTOR_PARTNER = "partner"

FieldGetter = Callable[[AIAsset], Any]
_FIELDS: dict[str, FieldGetter] = {}


def register_attestable_field(name: str, getter: FieldGetter) -> None:
    """Make `name` attestable. `getter(asset)` returns its current value,
    which must be JSON serializable (str, number, None, list or dict)."""
    _FIELDS[name] = getter


def attestable_fields() -> tuple[str, ...]:
    return tuple(_FIELDS)


register_attestable_field(
    "asset",
    lambda a: {
        "vendor": a.vendor,
        "model": a.model,
        "use_case_name": a.use_case_name,
        "business_unit": a.business_unit,
        "environment": a.environment,
    },
)
register_attestable_field("owner", lambda a: a.owner_email)
register_attestable_field("data_classification", lambda a: a.data_classification)


def _jsonable(value: Any) -> Any:
    """Normalise to what a JSONB round trip returns, so snapshots compare equal."""
    return json.loads(json.dumps(value, default=str))


def current_value(asset: AIAsset, field: str) -> Any:
    return _jsonable(_FIELDS[field](asset))


def validate(field: str, state: str, comment: str | None) -> None:
    """Raise ValueError if this attestation cannot be recorded."""
    if field not in _FIELDS:
        raise ValueError(
            f"Unknown field {field!r}. Attestable fields: {sorted(_FIELDS)}"
        )
    if state not in ATTESTATION_STATES:
        raise ValueError(
            f"Unknown state {state!r}. Allowed: {list(ATTESTATION_STATES)}"
        )
    if state == DISPUTED and not (comment and comment.strip()):
        raise ValueError("A comment is required when disputing a field")


async def record_attestation(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    asset: AIAsset,
    field: str,
    state: str,
    attested_by: str,
    actor_type: str,
    comment: str | None = None,
    partner_id: UUID | None = None,
    api_key_prefix: str | None = None,
    now: datetime | None = None,
) -> AssetAttestation:
    """Append one attestation row and flush. Does not commit.

    Raises ValueError for an unknown field or state, a dispute without a
    comment, or an asset outside `tenant_id`.
    """
    validate(field, state, comment)
    if asset.tenant_id != tenant_id:
        raise ValueError("Asset does not belong to this tenant")
    now = now or datetime.now(UTC)
    row = AssetAttestation(
        tenant_id=tenant_id,
        asset_id=asset.id,
        field=field,
        state=state,
        value=current_value(asset, field),
        comment=comment,
        attested_by=attested_by,
        actor_type=actor_type,
        partner_id=partner_id,
        api_key_prefix=api_key_prefix,
        created_at=now,
    )
    db.add(row)
    # Surface the change in the delta feed. `confidence` is left untouched.
    asset.updated_at = now
    await db.flush()
    return row


async def latest_attestations(
    db: AsyncSession, tenant_id: UUID, asset_ids: Sequence[UUID]
) -> dict[UUID, dict[str, AssetAttestation]]:
    """Newest attestation per (asset, field), keyed by asset id then field."""
    if not asset_ids:
        return {}
    rank = (
        func.row_number()
        .over(
            partition_by=(AssetAttestation.asset_id, AssetAttestation.field),
            order_by=(AssetAttestation.created_at.desc(), AssetAttestation.id.desc()),
        )
        .label("rank")
    )
    ranked = (
        select(AssetAttestation.id, rank)
        .where(
            AssetAttestation.tenant_id == tenant_id,
            AssetAttestation.asset_id.in_(list(asset_ids)),
        )
        .subquery()
    )
    stmt = select(AssetAttestation).join(
        ranked, (ranked.c.id == AssetAttestation.id) & (ranked.c.rank == 1)
    )
    out: dict[UUID, dict[str, AssetAttestation]] = {}
    for row in (await db.execute(stmt)).scalars().all():
        out.setdefault(row.asset_id, {})[row.field] = row
    return out


async def attestation_history(
    db: AsyncSession,
    tenant_id: UUID,
    asset_id: UUID,
    *,
    field: str | None = None,
    limit: int = 100,
) -> list[AssetAttestation]:
    """Attestations for one asset, newest first."""
    stmt = select(AssetAttestation).where(
        AssetAttestation.tenant_id == tenant_id,
        AssetAttestation.asset_id == asset_id,
    )
    if field is not None:
        stmt = stmt.where(AssetAttestation.field == field)
    stmt = stmt.order_by(
        AssetAttestation.created_at.desc(), AssetAttestation.id.desc()
    ).limit(limit)
    return list((await db.execute(stmt)).scalars())


def serialize_attestation(row: AssetAttestation) -> dict:
    return {
        "id": str(row.id),
        "asset_id": str(row.asset_id),
        "field": row.field,
        "state": row.state,
        "value": row.value,
        "comment": row.comment,
        "attested_by": row.attested_by,
        "attested_at": row.created_at.isoformat() if row.created_at else None,
        "actor_type": row.actor_type,
        "partner_id": str(row.partner_id) if row.partner_id else None,
    }


def summarize(asset: AIAsset, latest: dict[str, AssetAttestation]) -> dict:
    """Current attestation state of every attestable field of `asset`.

    `value_changed` is true when the registry value differs from the value
    that was signed off, i.e. the decision may be out of date.
    """
    out: dict[str, dict] = {}
    for field in _FIELDS:
        row = latest.get(field)
        if row is None:
            out[field] = {
                "state": UNREVIEWED,
                "attested_by": None,
                "attested_at": None,
                "comment": None,
                "value_changed": False,
            }
            continue
        out[field] = {
            "state": row.state,
            "attested_by": row.attested_by,
            "attested_at": row.created_at.isoformat() if row.created_at else None,
            "comment": row.comment,
            "value_changed": row.value != current_value(asset, field),
        }
    return out


def attestation_changes_since(summary: dict, since: datetime) -> list[str]:
    """Fields in a `summarize` result whose latest attestation is at or
    after `since`, for the delta feed."""
    changed = []
    for field, entry in summary.items():
        at = entry.get("attested_at")
        if at and datetime.fromisoformat(at) >= since:
            changed.append(field)
    return changed


async def add_attestation_summaries(
    db: AsyncSession,
    tenant_id: UUID,
    assets: Sequence[AIAsset],
    serialized: Sequence[dict],
) -> None:
    """Set `attestation` on each serialized asset dict, in the same order as
    `assets`. One query for the whole page."""
    latest = await latest_attestations(db, tenant_id, [a.id for a in assets])
    for asset, body in zip(assets, serialized, strict=True):
        body["attestation"] = summarize(asset, latest.get(asset.id, {}))


__all__ = [
    "ATTESTATION_STATES",
    "AttestationState",
    "add_attestation_summaries",
    "attestable_fields",
    "attestation_changes_since",
    "attestation_history",
    "latest_attestations",
    "record_attestation",
    "register_attestable_field",
    "serialize_attestation",
    "summarize",
    "validate",
]
