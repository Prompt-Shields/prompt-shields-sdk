from uuid import UUID
from datetime import datetime, timezone
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from db.models import AIAsset
from collector.lifecycle import reactivate


CLASSIFICATION_ORDER = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}


def compute_confidence(sources: list[str]) -> str:
    if len(sources) >= 2:
        return "verified"
    source = sources[0] if sources else ""
    if source in ("sdk", "gateway"):
        return "high"
    if source in ("browser_extension", "macos_app", "platform_signal"):
        return "medium"
    return "low"


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
    """Find existing asset by merge key, or create new one. Returns (asset, is_new)."""
    now = datetime.now(timezone.utc)

    conditions = [
        AIAsset.tenant_id == tenant_id,
        AIAsset.vendor == vendor,
    ]
    if model:
        conditions.append(AIAsset.model == model)
    if use_case_name:
        conditions.append(AIAsset.use_case_name == use_case_name)
    if business_unit:
        conditions.append(AIAsset.business_unit == business_unit)
    if environment:
        conditions.append(AIAsset.environment == environment)

    stmt = select(AIAsset).where(and_(*conditions))
    result = await db.execute(stmt)
    asset = result.scalar_one_or_none()

    if asset is not None:
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
