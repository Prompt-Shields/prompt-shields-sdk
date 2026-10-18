"""Merge duplicate assets left behind by past ingest races.

Before ingest locked asset creation (#195), two concurrent batches for a
new key could both create an asset for it. `dedup.find_or_create_asset`
then sends every later event for the key to one of them (the oldest, see
`dedup.OLDEST_FIRST`), and the others sit in the registry as duplicates
with the early events.

Which assets are duplicates
  Assets of one tenant with the same merge key, the five fields
  `find_or_create_asset` matches on (vendor, model, use case, business
  unit, environment), compared exactly: an empty field only equals an
  empty field. Two such assets can only come from a race, because a
  lookup for that key always finds the existing one.

  Assets the provider usage pull and the cloud inventory create are left
  out: they deliberately keep one asset per (project, API key, model) and
  per deployment, often with the same merge key. They are recognised by
  their deterministic UUIDv5 ids (`provider_usage.asset_id_for`,
  `cloud_inventory.asset_id_for`); assets ingest creates have UUIDv7 ids.

Which asset survives
  The oldest (`dedup.OLDEST_FIRST`: created first), so the asset that
  keeps receiving the key's events stays. Attestations are append-only
  (database triggers reject UPDATE and DELETE), so an attested asset
  cannot be merged away: if exactly one asset of a group carries
  attestations, it survives instead; any other attested asset in the group
  is left in place and reported as `skipped` for a person to resolve.

What a merge does, per duplicate, in one transaction per tenant
  - usage events and webhook delivery history (`webhook_deliveries.asset_id`,
    no foreign key; the payloads already sent keep the id they carried)
    are re-pointed to the survivor;
  - data flows are folded into the survivor's (event counts add up, seen
    times and PII categories widen, the higher classification wins);
  - risk mappings move to the survivor; where both have a row for the same
    rule, a reviewed row (confirmed or rejected) beats a suggestion and
    the survivor's row wins a tie;
  - the survivor takes the union of discovery sources (confidence is
    recomputed), the earliest `first_seen`, the latest `last_seen` (with
    that asset's lifecycle state, unless the survivor is owner-retired),
    the higher data classification and any field it lacks;
  - the duplicate is deleted.
  `partner_audit_log.asset_ids` (a record of what was read) and assets
  already pushed to ServiceNow or LeanIX are not rewritten.

  One `retention_audit_log` row (action `merge_assets`, details: survivor
  -> merged ids and row counts) records each tenant's merge, and one
  `asset.changed` webhook per survivor carries `merged_asset_ids`. There is
  no event type for a removed asset; partners drop the listed ids.

CLI (dry run unless --apply; runs each tenant under its own RLS context)::

    python -m collector.dedup merge-duplicates [--tenant <id> ...] [--apply]

Running it again after an applied merge finds nothing to do.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from uuid import UUID

from db.models import (
    AIAsset,
    AIUsageEvent,
    AssetAttestation,
    DataFlow,
    RetentionAuditLog,
    RiskMapping,
    WebhookDelivery,
)
from sqlalchemy import delete, null, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from collector import data_flows
from collector.dedup import CLASSIFICATION_ORDER, OLDEST_FIRST, compute_confidence
from collector.lifecycle import lock_assets
from collector.webhooks import changed_fields, enqueue_asset_merged

AUDIT_ACTION = "merge_assets"
KEY_COLUMNS = ("vendor", "model", "use_case_name", "business_unit", "environment")
RISK_REVIEWED = ("confirmed", "rejected")


def is_pulled(asset_id: UUID) -> bool:
    """True for the deterministic ids of provider usage and cloud inventory
    assets, which may share a merge key on purpose."""
    return asset_id.version == 5


@dataclass
class MergeGroup:
    key: dict
    survivor: UUID
    merged: list[UUID]
    # Attested assets that cannot be merged away (see the module docstring).
    skipped: list[UUID] = field(default_factory=list)


@dataclass
class TenantMerge:
    tenant_id: UUID
    applied: bool
    groups: list[MergeGroup] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "tenant_id": str(self.tenant_id),
            "applied": self.applied,
            "groups": [
                {
                    "key": g.key,
                    "survivor": str(g.survivor),
                    "merged": [str(i) for i in g.merged],
                    "skipped": [str(i) for i in g.skipped],
                }
                for g in self.groups
            ],
            "counts": self.counts,
        }


async def find_duplicate_groups(db: AsyncSession, tenant_id: UUID) -> list[MergeGroup]:
    """Groups of duplicates in one tenant, survivor chosen. Reads only."""
    rows = (
        await db.execute(
            select(AIAsset.id, *(getattr(AIAsset, c) for c in KEY_COLUMNS))
            .where(AIAsset.tenant_id == tenant_id)
            .order_by(*OLDEST_FIRST)
        )
    ).all()
    by_key: dict[tuple, list[UUID]] = defaultdict(list)
    for row in rows:
        if not is_pulled(row.id):
            by_key[tuple(getattr(row, c) for c in KEY_COLUMNS)].append(row.id)
    dup_sets = {k: ids for k, ids in by_key.items() if len(ids) > 1}
    if not dup_sets:
        return []

    candidates = [i for ids in dup_sets.values() for i in ids]
    attested = set(
        (
            await db.execute(
                select(AssetAttestation.asset_id)
                .where(
                    AssetAttestation.tenant_id == tenant_id,
                    AssetAttestation.asset_id.in_(candidates),
                )
                .distinct()
            )
        ).scalars()
    )
    groups = []
    for key, ids in dup_sets.items():  # ids are oldest first
        with_history = [i for i in ids if i in attested]
        survivor = with_history[0] if with_history else ids[0]
        skipped = with_history[1:]
        merged = [i for i in ids if i != survivor and i not in skipped]
        if merged:
            groups.append(
                MergeGroup(
                    key=dict(zip(KEY_COLUMNS, key, strict=True)),
                    survivor=survivor,
                    merged=merged,
                    skipped=skipped,
                )
            )
    return groups


def _absorb(survivor: AIAsset, dup: AIAsset) -> None:
    """Fold `dup`'s asset fields into `survivor` (see the module docstring)."""
    sources = list(survivor.discovery_source or [])
    for s in dup.discovery_source or []:
        if s not in sources:
            sources.append(s)
    if sources != list(survivor.discovery_source or []):
        survivor.discovery_source = sources
        survivor.confidence = compute_confidence(sources)
    if dup.first_seen is not None and (
        survivor.first_seen is None or dup.first_seen < survivor.first_seen
    ):
        survivor.first_seen = dup.first_seen
    if dup.last_seen is not None and (
        survivor.last_seen is None or dup.last_seen > survivor.last_seen
    ):
        survivor.last_seen = dup.last_seen
        # The most recently seen asset's state is the current one. An owner's
        # retirement of the survivor stands (the duplicate has no
        # attestations, so it is never owner-retired itself).
        if survivor.retired_by is None and dup.lifecycle_state != survivor.lifecycle_state:
            survivor.lifecycle_state = dup.lifecycle_state
            survivor.lifecycle_changed_at = dup.lifecycle_changed_at
    if CLASSIFICATION_ORDER.get(dup.data_classification, -1) > CLASSIFICATION_ORDER.get(
        survivor.data_classification, -1
    ):
        survivor.data_classification = dup.data_classification
    for name in ("owner_email", "calling_service", "region", "modality"):
        if getattr(survivor, name) is None and getattr(dup, name) is not None:
            setattr(survivor, name, getattr(dup, name))
    if survivor.embedding is None and dup.embedding is not None:
        survivor.embedding = dup.embedding


async def _move_flows(db: AsyncSession, tenant_id: UUID, dup: UUID, survivor: UUID) -> int:
    flows = (
        await db.execute(
            select(DataFlow).where(DataFlow.tenant_id == tenant_id, DataFlow.asset_id == dup)
        )
    ).scalars().all()
    if not flows:
        return 0
    rows = [
        {
            "id": f.id,
            "tenant_id": tenant_id,
            "asset_id": survivor,
            "source_system": f.source_system,
            "destination_system": f.destination_system,
            "direction": f.direction,
            "data_classification": f.data_classification,
            # SQL NULL, not a JSON null (as data_flows writes it).
            "detected_pii_types": (
                f.detected_pii_types if f.detected_pii_types is not None else null()
            ),
            "event_count": f.event_count or 0,
            "first_seen": f.first_seen,
            "last_seen": f.last_seen,
        }
        for f in flows
    ]
    for f in flows:
        db.expunge(f)
    # Delete first: an upserted row keeps the duplicate's id when the
    # survivor has no flow with that key.
    await db.execute(
        delete(DataFlow).where(DataFlow.tenant_id == tenant_id, DataFlow.asset_id == dup)
    )
    # A duplicate's flows are unique by key, so one statement is safe.
    await data_flows._upsert(db, rows, additive=True)
    return len(rows)


async def _move_risks(db: AsyncSession, tenant_id: UUID, dup: UUID, survivor: UUID) -> int:
    def rows_of(asset_id):
        return select(RiskMapping).where(
            RiskMapping.tenant_id == tenant_id, RiskMapping.asset_id == asset_id
        )

    kept = {
        r.rule_id: r
        for r in (await db.execute(rows_of(survivor))).scalars()
        if r.rule_id is not None
    }
    drop: list[UUID] = []
    for r in (await db.execute(rows_of(dup))).scalars():
        other = kept.get(r.rule_id) if r.rule_id is not None else None
        if other is None:
            continue
        if other.status not in RISK_REVIEWED and r.status in RISK_REVIEWED:
            drop.append(other.id)
        else:
            drop.append(r.id)
    if drop:
        await db.execute(delete(RiskMapping).where(RiskMapping.id.in_(drop)))
    moved = await db.execute(
        update(RiskMapping)
        .where(RiskMapping.tenant_id == tenant_id, RiskMapping.asset_id == dup)
        .values(asset_id=survivor)
        .execution_options(synchronize_session=False)
    )
    return moved.rowcount or 0


async def _repoint(db: AsyncSession, model, tenant_id: UUID, dup: UUID, survivor: UUID) -> int:
    result = await db.execute(
        update(model)
        .where(model.tenant_id == tenant_id, model.asset_id == dup)
        .values(asset_id=survivor)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


async def merge_tenant_duplicates(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    apply: bool = False,
    actor: str = "cli",
) -> TenantMerge:
    """Find (and with `apply`, merge) one tenant's duplicate assets.

    The session must be scoped to the tenant (`rls.set_tenant`). With
    `apply` it commits; without, it only reads.
    """
    result = TenantMerge(tenant_id=tenant_id, applied=apply)
    groups = await find_duplicate_groups(db, tenant_id)
    if not apply or not groups:
        result.groups = groups
        return result

    # Lock every asset involved in id order (the order ingest takes them
    # in), then choose again: a person may have attested one meanwhile.
    await lock_assets(db, [i for g in groups for i in (g.survivor, *g.merged)])
    groups = await find_duplicate_groups(db, tenant_id)
    result.groups = groups
    if not groups:
        await db.commit()
        return result

    counts = defaultdict(int)
    for group in groups:
        survivor = await db.get(AIAsset, group.survivor)
        dups = [await db.get(AIAsset, i) for i in group.merged]
        for dup in dups:
            _absorb(survivor, dup)
        # Before anything below flushes the session.
        fields = changed_fields(survivor)
        for dup in dups:
            counts["usage_events"] += await _repoint(
                db, AIUsageEvent, tenant_id, dup.id, survivor.id
            )
            counts["webhook_deliveries"] += await _repoint(
                db, WebhookDelivery, tenant_id, dup.id, survivor.id
            )
            counts["data_flows"] += await _move_flows(db, tenant_id, dup.id, survivor.id)
            counts["risk_mappings"] += await _move_risks(db, tenant_id, dup.id, survivor.id)
            db.expunge(dup)
            await db.execute(
                delete(AIAsset).where(AIAsset.tenant_id == tenant_id, AIAsset.id == dup.id)
            )
            counts["assets_merged"] += 1
        await db.flush()
        counts["webhooks_queued"] += await enqueue_asset_merged(
            db, tenant_id, survivor, group.merged, fields
        )

    result.counts = dict(counts)
    db.add(
        RetentionAuditLog(
            tenant_id=tenant_id,
            action=AUDIT_ACTION,
            actor=actor,
            details={
                "merged": {str(g.survivor): [str(i) for i in g.merged] for g in groups},
                "skipped": [str(i) for g in groups for i in g.skipped],
                **result.counts,
            },
        )
    )
    await db.commit()
    return result
