"""Microsoft Sentinel export (#32) on PostgreSQL: the push job against
mocked Entra ID and Logs Ingestion endpoints, cursors, partial failure,
the operator routes and the NDJSON pull endpoints. No network."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from collector import audit_trail
from collector import sentinel_export as se
from collector.app import app
from db.models import AIAsset, AIUsageEvent, Tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.test_sentinel_export import (
    INGEST_HOST,
    SECRET,
    FakeAzure,
    _conn,
    _no_sleep,
    _public,
)

ADMIN = {"Authorization": "Bearer ps-test-key-12345"}
OPERATOR = "operator-token-for-tests"


@pytest.fixture
def factory(seeded_db):
    from collector import app as app_module

    f = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    app_module.SessionLocal = f
    return f


async def _seed(factory, n_calls: int = 3, recent: int = 1) -> tuple:
    """`n_calls` settled calls (minutes apart), `recent` too new to send,
    a call of another tenant and two audit rows."""
    now = datetime.now(UTC)
    async with factory() as db:
        tid = (await db.execute(select(Tenant))).scalar_one().id
        other = Tenant(name="Other", domain="o.com", settings={})
        db.add(other)
        await db.flush()
        asset = AIAsset(
            tenant_id=tid, vendor="mistral", business_unit="Retail", discovery_source=["sdk"]
        )
        other_asset = AIAsset(tenant_id=other.id, vendor="openai", discovery_source=["sdk"])
        db.add_all([asset, other_asset])
        await db.flush()
        for i in range(n_calls):
            db.add(
                AIUsageEvent(
                    tenant_id=tid,
                    asset_id=asset.id,
                    source="gateway",
                    timestamp=now - timedelta(hours=1) + timedelta(minutes=i),
                    served_provider="mistral",
                    served_jurisdiction="EEA",
                    policy_version="v3",
                    sensitivity_level="confidential",
                    sensitivity_source="declared",
                    cost=Decimal("0.01"),
                    call_id=f"call-{i}",
                    prompt_hash="secret-prompt-hash",
                )
            )
        for i in range(recent):
            db.add(
                AIUsageEvent(
                    tenant_id=tid,
                    asset_id=asset.id,
                    source="sdk",
                    timestamp=now - timedelta(seconds=10),
                    call_id=f"recent-{i}",
                )
            )
        db.add(
            AIUsageEvent(
                tenant_id=other.id,
                asset_id=other_asset.id,
                source="sdk",
                timestamp=now - timedelta(hours=1),
                call_id="other-tenant",
            )
        )
        for action in ("partner_credential.create", "webhook_subscription.create"):
            await audit_trail.record(
                db, tenant_id=tid, action=action, actor=audit_trail.OPERATOR, export=False
            )
        await db.commit()
        return tid, other.id


def _kw(tid, fake, **over) -> dict:
    return {
        "settings": se.SentinelSettings(sentinel_export={str(tid): _conn(**over)}),
        "transport": httpx.MockTransport(fake),
        "resolver": _public,
        "sleep": _no_sleep,
        "now": datetime.now(UTC),
    }


async def _state(factory, tid) -> dict:
    async with factory() as db:
        return (await db.get(Tenant, tid)).settings["connectors"]["sentinel"]


async def test_push_sends_settled_calls_and_audit_rows_once(factory, caplog):
    caplog.set_level(logging.DEBUG)
    tid, _ = await _seed(factory)
    fake = FakeAzure()
    async with factory() as db:
        out = await se.export_sentinel(db, tid, **_kw(tid, fake))
    assert out["status"] == "ok", out
    assert out["calls"] == 3 and out["audit"] == 2
    rows = [r for batch in fake.uploads for r in batch]
    calls = [r for r in rows if r["EventType"] == "AICall"]
    audit = [r for r in rows if r["EventType"] == "Audit"]
    # Oldest first; the call newer than settle_seconds and the other
    # tenant's call are not sent.
    assert [r["PsCallId"] for r in calls] == ["call-0", "call-1", "call-2"]
    assert {r["PsTenantId"] for r in rows} == {str(tid)}
    assert calls[0]["PsServedJurisdiction"] == "EEA" and calls[0]["PsPolicyVersion"] == "v3"
    assert calls[0]["PsSensitivityLevel"] == "confidential"
    assert calls[0]["PsBusinessUnit"] == "Retail"
    assert [r["PsAuditSeq"] for r in audit] == [1, 2]
    assert "secret-prompt-hash" not in json.dumps(rows)
    assert fake.tokens == 1

    state = await _state(factory, tid)
    assert state["audit_seq"] == 2 and state["calls_cursor"]
    assert state["last_status"] == "ok"
    assert SECRET not in json.dumps(state) and SECRET not in caplog.text
    assert "token-1" not in caplog.text

    # Nothing new: nothing sent (not even a token request).
    fake2 = FakeAzure()
    async with factory() as db:
        out = await se.export_sentinel(db, tid, **_kw(tid, fake2))
    assert out["calls"] == 0 and out["audit"] == 0 and fake2.uploads == []

    # Once the recent call has settled it goes out, alone.
    later = dict(_kw(tid, fake2), now=datetime.now(UTC) + timedelta(minutes=10))
    async with factory() as db:
        out = await se.export_sentinel(db, tid, **later)
    assert out["calls"] == 1
    assert [r["PsCallId"] for r in fake2.uploads[0]] == ["recent-0"]


async def test_batches_and_run_limit(factory):
    tid, _ = await _seed(factory, n_calls=5, recent=0)
    fake = FakeAzure()
    async with factory() as db:
        out = await se.export_sentinel(
            db, tid, **_kw(tid, fake, batch_records=2, max_records_per_run=3, include=["calls"])
        )
    assert out["status"] == "ok" and out["calls"] == 3 and out["audit"] == 0
    assert [len(b) for b in fake.uploads] == [2, 1]
    async with factory() as db:
        out = await se.export_sentinel(
            db, tid, **_kw(tid, fake, batch_records=2, max_records_per_run=3, include=["calls"])
        )
    sent = [r["PsCallId"] for b in fake.uploads for r in b]
    assert sent == [f"call-{i}" for i in range(5)]


async def test_failed_batch_keeps_progress_and_resends_from_there(factory):
    tid, _ = await _seed(factory, n_calls=4, recent=0)
    # 1st batch accepted, 2nd refused with 400 (not retried).
    fake = FakeAzure(upload_status=[204, 400])
    async with factory() as db:
        out = await se.export_sentinel(
            db, tid, **_kw(tid, fake, batch_records=2, include=["calls"])
        )
    assert out["status"] == "error" and "HTTP 400" in out["error"]
    assert out["calls"] == 2
    state = await _state(factory, tid)
    assert state["last_status"] == "error" and state["calls_cursor"]

    fake2 = FakeAzure()
    async with factory() as db:
        out = await se.export_sentinel(
            db, tid, **_kw(tid, fake2, batch_records=2, include=["calls"])
        )
    assert out["status"] == "ok"
    assert [r["PsCallId"] for b in fake2.uploads for r in b] == ["call-2", "call-3"]


async def test_not_configured_and_bad_config(factory):
    tid, other = await _seed(factory)
    fake = FakeAzure()
    async with factory() as db:
        out = await se.export_sentinel(db, other, **_kw(tid, fake))
        assert out["status"] == "not_configured"
        out = await se.export_sentinel(
            db, tid, **_kw(tid, fake, ingestion_endpoint="https://collector.evil.example")
        )
        assert out["status"] == "error" and "ingest.monitor.azure.com" in out["error"]
    assert fake.requests == []


async def test_operator_routes(factory, monkeypatch):
    tid, _ = await _seed(factory)
    monkeypatch.setenv("PS_ADMIN_TOKEN", OPERATOR)
    monkeypatch.setenv("PS_SENTINEL_EXPORT", json.dumps({str(tid): _conn()}))
    op = {"Authorization": f"Bearer {OPERATOR}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/api/v1/admin/connectors/sentinel/sync", json={"tenant_id": str(tid)}, headers=ADMIN
        )
        assert r.status_code == 401
        r = await c.post(
            "/api/v1/admin/connectors/sentinel/sync",
            json={"tenant_id": "00000000-0000-0000-0000-000000000000"},
            headers=op,
        )
        assert r.status_code == 404
        r = await c.get(
            "/api/v1/admin/connectors/sentinel/status",
            params={"tenant_id": str(tid)},
            headers=op,
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True and body["ingestion_host"] == INGEST_HOST
    assert body["cloud"] == "public" and body["include"] == ["calls", "audit"]
    assert SECRET not in r.text


async def _partner_key(scopes: list[str]) -> str:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/api/v1/admin/partner-credentials",
            headers=ADMIN,
            json={"partner_name": "siem", "scopes": scopes},
        )
        assert r.status_code == 201, r.text
        return r.json()["api_key"]


async def test_pull_endpoints_page_ndjson_by_cursor(factory):
    await _seed(factory, n_calls=3, recent=1)
    evidence = await _partner_key(["evidence:read"])
    auditor = await _partner_key(["audit:read"])
    registry = await _partner_key(["registry:read"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(
            "/partner/v1/siem/calls", headers={"Authorization": f"Bearer {registry}"}
        )
        assert r.status_code == 403
        r = await c.get(
            "/partner/v1/siem/audit", headers={"Authorization": f"Bearer {evidence}"}
        )
        assert r.status_code == 403

        h = {"Authorization": f"Bearer {evidence}"}
        r = await c.get("/partner/v1/siem/calls", params={"limit": 2}, headers=h)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("application/x-ndjson")
        assert r.headers["x-ps-has-more"] == "true" and "x-ratelimit-limit" in r.headers
        lines = [json.loads(line) for line in r.text.splitlines()]
        assert [x["PsCallId"] for x in lines] == ["call-0", "call-1"]
        cursor = r.headers["x-ps-cursor"]
        r = await c.get(
            "/partner/v1/siem/calls", params={"limit": 2, "cursor": cursor}, headers=h
        )
        assert [json.loads(x)["PsCallId"] for x in r.text.splitlines()] == ["call-2"]
        assert r.headers["x-ps-has-more"] == "false"
        # The recent call shows with no settle time; other tenants never.
        r = await c.get(
            "/partner/v1/siem/calls",
            params={"cursor": r.headers["x-ps-cursor"], "settle_seconds": 0},
            headers=h,
        )
        assert [json.loads(x)["PsCallId"] for x in r.text.splitlines()] == ["recent-0"]
        r = await c.get("/partner/v1/siem/calls", params={"cursor": "junk"}, headers=h)
        assert r.status_code == 400
        r = await c.get("/partner/v1/siem/calls", params={"since": "nope"}, headers=h)
        assert r.status_code == 400

        h = {"Authorization": f"Bearer {auditor}"}
        r = await c.get("/partner/v1/siem/audit", headers=h)
        assert r.status_code == 200, r.text
        seqs = [json.loads(x)["PsAuditSeq"] for x in r.text.splitlines()]
        # The two seeded rows plus the partner_credential.create rows the
        # admin API wrote while issuing the keys above.
        assert seqs == list(range(1, len(seqs) + 1)) and len(seqs) >= 2
        r = await c.get(
            "/partner/v1/siem/audit", params={"cursor": r.headers["x-ps-cursor"]}, headers=h
        )
        assert r.text == "" and r.headers["x-ps-cursor"] == str(seqs[-1])
