"""Hash-chained admin audit trail (collector.audit_trail) against PostgreSQL
(PS_TEST_DATABASE_URL): chain correctness, tamper detection, concurrent
writers, purge checkpoints and erasure, the routes that record events, the
`audit.*` webhook export, rate-limited auth failures and the verify CLI
and endpoint. RLS is covered in test_rls_db.py.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from collector import audit_trail, retention, rls, webhooks
from collector.app import app
from collector.config import settings
from collector.partner_auth import hash_secret
from db import audit_chain
from db.api_keys import api_key_lookup_prefix
from db.models import (
    AuditEvent,
    PartnerCredential,
    RetentionAuditLog,
    Tenant,
    WebhookDelivery,
    WebhookSubscription,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TENANT_KEY = "ps-test-key-12345"
H = {"Authorization": f"Bearer {TENANT_KEY}"}
TOKEN = "op-secret-token"
OP = {"Authorization": f"Bearer {TOKEN}"}
K1 = "1" * 40
ADMIN_KEY = "sk-admin-AUDITED-secret-0001"
ADMIN_KEY_2 = "sk-admin-AUDITED-secret-0002"
URL = "https://siem.partner.example/hook"
ACTOR = audit_trail.system_actor("test")


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret_keys", f"k1:{K1}")
    monkeypatch.setattr(settings, "webhook_secret_key", "")
    monkeypatch.setattr(settings, "admin_token", TOKEN)
    monkeypatch.setenv("PS_ADMIN_TOKEN", TOKEN)  # provider_usage_admin reads the env
    monkeypatch.delenv("PS_PROVIDER_ADMIN_KEYS", raising=False)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 0)

    async def public(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(webhooks, "_resolve_host", public)
    audit_trail.auth_failure_limiter.reset()


@pytest.fixture
async def env(seeded_db):
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        tenant = (await s.execute(select(Tenant))).scalar_one()
    return {"engine": seeded_db, "factory": factory, "tenant": tenant.id}


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _write(factory, tenant_id, n: int, action: str = "test.action") -> None:
    for i in range(n):
        async with factory() as s:
            await audit_trail.record(
                s, tenant_id=tenant_id, action=action, actor=ACTOR, details={"i": i}
            )
            await s.commit()


async def _rows(factory, tenant_id) -> list[AuditEvent]:
    async with factory() as s:
        return (
            await s.execute(
                select(AuditEvent)
                .where(AuditEvent.tenant_id == tenant_id)
                .order_by(AuditEvent.seq)
            )
        ).scalars().all()


async def _verify(factory, tenant_id) -> audit_trail.VerifyResult:
    async with factory() as s:
        return await audit_trail.verify_chain(s, tenant_id)


async def _tamper(engine, sql: str, params: dict) -> None:
    """Edit rows the way someone holding the table owner role could: with
    the append-only triggers switched off."""
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER USER"))
        await conn.execute(text(sql), params)
        await conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER USER"))


# ── chain correctness ─────────────────────────────────────────────────


async def test_chain_links_every_row(env):
    f, t = env["factory"], env["tenant"]
    other = uuid.uuid4()
    await _write(f, t, 5)
    await _write(f, other, 2)
    rows = await _rows(f, t)
    assert [r.seq for r in rows] == [1, 2, 3, 4, 5]
    assert rows[0].prev_hash == audit_chain.GENESIS_HASH
    for prev, row in itertools.pairwise(rows):
        assert row.prev_hash == prev.row_hash
    for row in rows:
        assert row.row_hash == audit_chain.compute_row_hash(
            row.prev_hash, audit_trail.chained_fields(row)
        )
    result = await _verify(f, t)
    assert (result.ok, result.rows, result.anchor) == (True, 5, "genesis")
    assert result.head_hash == rows[-1].row_hash
    # Chains are per tenant: the other tenant's starts at genesis too.
    assert [r.seq for r in await _rows(f, other)] == [1, 2]
    assert (await _verify(f, other)).ok


async def test_rows_are_append_only(env):
    f, t = env["factory"], env["tenant"]
    await _write(f, t, 2)
    async with f() as s:
        with pytest.raises(DBAPIError, match="immutable"):
            await s.execute(text("UPDATE audit_events SET outcome = 'failure'"))
    async with f() as s:
        with pytest.raises(DBAPIError, match="append-only"):
            await s.execute(text("DELETE FROM audit_events"))


# ── tamper detection ──────────────────────────────────────────────────


async def test_modified_row_is_reported(env):
    f, t = env["factory"], env["tenant"]
    await _write(f, t, 5)
    await _tamper(
        env["engine"],
        "UPDATE audit_events SET details = '{\"i\": 99}' WHERE tenant_id = :t AND seq = 3",
        {"t": t},
    )
    result = await _verify(f, t)
    assert not result.ok
    assert result.first_break["seq"] == 3
    assert "row modified" in result.first_break["reason"]


async def test_modified_row_with_recomputed_hash_breaks_the_next_link(env):
    f, t = env["factory"], env["tenant"]
    await _write(f, t, 4)
    row = (await _rows(f, t))[1]
    row.outcome = "failure"
    forged = audit_chain.compute_row_hash(row.prev_hash, audit_trail.chained_fields(row))
    await _tamper(
        env["engine"],
        "UPDATE audit_events SET outcome = 'failure', row_hash = :h "
        "WHERE tenant_id = :t AND seq = 2",
        {"t": t, "h": forged},
    )
    result = await _verify(f, t)
    assert (result.ok, result.first_break["seq"]) == (False, 3)
    assert "prev_hash" in result.first_break["reason"]


async def test_deleted_row_is_a_gap(env):
    f, t = env["factory"], env["tenant"]
    await _write(f, t, 4)
    await _tamper(
        env["engine"], "DELETE FROM audit_events WHERE tenant_id = :t AND seq = 2", {"t": t}
    )
    result = await _verify(f, t)
    assert (result.ok, result.first_break["seq"]) == (False, 3)
    assert "seq gap" in result.first_break["reason"]


async def test_deleted_prefix_without_checkpoint_is_reported(env):
    f, t = env["factory"], env["tenant"]
    await _write(f, t, 3)
    await _tamper(
        env["engine"], "DELETE FROM audit_events WHERE tenant_id = :t AND seq = 1", {"t": t}
    )
    result = await _verify(f, t)
    assert not result.ok
    assert "no audit.purge checkpoint" in result.first_break["reason"]


async def test_verify_cli_exit_code(env, monkeypatch, capsys):
    f, t = env["factory"], env["tenant"]
    await _write(f, t, 3)
    url = env["engine"].url.render_as_string(hide_password=False)
    monkeypatch.setattr(settings, "database_url", url)
    assert await asyncio.to_thread(audit_trail.main, ["verify", "--tenant", str(t)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    await _tamper(
        env["engine"],
        "UPDATE audit_events SET action = 'x' WHERE tenant_id = :t AND seq = 2",
        {"t": t},
    )
    assert await asyncio.to_thread(audit_trail.main, ["verify"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["data"][0]["first_break"]["seq"] == 2


async def test_verify_endpoint_reports_the_first_break(env):
    f, t = env["factory"], env["tenant"]
    await _write(f, t, 3)
    async with _client() as c:
        resp = await c.get("/api/v1/operator/audit/verify", headers=OP, params={"tenant_id": str(t)})
        assert resp.status_code == 200 and resp.json()["ok"] is True
        await _tamper(
            env["engine"],
            "UPDATE audit_events SET actor_id = 'someone-else' WHERE tenant_id = :t AND seq = 1",
            {"t": t},
        )
        resp = await c.get("/api/v1/operator/audit/verify", headers=OP, params={"tenant_id": str(t)})
    body = resp.json()
    assert body["ok"] is False
    assert body["data"][0]["first_break"]["seq"] == 1


# ── concurrent writers ────────────────────────────────────────────────


async def _concurrent(factory, tenant_id, n: int) -> list:
    async def one(i):
        async with factory() as s:
            await audit_trail.record(
                s, tenant_id=tenant_id, action="test.concurrent", actor=ACTOR, details={"i": i}
            )
            await s.commit()

    return await asyncio.gather(*(one(i) for i in range(n)), return_exceptions=True)


async def _slow_head(monkeypatch):
    """Widen the read-head-then-insert window so an unserialised race is
    certain rather than likely."""
    real = audit_trail._chain_head

    async def slow(db, tenant_id):
        head = await real(db, tenant_id)
        await asyncio.sleep(0.05)
        return head

    monkeypatch.setattr(audit_trail, "_chain_head", slow)


async def test_concurrent_writers_are_serialised_per_tenant(env, monkeypatch):
    f, t = env["factory"], env["tenant"]
    await _slow_head(monkeypatch)
    outcomes = await _concurrent(f, t, 12)
    assert [o for o in outcomes if o is not None] == []
    rows = await _rows(f, t)
    assert [r.seq for r in rows] == list(range(1, 13))
    assert sorted(r.details["i"] for r in rows) == list(range(12))
    assert (await _verify(f, t)).ok


async def test_without_the_lock_concurrent_writers_collide(env, monkeypatch):
    """The control for the test above: the advisory lock is what makes it
    pass. Without it the unique (tenant_id, seq) constraint refuses the
    writers that read the same head, and nothing forks the chain."""
    f, t = env["factory"], env["tenant"]
    await _slow_head(monkeypatch)

    async def no_lock(db, tenant_id):
        return None

    monkeypatch.setattr(audit_trail, "_lock_chain", no_lock)
    outcomes = await _concurrent(f, t, 6)
    assert any(isinstance(o, IntegrityError) for o in outcomes)
    assert (await _verify(f, t)).ok


async def test_tenants_do_not_block_each_other(env, monkeypatch):
    f = env["factory"]
    t1, t2 = uuid.uuid4(), uuid.uuid4()
    await _slow_head(monkeypatch)
    await asyncio.gather(_concurrent(f, t1, 4), _concurrent(f, t2, 4))
    assert [r.seq for r in await _rows(f, t1)] == [1, 2, 3, 4]
    assert [r.seq for r in await _rows(f, t2)] == [1, 2, 3, 4]


# ── purge checkpoints and erasure ─────────────────────────────────────


async def test_purge_leaves_a_verifiable_checkpoint(env):
    f, t = env["factory"], env["tenant"]
    old = datetime.now(UTC) - timedelta(days=400)
    for i in range(3):
        async with f() as s:
            await audit_trail.record(
                s, tenant_id=t, action="test.old", actor=ACTOR, now=old + timedelta(minutes=i)
            )
            await s.commit()
    await _write(f, t, 2, action="test.new")
    third = (await _rows(f, t))[2]

    async with f() as s:
        await rls.bypass(s, "test")
        results = await audit_trail.purge_expired(s, 365)
    assert [(r.rows_deleted, r.through_seq, r.checkpoint_seq) for r in results] == [(3, 3, 6)]

    rows = await _rows(f, t)
    assert [r.seq for r in rows] == [4, 5, 6]
    checkpoint = rows[-1]
    assert checkpoint.action == audit_chain.CHECKPOINT_ACTION
    assert checkpoint.details["through_seq"] == 3
    assert checkpoint.details["through_hash"] == third.row_hash
    result = await _verify(f, t)
    assert (result.ok, result.anchor, result.first_seq, result.rows) == (True, "checkpoint", 4, 3)

    # Purging again with nothing old is a no-op; a forged checkpoint
    # (through_hash changed) is caught as a modified row.
    async with f() as s:
        assert (await audit_trail.purge_expired(s, 365))[0].rows_deleted == 0
    await _tamper(
        env["engine"],
        "UPDATE audit_events SET details = jsonb_set(details, '{through_hash}', '\"00\"') "
        "WHERE tenant_id = :t AND seq = 6",
        {"t": t},
    )
    assert not (await _verify(f, t)).ok


async def test_erasure_keeps_the_chain_and_ends_it_with_the_erase(env):
    f, t = env["factory"], env["tenant"]
    async with _client() as c:
        resp = await c.put(
            f"/api/v1/operator/tenants/{t}/retention", headers=OP, json={"retention_days": 30}
        )
        assert resp.status_code == 200, resp.text
        resp = await c.delete(f"/api/v1/operator/tenants/{t}", headers=OP, params={"confirm": str(t)})
        assert resp.status_code == 200, resp.text
    assert "audit_events" not in resp.json()["rows_deleted"]

    async with f() as s:
        assert await s.get(Tenant, t) is None
    rows = await _rows(f, t)
    assert [r.action for r in rows] == ["tenant.retention.set", "tenant.erase"]
    erase = rows[-1]
    assert (erase.actor_type, erase.actor_id, erase.target_id) == ("operator", "operator-api", str(t))
    assert erase.details["tenants"] == 1
    assert erase.request_id
    assert (await _verify(f, t)).ok


async def test_retention_purge_run_is_recorded(env):
    f, t = env["factory"], env["tenant"]
    from db.models import AIAsset, AIUsageEvent

    async with f() as s:
        asset = AIAsset(tenant_id=t, vendor="v", discovery_source=["sdk"])
        s.add(asset)
        await s.flush()
        s.add(
            AIUsageEvent(
                tenant_id=t, asset_id=asset.id, source="sdk",
                timestamp=datetime.now(UTC) - timedelta(days=500),
            )
        )
        await s.commit()
    async with f() as s:
        await retention.purge_expired_events(s, actor="cli")
    rows = await _rows(f, t)
    assert [(r.action, r.actor_type, r.actor_id) for r in rows] == [
        ("retention.purge", "system", "cli")
    ]
    assert rows[0].details["ai_usage_events"] == 1 and "cutoff" in rows[0].details
    async with f() as s:
        legacy = (await s.execute(select(RetentionAuditLog))).scalars().all()
    assert [x.action for x in legacy] == ["purge"]


# ── recorded admin actions ────────────────────────────────────────────


async def _all_text(factory) -> str:
    async with factory() as s:
        rows = (await s.execute(text("SELECT row_to_json(a)::text FROM audit_events a"))).scalars()
        deliveries = (
            await s.execute(text("SELECT payload::text FROM webhook_deliveries"))
        ).scalars()
        return "\n".join([*rows, *deliveries])


async def test_provider_credential_lifecycle_is_recorded_without_the_key(env):
    f, t = env["factory"], env["tenant"]
    path = "/api/v1/admin/provider-credentials/openai"
    async with _client() as c:
        assert (await c.post(path, headers=H, json={"admin_key": ADMIN_KEY})).status_code == 201
        # Refused: already stored. Rolled back, but the attempt is recorded.
        assert (await c.post(path, headers=H, json={"admin_key": ADMIN_KEY})).status_code == 409
        resp = await c.post(path + "/rotate", headers=H, json={"admin_key": ADMIN_KEY_2})
        assert resp.status_code == 200
        resp = await c.put(
            "/api/v1/admin/provider-key-fingerprints/openai/key_abc",
            headers=H,
            json={"api_key_fingerprint": "0123456789abcdef"},
        )
        assert resp.status_code == 201
        assert (await c.delete(path, headers={**H, "X-Request-ID": "req-123"})).status_code == 204
        assert (await c.get(path, headers=H)).status_code == 404  # reads are not audited

    rows = await _rows(f, t)
    assert [(r.action, r.outcome, r.status_code) for r in rows] == [
        ("provider_credential.create", "success", 201),
        ("provider_credential.create", "failure", 409),
        ("provider_credential.rotate", "success", 200),
        ("provider_key_fingerprint.put", "success", 201),
        ("provider_credential.delete", "success", 204),
    ]
    assert rows[0].actor_type == "tenant_key"
    assert rows[0].actor_id == api_key_lookup_prefix(TENANT_KEY)
    assert (rows[0].target_type, rows[0].target_id) == ("provider_credential", "openai")
    assert rows[3].details == {"provider": "openai", "api_key_id": "key_abc"}
    assert rows[-1].request_id == "req-123"
    everything = await _all_text(f)
    for secret in (ADMIN_KEY, ADMIN_KEY_2, TENANT_KEY):
        assert secret not in everything
    assert (await _verify(f, t)).ok


async def test_provider_credential_use_is_recorded(env, monkeypatch):
    from collector import provider_usage as pu

    f, t = env["factory"], env["tenant"]
    async with _client() as c:
        resp = await c.post(
            "/api/v1/admin/provider-credentials/openai", headers=H, json={"admin_key": ADMIN_KEY}
        )
        assert resp.status_code == 201

    async def fake_fetch(key, start, end, **kw):
        # The chain lock is released before the provider is called.
        async with f() as other:
            await audit_trail.record(other, tenant_id=t, action="test.during_pull", actor=ACTOR)
            await other.commit()
        return []

    monkeypatch.setitem(pu.FETCHERS, "openai", fake_fetch)
    async with f() as s:
        await rls.set_tenant(s, t)
        result = await asyncio.wait_for(
            pu.pull_provider_usage(s, t, providers=["openai"]), timeout=10
        )
    assert result["providers"]["openai"]["credential_source"] == "stored"
    actions = [(r.action, r.actor_id) for r in await _rows(f, t)]
    assert actions[-2:] == [
        ("provider_credential.use", "provider-usage-pull"),
        ("test.during_pull", "test"),
    ]


async def test_partner_scope_and_webhook_admin_are_recorded(env):
    f, t = env["factory"], env["tenant"]
    async with _client() as c:
        resp = await c.post(
            "/api/v1/admin/partner-credentials",
            headers=H,
            json={"partner_name": "siem", "scopes": ["audit:read"]},
        )
        assert resp.status_code == 201, resp.text
        created = resp.json()
        pid = created["id"]
        resp = await c.patch(
            f"/api/v1/admin/partner-credentials/{pid}",
            headers=H,
            json={"scopes": ["audit:read", "assets:read"], "rate_limit_tier": "basic"},
        )
        assert resp.status_code == 200
        resp = await c.patch(
            f"/api/v1/admin/partner-credentials/{pid}", headers=H, json={"scopes": ["nope"]}
        )
        assert resp.status_code == 400
        resp = await c.post(
            "/api/v1/admin/webhooks/subscriptions",
            headers=H,
            json={"partner_id": pid, "url": URL, "event_types": ["audit.*"]},
        )
        assert resp.status_code == 201, resp.text
        sid = resp.json()["id"]
        sub_secret = resp.json()["secret"]
        for call in (
            c.patch(f"/api/v1/admin/webhooks/subscriptions/{sid}", headers=H, json={"is_active": False}),
            c.post(f"/api/v1/admin/webhooks/subscriptions/{sid}/rotate-secret", headers=H, json={}),
            c.delete(f"/api/v1/admin/webhooks/subscriptions/{sid}", headers=H),
            c.delete(f"/api/v1/admin/partner-credentials/{pid}", headers=H),
        ):
            assert (await call).status_code in (200, 204)

    rows = await _rows(f, t)
    assert [(r.action, r.outcome) for r in rows] == [
        ("partner_credential.create", "success"),
        ("partner_credential.update", "success"),
        ("partner_credential.update", "failure"),
        ("webhook_subscription.create", "success"),
        ("webhook_subscription.update", "success"),
        ("webhook_subscription.rotate_secret", "success"),
        ("webhook_subscription.delete", "success"),
        ("partner_credential.revoke", "success"),
    ]
    update = rows[1].details
    assert update["scopes_added"] == ["assets:read"] and update["scopes_removed"] == []
    assert update["rate_limit_tier"] == "basic"
    assert rows[4].details["changed_fields"] == ["is_active"]
    everything = await _all_text(f)
    for secret in (created["client_secret"], created["api_key"], sub_secret, URL):
        assert secret not in everything
    assert (await _verify(f, t)).ok


async def test_attestation_and_retirement_are_recorded(env):
    from db.models import AIAsset

    f, t = env["factory"], env["tenant"]
    async with f() as s:
        asset = AIAsset(tenant_id=t, vendor="openai", model="gpt", discovery_source=["sdk"])
        s.add(asset)
        await s.commit()
    async with _client() as c:
        resp = await c.post(
            f"/api/v1/registry/assets/{asset.id}/attestations",
            headers=H,
            json={"field": "asset", "state": "retired", "attested_by": "owner@bank.example"},
        )
        assert resp.status_code == 201, resp.text
        resp = await c.post("/api/v1/admin/asset-lifecycle/sweep", headers=H)
        assert resp.status_code == 200
    rows = await _rows(f, t)
    assert [r.action for r in rows] == [
        "registry.attest",
        "registry.retire",
        "registry.lifecycle_sweep",
    ]
    assert rows[0].details["field"] == "asset" and rows[0].target_id == str(asset.id)
    assert "owner@bank.example" not in await _all_text(f)


# ── auth failures ─────────────────────────────────────────────────────


async def _system_rows(factory):
    return await _rows(factory, audit_chain.SYSTEM_TENANT_ID)


async def test_operator_auth_failures_are_recorded_and_rate_limited(env):
    f = env["factory"]
    async with _client() as c:
        for _ in range(8):
            resp = await c.post(
                "/api/v1/operator/retention/purge",
                headers={"Authorization": "Bearer wrong"},
                json={},
            )
            assert resp.status_code == 401
        resp = await c.post("/api/v1/admin/provider-usage/pull", json={"tenant_id": str(uuid.uuid4())})
        assert resp.status_code == 401
    rows = await _system_rows(f)
    assert len(rows) == audit_trail.auth_failure_limiter.per_source  # 5 of 9
    first = rows[0]
    assert (first.action, first.outcome, first.status_code) == ("auth.failure", "failure", 401)
    assert first.details["realm"] == "operator"
    assert first.details["reason"] == "invalid_credentials"
    assert first.details["route"] == "/api/v1/operator/retention/purge"
    assert first.actor_type == "anonymous"
    assert first.actor_id == api_key_lookup_prefix("wrong")  # never the token
    assert "wrong" not in await _all_text(f)

    # After the window, the next row says how many were dropped.
    audit_trail.auth_failure_limiter._hits.clear()
    audit_trail.auth_failure_limiter._all.clear()
    async with _client() as c:
        await c.post("/api/v1/operator/retention/purge", json={})
    last = (await _system_rows(f))[-1]
    assert last.details["reason"] == "missing_credentials"
    assert last.details["suppressed"] == 4
    assert (await _verify(f, audit_chain.SYSTEM_TENANT_ID)).ok


async def test_tenant_admin_and_partner_scope_failures(env):
    f, t = env["factory"], env["tenant"]
    async with f() as s:
        partner = PartnerCredential(
            tenant_id=t,
            partner_name="reader",
            client_id="reader-client",
            client_secret_hash=hash_secret("x"),
            api_key_hash=hash_secret("ps-pk-reader-key"),
            scopes=["assets:read"],
            is_active="true",
        )
        s.add(partner)
        await s.commit()
    async with _client() as c:
        resp = await c.get(
            "/api/v1/admin/partner-credentials", headers={"Authorization": "Bearer nope"}
        )
        assert resp.status_code == 401
        # Registry (non-admin) 401s are not admin auth failures.
        resp = await c.get("/api/v1/registry/assets", headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 401
        resp = await c.get(
            "/partner/v1/provider-credentials",
            headers={"Authorization": "Bearer ps-pk-reader-key"},
        )
        assert resp.status_code == 403
        resp = await c.get(
            "/partner/v1/assets/" + str(uuid.uuid4()) + "/risks",
            headers={"Authorization": "Bearer ps-pk-reader-key"},
        )
        assert resp.status_code == 403  # a read scope: partner_audit_log only

    system = await _system_rows(f)
    assert [(r.details["realm"], r.details["route"]) for r in system] == [
        ("tenant_admin", "/api/v1/admin/partner-credentials")
    ]
    rows = await _rows(f, t)
    assert [(r.action, r.actor_type, r.actor_id, r.status_code) for r in rows] == [
        ("auth.failure", "partner", str(partner.id), 403)
    ]
    assert rows[0].details["reason"] == "insufficient_scope"


async def test_operator_access_is_recorded(env):
    async with _client() as c:
        resp = await c.post(
            "/api/v1/admin/provider-usage/pull", headers=OP, json={"tenant_id": str(env["tenant"])}
        )
        assert resp.status_code == 200, resp.text
    rows = await _system_rows(env["factory"])
    assert [(r.action, r.actor_type, r.details["route"]) for r in rows] == [
        ("operator.access", "operator", "/api/v1/admin/provider-usage/pull")
    ]


# ── export through the webhook outbox ─────────────────────────────────


async def _siem_partner(factory, tenant_id, scopes):
    async with factory() as s:
        p = PartnerCredential(
            tenant_id=tenant_id,
            partner_name="siem",
            client_id=f"siem-{uuid.uuid4().hex[:6]}",
            client_secret_hash=hash_secret("x"),
            scopes=list(scopes),
            is_active="true",
        )
        s.add(p)
        await s.commit()
        return p.id


async def test_audit_events_export_through_the_outbox(env):
    f, t = env["factory"], env["tenant"]
    siem = await _siem_partner(f, t, ["audit:read"])
    assets_only = await _siem_partner(f, t, ["changes:read"])
    async with _client() as c:
        resp = await c.post(
            "/api/v1/admin/webhooks/subscriptions",
            headers=H,
            json={"partner_id": str(siem), "url": URL, "event_types": ["audit.*"]},
        )
        assert resp.status_code == 201, resp.text
        secret = resp.json()["secret"]
        # audit.* needs audit:read.
        resp = await c.post(
            "/api/v1/admin/webhooks/subscriptions",
            headers=H,
            json={"partner_id": str(assets_only), "url": URL, "event_types": ["audit.*"]},
        )
        assert resp.status_code == 400 and "audit:read" in resp.json()["detail"]["detail"]
        # An asset-event subscription gets no audit events.
        resp = await c.post(
            "/api/v1/admin/webhooks/subscriptions",
            headers=H,
            json={"partner_id": str(assets_only), "url": URL + "/assets"},
        )
        assert resp.status_code == 201
        resp = await c.post(
            "/api/v1/admin/provider-credentials/openai", headers=H, json={"admin_key": ADMIN_KEY}
        )
        assert resp.status_code == 201

    async with f() as s:
        deliveries = (
            await s.execute(select(WebhookDelivery).order_by(WebhookDelivery.created_at))
        ).scalars().all()
        subs = {x.id: x for x in (await s.execute(select(WebhookSubscription))).scalars()}
    # The siem subscription's own creation, the assets subscription and the
    # credential: three audit rows, all to the audit.* subscription.
    types = [d.event_type for d in deliveries]
    assert types == [
        "audit.webhook_subscription.create",
        "audit.webhook_subscription.create",
        "audit.provider_credential.create",
    ]
    assert {subs[d.subscription_id].partner_id for d in deliveries} == {siem}
    rows = {r.id: r for r in await _rows(f, t)}
    for d in deliveries:
        row = rows[d.event_id]  # event id = audit row id (idempotency key)
        audit = d.payload["data"]["audit"]
        assert audit == audit_trail.audit_payload(row)
        assert audit["row_hash"] == row.row_hash and audit["seq"] == row.seq
        assert d.asset_id is None

    received: list[httpx.Request] = []

    def handler(request):
        received.append(request)
        return httpx.Response(200)

    run = await webhooks.deliver_due(f, transport=httpx.MockTransport(handler))
    assert (run.claimed, run.succeeded) == (3, 3)
    # Attempts run concurrently, so arrival order is not seq order.
    by_seq = sorted(received, key=lambda r: json.loads(r.content)["data"]["audit"]["seq"])
    last = by_seq[-1]
    assert webhooks.verify_signature(secret, last.content, last.headers[webhooks.SIGNATURE_HEADER])
    event = json.loads(last.content)
    assert event["type"] == "audit.provider_credential.create"
    assert last.headers[webhooks.EVENT_ID_HEADER] == event["id"]
    # A receiver can check the exported chain on its own.
    exported = [json.loads(r.content)["data"]["audit"] for r in by_seq]
    for prev, cur in itertools.pairwise(exported):
        assert cur["prev_hash"] == prev["row_hash"]
        assert cur["row_hash"] == audit_chain.compute_row_hash(
            cur["prev_hash"],
            {
                **cur,
                "actor_type": cur["actor"]["type"],
                "actor_id": cur["actor"]["id"],
                "target_type": cur["target"]["type"],
                "target_id": cur["target"]["id"],
            },
        )
    assert ADMIN_KEY not in b"".join(r.content for r in received).decode()


async def test_audit_export_is_retried_then_dead_lettered(env, monkeypatch):
    monkeypatch.setattr(settings, "webhook_max_attempts", 2)
    f, t = env["factory"], env["tenant"]
    siem = await _siem_partner(f, t, ["audit:read"])
    async with _client() as c:
        resp = await c.post(
            "/api/v1/admin/webhooks/subscriptions",
            headers=H,
            json={"partner_id": str(siem), "url": URL, "event_types": ["audit.*"]},
        )
        assert resp.status_code == 201
    failing = httpx.MockTransport(lambda r: httpx.Response(503))
    assert (await webhooks.deliver_due(f, transport=failing)).retrying == 1
    later = datetime.now(UTC) + timedelta(days=1)
    assert (await webhooks.deliver_due(f, transport=failing, now=later)).dead == 1

    async with f() as s:
        d = (await s.execute(select(WebhookDelivery))).scalar_one()
        assert (d.status, d.event_type) == ("dead", "audit.webhook_subscription.create")
