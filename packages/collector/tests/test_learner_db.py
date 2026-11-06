"""Routing learner snapshots and recommendation review (#47, #48) against a
real PostgreSQL database (PS_TEST_DATABASE_URL): snapshot upload (integrity,
idempotency, recommend mode only, policy candidates), Partner API listing,
human review with audit trail, realised vs. projected savings, scopes,
tenant isolation and erasure. Row-level security is in test_rls_db.py."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from collector import retention
from collector.app import app
from collector.learner import expected_version
from collector.partner_auth import hash_secret, issue_access_token
from db.models import (
    AIAsset,
    AIUsageEvent,
    AuditEvent,
    CallFeedback,
    LearnerRecommendation,
    LearnerSnapshot,
    PartnerCredential,
    Tenant,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TENANT_KEY = {"Authorization": "Bearer ps-test-key-12345"}
FRONTIER = "openai:gpt-4o"
SMALL = "mistral:mistral-small"


def _snapshot(*, seed: int = 0, recs=None, **overrides) -> dict:
    """A snapshot as prompt_shields.learner.train writes it (shape only)."""
    rec = {
        "use_case": "support",
        "sensitivity": "confidential",
        "current_model": FRONTIER,
        "recommended_model": SMALL,
        "candidates": [SMALL, FRONTIER],
        "projection": {
            "calls": 1000,
            "current": {"model": FRONTIER, "quality": 0.92, "cost_per_call": 0.01,
                        "latency_ms": 900.0, "reward": 0.7,
                        "quality_method": "dm", "cost_method": "priced"},
            "recommended": {"model": SMALL, "quality": 0.915, "cost_per_call": 0.002,
                            "latency_ms": 800.0, "reward": 0.87,
                            "quality_method": "dm", "cost_method": "priced"},
            "quality_delta": -0.005,
            "reward_delta": 0.17,
            "savings_per_call": 0.008,
            "savings_total": 8.0,
            "savings_pct": 0.8,
            "prob_best": 0.99,
            "arm_samples": {FRONTIER: 700, SMALL: 300},
        },
    }
    snap = {
        "schema": "ps-learner-snapshot.v1",
        "mode": "recommend",
        "algorithm": "linear-thompson.v1",
        "feature_schema": "ps-prompt-features.v1",
        "context_features": ["bias"],
        "config": {"seed": seed},
        "policy_version": "p1",
        "parent_version": None,
        "data": {
            "fingerprint": hashlib.sha256(str(seed).encode()).hexdigest(),
            "rows": 1000,
            "scored_rows": 900,
            "skipped": {"no_model": 0},
        },
        "groups": [],
        "recommendations": [rec] if recs is None else recs,
    }
    snap.update(overrides)
    snap["learner_version"] = expected_version(snap)
    return snap


async def _seed(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        tenant = (
            await s.execute(select(Tenant).where(Tenant.name == "Test Corp"))
        ).scalar_one()
        other = Tenant(name="Other", settings={})
        s.add(other)
        await s.flush()
        creds = {
            name: PartnerCredential(
                tenant_id=tid,
                partner_name=name,
                client_id=f"{name}-client",
                client_secret_hash=hash_secret("x"),
                scopes=scopes,
            )
            for name, tid, scopes in (
                ("ro", tenant.id, ["learner:read"]),
                ("legacy", tenant.id, ["registry:read"]),
                ("rw", tenant.id, ["learner:read", "learner:review"]),
                ("foreign", other.id, ["learner:read", "learner:review"]),
            )
        }
        s.add_all(creds.values())
        await s.commit()
        headers = {}
        for name, cred in creds.items():
            token, _ = issue_access_token(cred.tenant_id, cred.id, cred.client_id, cred.scopes)
            headers[name] = {"Authorization": f"Bearer {token}"}
        return SimpleNamespace(
            factory=factory, tenant_id=tenant.id, other_id=other.id,
            rw_id=creds["rw"].id, **headers,
        )


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _upload(c, snap, headers=TENANT_KEY):
    return await c.post("/ingest/learner-snapshots", headers=headers, json=snap)


@pytest.mark.asyncio
async def test_upload_stores_snapshot_and_recommendations(seeded_db):
    w = await _seed(seeded_db)
    snap = _snapshot()
    async with _client() as c:
        first = await _upload(c, snap)
        again = await _upload(c, snap)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["created"] is True and body["recommendations"] == 1
    assert body["learner_version"] == snap["learner_version"]
    # A resend stores nothing new.
    assert again.json()["created"] is False
    assert again.json()["snapshot_id"] == body["snapshot_id"]
    async with w.factory() as s:
        assert await s.scalar(select(func.count(LearnerSnapshot.id))) == 1
        (rec,) = (await s.execute(select(LearnerRecommendation))).scalars().all()
        stored = await s.get(LearnerSnapshot, rec.snapshot_id)
        trail = (await s.execute(
            select(AuditEvent).where(AuditEvent.action == "learner.snapshot_upload")
        )).scalars().all()
    assert rec.status == "pending" and rec.recommended_model == SMALL
    assert rec.sensitivity_level == "confidential" and rec.use_case == "support"
    assert stored.snapshot == snap and stored.training_rows == 1000
    assert len(trail) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "detail"),
    [
        (lambda s: s.update(mode="auto"), "recommend"),
        (lambda s: s.update(schema="other"), "schema"),
        (lambda s: s.update(learner_version="lv1-0000000000000000"), "does not match"),
        (lambda s: s["recommendations"][0].update(recommended_model="openai:gpt-4o-mini"),
         "policy candidates"),
        (lambda s: s["recommendations"][0].update(recommended_model=FRONTIER,
                                                  current_model=FRONTIER),
         "equals current_model"),
        (lambda s: s["recommendations"][0]["projection"].pop("savings_pct"), "savings_pct"),
        (lambda s: s["data"].update(fingerprint="x"), "fingerprint"),
        (lambda s: s["recommendations"].append(copy.deepcopy(s["recommendations"][0])),
         "at most one"),
    ],
)
async def test_upload_refuses_invalid_snapshots(seeded_db, mutate, detail):
    await _seed(seeded_db)
    snap = _snapshot()
    keep = snap["learner_version"]
    mutate(snap)
    if snap["learner_version"] == keep:  # re-sign so only the mutation is wrong
        snap["learner_version"] = expected_version(snap)
    async with _client() as c:
        resp = await _upload(c, snap)
    assert resp.status_code == 422, resp.text
    assert detail in json.dumps(resp.json())


@pytest.mark.asyncio
async def test_upload_needs_a_tenant_key_and_bounds_the_body(seeded_db):
    await _seed(seeded_db)
    async with _client() as c:
        assert (await c.post("/ingest/learner-snapshots", json=_snapshot())).status_code == 401
        big = await c.post(
            "/ingest/learner-snapshots", headers=TENANT_KEY,
            content=b"{" + b" " * (2 * 1024 * 1024 + 1) + b"}",
        )
        assert big.status_code == 413
        bad = await c.post("/ingest/learner-snapshots", headers=TENANT_KEY, content=b"{")
        assert bad.status_code == 400


@pytest.mark.asyncio
async def test_newer_snapshot_supersedes_pending(seeded_db):
    w = await _seed(seeded_db)
    async with _client() as c:
        await _upload(c, _snapshot(seed=1))
        resp = await _upload(c, _snapshot(seed=2, parent_version=_snapshot(seed=1)["learner_version"]))
        assert resp.json()["superseded"] == 1
        listed = await c.get("/partner/v1/learner/recommendations", headers=w.ro)
    assert listed.status_code == 200, listed.text
    statuses = sorted(r["status"] for r in listed.json()["data"])
    assert statuses == ["pending", "superseded"]
    snaps = None
    async with _client() as c:
        snaps = await c.get("/partner/v1/learner/snapshots", headers=w.ro)
    data = snaps.json()["data"]
    assert [d["recommendations"] for d in data] == [1, 1]
    assert data[0]["parent_version"] == data[1]["learner_version"]
    assert "snapshot" not in data[0]


@pytest.mark.asyncio
async def test_partner_review_with_audit_and_scopes(seeded_db):
    w = await _seed(seeded_db)
    snap = _snapshot()
    async with _client() as c:
        await _upload(c, snap)
        (rec,) = (await c.get("/partner/v1/learner/recommendations?status=pending",
                              headers=w.ro)).json()["data"]
        assert rec["projection"]["savings_pct"] == 0.8 and rec["realised"] is None
        path = f"/partner/v1/learner/recommendations/{rec['id']}/review"
        review = {"decision": "accepted", "reviewed_by": "finops@test.com",
                  "comment": "  Pilot on support first.  "}
        # Read-only and legacy credentials cannot review; legacy cannot read.
        assert (await c.post(path, headers=w.ro, json=review)).status_code == 403
        assert (await c.get("/partner/v1/learner/recommendations",
                            headers=w.legacy)).status_code == 403
        # Another tenant's partner sees a 404.
        assert (await c.post(path, headers=w.foreign, json=review)).status_code == 404
        blank = await c.post(path, headers=w.rw, json={**review, "reviewed_by": "  "})
        assert blank.status_code == 422
        ok = await c.post(path, headers=w.rw, json=review)
        assert ok.status_code == 200, ok.text
        again = await c.post(path, headers=w.rw, json={**review, "decision": "rejected"})
        full = await c.get(f"/partner/v1/learner/snapshots/{snap['learner_version']}",
                           headers=w.ro)
        missing = await c.get("/partner/v1/learner/snapshots/lv1-ffffffffffffffff",
                              headers=w.ro)
    data = ok.json()["data"]
    assert data["status"] == "accepted"
    assert data["reviewed_by"] == "finops@test.com"
    assert data["review_comment"] == "Pilot on support first."
    assert data["reviewer_actor_type"] == "partner"
    assert data["realised"]["calls"] == 0
    assert again.status_code == 409
    assert full.status_code == 200 and full.json()["data"]["snapshot"] == snap
    assert missing.status_code == 404
    async with w.factory() as s:
        row = await s.get(LearnerRecommendation, rec["id"])
        trail = (await s.execute(
            select(AuditEvent).where(AuditEvent.action == "learner.recommendation_review")
        )).scalars().all()
    assert row.reviewer_partner_id == w.rw_id
    (t,) = trail
    assert t.details["decision"] == "accepted" and t.actor_type == "partner"
    # Names and comments stay out of the audit trail.
    assert "test.com" not in json.dumps(t.details) and "Pilot" not in json.dumps(t.details)


@pytest.mark.asyncio
async def test_realised_savings_after_acceptance(seeded_db):
    w = await _seed(seeded_db)
    async with _client() as c:
        await _upload(c, _snapshot())
        (rec,) = (await c.get("/partner/v1/learner/recommendations",
                              headers=w.ro)).json()["data"]
        await c.post(f"/partner/v1/learner/recommendations/{rec['id']}/review",
                     headers=w.rw, json={"decision": "accepted", "reviewed_by": "a@test.com"})
    async with w.factory() as s:
        row = await s.get(LearnerRecommendation, rec["id"])
        accepted = row.reviewed_at
        support = AIAsset(tenant_id=w.tenant_id, vendor="mistral", model="mistral-small",
                          use_case_name="support", discovery_source=["sdk"])
        other_uc = AIAsset(tenant_id=w.tenant_id, vendor="mistral", model="mistral-small",
                           use_case_name="billing", discovery_source=["sdk"])
        s.add_all([support, other_uc])
        await s.flush()

        def ev(asset, cost, *, served=None, ago=-1, level="confidential", call=None):
            return AIUsageEvent(
                tenant_id=w.tenant_id, asset_id=asset.id, source="sdk",
                timestamp=accepted - timedelta(minutes=ago), cost=cost,
                served_provider=served[0] if served else None,
                served_model=served[1] if served else None,
                sensitivity_level=level, call_id=call,
            )

        s.add_all([
            # Since acceptance, in the group: 3 on the small model, 1 still on gpt-4o.
            ev(support, 0.002, call="c1"),
            ev(support, 0.003, call="c2"),
            ev(support, 0.001),
            ev(support, 0.01, served=("openai", "gpt-4o"), call="c3"),
            # Outside the group or before the decision: ignored.
            ev(support, 0.5, ago=10),
            ev(support, 0.5, level="public"),
            ev(other_uc, 0.5),
            CallFeedback(tenant_id=w.tenant_id, call_id="c1", score=1.0, source="sdk"),
            CallFeedback(tenant_id=w.tenant_id, call_id="c2", score=0.5, source="sdk"),
            CallFeedback(tenant_id=w.tenant_id, call_id="c3", score=0.0, source="sdk"),
        ])
        await s.commit()
    async with _client() as c:
        got = await c.get(f"/partner/v1/learner/recommendations/{rec['id']}", headers=w.ro)
    r = got.json()["data"]["realised"]
    assert r["calls"] == 4 and r["calls_on_recommended"] == 3
    assert r["adoption"] == 0.75
    assert r["cost_per_call"] == pytest.approx(0.004)
    assert r["savings_per_call"] == pytest.approx(0.006)
    assert r["savings_pct"] == pytest.approx(0.6)
    assert r["projected_savings_pct"] == 0.8
    assert r["delta_points"] == pytest.approx(-20.0)
    # Feedback only on calls served by the recommended model.
    assert r["feedback_count"] == 2 and r["feedback_quality"] == pytest.approx(0.75)
    assert r["projected_quality"] == 0.915
    # The documented schema has exactly these keys.
    yaml = pytest.importorskip("yaml")
    spec = yaml.safe_load((Path(__file__).resolve().parents[3] / "docs" / "openapi.yaml")
                          .read_text())
    assert set(spec["components"]["schemas"]["LearnerRealised"]["required"]) == set(r)


@pytest.mark.asyncio
async def test_tenant_isolation_and_erasure(seeded_db):
    w = await _seed(seeded_db)
    async with _client() as c:
        await _upload(c, _snapshot())
        foreign = await c.get("/partner/v1/learner/recommendations", headers=w.foreign)
        foreign_snaps = await c.get("/partner/v1/learner/snapshots", headers=w.foreign)
    assert foreign.json()["data"] == [] and foreign_snaps.json()["data"] == []
    async with w.factory() as s:
        await retention.erase_tenant(s, w.tenant_id)
    async with w.factory() as s:
        assert await s.scalar(select(func.count(LearnerSnapshot.id))) == 0
        assert await s.scalar(select(func.count(LearnerRecommendation.id))) == 0


@pytest.mark.asyncio
async def test_snapshot_from_the_sdk_learner_is_accepted(seeded_db):
    """End to end with the SDK's learner, when it is importable: same
    version rule, and its recommendation is stored as sent."""
    sdk_learner = pytest.importorskip("prompt_shields.learner")
    import random

    assert sdk_learner.verify_snapshot(_snapshot())
    policy = {
        "default_group": "cheap",
        "quality_to_group": {},
        "data_policy": {"confidential": {"jurisdictions": ["EEA"]}},
        "groups": {
            "cheap": [{"provider": "mistral", "model": "mistral-small", "jurisdiction": "FR",
                       "input_cost_per_1k": 0.0002, "output_cost_per_1k": 0.0006}],
            "frontier": [{"provider": "openai", "model": "gpt-4o", "jurisdiction": "IE",
                          "input_cost_per_1k": 0.0025, "output_cost_per_1k": 0.01}],
        },
    }
    rng = random.Random(3)
    tuples = [
        {
            "use_case": "support", "data_classification": "confidential",
            "provider": p, "model": m, "features": {"approx_input_tokens": 500},
            "feedback_score": round(min(max(rng.gauss(0.9, 0.05), 0), 1), 4),
            "tokens_in": 500, "tokens_out": 200,
        }
        for p, m in [("openai", "gpt-4o")] * 300 + [("mistral", "mistral-small")] * 200
    ]
    snap = sdk_learner.train(tuples, policy)
    assert len(snap["recommendations"]) == 1
    await _seed(seeded_db)
    async with _client() as c:
        resp = await c.post(
            "/ingest/learner-snapshots", headers={**TENANT_KEY, "Content-Type":
                                                  "application/json"},
            content=sdk_learner.canonical_json(snap),
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["learner_version"] == snap["learner_version"]
