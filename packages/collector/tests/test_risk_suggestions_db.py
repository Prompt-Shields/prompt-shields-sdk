"""Risk classification suggestions (#97) against a real PostgreSQL database
(PS_TEST_DATABASE_URL): suggestions from ingested telemetry, idempotency,
human-set values left alone, withdrawal, review, reads and tenant isolation."""

from __future__ import annotations

import dataclasses

import pytest
from collector.app import app
from collector.partner_auth import hash_secret, issue_access_token
from collector.risk_suggestions import _run_cli, load_rulesets, suggest_risks
from db.api_keys import api_key_lookup_prefix, hash_api_key
from db.models import AIAsset, PartnerCredential, RiskMapping, Tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

KEY_A = "ps-test-key-12345"
KEY_B = "ps-other-key-67890"

HIRING_EVENT = {
    "vendor": "openai",
    "model": "gpt-4o",
    "use_case_name": "resume-screening",
    "business_unit": "HR",
    "calling_service": "hr-screener",
    "data_classification": "internal",
    "data_sources": ["candidates_db", "ats_api"],
    "output_destination": "hiring_dashboard",
    "detected_pii_types": ["email", "national_id"],
}
CHAT_EVENT = {
    "vendor": "anthropic",
    "model": "claude-sonnet",
    "use_case_name": "support-chatbot",
    "calling_service": "support-bot",
    "data_classification": "internal",
}

RECRUITMENT = "eu_ai_act.annex_iii.4a.recruitment"
NIST_SENSITIVE = "nist_ai_rmf.privacy.sensitive_pii"
ISO_A7 = "iso_42001.a7.personal_data"
ISO_A5 = "iso_42001.a5.impact_assessment"
HIRING_RULES = {RECRUITMENT, NIST_SENSITIVE, ISO_A7, ISO_A5}
# The support chatbot only gets the Article 50(1) transparency suggestion.
CHATBOT = "eu_ai_act.article_50.1.ai_interaction_disclosure"


def _session(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)()


async def _tenant_id(engine, name="Test Corp"):
    async with _session(engine) as db:
        return (
            await db.execute(select(Tenant.id).where(Tenant.name == name))
        ).scalar_one()


async def _add_tenant_b(engine):
    async with _session(engine) as db:
        t = Tenant(
            name="Other Corp",
            settings={},
            api_key_prefix=api_key_lookup_prefix(KEY_B),
            api_key_hash=hash_api_key(KEY_B),
        )
        db.add(t)
        await db.commit()
        return t.id


async def _ingest(events, key=KEY_A):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.post(
            "/ingest/events",
            json={"events": events},
            headers={"Authorization": f"Bearer {key}"},
        )
    assert r.status_code == 200, r.text


async def _asset_id(engine, tenant_id, use_case):
    async with _session(engine) as db:
        return (
            await db.execute(
                select(AIAsset.id).where(
                    AIAsset.tenant_id == tenant_id, AIAsset.use_case_name == use_case
                )
            )
        ).scalar_one()


async def _suggest(engine, tenant_id):
    async with _session(engine) as db:
        r = await suggest_risks(db, tenant_id)
        await db.commit()
        return r


async def _risks(engine, tenant_id):
    async with _session(engine) as db:
        rows = (
            (
                await db.execute(
                    select(RiskMapping).where(RiskMapping.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        )
    return {(r.rule_id or f"manual:{r.framework}:{r.risk_category}"): r for r in rows}


async def _call(method, path, key=KEY_A, **kw):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        return await c.request(
            method, path, headers={"Authorization": f"Bearer {key}"}, **kw
        )


@pytest.mark.asyncio
async def test_suggestions_from_registry_and_telemetry_flows(seeded_db):
    tenant = await _tenant_id(seeded_db)
    await _ingest([HIRING_EVENT, CHAT_EVENT])

    result = await _suggest(seeded_db, tenant)
    assert (result.assets, result.created, result.updated, result.withdrawn) == (
        2,
        5,
        0,
        0,
    )

    risks = await _risks(seeded_db, tenant)
    assert set(risks) == HIRING_RULES | {CHATBOT}
    assert risks[CHATBOT].asset_id == await _asset_id(
        seeded_db, tenant, "support-chatbot"
    )
    assert (risks[CHATBOT].risk_category, risks[CHATBOT].risk_level) == (
        "transparency",
        "medium",
    )
    hiring = await _asset_id(seeded_db, tenant, "resume-screening")
    r = risks[RECRUITMENT]
    assert r.asset_id == hiring
    assert (r.framework, r.risk_category, r.risk_level) == (
        "EU_AI_ACT",
        "employment",
        "high",
    )
    assert (r.status, r.source, r.confidence) == ("suggested", "rules", "high")
    assert (r.framework_ref, r.ruleset_version) == ("Annex III, point 4(a)", "1.2.0")
    assert r.reviewed_at is None and r.created_at is not None
    signals = {(e["signal"], e["role"]): e for e in r.evidence["signals"]}
    # Data sources and PII come from the telemetry-derived data flows.
    assert signals[("data_sources", "trigger")]["value"] == "ats_api"
    assert signals[("pii_types", "supporting")]["matched"] == ["national_id"]
    assert risks[NIST_SENSITIVE].evidence["signals"][0]["matched"] == ["national_id"]


@pytest.mark.asyncio
async def test_rerun_is_idempotent(seeded_db):
    tenant = await _tenant_id(seeded_db)
    await _ingest([HIRING_EVENT])
    await _suggest(seeded_db, tenant)
    before = {
        k: (r.id, r.updated_at) for k, r in (await _risks(seeded_db, tenant)).items()
    }

    again = await _suggest(seeded_db, tenant)
    assert (again.created, again.updated, again.withdrawn, again.unchanged) == (
        0,
        0,
        0,
        4,
    )
    after = {
        k: (r.id, r.updated_at) for k, r in (await _risks(seeded_db, tenant)).items()
    }
    assert after == before


@pytest.mark.asyncio
async def test_human_set_values_are_never_overwritten(seeded_db):
    tenant = await _tenant_id(seeded_db)
    await _ingest([HIRING_EVENT])
    asset = await _asset_id(seeded_db, tenant, "resume-screening")
    # A hand-entered NIST privacy mapping covers what nist_ai_rmf.privacy.* would suggest.
    async with _session(seeded_db) as db:
        db.add(
            RiskMapping(
                tenant_id=tenant,
                asset_id=asset,
                framework="NIST_AI_RMF",
                risk_category="privacy",
                risk_level="low",
            )
        )
        await db.commit()

    await _suggest(seeded_db, tenant)
    risks = await _risks(seeded_db, tenant)
    assert NIST_SENSITIVE not in risks
    manual = risks["manual:NIST_AI_RMF:privacy"]
    assert (manual.risk_level, manual.status, manual.source) == (
        "low",
        "confirmed",
        "manual",
    )

    # Confirm one suggestion, reject another, through the API.
    ok = await _call(
        "POST",
        f"/api/v1/admin/risk-mappings/{risks[RECRUITMENT].id}/review",
        json={"status": "confirmed", "reviewed_by": "dpo@example.com"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "confirmed" and ok.json()["reviewed_at"]
    assert ok.json()["reviewed_by"] == "dpo@example.com"
    anonymous = await _call(
        "POST",
        f"/api/v1/admin/risk-mappings/{risks[ISO_A7].id}/review",
        json={"status": "confirmed"},
    )
    assert anonymous.status_code == 422  # a review names the person deciding
    no = await _call(
        "POST",
        f"/api/v1/admin/risk-mappings/{risks[ISO_A5].id}/review",
        json={"status": "rejected", "reviewed_by": "dpo@example.com"},
    )
    assert no.status_code == 200
    bad = await _call(
        "POST",
        f"/api/v1/admin/risk-mappings/{risks[ISO_A7].id}/review",
        json={"status": "suggested", "reviewed_by": "dpo@example.com"},
    )
    assert bad.status_code == 422

    # The asset's registry fields change, so the evidence would change too.
    async with _session(seeded_db) as db:
        a = await db.get(AIAsset, asset)
        a.use_case_name, a.business_unit, a.calling_service = "summariser", None, "svc"
        await db.commit()
    async with _session(seeded_db) as db:
        await db.execute(
            RiskMapping.__table__.update()
            .where(RiskMapping.rule_id == ISO_A7)
            .values(risk_level="critical")
        )
        await db.commit()

    # Flows still carry PII, so ISO A.7 keeps firing and its edited level is refreshed.
    result = await _suggest(seeded_db, tenant)
    risks = await _risks(seeded_db, tenant)
    assert risks[ISO_A7].risk_level == "medium" and result.updated == 1
    # Recruitment (still fired by the ats_api data source), ISO A.5 and NIST
    # fire again but the confirmed, rejected and manual rows are left as they were.
    assert risks[RECRUITMENT].status == "confirmed"
    assert risks[RECRUITMENT].evidence["signals"][0]["value"] == "resume-screening"
    assert risks[ISO_A5].status == "rejected"
    assert risks["manual:NIST_AI_RMF:privacy"].risk_level == "low"
    assert result.skipped_human_set == 3
    assert result.withdrawn == 0


@pytest.mark.asyncio
async def test_rejection_survives_a_ruleset_version_bump(seeded_db):
    tenant = await _tenant_id(seeded_db)
    await _ingest([HIRING_EVENT])
    await _suggest(seeded_db, tenant)
    risks = await _risks(seeded_db, tenant)
    no = await _call(
        "POST",
        f"/api/v1/admin/risk-mappings/{risks[RECRUITMENT].id}/review",
        json={"status": "rejected", "reviewed_by": "dpo@example.com"},
    )
    assert no.status_code == 200

    # A new version of every ruleset (as when a rule's keywords change).
    bumped = [
        dataclasses.replace(
            rs,
            version="9.9.9",
            rules=tuple(
                dataclasses.replace(r, ruleset_version="9.9.9") for r in rs.rules
            ),
        )
        for rs in load_rulesets()
    ]
    async with _session(seeded_db) as db:
        result = await suggest_risks(db, tenant, rulesets=bumped)
        await db.commit()
    risks = await _risks(seeded_db, tenant)
    # The human decision stands; open suggestions move to the new version.
    assert risks[RECRUITMENT].status == "rejected"
    assert risks[RECRUITMENT].ruleset_version == "1.2.0"
    assert risks[ISO_A7].ruleset_version == "9.9.9"
    assert (result.updated, result.skipped_human_set) == (3, 1)


@pytest.mark.asyncio
async def test_stale_suggestions_are_withdrawn(seeded_db):
    tenant = await _tenant_id(seeded_db)
    await _ingest(
        [
            CHAT_EVENT,
            {
                **CHAT_EVENT,
                "use_case_name": "credit scoring",
                "calling_service": "lending",
            },
        ]
    )
    await _suggest(seeded_db, tenant)
    assert set(await _risks(seeded_db, tenant)) == {
        "eu_ai_act.annex_iii.5b.creditworthiness",
        CHATBOT,
    }

    asset = await _asset_id(seeded_db, tenant, "credit scoring")
    async with _session(seeded_db) as db:
        (await db.get(AIAsset, asset)).use_case_name = "credit scoring fraud detection"
        await db.commit()
    result = await _suggest(seeded_db, tenant)
    assert result.withdrawn == 1
    assert set(await _risks(seeded_db, tenant)) == {CHATBOT}


@pytest.mark.asyncio
async def test_tenant_isolation_and_reads(seeded_db, monkeypatch):
    tenant_a = await _tenant_id(seeded_db)
    tenant_b = await _add_tenant_b(seeded_db)
    await _ingest([HIRING_EVENT])
    await _ingest(
        [{**HIRING_EVENT, "use_case_name": "credit scoring", "detected_pii_types": []}],
        key=KEY_B,
    )

    run = await _call("POST", "/api/v1/admin/risk-suggestions/run")
    assert run.status_code == 200, run.text
    assert run.json()["created"] == 4 and run.json()["tenant_id"] == str(tenant_a)
    assert await _risks(seeded_db, tenant_b) == {}

    await _run_cli([tenant_b], seeded_db.url.render_as_string(hide_password=False))
    b_risks = await _risks(seeded_db, tenant_b)
    assert all(r.tenant_id == tenant_b for r in b_risks.values())
    assert "eu_ai_act.annex_iii.5b.creditworthiness" in b_risks

    a_risks = await _risks(seeded_db, tenant_a)
    hiring_a = await _asset_id(seeded_db, tenant_a, "resume-screening")
    # Tenant B cannot review or read tenant A's rows.
    other = await _call(
        "POST",
        f"/api/v1/admin/risk-mappings/{a_risks[RECRUITMENT].id}/review",
        key=KEY_B,
        json={"status": "rejected", "reviewed_by": "dpo@example.com"},
    )
    assert other.status_code == 404
    leak = await _call("GET", f"/api/v1/registry/assets/{hiring_a}/risks", key=KEY_B)
    assert leak.status_code == 200 and leak.json()["data"] == []

    await _call(
        "POST",
        f"/api/v1/admin/risk-mappings/{a_risks[ISO_A5].id}/review",
        json={"status": "rejected", "reviewed_by": "dpo@example.com"},
    )
    reg = await _call("GET", f"/api/v1/registry/assets/{hiring_a}/risks")
    rows = {r["rule_id"]: r for r in reg.json()["data"]}
    assert set(rows) == HIRING_RULES - {ISO_A5}  # rejected rows hidden by default
    rec = rows[RECRUITMENT]
    assert rec["status"] == "suggested" and rec["source"] == "rules"
    assert (
        rec["framework_ref"] == "Annex III, point 4(a)" and rec["confidence"] == "high"
    )
    assert rec["evidence"]["summary"].startswith("Annex III, point 4(a)")
    rejected = await _call(
        "GET", f"/api/v1/registry/assets/{hiring_a}/risks?status=rejected"
    )
    assert [r["rule_id"] for r in rejected.json()["data"]] == [ISO_A5]

    # Partner API: same shape and filter.
    async with _session(seeded_db) as db:
        cred = PartnerCredential(
            tenant_id=tenant_a,
            partner_name="p",
            client_id="p-client",
            client_secret_hash=hash_secret("x"),
            scopes=["registry:read"],
        )
        db.add(cred)
        await db.commit()
        token, _ = issue_access_token(tenant_a, cred.id, "p-client", ["registry:read"])
    from collector import app as app_module

    monkeypatch.setattr(
        app_module,
        "SessionLocal",
        async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        partner = await c.get(
            f"/partner/v1/assets/{hiring_a}/risks?status=suggested",
            headers={"Authorization": f"Bearer {token}"},
        )
        default = await c.get(
            f"/partner/v1/assets/{hiring_a}/risks",
            headers={"Authorization": f"Bearer {token}"},
        )
        bom = await c.get(
            "/partner/v1/export?format=cyclonedx",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert partner.status_code == 200, partner.text
    assert {r["rule_id"] for r in partner.json()["data"]} == HIRING_RULES - {ISO_A5}
    # By default the Partner API returns only confirmed mappings, so existing
    # integrations do not start receiving unreviewed suggestions.
    assert default.status_code == 200, default.text
    assert default.json()["data"] == []
    assert bom.status_code == 200, bom.text
    props = {
        p["name"]: p["value"]
        for comp in bom.json()["components"]
        for p in comp.get("properties", [])
    }
    assert props["promptshields:risk-suggestion:EU_AI_ACT:employment"] == "high"
    assert "promptshields:risk-suggestion:ISO_42001:impact_assessment" not in props
    assert not any(k.startswith("promptshields:risk:") for k in props)


@pytest.mark.asyncio
async def test_review_unknown_mapping_is_404(seeded_db):
    r = await _call(
        "POST",
        "/api/v1/admin/risk-mappings/00000000-0000-0000-0000-000000000000/review",
        json={"status": "confirmed", "reviewed_by": "dpo@example.com"},
    )
    assert r.status_code == 404



@pytest.mark.asyncio
async def test_review_during_a_run_is_not_overwritten(seeded_db):
    """A human review that commits while a run is in flight wins: the run's
    refresh only applies to rows still `suggested`."""
    tenant = await _tenant_id(seeded_db)
    await _ingest([HIRING_EVENT])
    await _suggest(seeded_db, tenant)
    async with _session(seeded_db) as db:
        await db.execute(
            RiskMapping.__table__.update()
            .where(RiskMapping.rule_id.in_([ISO_A7, NIST_SENSITIVE]))
            .values(risk_level="critical")
        )
        await db.commit()

    async with _session(seeded_db) as run_db:
        # The run's session already holds the rows as `suggested` (the
        # identity map keeps them on the run's own select) ...
        # (held here: the identity map only keeps referenced objects).
        held = (
            await run_db.execute(
                select(RiskMapping).where(RiskMapping.tenant_id == tenant)
            )
        ).scalars().all()
        assert {r.status for r in held} == {"suggested"}
        # ... when a reviewer confirms one in another transaction.
        async with _session(seeded_db) as review_db:
            await review_db.execute(
                RiskMapping.__table__.update()
                .where(RiskMapping.rule_id == ISO_A7)
                .values(status="confirmed", reviewed_by="dpo@example.com")
            )
            await review_db.commit()
        result = await suggest_risks(run_db, tenant)
        await run_db.commit()

    risks = await _risks(seeded_db, tenant)
    assert risks[ISO_A7].status == "confirmed"
    assert risks[ISO_A7].risk_level == "critical"  # not refreshed after the review
    assert risks[NIST_SENSITIVE].risk_level == "high"  # still suggested: refreshed
    assert result.updated == 1
