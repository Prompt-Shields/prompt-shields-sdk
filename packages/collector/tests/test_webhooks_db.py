"""Partner change webhooks against PostgreSQL (PS_TEST_DATABASE_URL).

Drives the real app: subscription admin API, ingest and the lifecycle sweep
as event producers, and the delivery worker against an httpx MockTransport.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from db.api_keys import api_key_lookup_prefix, hash_api_key
from db.models import (
    AIAsset,
    PartnerCredential,
    Tenant,
    WebhookDelivery,
    WebhookSubscription,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from collector import webhooks
from collector.app import app
from collector.config import settings
from collector.partner_auth import hash_secret

H_A = {"Authorization": "Bearer ps-test-key-12345"}  # tenant from seeded_db
H_B = {"Authorization": "Bearer ps-test-key-tenant-b"}
URL = "https://hooks.partner.example/ps"


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret_key", "s" * 40)
    monkeypatch.setattr(settings, "webhook_max_attempts", 3)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 0)

    async def public(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(webhooks, "_resolve_host", public)


@pytest.fixture
async def env(seeded_db):
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        tenant_a = (await s.execute(select(Tenant))).scalar_one()
        tenant_b = Tenant(
            name="B",
            settings={},
            api_key_prefix=api_key_lookup_prefix("ps-test-key-tenant-b"),
            api_key_hash=hash_api_key("ps-test-key-tenant-b"),
        )
        s.add(tenant_b)
        await s.flush()

        def partner(tenant, name, scopes=("registry:read",)):
            return PartnerCredential(
                tenant_id=tenant.id,
                partner_name=name,
                client_id=f"{name}-client",
                client_secret_hash=hash_secret("x"),
                scopes=list(scopes),
                is_active="true",
            )

        pa = partner(tenant_a, "pa")
        pa_noscope = partner(tenant_a, "pa2", scopes=("usage:read",))
        # Granular scopes (#150): changes:read alone qualifies, assets:read
        # alone does not.
        pa_changes = partner(tenant_a, "pa3", scopes=("changes:read",))
        pa_assets = partner(tenant_a, "pa4", scopes=("assets:read",))
        pb = partner(tenant_b, "pb")
        s.add_all([pa, pa_noscope, pa_changes, pa_assets, pb])
        await s.commit()
        return {
            "factory": factory,
            "tenant_a": tenant_a.id,
            "pa": pa.id,
            "pa_noscope": pa_noscope.id,
            "pa_changes": pa_changes.id,
            "pa_assets": pa_assets.id,
            "pb": pb.id,
        }


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _subscribe(client, partner_id, headers=H_A, **extra):
    return await client.post(
        "/api/v1/admin/webhooks/subscriptions",
        headers=headers,
        json={"partner_id": str(partner_id), "url": URL, **extra},
    )


async def _deliveries(factory):
    async with factory() as s:
        return (
            await s.execute(select(WebhookDelivery).order_by(WebhookDelivery.created_at))
        ).scalars().all()


class Receiver:
    def __init__(self, status=200):
        self.status = status
        self.requests: list[httpx.Request] = []

    def transport(self):
        def handler(request):
            self.requests.append(request)
            return httpx.Response(self.status)

        return httpx.MockTransport(handler)


# ── subscription admin ────────────────────────────────────────────────


async def test_create_returns_secret_once_and_stores_it_encrypted(env):
    async with _client() as c:
        resp = await _subscribe(c, env["pa"], event_types=["asset.created"])
        assert resp.status_code == 201, resp.text
        body = resp.json()
        secret = body["secret"]
        assert secret.startswith("whsec_")
        assert body["event_types"] == ["asset.created"]

        listed = (await c.get("/api/v1/admin/webhooks/subscriptions", headers=H_A)).json()
        assert [s["id"] for s in listed["data"]] == [body["id"]]
        assert "secret" not in json.dumps(listed)
        one = (await c.get(f"/api/v1/admin/webhooks/subscriptions/{body['id']}", headers=H_A))
        assert "secret" not in one.json()

    async with env["factory"]() as s:
        row = (await s.execute(select(WebhookSubscription))).scalar_one()
        assert secret not in row.secret_ciphertext
        assert webhooks.decrypt_secret(row.secret_ciphertext, row.id) == secret


@pytest.mark.parametrize(
    "url, resolved",
    [
        ("http://hooks.partner.example/ps", ["93.184.216.34"]),
        (URL, ["169.254.169.254"]),
        (URL, ["127.0.0.1"]),
        (URL, ["93.184.216.34", "10.0.0.8"]),
        ("https://user:pw@hooks.partner.example/", ["93.184.216.34"]),
    ],
)
async def test_create_rejects_unsafe_destinations(env, monkeypatch, url, resolved):
    async def resolve(host, port):
        return resolved

    monkeypatch.setattr(webhooks, "_resolve_host", resolve)
    async with _client() as c:
        resp = await c.post(
            "/api/v1/admin/webhooks/subscriptions",
            headers=H_A,
            json={"partner_id": str(env["pa"]), "url": url},
        )
    assert resp.status_code == 400
    assert await _deliveries(env["factory"]) == []


async def test_create_checks_partner_tenant_scope_and_config(env, monkeypatch):
    async with _client() as c:
        # Another tenant's partner looks like a missing one.
        assert (await _subscribe(c, env["pb"])).status_code == 404
        # Partner lacking changes:read (directly or via registry:read)
        # cannot receive asset events.
        assert (await _subscribe(c, env["pa_noscope"])).status_code == 400
        resp = await _subscribe(c, env["pa_assets"])
        assert resp.status_code == 400 and "changes:read" in resp.json()["detail"]["detail"]
        assert (await _subscribe(c, env["pa_changes"])).status_code == 201
        assert (
            await c.post(
                "/api/v1/admin/webhooks/subscriptions",
                headers=H_A,
                json={"partner_id": str(env["pa"]), "url": URL, "event_types": ["nope"]},
            )
        ).status_code == 422
        monkeypatch.setattr(settings, "webhook_secret_key", "")
        assert (await _subscribe(c, env["pa"])).status_code == 503
        # Unauthenticated
        assert (await _subscribe(c, env["pa"], headers={})).status_code == 401


async def test_subscriptions_are_tenant_scoped(env):
    async with _client() as c:
        sid = (await _subscribe(c, env["pa"])).json()["id"]
        base = "/api/v1/admin/webhooks/subscriptions"
        assert (await c.get(base, headers=H_B)).json()["data"] == []
        assert (await c.get(f"{base}/{sid}", headers=H_B)).status_code == 404
        assert (await c.delete(f"{base}/{sid}", headers=H_B)).status_code == 404
        assert (await c.post(f"{base}/{sid}/rotate-secret", headers=H_B)).status_code == 404
        assert (await c.delete(f"{base}/{sid}", headers=H_A)).status_code == 204
        assert (await c.get(base, headers=H_A)).json()["data"] == []


# ── producing events ──────────────────────────────────────────────────


async def test_ingest_queues_created_then_changed_events(env):
    event = {
        "vendor": "openai",
        "model": "gpt-4o",
        "use_case_name": "screening",
        "owner_email": "owner@corp.example",
        "prompt_text": "secret prompt",
    }
    async with _client() as c:
        await _subscribe(c, env["pa"])
        # Only created events for this one.
        await _subscribe(c, env["pa"], event_types=["asset.created"])
        assert (await c.post("/ingest/events", headers=H_A, json={"events": [event, event]})).status_code == 200

        rows = await _deliveries(env["factory"])
        assert sorted(r.event_type for r in rows) == ["asset.created", "asset.created"]
        assert rows[0].event_id == rows[1].event_id  # one event, two subscriptions
        payload = rows[0].payload
        assert payload["type"] == "asset.created"
        assert payload["data"]["asset"]["vendor"] == "openai"
        dumped = json.dumps(payload)
        assert "owner@corp.example" not in dumped and "secret prompt" not in dumped

        # Same telemetry again: nothing watched changes, nothing queued.
        await c.post("/ingest/events", headers=H_A, json={"events": [event]})
        assert len(await _deliveries(env["factory"])) == 2

        # A new discovery source is a change.
        await c.post(
            "/ingest/events", headers=H_A, json={"events": [{**event, "source": "gateway"}]}
        )
        rows = await _deliveries(env["factory"])
        changed = [r for r in rows if r.event_type == "asset.changed"]
        assert len(changed) == 1
        assert set(changed[0].payload["data"]["changed_fields"]) >= {
            "discovery_source",
            "confidence",
        }

        # Tenant B's ingest never reaches tenant A's partner.
        await c.post("/ingest/events", headers=H_B, json={"events": [{"vendor": "anthropic"}]})
        assert len(await _deliveries(env["factory"])) == 3


async def test_revoked_partner_gets_no_new_events(env):
    async with _client() as c:
        await _subscribe(c, env["pa"])
        async with env["factory"]() as s:
            await s.execute(update(PartnerCredential).values(is_active="false"))
            await s.commit()
        await c.post("/ingest/events", headers=H_A, json={"events": [{"vendor": "openai"}]})
    assert await _deliveries(env["factory"]) == []


async def test_lifecycle_sweep_queues_retired_event(env):
    async with _client() as c:
        await _subscribe(c, env["pa"])
        await c.post("/ingest/events", headers=H_A, json={"events": [{"vendor": "openai"}]})
        async with env["factory"]() as s:
            await s.execute(
                update(AIAsset).values(last_seen=datetime.now(UTC) - timedelta(days=120))
            )
            await s.commit()
        resp = await c.post(
            "/api/v1/admin/asset-lifecycle/sweep?stale_after_days=30&retire_after_days=90",
            headers=H_A,
        )
        assert resp.json()["marked_retired"] == 1
    types = [r.event_type for r in await _deliveries(env["factory"])]
    assert types == ["asset.created", "asset.retired"]


# ── delivering ────────────────────────────────────────────────────────


async def test_delivery_is_signed_and_marked_succeeded(env):
    async with _client() as c:
        secret = (await _subscribe(c, env["pa"])).json()["secret"]
        await c.post("/ingest/events", headers=H_A, json={"events": [{"vendor": "openai"}]})

    rx = Receiver(200)
    run = await webhooks.deliver_due(env["factory"], transport=rx.transport())
    assert (run.claimed, run.succeeded) == (1, 1)

    req = rx.requests[0]
    assert webhooks.verify_signature(secret, req.content, req.headers[webhooks.SIGNATURE_HEADER])
    assert not webhooks.verify_signature("whsec_wrong", req.content, req.headers[webhooks.SIGNATURE_HEADER])
    body = json.loads(req.content)
    assert req.headers[webhooks.EVENT_ID_HEADER] == body["id"]
    assert req.headers[webhooks.EVENT_TYPE_HEADER] == "asset.created"
    assert req.headers["Host"] == "hooks.partner.example"

    (row,) = await _deliveries(env["factory"])
    assert row.status == "succeeded" and row.attempts == 1 and row.delivered_at

    # Nothing left to do.
    assert (await webhooks.deliver_due(env["factory"], transport=rx.transport())).claimed == 0


async def test_failures_back_off_then_dead_letter_and_redeliver(env):
    async with _client() as c:
        await _subscribe(c, env["pa"])
        await c.post("/ingest/events", headers=H_A, json={"events": [{"vendor": "openai"}]})

        rx = Receiver(500)
        await webhooks.deliver_due(env["factory"], transport=rx.transport())
        (row,) = await _deliveries(env["factory"])
        assert row.status == "pending" and row.attempts == 1
        assert row.last_status_code == 500 and row.last_error == "HTTP 500"
        assert row.next_attempt_at > datetime.now(UTC) + timedelta(seconds=50)

        # Not due yet: nothing claimed.
        assert (await webhooks.deliver_due(env["factory"], transport=rx.transport())).claimed == 0

        future = datetime.now(UTC) + timedelta(days=10)
        for _ in range(2):  # attempts 2 and 3 (max_attempts=3)
            await webhooks.deliver_due(env["factory"], transport=rx.transport(), now=future)
            future += timedelta(days=10)
        (row,) = await _deliveries(env["factory"])
        assert row.status == "dead" and row.attempts == 3
        assert len(rx.requests) == 3

        dead = (await c.get("/api/v1/admin/webhooks/deliveries", headers=H_A)).json()["data"]
        assert [d["id"] for d in dead] == [str(row.id)]
        assert "payload" not in dead[0]
        # Other tenants see nothing and cannot redeliver it.
        assert (await c.get("/api/v1/admin/webhooks/deliveries", headers=H_B)).json()["data"] == []
        url = f"/api/v1/admin/webhooks/deliveries/{row.id}/redeliver"
        assert (await c.post(url, headers=H_B)).status_code == 404

        resp = await c.post(url, headers=H_A)
        assert resp.status_code == 200 and resp.json()["status"] == "pending"
        assert (await c.post(url, headers=H_A)).status_code == 409

    ok = Receiver(204)
    assert (await webhooks.deliver_due(env["factory"], transport=ok.transport())).succeeded == 1


async def test_revoked_partner_deliveries_go_dead_without_sending(env):
    async with _client() as c:
        await _subscribe(c, env["pa"])
        await c.post("/ingest/events", headers=H_A, json={"events": [{"vendor": "openai"}]})
    async with env["factory"]() as s:
        await s.execute(update(PartnerCredential).values(scopes=["usage:read"]))
        await s.commit()

    rx = Receiver(200)
    run = await webhooks.deliver_due(env["factory"], transport=rx.transport())
    assert run.dead == 1 and rx.requests == []
    (row,) = await _deliveries(env["factory"])
    assert "lacks scope" in row.last_error


async def test_granular_changes_scope_receives_and_narrowing_stops(env):
    async with _client() as c:
        await _subscribe(c, env["pa_changes"])
        await c.post("/ingest/events", headers=H_A, json={"events": [{"vendor": "openai"}]})
    (row,) = await _deliveries(env["factory"])
    assert row.event_type == "asset.created"

    # Narrowed to assets:read before delivery: dead, not sent.
    async with env["factory"]() as s:
        await s.execute(
            update(PartnerCredential)
            .where(PartnerCredential.id == env["pa_changes"])
            .values(scopes=["assets:read"])
        )
        await s.commit()
    rx = Receiver(200)
    run = await webhooks.deliver_due(env["factory"], transport=rx.transport())
    assert run.dead == 1 and rx.requests == []
    (row,) = await _deliveries(env["factory"])
    assert row.last_error == "partner credential lacks scope changes:read"

    # And no new events are queued for it.
    async with _client() as c:
        await c.post("/ingest/events", headers=H_A, json={"events": [{"vendor": "anthropic"}]})
    assert len(await _deliveries(env["factory"])) == 1


async def test_destination_turning_private_is_not_contacted(env, monkeypatch):
    async with _client() as c:
        await _subscribe(c, env["pa"])
        await c.post("/ingest/events", headers=H_A, json={"events": [{"vendor": "openai"}]})

    async def rebound(host, port):
        return ["10.0.0.7"]

    monkeypatch.setattr(webhooks, "_resolve_host", rebound)
    rx = Receiver(200)
    run = await webhooks.deliver_due(env["factory"], transport=rx.transport())
    assert run.retrying == 1 and rx.requests == []
    (row,) = await _deliveries(env["factory"])
    assert row.last_error.startswith("destination not allowed")


async def test_rotate_secret_changes_signature(env):
    async with _client() as c:
        sub = (await _subscribe(c, env["pa"])).json()
        rotated = (
            await c.post(
                f"/api/v1/admin/webhooks/subscriptions/{sub['id']}/rotate-secret", headers=H_A
            )
        ).json()
        assert rotated["secret"] != sub["secret"]
        await c.post("/ingest/events", headers=H_A, json={"events": [{"vendor": "openai"}]})

    rx = Receiver(200)
    await webhooks.deliver_due(env["factory"], transport=rx.transport())
    req = rx.requests[0]
    header = req.headers[webhooks.SIGNATURE_HEADER]
    assert webhooks.verify_signature(rotated["secret"], req.content, header)
    assert not webhooks.verify_signature(sub["secret"], req.content, header)


async def test_purge_removes_old_finished_deliveries(env):
    async with _client() as c:
        await _subscribe(c, env["pa"])
        await c.post("/ingest/events", headers=H_A, json={"events": [{"vendor": "openai"}]})
    await webhooks.deliver_due(env["factory"], transport=Receiver(200).transport())
    async with env["factory"]() as s:
        assert await webhooks.purge_old_deliveries(s) == 0
        later = datetime.now(UTC) + timedelta(days=settings.webhook_delivery_retention_days + 1)
        assert await webhooks.purge_old_deliveries(s, now=later) == 1
