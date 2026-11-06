"""Spend anomaly alerts (#30) against PostgreSQL (PS_TEST_DATABASE_URL).

The job runs on synthetic spend for fixed past days with an explicit
``now``, so nothing depends on the wall clock. Covers the alert rows, rerun
idempotency, tenant thresholds, the signed ``spend.anomaly`` webhook and
the Partner API. Row-level security is covered in test_rls_db.py, retention
and erasure in test_retention.py.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from collector import spend_anomalies, webhooks
from collector.app import app
from collector.config import settings
from collector.partner_auth import hash_secret, issue_access_token
from db.api_keys import api_key_lookup_prefix, hash_api_key
from db.models import (
    AIAsset,
    AIUsageEvent,
    PartnerAuditLog,
    PartnerCredential,
    SpendAlert,
    Tenant,
    WebhookDelivery,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

H_A = {"Authorization": "Bearer ps-test-key-12345"}  # tenant from seeded_db
DAY = date(2026, 3, 15)
NOW = datetime(2026, 3, 16, 2, 0, tzinfo=UTC)
URL = "https://hooks.partner.example/spend"
TOKEN = "op-secret-token"
READ = ["spend-alerts:read"]
BOTH = ["spend-alerts:read", "spend-alerts:acknowledge"]


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret_key", "s" * 40)
    monkeypatch.setattr(settings, "webhook_secret_keys", "")
    monkeypatch.setattr(settings, "rate_limit_per_minute", 0)

    async def public(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(webhooks, "_resolve_host", public)


def _at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


@pytest.fixture
async def env(seeded_db):
    """Tenant A: `support` spends ~$10/day for 28 days then $40 on DAY;
    `search` spends a steady $5/day including DAY. Tenant B: a spike too."""
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        a = (await s.execute(select(Tenant))).scalar_one()
        b = Tenant(
            name="B",
            settings={},
            api_key_prefix=api_key_lookup_prefix("ps-test-key-tenant-b"),
            api_key_hash=hash_api_key("ps-test-key-tenant-b"),
        )
        s.add(b)
        await s.flush()
        support = AIAsset(tenant_id=a.id, vendor="openai", model="gpt-4o",
                          use_case_name="support", discovery_source=["sdk"])
        search = AIAsset(tenant_id=a.id, vendor="openai", model="gpt-4o-mini",
                         use_case_name="search", discovery_source=["sdk"])
        other = AIAsset(tenant_id=b.id, vendor="openai", model="gpt-4o",
                        use_case_name="support", discovery_source=["sdk"])
        s.add_all([support, search, other])
        await s.flush()

        def ev(tenant, asset, day, cost, hour=12):
            return AIUsageEvent(tenant_id=tenant.id, asset_id=asset.id, source="sdk",
                                timestamp=_at(day, hour), cost=Decimal(str(cost)))

        rows = []
        for i in range(1, 29):
            d = DAY - timedelta(days=i)
            # Two events a day, ±$1 around $10, so the series is not flat.
            rows += [ev(a, support, d, 4.5 + (i % 3) * 0.5), ev(a, support, d, 5.0, hour=23)]
            rows.append(ev(a, search, d, 5.0))
            rows.append(ev(b, other, d, 10.0))
        rows += [
            ev(a, support, DAY, 25.0, hour=0),
            ev(a, support, DAY, 15.0, hour=23),
            ev(a, search, DAY, 5.0),
            # The next day's spend never counts towards DAY.
            ev(a, support, DAY + timedelta(days=1), 500.0, hour=0),
            # A cost-less event is ignored.
            AIUsageEvent(tenant_id=a.id, asset_id=search.id, source="sdk",
                         timestamp=_at(DAY)),
            ev(b, other, DAY, 90.0),
        ]
        s.add_all(rows)

        def partner(tenant, name, scopes):
            return PartnerCredential(tenant_id=tenant.id, partner_name=name,
                                     client_id=f"{name}-client",
                                     client_secret_hash=hash_secret("x"),
                                     scopes=list(scopes), is_active="true")

        pa = partner(a, "pa", BOTH)
        pa_read = partner(a, "pa-read", READ)
        pa_changes = partner(a, "pa-changes", ["changes:read"])
        pb = partner(b, "pb", BOTH)
        s.add_all([pa, pa_read, pa_changes, pb])
        await s.commit()

    def bearer(tenant, p, scopes):
        token, _ = issue_access_token(tenant.id, p.id, p.client_id, scopes)
        return {"Authorization": f"Bearer {token}"}

    return {
        "factory": factory,
        "a": a.id,
        "b": b.id,
        "support": support.id,
        "pa": pa.id,
        "pa_read": pa_read.id,
        "pa_changes": pa_changes.id,
        "h_pa": bearer(a, pa, BOTH),
        "h_pa_read": bearer(a, pa_read, READ),
        "h_pb": bearer(b, pb, BOTH),
    }


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _alerts(factory, tenant_id=None) -> list[SpendAlert]:
    async with factory() as s:
        stmt = select(SpendAlert).order_by(SpendAlert.group_key)
        if tenant_id:
            stmt = stmt.where(SpendAlert.tenant_id == tenant_id)
        return (await s.execute(stmt)).scalars().all()


async def _run(env, **kw):
    return await spend_anomalies.run_all_tenants(
        env["factory"], day=DAY, now=NOW, list_factory=env["factory"], **kw
    )


async def _set_settings(env, tenant_id, value):
    async with env["factory"]() as s:
        await s.execute(update(Tenant).where(Tenant.id == tenant_id).values(settings=value))
        await s.commit()


async def test_job_records_one_alert_per_spiking_use_case(env):
    summary = await _run(env)
    assert summary["days"] == ["2026-03-15"]
    (run_a,) = summary["tenants"][str(env["a"])]
    assert run_a["groups_checked"] == 2 and run_a["alerts_created"] == 1

    (alert,) = await _alerts(env["factory"], env["a"])
    assert (alert.group_by, alert.group_key, alert.day) == ("use_case", "support", DAY)
    assert alert.observed_cost == Decimal("40.000000")
    assert 9.5 < alert.baseline_cost < 10.5
    assert alert.z_score >= 6 and alert.severity == "critical"
    assert (alert.threshold_sigma, alert.history_days, alert.status) == (3.0, 28, "open")
    assert alert.created_at == NOW

    # Tenant B's spike is its own alert.
    (alert_b,) = await _alerts(env["factory"], env["b"])
    assert alert_b.group_key == "support" and alert_b.observed_cost == Decimal("90.000000")


async def test_rerun_creates_no_duplicates_and_no_new_webhooks(env):
    async with _client() as c:
        resp = await c.post("/api/v1/admin/webhooks/subscriptions", headers=H_A, json={
            "partner_id": str(env["pa"]), "url": URL, "event_types": ["spend.anomaly"]})
        assert resp.status_code == 201, resp.text
    first = await _run(env)
    second = await _run(env)
    assert first["tenants"][str(env["a"])][0]["webhooks_queued"] == 1
    assert second["tenants"][str(env["a"])][0]["alerts_created"] == 0
    assert second["tenants"][str(env["a"])][0]["webhooks_queued"] == 0
    assert len(await _alerts(env["factory"])) == 2
    async with env["factory"]() as s:
        assert len((await s.execute(select(WebhookDelivery))).scalars().all()) == 1


async def test_concurrent_runs_record_one_alert_and_one_webhook(env):
    # Two overlapping cron runs: the unique (tenant, group, day) constraint
    # makes the second insert wait for the first, then do nothing.
    async with _client() as c:
        resp = await c.post("/api/v1/admin/webhooks/subscriptions", headers=H_A, json={
            "partner_id": str(env["pa"]), "url": URL, "event_types": ["spend.anomaly"]})
        assert resp.status_code == 201, resp.text
    runs = await asyncio.gather(*(_run(env) for _ in range(3)))
    created = sum(r["tenants"][str(env["a"])][0]["alerts_created"] for r in runs)
    queued = sum(r["tenants"][str(env["a"])][0]["webhooks_queued"] for r in runs)
    assert (created, queued) == (1, 1)
    assert len(await _alerts(env["factory"], env["a"])) == 1
    async with env["factory"]() as s:
        assert len((await s.execute(select(WebhookDelivery))).scalars().all()) == 1


async def test_tenant_thresholds_and_group_by(env):
    await _set_settings(env, env["a"], {"spend_anomaly": {"sigma": 20, "min_delta_usd": 100}})
    await _set_settings(env, env["b"], {"spend_anomaly": {"enabled": False}})
    summary = await _run(env)
    assert summary["tenants"][str(env["a"])][0]["alerts_created"] == 0
    assert summary["tenants"][str(env["b"])][0]["skipped"] == "disabled"
    assert await _alerts(env["factory"]) == []

    await _set_settings(env, env["a"], {"spend_anomaly": {"group_by": "asset"}})
    await _run(env)
    (alert,) = await _alerts(env["factory"], env["a"])
    assert (alert.group_by, alert.group_key) == ("asset", str(env["support"]))


async def test_backfill_checks_each_complete_day(env):
    summary = await spend_anomalies.run_all_tenants(
        env["factory"], days=3, now=NOW, list_factory=env["factory"]
    )
    assert summary["days"] == ["2026-03-13", "2026-03-14", "2026-03-15"]
    assert [r["alerts_created"] for r in summary["tenants"][str(env["a"])]] == [0, 0, 1]


async def test_operator_validates_spend_anomaly_settings(env, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", TOKEN)
    op = {"Authorization": f"Bearer {TOKEN}"}
    async with _client() as c:
        bad = await c.patch(f"/api/v1/operator/tenants/{env['a']}", headers=op,
                            json={"settings": {"spend_anomaly": {"sigma": 0}}})
        assert bad.status_code == 400, bad.text
        assert "sigma must be between" in bad.json()["detail"]["detail"]
        ok = await c.patch(f"/api/v1/operator/tenants/{env['a']}", headers=op,
                           json={"settings": {"spend_anomaly": {"sigma": 4.5}}})
        assert ok.status_code == 200, ok.text
        assert ok.json()["settings"]["spend_anomaly"] == {"sigma": 4.5}


# ── webhooks ──────────────────────────────────────────────────────────


async def test_subscribing_needs_the_spend_alerts_scope(env):
    async with _client() as c:
        resp = await c.post("/api/v1/admin/webhooks/subscriptions", headers=H_A, json={
            "partner_id": str(env["pa_changes"]), "url": URL,
            "event_types": ["spend.anomaly"]})
        assert resp.status_code in (400, 403, 422), resp.text
        assert "spend-alerts:read" in resp.text
        # spend.anomaly is not part of the default event list.
        resp = await c.post("/api/v1/admin/webhooks/subscriptions", headers=H_A, json={
            "partner_id": str(env["pa_changes"]), "url": URL})
        assert resp.status_code == 201, resp.text
        assert "spend.anomaly" not in resp.json()["event_types"]
    await _run(env)
    async with env["factory"]() as s:
        assert (await s.execute(select(WebhookDelivery))).scalars().all() == []


async def test_alert_is_delivered_as_a_signed_webhook(env):
    async with _client() as c:
        resp = await c.post("/api/v1/admin/webhooks/subscriptions", headers=H_A, json={
            "partner_id": str(env["pa"]), "url": URL, "event_types": ["spend.anomaly"]})
        secret = resp.json()["secret"]
    await _run(env)
    (alert,) = await _alerts(env["factory"], env["a"])

    requests: list[httpx.Request] = []

    def handler(request):
        requests.append(request)
        return httpx.Response(204)

    run = await webhooks.deliver_due(env["factory"], transport=httpx.MockTransport(handler))
    assert (run.claimed, run.succeeded) == (1, 1)
    (req,) = requests
    assert webhooks.verify_signature(secret, req.content, req.headers[webhooks.SIGNATURE_HEADER])
    assert req.headers[webhooks.EVENT_TYPE_HEADER] == "spend.anomaly"
    body = json.loads(req.content)
    assert body["id"] == str(alert.id) == req.headers[webhooks.EVENT_ID_HEADER]
    assert body["type"] == "spend.anomaly" and body["tenant_id"] == str(env["a"])
    data = body["data"]["alert"]
    assert data == spend_anomalies.serialize_alert(alert)
    assert (data["group_key"], data["day"], data["observed_cost"]) == ("support", "2026-03-15", 40.0)


async def test_scope_removed_before_delivery_dead_letters(env):
    async with _client() as c:
        await c.post("/api/v1/admin/webhooks/subscriptions", headers=H_A, json={
            "partner_id": str(env["pa"]), "url": URL, "event_types": ["spend.anomaly"]})
    await _run(env)
    async with env["factory"]() as s:
        await s.execute(update(PartnerCredential).where(PartnerCredential.id == env["pa"])
                        .values(scopes=["changes:read"]))
        await s.commit()
    run = await webhooks.deliver_due(env["factory"], transport=httpx.MockTransport(
        lambda r: pytest.fail("must not send")))
    assert run.dead == 1
    async with env["factory"]() as s:
        (d,) = (await s.execute(select(WebhookDelivery))).scalars().all()
    assert d.last_error == "partner credential lacks scope spend-alerts:read"


# ── Partner API ───────────────────────────────────────────────────────


async def test_partner_lists_gets_and_acknowledges(env):
    await _run(env)
    (alert,) = await _alerts(env["factory"], env["a"])
    async with _client() as c:
        listed = await c.get("/partner/v1/spend-alerts", headers=env["h_pa_read"])
        assert listed.status_code == 200, listed.text
        assert [a["id"] for a in listed.json()["data"]] == [str(alert.id)]
        assert listed.json()["meta"] == {"total": 1}

        got = await c.get(f"/partner/v1/spend-alerts/{alert.id}", headers=env["h_pa_read"])
        assert got.json()["data"]["status"] == "open"

        # Read-only credentials cannot acknowledge.
        denied = await c.post(f"/partner/v1/spend-alerts/{alert.id}/acknowledge",
                              headers=env["h_pa_read"])
        assert denied.status_code == 403

        acked = await c.post(f"/partner/v1/spend-alerts/{alert.id}/acknowledge",
                             headers=env["h_pa"])
        assert acked.status_code == 200, acked.text
        data = acked.json()["data"]
        assert data["status"] == "acknowledged"
        assert data["acknowledged_by"] == f"partner:{env['pa']}"
        again = await c.post(f"/partner/v1/spend-alerts/{alert.id}/acknowledge",
                             headers=env["h_pa"])
        assert again.json()["data"] == data

        open_only = await c.get("/partner/v1/spend-alerts?status=open", headers=env["h_pa"])
        assert open_only.json()["data"] == []
        acked_only = await c.get("/partner/v1/spend-alerts",
                                 params={"status": "acknowledged", "since": "2026-03-15",
                                         "until": "2026-03-15", "severity": "critical"},
                                 headers=env["h_pa"])
        assert len(acked_only.json()["data"]) == 1
        later = await c.get("/partner/v1/spend-alerts?since=2026-03-16", headers=env["h_pa"])
        assert later.json()["data"] == []

    async with env["factory"]() as s:
        audited = (await s.execute(select(PartnerAuditLog.endpoint, PartnerAuditLog.status_code)
                                   .where(PartnerAuditLog.partner_id == env["pa"]))).all()
    assert (f"/partner/v1/spend-alerts/{alert.id}/acknowledge", 200) in audited


async def test_partner_api_is_tenant_scoped(env):
    await _run(env)
    (alert_a,) = await _alerts(env["factory"], env["a"])
    async with _client() as c:
        listed = await c.get("/partner/v1/spend-alerts", headers=env["h_pb"])
        assert [a["id"] for a in listed.json()["data"]] != [str(alert_a.id)]
        assert len(listed.json()["data"]) == 1
        for resp in (
            await c.get(f"/partner/v1/spend-alerts/{alert_a.id}", headers=env["h_pb"]),
            await c.post(f"/partner/v1/spend-alerts/{alert_a.id}/acknowledge",
                         headers=env["h_pb"]),
            await c.get(f"/partner/v1/spend-alerts/{uuid.uuid4()}", headers=env["h_pb"]),
        ):
            assert resp.status_code == 404
    (still,) = await _alerts(env["factory"], env["a"])
    assert still.status == "open"
