"""AI-BOM (CycloneDX 1.6) export tests.

No Postgres: the endpoint tests run the partner router on a bare FastAPI app
with an in-memory fake session that applies each query's tenant filter.
Every BOM is validated against the vendored CycloneDX 1.6 JSON schema
(tests/schemas/cyclonedx, Apache-2.0).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from collector import aibom
from collector.partner_auth import PartnerPrincipal
from db.models import AIAsset, DataFlow, PartnerAuditLog, RiskMapping
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

jsonschema = pytest.importorskip("jsonschema")
referencing = pytest.importorskip("referencing")

SCHEMA_DIR = Path(__file__).parent / "schemas" / "cyclonedx"

TENANT_A = uuid.UUID("0190a000-0000-7000-8000-00000000000a")
TENANT_B = uuid.UUID("0190b000-0000-7000-8000-00000000000b")


# ──────────────────────────────────────────────────────────────────────
# Schema validation
# ──────────────────────────────────────────────────────────────────────


def _validator():
    resources = []
    for name in ("bom-1.6.schema.json", "spdx.schema.json", "jsf-0.82.schema.json"):
        schema = json.loads((SCHEMA_DIR / name).read_text())
        resources.append((schema["$id"], referencing.Resource.from_contents(schema)))
    registry = referencing.Registry().with_resources(resources)
    bom_schema = json.loads((SCHEMA_DIR / "bom-1.6.schema.json").read_text())
    cls = jsonschema.Draft7Validator
    return cls(bom_schema, registry=registry, format_checker=cls.FORMAT_CHECKER)


VALIDATOR = _validator()


def assert_valid(bom: dict) -> None:
    errors = sorted(VALIDATOR.iter_errors(bom), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors[:10])


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


def _asset(tenant_id, **kw) -> AIAsset:
    defaults = {
        "id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "vendor": "openai",
        "model": "gpt-4o",
        "use_case_name": "Support summariser",
        "business_unit": "Support",
        "owner_email": "owner@example.com",
        "environment": "production",
        "status": "active",
        "data_classification": "confidential",
        "discovery_source": ["sdk", "gateway"],
        "confidence": "high",
        "calling_service": "support-api",
        "first_seen": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        "last_seen": datetime(2026, 2, 3, 4, 5, 6),  # noqa: DTZ001 - naive on purpose
    }
    defaults.update(kw)
    return AIAsset(**defaults)


def _registry(tenant_id, prefix=""):
    a1 = _asset(tenant_id, use_case_name=f"{prefix}Support summariser")
    a2 = _asset(
        tenant_id,
        vendor="anthropic",
        model="claude-sonnet",
        use_case_name=f"{prefix}Contract review",
        discovery_source=["cloud_scan"],
        confidence="medium",
    )
    a3 = _asset(
        tenant_id,
        model=None,
        use_case_name=None,
        calling_service=f"{prefix}batch-job",
        owner_email=None,
        confidence=None,
    )
    a4 = _asset(tenant_id, use_case_name=f"{prefix}Search")  # same model as a1
    flows = [
        DataFlow(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            asset_id=a1.id,
            source_system="zendesk",
            destination_system="openai",
            data_classification="pii",
            direction="outbound",
            detected_pii_types=["email"],
        ),
        DataFlow(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            asset_id=a2.id,
            source_system="anthropic",
            destination_system="docs",
            data_classification=None,
            direction="sideways",
            detected_pii_types=None,
        ),
    ]
    risks = [
        RiskMapping(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            asset_id=a2.id,
            risk_category="high-risk",
            risk_level="high",
            framework="eu-ai-act",
        ),
        RiskMapping(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            asset_id=a1.id,
            risk_category="data-leakage",
            risk_level="medium",
            framework=None,
        ),
    ]
    return [a1, a2, a3, a4], flows, risks


def _props(component: dict) -> list[tuple[str, str]]:
    return [(p["name"], p["value"]) for p in component.get("properties", [])]


# ──────────────────────────────────────────────────────────────────────
# build_bom
# ──────────────────────────────────────────────────────────────────────


class TestBuildBom:
    def test_validates_against_cyclonedx_1_6(self):
        assets, flows, risks = _registry(TENANT_A)
        bom = aibom.build_bom(TENANT_A, assets, flows, risks)
        assert bom["specVersion"] == "1.6"
        assert_valid(bom)

    def test_empty_registry_validates(self):
        bom = aibom.build_bom(TENANT_A, [])
        assert bom["components"] == []
        assert_valid(bom)

    def test_validator_rejects_bad_bom(self):
        # Guard against a validator that silently accepts everything.
        bom = aibom.build_bom(TENANT_A, _registry(TENANT_A)[0])
        bom["components"][0]["type"] = "not-a-type"
        assert list(VALIDATOR.iter_errors(bom))

    def test_models_are_ml_components_deduplicated(self):
        assets, flows, risks = _registry(TENANT_A)
        bom = aibom.build_bom(TENANT_A, assets, flows, risks)
        models = [c for c in bom["components"] if c["type"] == "machine-learning-model"]
        assert sorted((m["supplier"]["name"], m["name"]) for m in models) == [
            ("anthropic", "claude-sonnet"),
            ("openai", "gpt-4o"),
        ]
        gpt = next(m for m in models if m["name"] == "gpt-4o")
        assert ("promptshields:asset_count", "2") in _props(gpt)

    def test_providers_are_services_with_data_flows(self):
        assets, flows, risks = _registry(TENANT_A)
        bom = aibom.build_bom(TENANT_A, assets, flows, risks)
        services = {s["name"]: s for s in bom["services"]}
        assert set(services) == {"openai", "anthropic"}
        (oai_flow,) = services["openai"]["data"]
        assert oai_flow["flow"] == "outbound"
        assert oai_flow["classification"] == "pii"
        (anth_flow,) = services["anthropic"]["data"]
        assert anth_flow["flow"] == "unknown"
        assert anth_flow["classification"] == "unclassified"

    def test_dependencies_link_apps_to_models_and_providers(self):
        assets, flows, risks = _registry(TENANT_A)
        a1, _, a3, _ = assets
        bom = aibom.build_bom(TENANT_A, assets, flows, risks)
        deps = {d["ref"]: d["dependsOn"] for d in bom["dependencies"]}
        assert deps[f"promptshields:asset:{a1.id}"] == [
            "promptshields:model:openai/gpt-4o",
            "promptshields:provider:openai",
        ]
        # No model recorded: the app depends on the provider only.
        assert deps[f"promptshields:asset:{a3.id}"] == ["promptshields:provider:openai"]
        root = bom["metadata"]["component"]["bom-ref"]
        assert len(deps[root]) == len(assets)
        # Every ref in the dependency graph points at a declared bom-ref.
        declared = {c["bom-ref"] for c in bom["components"]}
        declared |= {s["bom-ref"] for s in bom["services"]}
        declared.add(root)
        for ref, on in deps.items():
            assert ref in declared
            assert set(on) <= declared

    def test_promptshields_properties(self):
        assets, flows, risks = _registry(TENANT_A)
        a1, a2, a3, _ = assets
        bom = aibom.build_bom(TENANT_A, assets, flows, risks)
        by_ref = {c["bom-ref"]: c for c in bom["components"]}

        p1 = _props(by_ref[f"promptshields:asset:{a1.id}"])
        assert ("promptshields:discovery_source", "sdk") in p1
        assert ("promptshields:discovery_source", "gateway") in p1
        assert ("promptshields:confidence", "high") in p1
        assert ("promptshields:owner", "owner@example.com") in p1
        assert ("promptshields:risk:unspecified:data-leakage", "medium") in p1
        assert ("promptshields:last_seen", "2026-02-03T04:05:06+00:00") in p1

        p2 = _props(by_ref[f"promptshields:asset:{a2.id}"])
        assert ("promptshields:risk:eu-ai-act:high-risk", "high") in p2

        c3 = by_ref[f"promptshields:asset:{a3.id}"]
        assert c3["name"] == "batch-job"
        names3 = {n for n, _ in _props(c3)}
        assert "promptshields:owner" not in names3
        assert "promptshields:confidence" not in names3
        for name, _ in p1 + p2:
            assert name.startswith("promptshields:")

    def test_modality_property(self):
        """#75: the asset's modality is exported; none, no property."""
        audio = _asset(TENANT_A, vendor="deepgram", model="nova-3", modality="audio")
        plain = _asset(TENANT_A)
        bom = aibom.build_bom(TENANT_A, [audio, plain])
        assert_valid(bom)
        by_ref = {c["bom-ref"]: c for c in bom["components"]}
        assert ("promptshields:modality", "audio") in _props(
            by_ref[f"promptshields:asset:{audio.id}"]
        )
        assert "promptshields:modality" not in {
            n for n, _ in _props(by_ref[f"promptshields:asset:{plain.id}"])
        }

    def test_risk_properties_are_unique_per_framework_and_category(self):
        assets, flows, _ = _registry(TENANT_A)
        a1 = assets[0]

        def risk(category, level, *, rule_id=None, status="suggested"):
            return RiskMapping(
                id=uuid.uuid4(),
                tenant_id=TENANT_A,
                asset_id=a1.id,
                framework="EU_AI_ACT",
                risk_category=category,
                risk_level=level,
                rule_id=rule_id,
                status=status,
            )

        art5 = "eu_ai_act.article_5."
        risks = [
            risk("prohibited_practice", "critical", rule_id=art5 + "1f.emotion"),
            risk("prohibited_practice", "critical", rule_id=art5 + "1a.manipulative"),
            risk("prohibited_practice", "critical", rule_id=art5 + "1g.biometric"),
            risk(
                "prohibited_practice",
                "critical",
                rule_id=art5 + "1c.x",
                status="rejected",
            ),
            risk("biometrics", "high", rule_id="eu_ai_act.annex_iii.1c.emotion"),
            risk("biometrics", "medium", rule_id="eu_ai_act.annex_iii.1a.rbi"),
            # Hand-entered mappings of one category: the highest level wins.
            risk("employment", "medium", status="confirmed"),
            risk("employment", "high", status="confirmed"),
        ]
        bom = aibom.build_bom(TENANT_A, assets, flows, risks)
        assert_valid(bom)
        comp = next(
            c
            for c in bom["components"]
            if c["bom-ref"] == f"promptshields:asset:{a1.id}"
        )
        names = [n for n, _ in _props(comp) if n.startswith("promptshields:risk")]
        assert len(names) == len(set(names)) == 5
        props = dict(_props(comp))
        ns = "promptshields:risk-suggestion:EU_AI_ACT"
        assert props[f"{ns}:prohibited_practice"] == "critical"
        assert props[f"{ns}:prohibited_practice:rules"] == ",".join(
            [art5 + "1a.manipulative", art5 + "1f.emotion", art5 + "1g.biometric"]
        )
        assert props[f"{ns}:biometrics"] == "high"
        assert props[f"{ns}:biometrics:rules"] == (
            "eu_ai_act.annex_iii.1a.rbi,eu_ai_act.annex_iii.1c.emotion"
        )
        assert props["promptshields:risk:EU_AI_ACT:employment"] == "high"
        assert "promptshields:risk:EU_AI_ACT:employment:rules" not in props

    def test_drops_rows_from_other_tenants(self):
        a_assets, a_flows, a_risks = _registry(TENANT_A, prefix="A ")
        b_assets, b_flows, b_risks = _registry(TENANT_B, prefix="B ")
        b_assets[0].vendor = "tenant-b-only-vendor"
        bom = aibom.build_bom(
            TENANT_A, a_assets + b_assets, a_flows + b_flows, a_risks + b_risks
        )
        text = json.dumps(bom)
        assert "B " not in text and "tenant-b-only-vendor" not in text
        for b in b_assets:
            assert str(b.id) not in text
        assert_valid(bom)

    def test_serial_and_timestamp(self):
        serial = uuid.UUID("12345678-1234-5678-1234-567812345678")
        now = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
        bom = aibom.build_bom(TENANT_A, [], now=now, serial=serial)
        assert bom["serialNumber"] == f"urn:uuid:{serial}"
        assert bom["metadata"]["timestamp"] == "2026-10-13T12:00:00+00:00"


# ──────────────────────────────────────────────────────────────────────
# GET /partner/v1/export?format=cyclonedx
# ──────────────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar(self):
        return self._scalar


class FakeSession:
    """Minimal AsyncSession stand-in. Applies each SELECT's tenant_id filter
    to in-memory rows, and records which tenants were queried."""

    def __init__(self, rows: dict[type, list]):
        self.rows = rows
        self.queried_tenants: list[uuid.UUID] = []
        self.added: list = []

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity not in self.rows:
            return _Result(scalar=None)  # e.g. the rate-limit lookup
        params = stmt.compile().params
        tenants = [v for k, v in params.items() if k.startswith("tenant_id")]
        assert len(tenants) == 1, f"query on {entity.__name__} not tenant-scoped"
        self.queried_tenants.append(tenants[0])
        return _Result(rows=[r for r in self.rows[entity] if r.tenant_id == tenants[0]])

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def rollback(self):
        pass


@pytest.fixture
def fake_db():
    a_assets, a_flows, a_risks = _registry(TENANT_A, prefix="A ")
    b_assets, b_flows, b_risks = _registry(TENANT_B, prefix="B ")
    return FakeSession(
        {
            AIAsset: a_assets + b_assets,
            DataFlow: a_flows + b_flows,
            RiskMapping: a_risks + b_risks,
        }
    )


@pytest.fixture
def client(fake_db, monkeypatch):
    from collector import partner_auth
    from collector.partner_registry import router

    principals = {
        "tenant-a": PartnerPrincipal(TENANT_A, uuid.uuid4(), "a", {"registry:read"}),
        "tenant-b": PartnerPrincipal(TENANT_B, uuid.uuid4(), "b", {"registry:read"}),
        "no-scope": PartnerPrincipal(TENANT_A, uuid.uuid4(), "c", set()),
    }

    async def fake_resolve(request: Request) -> PartnerPrincipal:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in principals:
            raise HTTPException(status_code=401, detail="unauthorized")
        return principals[token]

    monkeypatch.setattr(partner_auth, "resolve_partner", fake_resolve)

    app = FastAPI()

    @app.middleware("http")
    async def _db(request: Request, call_next):
        request.state.db = fake_db
        return await call_next(request)

    app.include_router(router)
    return TestClient(app)


def _export(client, token, fmt="cyclonedx"):
    return client.get(
        "/partner/v1/export",
        params={"format": fmt},
        headers={"Authorization": f"Bearer {token}"},
    )


class TestExportEndpoint:
    def test_returns_valid_cyclonedx(self, client, fake_db):
        resp = _export(client, "tenant-a")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/vnd.cyclonedx+json")
        assert "X-RateLimit-Limit" in resp.headers
        bom = resp.json()
        assert_valid(bom)
        apps = [c for c in bom["components"] if c["type"] == "application"]
        assert len(apps) == 4

        (audit,) = [o for o in fake_db.added if isinstance(o, PartnerAuditLog)]
        assert audit.endpoint == "/partner/v1/export"
        assert audit.tenant_id == TENANT_A
        assert audit.response_count == 4

    def test_tenant_isolation(self, client, fake_db):
        bom_a = _export(client, "tenant-a").json()
        assert set(fake_db.queried_tenants) == {TENANT_A}
        bom_b = _export(client, "tenant-b").json()
        assert set(fake_db.queried_tenants) == {TENANT_A, TENANT_B}

        text_a, text_b = json.dumps(bom_a), json.dumps(bom_b)
        assert "A Support summariser" in text_a and "B " not in text_a
        assert "B Support summariser" in text_b and "A " not in text_b
        b_ids = {str(a.id) for a in fake_db.rows[AIAsset] if a.tenant_id == TENANT_B}
        for b_id in b_ids:
            assert b_id not in text_a
        assert str(TENANT_B) not in text_a

    def test_requires_registry_read_scope(self, client):
        assert _export(client, "no-scope").status_code == 403

    def test_requires_auth(self, client):
        assert _export(client, "bogus").status_code == 401

    def test_unknown_format_rejected(self, client):
        resp = _export(client, "tenant-a", fmt="spdx")
        assert resp.status_code == 400

    def test_json_format_unchanged(self, client):
        resp = _export(client, "tenant-a", fmt="json")
        assert resp.status_code == 200
        body = resp.json()
        assert body["meta"]["total"] == 4
        assert {a["vendor"] for a in body["data"]} == {"openai", "anthropic"}
