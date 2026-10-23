"""Risk mapping reviews (#98) against a real PostgreSQL database
(PS_TEST_DATABASE_URL): reviewer comment, append-only history, admin and
Partner API routes, scopes, tenant isolation, audit trail, delta feed and
erasure."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from collector import retention
from collector.app import app
from collector.partner_auth import hash_secret, issue_access_token
from collector.risk_reviews import partner_router
from collector.risk_suggestions import suggest_risks
from db.models import (
    AIAsset,
    AuditEvent,
    PartnerAuditLog,
    PartnerCredential,
    RiskMapping,
    RiskMappingReview,
    Tenant,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TENANT_KEY = {"Authorization": "Bearer ps-test-key-12345"}
OLD = datetime(2026, 1, 1, tzinfo=UTC)


async def _seed(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        tenant = (
            await s.execute(select(Tenant).where(Tenant.name == "Test Corp"))
        ).scalar_one()
        other = Tenant(name="Other", settings={})
        s.add(other)
        await s.flush()
        asset = AIAsset(
            tenant_id=tenant.id,
            vendor="openai",
            model="gpt-4o",
            use_case_name="resume screening",
            confidence="high",
            discovery_source=["sdk", "gateway"],
            updated_at=OLD,
        )
        sibling = AIAsset(tenant_id=tenant.id, vendor="anthropic", discovery_source=["sdk"])
        foreign = AIAsset(tenant_id=other.id, vendor="mistral", discovery_source=["sdk"])
        s.add_all([asset, sibling, foreign])
        await s.flush()
        risk = RiskMapping(
            tenant_id=tenant.id,
            asset_id=asset.id,
            risk_category="employment",
            risk_level="high",
            framework="EU_AI_ACT",
            status="suggested",
            source="rules",
            rule_id="eu_ai_act.annex_iii.4a.recruitment",
            ruleset_version="1.1.0",
            framework_ref="Annex III, point 4(a)",
            confidence="medium",
        )
        foreign_risk = RiskMapping(
            tenant_id=other.id,
            asset_id=foreign.id,
            risk_category="employment",
            risk_level="high",
            status="suggested",
        )
        creds = {
            name: PartnerCredential(
                tenant_id=tenant.id,
                partner_name=name,
                client_id=f"{name}-client",
                client_secret_hash=hash_secret("x"),
                scopes=scopes,
            )
            for name, scopes in (
                ("ro", ["risks:read"]),
                ("legacy", ["registry:read"]),
                ("rw", ["risks:read", "risks:review"]),
            )
        }
        s.add_all([risk, foreign_risk, *creds.values()])
        await s.commit()
        headers = {}
        for name, cred in creds.items():
            token, _ = issue_access_token(tenant.id, cred.id, cred.client_id, cred.scopes)
            headers[name] = {"Authorization": f"Bearer {token}"}
        return SimpleNamespace(
            factory=factory,
            tenant_id=tenant.id,
            other_id=other.id,
            asset_id=asset.id,
            sibling_id=sibling.id,
            risk_id=risk.id,
            foreign_risk_id=foreign_risk.id,
            foreign_asset_id=foreign.id,
            rw_id=creds["rw"].id,
            **headers,
        )


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _admin(risk_id, suffix="review"):
    return f"/api/v1/admin/risk-mappings/{risk_id}/{suffix}"


def _partner(asset_id, risk_id, suffix="review"):
    return f"/partner/v1/assets/{asset_id}/risks/{risk_id}/{suffix}"


@pytest.mark.asyncio
async def test_admin_review_records_comment_and_history(seeded_db):
    w = await _seed(seeded_db)
    async with _client() as c:
        first = await c.post(
            _admin(w.risk_id),
            headers=TENANT_KEY,
            json={
                "status": "confirmed",
                "reviewed_by": "dpo@test.com",
                "comment": "  Screens applicants; in scope.  ",
            },
        )
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "confirmed"
        assert first.json()["review_comment"] == "Screens applicants; in scope."
        # Reviewed again: a second history row, previous status recorded.
        second = await c.post(
            _admin(w.risk_id),
            headers=TENANT_KEY,
            json={"status": "rejected", "reviewed_by": "ciso@test.com"},
        )
        assert second.status_code == 200, second.text
        assert second.json()["review_comment"] is None
        hist = await c.get(_admin(w.risk_id, "reviews"), headers=TENANT_KEY)
    assert hist.status_code == 200, hist.text
    body = hist.json()
    assert body["meta"]["total"] == 2
    assert body["meta"]["current"]["status"] == "rejected"
    newest, oldest = body["data"]
    assert (newest["status"], newest["previous_status"]) == ("rejected", "confirmed")
    assert (oldest["status"], oldest["previous_status"]) == ("confirmed", "suggested")
    assert oldest["comment"] == "Screens applicants; in scope."
    assert oldest["reviewed_by"] == "dpo@test.com" and newest["reviewed_by"] == "ciso@test.com"
    assert oldest["actor_type"] == "tenant_key" and oldest["partner_id"] is None
    assert oldest["mapping"]["rule_id"] == "eu_ai_act.annex_iii.4a.recruitment"
    assert oldest["mapping"]["risk_level"] == "high"

    async with w.factory() as s:
        risk = await s.get(RiskMapping, w.risk_id)
        asset = await s.get(AIAsset, w.asset_id)
        trail = (
            await s.execute(
                select(AuditEvent).where(AuditEvent.action == "registry.risk_review")
            )
        ).scalars().all()
        reviews = (await s.execute(select(RiskMappingReview))).scalars().all()
    # Review never changes confidence (mapping's or asset's).
    assert risk.confidence == "medium" and asset.confidence == "high"
    assert asset.updated_at > OLD  # surfaces in the delta feed
    assert all(r.api_key_prefix for r in reviews)
    assert len(trail) == 2
    assert {t.details["status"] for t in trail} == {"confirmed", "rejected"}
    assert all(t.target_id == str(w.risk_id) for t in trail)
    # Names and comments stay out of the audit trail.
    assert not any("test.com" in str(t.details) for t in trail)


@pytest.mark.asyncio
async def test_engine_keeps_a_commented_rejection(seeded_db):
    w = await _seed(seeded_db)
    async with _client() as c:
        resp = await c.post(
            _admin(w.risk_id),
            headers=TENANT_KEY,
            json={"status": "rejected", "reviewed_by": "dpo@test.com", "comment": "no"},
        )
    assert resp.status_code == 200
    async with w.factory() as s:
        await suggest_risks(s, w.tenant_id)
        await s.commit()
    async with w.factory() as s:
        risk = await s.get(RiskMapping, w.risk_id)
    assert (risk.status, risk.review_comment) == ("rejected", "no")


@pytest.mark.asyncio
async def test_history_is_append_only_until_erasure(seeded_db):
    w = await _seed(seeded_db)
    async with _client() as c:
        for status in ("confirmed", "rejected"):
            resp = await c.post(
                _admin(w.risk_id),
                headers=TENANT_KEY,
                json={"status": status, "reviewed_by": "dpo@test.com"},
            )
            assert resp.status_code == 200
    async with w.factory() as s:
        with pytest.raises(DBAPIError, match="immutable"):
            await s.execute(text("UPDATE risk_mapping_reviews SET status = 'confirmed'"))
    async with w.factory() as s:
        with pytest.raises(DBAPIError, match="append-only"):
            await s.execute(text("DELETE FROM risk_mapping_reviews"))
    async with w.factory() as s:
        assert len((await s.execute(select(RiskMappingReview))).scalars().all()) == 2

    # The history outlives the mapping (e.g. a duplicate-asset merge drops it).
    async with w.factory() as s:
        await s.execute(RiskMapping.__table__.delete().where(RiskMapping.id == w.risk_id))
        await s.commit()
    async with _client() as c:
        hist = await c.get(_admin(w.risk_id, "reviews"), headers=TENANT_KEY)
    assert hist.status_code == 200, hist.text
    assert hist.json()["meta"]["current"] is None and hist.json()["meta"]["total"] == 2

    async with w.factory() as s:
        result = await retention.erase_tenant(s, w.tenant_id)
    assert result.rows_deleted["risk_mapping_reviews"] == 2
    async with w.factory() as s:
        assert (await s.execute(select(RiskMappingReview))).first() is None


@pytest.mark.asyncio
async def test_partner_review_scopes_and_history(seeded_db, monkeypatch):
    from collector import app as app_module

    # As in production (collector.app): rows stay readable after the audit commit.
    monkeypatch.setattr(
        app_module,
        "SessionLocal",
        async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False),
    )
    w = await _seed(seeded_db)
    body = {"status": "confirmed", "reviewed_by": "dpo@test.com", "comment": "agreed"}
    since = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    async with _client() as c:
        # risks:read and the legacy registry:read alias cannot review.
        for who in (w.ro, w.legacy):
            resp = await c.post(_partner(w.asset_id, w.risk_id), headers=who, json=body)
            assert resp.status_code == 403, resp.text
        ok = await c.post(_partner(w.asset_id, w.risk_id), headers=w.rw, json=body)
        assert ok.status_code == 201, ok.text
        assert ok.json()["data"]["actor_type"] == "partner"
        assert ok.json()["data"]["partner_id"] == str(w.rw_id)
        assert ok.json()["data"]["comment"] == "agreed"
        assert ok.json()["meta"]["current"]["status"] == "confirmed"
        assert ok.json()["meta"]["current"]["review_comment"] == "agreed"

        # Read history with risks:read and with the legacy alias.
        for who in (w.ro, w.legacy):
            hist = await c.get(_partner(w.asset_id, w.risk_id, "reviews"), headers=who)
            assert hist.status_code == 200, hist.text
            assert [r["status"] for r in hist.json()["data"]] == ["confirmed"]
        # The mapping now shows up in the default (confirmed) risk list,
        # with its comment, and the asset in the delta feed.
        risks = await c.get(f"/partner/v1/assets/{w.asset_id}/risks", headers=w.legacy)
        assert [r["review_comment"] for r in risks.json()["data"]] == ["agreed"]
        changes = await c.get(
            "/partner/v1/changes", params={"since": since}, headers=w.legacy
        )
        assert changes.status_code == 200, changes.text
        assert str(w.asset_id) in {d["asset"]["id"] for d in changes.json()["data"]}

        # Wrong asset, another tenant's mapping, unknown mapping: all 404.
        for asset_id, risk_id in (
            (w.sibling_id, w.risk_id),
            (w.foreign_asset_id, w.foreign_risk_id),
            (w.asset_id, w.foreign_risk_id),
        ):
            resp = await c.post(_partner(asset_id, risk_id), headers=w.rw, json=body)
            assert resp.status_code == 404, (asset_id, risk_id)
            resp = await c.get(_partner(asset_id, risk_id, "reviews"), headers=w.rw)
            assert resp.status_code == 404, (asset_id, risk_id)
        # A review names the person deciding.
        resp = await c.post(
            _partner(w.asset_id, w.risk_id), headers=w.rw, json={"status": "rejected"}
        )
        assert resp.status_code == 422

    async with w.factory() as s:
        audited = (
            await s.execute(
                select(PartnerAuditLog.endpoint, PartnerAuditLog.status_code).where(
                    PartnerAuditLog.endpoint.like("%/review%")
                )
            )
        ).all()
        foreign = await s.get(RiskMapping, w.foreign_risk_id)
        trail = (
            await s.execute(
                select(AuditEvent).where(AuditEvent.action == "registry.risk_review")
            )
        ).scalar_one()
    assert (_partner(w.asset_id, w.risk_id), 201) in audited
    assert foreign.status == "suggested" and foreign.reviewed_at is None
    assert (trail.actor_type, trail.actor_id) == ("partner", str(w.rw_id))


@pytest.mark.asyncio
async def test_admin_routes_are_tenant_isolated(seeded_db):
    w = await _seed(seeded_db)
    body = {"status": "rejected", "reviewed_by": "dpo@test.com"}
    async with _client() as c:
        resp = await c.post(_admin(w.foreign_risk_id), headers=TENANT_KEY, json=body)
        assert resp.status_code == 404
        resp = await c.get(_admin(w.foreign_risk_id, "reviews"), headers=TENANT_KEY)
        assert resp.status_code == 404
    async with w.factory() as s:
        assert (await s.execute(select(RiskMappingReview))).first() is None


def test_every_review_partner_route_is_scoped():
    for route in partner_router.routes:
        deps = [d.call for d in route.dependant.dependencies]
        assert any(
            getattr(d, "__qualname__", "").startswith("require_scope") for d in deps
        ), route.path
