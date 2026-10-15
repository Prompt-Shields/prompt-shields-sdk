"""Partner change webhooks against PostgreSQL (PS_TEST_DATABASE_URL).

Drives the real app: subscription admin API, ingest and the lifecycle sweep
as event producers, and the delivery worker against an httpx MockTransport.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from db.api_keys import api_key_lookup_prefix, hash_api_key
from db.models import (
    AIAsset,
    PartnerAuditLog,
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
    monkeypatch.setattr(settings, "webhook_secret_keys", "")
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


# ── secret rotation grace period ──────────────────────────────────────


async def _rotate(c, sid, **body):
    return await c.post(
        f"/api/v1/admin/webhooks/subscriptions/{sid}/rotate-secret",
        headers=H_A,
        json=body or None,
    )


async def test_rotate_with_grace_period_signs_with_both_secrets(env):
    async with _client() as c:
        sub = (await _subscribe(c, env["pa"])).json()
        assert sub["rotation_grace_ends_at"] is None
        resp = await _rotate(c, sub["id"], grace_period_seconds=3600)
        assert resp.status_code == 200, resp.text
        rotated = resp.json()
        ends = datetime.fromisoformat(rotated["rotation_grace_ends_at"])
        assert timedelta(minutes=59) < ends - datetime.now(UTC) <= timedelta(hours=1)
        await c.post("/ingest/events", headers=H_A, json={"events": [{"vendor": "openai"}]})

    rx = Receiver(200)
    await webhooks.deliver_due(env["factory"], transport=rx.transport())
    req = rx.requests[0]
    header = req.headers[webhooks.SIGNATURE_HEADER]
    assert header.count("v1=") == 2
    assert webhooks.verify_signature(rotated["secret"], req.content, header)
    assert webhooks.verify_signature(sub["secret"], req.content, header)

    # Grace over: the old secret stops signing and is dropped from the row.
    async with env["factory"]() as s:
        await s.execute(
            update(WebhookSubscription).values(
                previous_secret_expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
        await s.commit()
    async with _client() as c:
        listed = (await c.get("/api/v1/admin/webhooks/subscriptions", headers=H_A)).json()
        assert listed["data"][0]["rotation_grace_ends_at"] is None
        await c.post("/ingest/events", headers=H_A, json={"events": [{"vendor": "anthropic"}]})
    await webhooks.deliver_due(env["factory"], transport=rx.transport())
    req = rx.requests[1]
    header = req.headers[webhooks.SIGNATURE_HEADER]
    assert header.count("v1=") == 1
    assert webhooks.verify_signature(rotated["secret"], req.content, header)
    assert not webhooks.verify_signature(sub["secret"], req.content, header)
    async with env["factory"]() as s:
        row = (await s.execute(select(WebhookSubscription))).scalar_one()
        assert row.previous_secret_ciphertext is None and row.previous_secret_expires_at is None


async def test_rotate_without_grace_drops_any_previous_secret(env):
    async with _client() as c:
        sub = (await _subscribe(c, env["pa"])).json()
        first = (await _rotate(c, sub["id"], grace_period_seconds=600)).json()
        second = (await _rotate(c, sub["id"])).json()  # no body: immediate
        assert second["rotation_grace_ends_at"] is None
        await c.post("/ingest/events", headers=H_A, json={"events": [{"vendor": "openai"}]})
    rx = Receiver(200)
    await webhooks.deliver_due(env["factory"], transport=rx.transport())
    req = rx.requests[0]
    header = req.headers[webhooks.SIGNATURE_HEADER]
    assert header.count("v1=") == 1
    assert webhooks.verify_signature(second["secret"], req.content, header)
    for old in (sub["secret"], first["secret"]):
        assert not webhooks.verify_signature(old, req.content, header)


@pytest.mark.parametrize("grace", [-1, 7 * 86400 + 1])
async def test_rotate_grace_period_is_bounded(env, grace):
    async with _client() as c:
        sub = (await _subscribe(c, env["pa"])).json()
        assert (await _rotate(c, sub["id"], grace_period_seconds=grace)).status_code == 422


# ── encryption key rotation ───────────────────────────────────────────


async def _subscriptions(factory) -> dict[str, WebhookSubscription]:
    async with factory() as s:
        return {str(r.id): r for r in (await s.execute(select(WebhookSubscription))).scalars()}


async def test_keyring_rotation_rekeys_on_use_and_with_the_rekey_command(env, monkeypatch):
    async with _client() as c:
        one = (await _subscribe(c, env["pa"])).json()
        two = (await _subscribe(c, env["pa"], event_types=["asset.retired"])).json()
    subs = await _subscriptions(env["factory"])
    assert all(r.secret_ciphertext.startswith("v1.") for r in subs.values())

    # New active key; the legacy key stays configured for old ciphertexts.
    monkeypatch.setattr(settings, "webhook_secret_keys", "2026b:" + "n" * 40)
    async with _client() as c:
        await c.post("/ingest/events", headers=H_A, json={"events": [{"vendor": "openai"}]})
    rx = Receiver(200)
    assert (await webhooks.deliver_due(env["factory"], transport=rx.transport())).succeeded == 1
    req = rx.requests[0]
    assert webhooks.verify_signature(
        one["secret"], req.content, req.headers[webhooks.SIGNATURE_HEADER]
    )

    subs = await _subscriptions(env["factory"])
    # Re-encrypted on use, without touching updated_at.
    assert subs[one["id"]].secret_ciphertext.startswith("v2.2026b.")
    assert subs[one["id"]].updated_at.isoformat() == one["updated_at"]
    assert subs[two["id"]].secret_ciphertext.startswith("v1.")

    result = await webhooks.rekey_all(env["factory"])
    assert (result.checked, result.rekeyed, result.unchanged, result.unreadable) == (2, 1, 1, 0)
    assert (await webhooks.rekey_all(env["factory"])).unchanged == 2

    # The legacy key can go now; both secrets are still readable.
    monkeypatch.setattr(settings, "webhook_secret_key", "")
    for sid, row in (await _subscriptions(env["factory"])).items():
        assert row.secret_ciphertext.startswith("v2.2026b.")
        expected = one["secret"] if sid == one["id"] else two["secret"]
        assert webhooks.decrypt_secret(row.secret_ciphertext, row.id) == expected


async def test_rekey_reports_unreadable_secrets_and_keeps_a_grace_secret(env, monkeypatch):
    async with _client() as c:
        sub = (await _subscribe(c, env["pa"])).json()
        await _rotate(c, sub["id"], grace_period_seconds=3600)
        broken = (await _subscribe(c, env["pa"])).json()
    async with env["factory"]() as s:
        await s.execute(
            update(WebhookSubscription)
            .where(WebhookSubscription.id == broken["id"])
            .values(secret_ciphertext="v1.AAAA")
        )
        await s.commit()

    monkeypatch.setattr(settings, "webhook_secret_keys", "k2:" + "n" * 40)
    result = await webhooks.rekey_all(env["factory"])
    assert (result.rekeyed, result.unreadable) == (1, 1)
    row = (await _subscriptions(env["factory"]))[sub["id"]]
    assert row.secret_ciphertext.startswith("v2.k2.")
    assert row.previous_secret_ciphertext.startswith("v2.k2.")
    assert webhooks.decrypt_secret(row.previous_secret_ciphertext, row.id) == sub["secret"]


async def test_unknown_key_id_is_retried_then_delivered_by_a_process_with_the_key(
    env, monkeypatch, caplog
):
    """A rolling restart that adds a key: a process without it backs off
    (logging once) instead of dead-lettering; one with the key delivers."""
    monkeypatch.setattr(webhooks, "_missing_keys_logged", set())
    new_keys = "new:" + "n" * 40 + ",old:" + "o" * 40
    monkeypatch.setattr(settings, "webhook_secret_keys", new_keys)
    async with _client() as c:
        sub = (await _subscribe(c, env["pa"])).json()
        await _subscribe(c, env["pa"], event_types=["asset.created"])
        await c.post("/ingest/events", headers=H_A, json={"events": [{"vendor": "openai"}]})

    # This process has not been restarted with the new key yet.
    monkeypatch.setattr(settings, "webhook_secret_keys", "old:" + "o" * 40)
    caplog.set_level(logging.WARNING, logger="collector.webhooks")
    rx = Receiver(200)
    run = await webhooks.deliver_due(env["factory"], transport=rx.transport())
    assert (run.claimed, run.retrying, run.dead) == (2, 2, 0)
    assert rx.requests == []
    for row in await _deliveries(env["factory"]):
        assert row.status == "pending" and row.attempts == 1
        assert row.last_error == (
            "signing secret uses key id new, not configured on this worker; will retry"
        )
        assert row.next_attempt_at > datetime.now(UTC) + timedelta(seconds=50)
    warnings = [r for r in caplog.records if "key id 'new'" in r.getMessage()]
    assert len(warnings) == 1  # once per key, not per delivery

    # A process with the key picks the retry up and signs it normally.
    monkeypatch.setattr(settings, "webhook_secret_keys", new_keys)
    later = datetime.now(UTC) + timedelta(minutes=2)
    run = await webhooks.deliver_due(env["factory"], transport=rx.transport(), now=later)
    assert run.succeeded == 2
    signed = [r for r in rx.requests if webhooks.verify_signature(
        sub["secret"], r.content, r.headers[webhooks.SIGNATURE_HEADER]
    )]
    assert len(signed) == 1
    assert {r.attempts for r in await _deliveries(env["factory"])} == {2}


async def test_unknown_key_id_goes_dead_only_after_the_normal_attempts(env, monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret_keys", "gone:" + "g" * 40)
    async with _client() as c:
        await _subscribe(c, env["pa"])
        await c.post("/ingest/events", headers=H_A, json={"events": [{"vendor": "openai"}]})
    monkeypatch.setattr(settings, "webhook_secret_keys", "new:" + "n" * 40)
    rx = Receiver(200)
    future = datetime.now(UTC)
    outcomes = []
    for _ in range(3):  # max_attempts=3
        run = await webhooks.deliver_due(env["factory"], transport=rx.transport(), now=future)
        outcomes.append((run.retrying, run.dead))
        future += timedelta(days=10)
    assert outcomes == [(1, 0), (1, 0), (0, 1)]
    assert rx.requests == []
    (row,) = await _deliveries(env["factory"])
    assert row.status == "dead" and row.attempts == 3
    assert "key id gone" in row.last_error
    assert "gave up" in row.last_error and "will retry" not in row.last_error


async def test_documented_rollout_order_never_strands_a_delivery(env, monkeypatch):
    """Step 1 (new key added after the active one) and step 2 (new key
    first) may overlap during a restart: either process reads the other's
    secrets."""
    old, new = "old:" + "o" * 40, "new:" + "n" * 40
    monkeypatch.setattr(settings, "webhook_secret_keys", old)
    async with _client() as c:
        sub = (await _subscribe(c, env["pa"])).json()

    vendors = iter(range(100))

    async def deliver_with(keys: str) -> str:
        monkeypatch.setattr(settings, "webhook_secret_keys", keys)
        event = {"vendor": f"vendor-{next(vendors)}"}
        async with _client() as c:
            await c.post("/ingest/events", headers=H_A, json={"events": [event]})
        rx = Receiver(200)
        assert (await webhooks.deliver_due(env["factory"], transport=rx.transport())).succeeded == 1
        req = rx.requests[0]
        assert webhooks.verify_signature(
            sub["secret"], req.content, req.headers[webhooks.SIGNATURE_HEADER]
        )
        return (await _subscriptions(env["factory"]))[sub["id"]].secret_ciphertext

    assert (await deliver_with(f"{old},{new}")).startswith("v2.old.")  # step 1
    assert (await deliver_with(f"{new},{old}")).startswith("v2.new.")  # step 2, restarted
    assert (await deliver_with(f"{old},{new}")).startswith("v2.old.")  # step 2, not yet
    assert (await deliver_with(f"{new},{old}")).startswith("v2.new.")


async def test_corrupt_secret_still_dead_letters_at_once(env):
    async with _client() as c:
        sub = (await _subscribe(c, env["pa"])).json()
        await c.post("/ingest/events", headers=H_A, json={"events": [{"vendor": "openai"}]})
    async with env["factory"]() as s:
        await s.execute(
            update(WebhookSubscription)
            .where(WebhookSubscription.id == sub["id"])
            .values(secret_ciphertext="v1.AAAA")
        )
        await s.commit()
    run = await webhooks.deliver_due(env["factory"], transport=Receiver(200).transport())
    assert run.dead == 1
    (row,) = await _deliveries(env["factory"])
    assert row.last_error == "signing secret unreadable; rotate the secret"


async def test_malformed_keyring_fails_fast_without_burning_attempts(env, monkeypatch):
    async with _client() as c:
        await _subscribe(c, env["pa"])
        await c.post("/ingest/events", headers=H_A, json={"events": [{"vendor": "openai"}]})
    monkeypatch.setattr(settings, "webhook_secret_keys", "oops")
    with pytest.raises(webhooks.WebhooksNotConfigured):
        await webhooks.deliver_due(env["factory"], transport=Receiver(200).transport())
    (row,) = await _deliveries(env["factory"])
    assert row.status == "pending" and row.attempts == 0


# ── attestation events ────────────────────────────────────────────────


async def test_attestation_queues_asset_attested_without_personal_data(env):
    async with _client() as c:
        await _subscribe(c, env["pa"])  # default: every event type
        await _subscribe(c, env["pa"], event_types=["asset.created"])
        await c.post(
            "/ingest/events",
            headers=H_A,
            json={"events": [{"vendor": "openai", "owner_email": "owner@corp.example"}]},
        )
        async with env["factory"]() as s:
            asset_id = (await s.execute(select(AIAsset.id))).scalar_one()
        resp = await c.post(
            f"/api/v1/registry/assets/{asset_id}/attestations",
            headers=H_A,
            json={
                "field": "owner",
                "state": "disputed",
                "attested_by": "reviewer@corp.example",
                "comment": "Jane Doe left; ask jane.doe@corp.example",
            },
        )
        assert resp.status_code == 201, resp.text

    rows = [r for r in await _deliveries(env["factory"]) if r.event_type == "asset.attested"]
    assert len(rows) == 1  # not to the asset.created-only subscription
    payload = rows[0].payload
    assert payload["type"] == "asset.attested"
    assert rows[0].asset_id == asset_id
    assert payload["data"]["attestation"] == {
        "id": resp.json()["data"]["id"],
        "field": "owner",
        "state": "disputed",
        "actor_type": "tenant",
        "attested_at": resp.json()["data"]["attested_at"],
    }
    assert payload["data"]["asset"]["id"] == str(asset_id)
    dumped = json.dumps(payload)
    for personal in ("reviewer@corp.example", "owner@corp.example", "Jane Doe"):
        assert personal not in dumped

    rx = Receiver(200)
    await webhooks.deliver_due(env["factory"], transport=rx.transport())
    assert "asset.attested" in [r.headers[webhooks.EVENT_TYPE_HEADER] for r in rx.requests]


async def test_attestation_event_needs_changes_scope(env):
    async with _client() as c:
        await _subscribe(c, env["pa_changes"], event_types=["asset.attested"])
        resp = await _subscribe(c, env["pa_assets"], event_types=["asset.attested"])
        assert resp.status_code == 400 and "changes:read" in resp.text
        await c.post("/ingest/events", headers=H_A, json={"events": [{"vendor": "openai"}]})
        async with env["factory"]() as s:
            asset_id = (await s.execute(select(AIAsset.id))).scalar_one()
            await s.execute(
                update(PartnerCredential)
                .where(PartnerCredential.id == env["pa_changes"])
                .values(scopes=["assets:read"])
            )
            await s.commit()
        resp = await c.post(
            f"/api/v1/registry/assets/{asset_id}/attestations",
            headers=H_A,
            json={"field": "asset", "state": "attested", "attested_by": "r@corp.example"},
        )
        assert resp.status_code == 201
    assert await _deliveries(env["factory"]) == []


# ── idle subscriptions: expired previous secrets are swept ────────────


async def test_sweep_clears_expired_previous_secrets_on_idle_subscriptions(env):
    async with _client() as c:
        expired = (await _subscribe(c, env["pa"])).json()
        await _rotate(c, expired["id"], grace_period_seconds=60)
        current = (await _subscribe(c, env["pa"])).json()
        await _rotate(c, current["id"], grace_period_seconds=3600)
        untouched = (await _subscribe(c, env["pa"])).json()
    async with env["factory"]() as s:
        await s.execute(
            update(WebhookSubscription)
            .where(WebhookSubscription.id == expired["id"])
            .values(previous_secret_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await s.commit()
    before = await _subscriptions(env["factory"])

    assert await webhooks.clear_expired_previous_secrets(env["factory"]) == 1
    after = await _subscriptions(env["factory"])
    row = after[expired["id"]]
    assert row.previous_secret_ciphertext is None and row.previous_secret_expires_at is None
    assert row.updated_at == before[expired["id"]].updated_at
    assert after[current["id"]].previous_secret_ciphertext is not None
    assert after[untouched["id"]].previous_secret_ciphertext is None
    # Nothing left; later, the other grace period ends too.
    assert await webhooks.clear_expired_previous_secrets(env["factory"]) == 0
    later = datetime.now(UTC) + timedelta(hours=2)
    assert await webhooks.clear_expired_previous_secrets(env["factory"], now=later) == 1


# ── PATCH /subscriptions/{id} ─────────────────────────────────────────


def _patch(c, sid, headers=H_A, **body):
    return c.patch(f"/api/v1/admin/webhooks/subscriptions/{sid}", headers=headers, json=body)


async def _patch_audit(factory) -> list[PartnerAuditLog]:
    async with factory() as s:
        return (
            await s.execute(
                select(PartnerAuditLog)
                .where(PartnerAuditLog.method == "PATCH")
                .order_by(PartnerAuditLog.timestamp)
            )
        ).scalars().all()


async def test_patch_updates_url_and_event_types_and_is_audited(env):
    async with _client() as c:
        sub = (await _subscribe(c, env["pa"])).json()
        resp = await _patch(
            c,
            sub["id"],
            url="https://new.partner.example/hook?token=abc",
            event_types=["asset.retired", "asset.created", "asset.created"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["url"] == "https://new.partner.example/hook?token=abc"
        assert body["event_types"] == ["asset.created", "asset.retired"]
        assert "secret" not in body
        got = (await c.get(f"/api/v1/admin/webhooks/subscriptions/{sub['id']}", headers=H_A)).json()
        assert got == body

        await c.post("/ingest/events", headers=H_A, json={"events": [{"vendor": "openai"}]})
    rx = Receiver(200)
    await webhooks.deliver_due(env["factory"], transport=rx.transport())
    (req,) = rx.requests
    assert req.headers["Host"] == "new.partner.example"
    # The secret is unchanged.
    assert webhooks.verify_signature(sub["secret"], req.content, req.headers[webhooks.SIGNATURE_HEADER])

    (audit,) = await _patch_audit(env["factory"])
    assert audit.tenant_id == env["tenant_a"] and audit.status_code == 200
    assert audit.endpoint == "/api/v1/admin/webhooks/subscriptions/{subscription_id}"
    assert audit.query_params == {"changed_fields": ["url", "event_types"]}
    assert audit.api_key_prefix and audit.partner_id is None
    assert "token=abc" not in json.dumps(audit.query_params)


async def test_patch_pause_stops_events_and_resume_restarts_them(env):
    async with _client() as c:
        sub = (await _subscribe(c, env["pa"])).json()
        await c.post("/ingest/events", headers=H_A, json={"events": [{"vendor": "queued"}]})
        resp = await _patch(c, sub["id"], is_active=False)
        assert resp.status_code == 200 and resp.json()["is_active"] is False
        await c.post("/ingest/events", headers=H_A, json={"events": [{"vendor": "paused"}]})
        assert len(await _deliveries(env["factory"])) == 1

        rx = Receiver(200)
        assert (await webhooks.deliver_due(env["factory"], transport=rx.transport())).dead == 1
        assert rx.requests == []
        (row,) = await _deliveries(env["factory"])
        assert row.last_error == "subscription disabled"

        assert (await _patch(c, sub["id"], is_active=True)).json()["is_active"] is True
        await c.post("/ingest/events", headers=H_A, json={"events": [{"vendor": "resumed"}]})
        assert (await webhooks.deliver_due(env["factory"], transport=rx.transport())).succeeded == 1
    assert [a.query_params["changed_fields"] for a in await _patch_audit(env["factory"])] == [
        ["is_active"],
        ["is_active"],
    ]


async def test_patch_checks_scopes_on_new_events_and_on_resume_but_not_on_pause(env):
    async with _client() as c:
        sub = (await _subscribe(c, env["pa"])).json()
        async with env["factory"]() as s:
            await s.execute(
                update(PartnerCredential)
                .where(PartnerCredential.id == env["pa"])
                .values(scopes=["assets:read"])
            )
            await s.commit()
        resp = await _patch(c, sub["id"], event_types=["asset.retired"])
        assert resp.status_code == 400 and "changes:read" in resp.text
        # Pausing still works; resuming needs the scope back.
        assert (await _patch(c, sub["id"], is_active=False)).status_code == 200
        resp = await _patch(c, sub["id"], is_active=True)
        assert resp.status_code == 400 and "changes:read" in resp.text
        async with env["factory"]() as s:
            await s.execute(
                update(PartnerCredential)
                .where(PartnerCredential.id == env["pa"])
                .values(is_active="false", scopes=["changes:read"])
            )
            await s.commit()
        resp = await _patch(c, sub["id"], is_active=True)
        assert resp.status_code == 400 and "revoked" in resp.text
        got = (await c.get(f"/api/v1/admin/webhooks/subscriptions/{sub['id']}", headers=H_A)).json()
        assert got["is_active"] is False and got["event_types"] == sub["event_types"]
    assert [(a.status_code, a.query_params) for a in await _patch_audit(env["factory"])] == [
        (400, None),
        (200, {"changed_fields": ["is_active"]}),
        (400, None),
        (400, None),
    ]


@pytest.mark.parametrize(
    "url, resolved",
    [
        ("http://hooks.partner.example/ps", "93.184.216.34"),
        ("https://user:pw@hooks.partner.example/ps", "93.184.216.34"),
        ("https://internal.example/ps", "10.0.0.5"),
        ("https://metadata.example/ps", "169.254.169.254"),
    ],
)
async def test_patch_url_gets_the_full_destination_checks(env, monkeypatch, url, resolved):
    async with _client() as c:
        sub = (await _subscribe(c, env["pa"])).json()

        async def resolve(host, port):
            return [resolved]

        monkeypatch.setattr(webhooks, "_resolve_host", resolve)
        resp = await _patch(c, sub["id"], url=url)
        assert resp.status_code == 400 and "Invalid webhook URL" in resp.text
        got = (await c.get(f"/api/v1/admin/webhooks/subscriptions/{sub['id']}", headers=H_A)).json()
        assert got["url"] == URL


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"event_types": ["asset.deleted"]},
        {"event_types": []},
        {"url": None},
        {"is_active": None},
        {"url": ""},
        {"secret": "whsec_mine"},
        {"partner_id": "00000000-0000-0000-0000-000000000000"},
    ],
)
async def test_patch_rejects_invalid_bodies(env, body):
    async with _client() as c:
        sub = (await _subscribe(c, env["pa"])).json()
        resp = await c.patch(
            f"/api/v1/admin/webhooks/subscriptions/{sub['id']}", headers=H_A, json=body
        )
        assert resp.status_code == 422, resp.text


async def test_patch_is_tenant_scoped(env):
    async with _client() as c:
        sub = (await _subscribe(c, env["pa"])).json()
        resp = await _patch(c, sub["id"], headers=H_B, is_active=False)
        assert resp.status_code == 404
        assert (await _patch(c, sub["id"], headers={"Authorization": "Bearer nope"}, is_active=False)).status_code == 401
        got = (await c.get(f"/api/v1/admin/webhooks/subscriptions/{sub['id']}", headers=H_A)).json()
        assert got["is_active"] is True
    (audit,) = await _patch_audit(env["factory"])
    assert audit.status_code == 404 and audit.tenant_id != env["tenant_a"]


def test_patch_is_in_the_openapi_schema():
    schema = app.openapi()
    op = schema["paths"]["/api/v1/admin/webhooks/subscriptions/{subscription_id}"]["patch"]
    ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    model = schema["components"]["schemas"][ref.rsplit("/", 1)[1]]
    assert set(model["properties"]) == {"url", "event_types", "is_active"}
    assert model.get("additionalProperties") is False
