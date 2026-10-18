import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import NamedTuple
from uuid import UUID

from db.models import AIAsset
from sqlalchemy import and_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from collector.lifecycle import lock_for_sighting, reactivate

CLASSIFICATION_ORDER = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}


# Single-source confidence. Runtime observation of real LLM calls is "high":
# the SDK and gateway see them directly, and ``otel`` GenAI spans are the same
# evidence emitted by third-party instrumentation (#92). Signals that show AI
# is reachable or configured, but not that a given call happened, are
# "medium": endpoint and platform signals, and ``code_scan`` (#95), which
# proves the code references a provider/model but not that it runs in
# production. Self-reported sources (``survey``) and anything unknown are "low".
# Two or more distinct sources corroborate each other: "verified".
HIGH_CONFIDENCE_SOURCES = frozenset({"sdk", "gateway", "otel"})
MEDIUM_CONFIDENCE_SOURCES = frozenset(
    {"browser_extension", "macos_app", "platform_signal", "code_scan"}
)


def compute_confidence(sources: list[str]) -> str:
    if len(set(sources)) >= 2:
        return "verified"
    source = sources[0] if sources else ""
    if source in HIGH_CONFIDENCE_SOURCES:
        return "high"
    if source in MEDIUM_CONFIDENCE_SOURCES:
        return "medium"
    return "low"


class AssetKey(NamedTuple):
    """The merge key ``find_or_create_asset`` matches on. A field that is
    empty is not matched on, so an event that omits it joins any asset that
    agrees on the rest."""

    vendor: str
    model: str | None = None
    use_case_name: str | None = None
    business_unit: str | None = None
    environment: str | None = None


def _conditions(tenant_id: UUID, key: AssetKey) -> list:
    conditions = [AIAsset.tenant_id == tenant_id, AIAsset.vendor == key.vendor]
    if key.model:
        conditions.append(AIAsset.model == key.model)
    if key.use_case_name:
        conditions.append(AIAsset.use_case_name == key.use_case_name)
    if key.business_unit:
        conditions.append(AIAsset.business_unit == key.business_unit)
    if key.environment:
        conditions.append(AIAsset.environment == key.environment)
    return conditions


async def _lookup(db: AsyncSession, tenant_id: UUID, key: AssetKey) -> AIAsset | None:
    # Several assets can match one key, by design: the provider usage puller
    # keeps one platform_signal asset per (project, API key, model), and a
    # key that omits use case/BU/environment matches every asset that sets
    # them. Pick one deterministically, the oldest, so repeated events for
    # the key always land on the same asset (and never raise
    # MultipleResultsFound).
    stmt = (
        select(AIAsset)
        .where(and_(*_conditions(tenant_id, key)))
        .order_by(AIAsset.first_seen.asc().nulls_last(), AIAsset.id.asc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


# Creation locks are taken per bucket of keys, not per key, so a batch with
# many new keys holds at most this many advisory locks. Each one uses a slot
# in Postgres's shared lock table (sized max_locks_per_transaction (64) x
# connections, shared by every tenant), so one lock per key would let a
# single large batch of new keys exhaust it ("out of shared memory") for
# the whole database. Keys sharing a bucket only serialise their creation.
ASSET_LOCK_BUCKETS = 64


def _lock_id(tenant_id: UUID, key: AssetKey) -> int:
    parts = [key.vendor] + [p or None for p in key[1:]]
    bucket = int.from_bytes(
        hashlib.sha256(json.dumps(parts).encode()).digest()[:8], "big"
    ) % ASSET_LOCK_BUCKETS
    digest = hashlib.sha256(
        json.dumps(["ai_asset", str(tenant_id), bucket]).encode()
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


async def _lock(db: AsyncSession, lock_id: int) -> None:
    # Transaction-scoped: released on commit/rollback. ai_assets has no unique
    # constraint on the merge key (it can't: see _lookup), so this is what
    # stops two concurrent ingests from both creating the same new asset.
    await db.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": lock_id})


async def lock_asset_keys(
    db: AsyncSession, tenant_id: UUID, keys: Iterable[AssetKey]
) -> None:
    """Take every lock a batch will need, in a fixed order, before it calls
    ``find_or_create_asset`` per event: the creation lock of each key that
    has no asset yet (sorted by lock id; at most ``ASSET_LOCK_BUCKETS``),
    then the rows of the assets the keys resolve to (sorted by id).

    ``find_or_create_asset`` locks a missing key on its own, and updates
    each matched asset's row, in event order. Two batches doing that for
    the same keys in different orders would deadlock (one aborted with a
    500). Locking everything up front in one global order cannot.
    """
    keys = set(keys)
    asset_ids = set()
    missing = []
    for key in keys:
        asset = await _lookup(db, tenant_id, key)
        if asset is None:
            missing.append(key)
        else:
            asset_ids.add(asset.id)
    for lock_id in sorted({_lock_id(tenant_id, key) for key in missing}):
        await _lock(db, lock_id)
    # A transaction we waited on may have just created some of them.
    for key in missing:
        asset = await _lookup(db, tenant_id, key)
        if asset is not None:
            asset_ids.add(asset.id)
    if asset_ids:
        # populate_existing: reload what the lookups cached, so updates
        # build on the locked row, not on a version read before the lock.
        await db.execute(
            select(AIAsset)
            .where(AIAsset.id.in_(asset_ids))
            .order_by(AIAsset.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )


async def find_or_create_asset(
    db: AsyncSession,
    tenant_id: UUID,
    vendor: str,
    model: str | None,
    use_case_name: str | None,
    business_unit: str | None,
    calling_service: str | None,
    source: str,
    owner_email: str | None = None,
    environment: str | None = None,
    data_classification: str | None = None,
    modality: str | None = None,
) -> tuple[AIAsset, bool]:
    """Find existing asset by merge key, or create new one. Returns (asset, is_new).

    Callers writing several assets in one transaction should call
    ``lock_asset_keys`` first (see there).
    """
    now = datetime.now(UTC)
    key = AssetKey(vendor, model, use_case_name, business_unit, environment)

    asset = await _lookup(db, tenant_id, key)
    if asset is None:
        # Serialise creation of this key with concurrent ingests, then look
        # again: the transaction we waited on may have just created it.
        await _lock(db, _lock_id(tenant_id, key))
        asset = await _lookup(db, tenant_id, key)

    if asset is not None:
        await lock_for_sighting(db, asset)
        if source not in asset.discovery_source:
            asset.discovery_source = asset.discovery_source + [source]
        asset.confidence = compute_confidence(asset.discovery_source)
        asset.last_seen = now
        reactivate(asset, now)
        if owner_email:
            asset.owner_email = owner_email
        if data_classification:
            existing = CLASSIFICATION_ORDER.get(asset.data_classification, -1)
            incoming = CLASSIFICATION_ORDER.get(data_classification, -1)
            if incoming > existing:
                asset.data_classification = data_classification
        # First reported modality wins; an asset is one model on one surface.
        if modality and not asset.modality:
            asset.modality = modality
        return asset, False

    asset = AIAsset(
        tenant_id=tenant_id,
        vendor=vendor,
        model=model,
        use_case_name=use_case_name,
        business_unit=business_unit,
        owner_email=owner_email,
        environment=environment,
        data_classification=data_classification,
        modality=modality,
        discovery_source=[source],
        confidence=compute_confidence([source]),
        calling_service=calling_service,
        first_seen=now,
        last_seen=now,
    )
    db.add(asset)
    return asset, True
