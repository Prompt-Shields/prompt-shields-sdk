"""AI bill of materials (AI-BOM) export as CycloneDX 1.6 JSON.

Maps one tenant's AI asset registry onto a CycloneDX 1.6 BOM:

  - each registered AI asset (an app or use case calling AI) becomes an
    ``application`` component;
  - each distinct (vendor, model) pair becomes a ``machine-learning-model``
    component, supplied by the vendor;
  - each vendor becomes a ``service`` (the hosted provider API), carrying the
    tenant's recorded data flows to that provider;
  - ``dependencies`` link every app to the model(s) and provider it calls.

Prompt Shields data with no CycloneDX field (discovery source, confidence,
owner, risk classification, ...) goes into ``properties`` under the
``promptshields:`` namespace.

The JSON is built by hand so the collector needs no CycloneDX library at
runtime. ``tests/test_aibom.py`` validates the output against the official
CycloneDX 1.6 JSON schema.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from db.models import AIAsset, DataFlow, RiskMapping
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

SPEC_VERSION = "1.6"
MEDIA_TYPE = "application/vnd.cyclonedx+json; version=1.6"
NS = "promptshields"

_FLOW_DIRECTIONS = {
    "inbound": "inbound",
    "outbound": "outbound",
    "bi-directional": "bi-directional",
    "bidirectional": "bi-directional",
    "both": "bi-directional",
}


async def load_registry(
    db: AsyncSession, tenant_id: UUID
) -> tuple[list[AIAsset], list[DataFlow], list[RiskMapping]]:
    """Load everything the AI-BOM needs for one tenant. Every query is
    filtered on ``tenant_id``."""
    assets = (
        (
            await db.execute(
                select(AIAsset)
                .where(AIAsset.tenant_id == tenant_id)
                .order_by(AIAsset.id)
            )
        )
        .scalars()
        .all()
    )
    flows = (
        (
            await db.execute(
                select(DataFlow)
                .where(DataFlow.tenant_id == tenant_id)
                .order_by(DataFlow.id)
            )
        )
        .scalars()
        .all()
    )
    risks = (
        (
            await db.execute(
                select(RiskMapping)
                .where(RiskMapping.tenant_id == tenant_id)
                .order_by(RiskMapping.id)
            )
        )
        .scalars()
        .all()
    )
    return list(assets), list(flows), list(risks)


def _prop(name: str, value: Any) -> dict[str, str]:
    return {"name": f"{NS}:{name}", "value": str(value)}


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _asset_ref(a: AIAsset) -> str:
    return f"{NS}:asset:{a.id}"


def _model_ref(vendor: str, model: str) -> str:
    return f"{NS}:model:{vendor}/{model}"


def _provider_ref(vendor: str) -> str:
    return f"{NS}:provider:{vendor}"


def _asset_name(a: AIAsset) -> str:
    return (
        a.use_case_name
        or a.calling_service
        or (f"{a.vendor} {a.model}" if a.model else a.vendor)
    )


def build_bom(
    tenant_id: UUID,
    assets: Iterable[AIAsset],
    flows: Iterable[DataFlow] = (),
    risks: Iterable[RiskMapping] = (),
    *,
    now: datetime | None = None,
    serial: UUID | None = None,
) -> dict[str, Any]:
    """Build a CycloneDX 1.6 BOM dict for one tenant's registry.

    Rows whose ``tenant_id`` differs from ``tenant_id`` are dropped, so a
    caller bug upstream cannot leak another tenant's assets into the BOM.
    """
    assets = [a for a in assets if a.tenant_id == tenant_id]
    asset_ids = {a.id for a in assets}
    vendor_of = {a.id: a.vendor for a in assets}
    flows = [f for f in flows if f.tenant_id == tenant_id and f.asset_id in asset_ids]
    risks = [r for r in risks if r.tenant_id == tenant_id and r.asset_id in asset_ids]

    risks_by_asset: dict[UUID, list[RiskMapping]] = {}
    for r in risks:
        risks_by_asset.setdefault(r.asset_id, []).append(r)

    components: list[dict[str, Any]] = []
    models: dict[str, dict[str, Any]] = {}
    model_assets: dict[str, int] = {}
    vendors: list[str] = []
    dependencies: list[dict[str, Any]] = []

    for a in assets:
        if a.vendor not in vendors:
            vendors.append(a.vendor)
        depends_on = []
        if a.model:
            mref = _model_ref(a.vendor, a.model)
            depends_on.append(mref)
            model_assets[mref] = model_assets.get(mref, 0) + 1
            models.setdefault(
                mref,
                {
                    "type": "machine-learning-model",
                    "bom-ref": mref,
                    "supplier": {"name": a.vendor},
                    "group": a.vendor,
                    "name": a.model,
                },
            )
        depends_on.append(_provider_ref(a.vendor))
        dependencies.append({"ref": _asset_ref(a), "dependsOn": depends_on})

        props = [_prop("asset_id", a.id), _prop("vendor", a.vendor)]
        if a.model:
            props.append(_prop("model", a.model))
        for src in a.discovery_source or []:
            props.append(_prop("discovery_source", src))
        optional = [
            ("confidence", a.confidence),
            ("owner", a.owner_email),
            ("business_unit", a.business_unit),
            ("environment", a.environment),
            ("region", a.region),
            ("status", a.status),
            ("data_classification", a.data_classification),
            ("calling_service", a.calling_service),
            ("first_seen", _iso(a.first_seen)),
            ("last_seen", _iso(a.last_seen)),
        ]
        props.extend(_prop(k, v) for k, v in optional if v)
        for r in risks_by_asset.get(a.id, []):
            if r.status == "rejected":
                continue
            framework = r.framework or "unspecified"
            # Rule-based suggestions nobody has confirmed are kept apart from
            # risk classifications (#97).
            kind = "risk-suggestion" if r.status == "suggested" else "risk"
            props.append(_prop(f"{kind}:{framework}:{r.risk_category}", r.risk_level))

        components.append(
            {
                "type": "application",
                "bom-ref": _asset_ref(a),
                "name": _asset_name(a),
                "properties": props,
            }
        )

    for mref, comp in models.items():
        comp["properties"] = [_prop("asset_count", model_assets[mref])]
        components.append(comp)
        dependencies.append({"ref": mref, "dependsOn": []})

    flows_by_vendor: dict[str, list[dict[str, Any]]] = {}
    for f in flows:
        entry: dict[str, Any] = {
            "flow": _FLOW_DIRECTIONS.get((f.direction or "").lower(), "unknown"),
            "classification": f.data_classification or "unclassified",
            "name": f"{f.source_system} -> {f.destination_system}",
            "description": f"Data flow {f.id} recorded for asset "
            f"{f.asset_id} ({NS}:asset:{f.asset_id}).",
        }
        if f.detected_pii_types:
            entry["description"] += f" Detected PII types: {f.detected_pii_types}."
        flows_by_vendor.setdefault(vendor_of[f.asset_id], []).append(entry)

    services = []
    for vendor in vendors:
        svc: dict[str, Any] = {
            "bom-ref": _provider_ref(vendor),
            "provider": {"name": vendor},
            "name": vendor,
            "properties": [_prop("kind", "ai-provider")],
        }
        if vendor in flows_by_vendor:
            svc["data"] = flows_by_vendor[vendor]
        services.append(svc)
        dependencies.append({"ref": _provider_ref(vendor), "dependsOn": []})

    root_ref = f"{NS}:tenant:{tenant_id}"
    dependencies.insert(
        0, {"ref": root_ref, "dependsOn": [_asset_ref(a) for a in assets]}
    )

    now = now or datetime.now(UTC)
    bom: dict[str, Any] = {
        "bomFormat": "CycloneDX",
        "specVersion": SPEC_VERSION,
        "serialNumber": f"urn:uuid:{serial or uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": _iso(now),
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "group": "Prompt Shields",
                        "name": "prompt-shields-collector",
                    }
                ]
            },
            "component": {
                "type": "application",
                "bom-ref": root_ref,
                "name": "AI asset registry",
                "properties": [_prop("tenant_id", tenant_id)],
            },
        },
        "components": components,
        "services": services,
        "dependencies": dependencies,
    }
    return bom
