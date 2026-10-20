"""Scoped admin tokens, tenant ingest key CRUD, operator tenant CRUD and
partner credential consistency, against a real PostgreSQL database
(PS_TEST_DATABASE_URL).

Covers: scopes allowed and denied, hashed storage, expiry and revocation,
the key rotation grace window, legacy key adoption, tenant isolation with
row-level security enforced, operator-only tenant routes, audit rows, the
backwards-compatible default (tenant keys keep full admin access) and
PS_ADMIN_REQUIRE_SCOPED_TOKENS.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from collector import audit_trail, rls
from collector.admin_auth import ADMIN_SCOPES
from collector.app import app
from collector.config import settings
from db.api_keys import api_key_lookup_prefix, hash_api_key, verify_api_key
from db.models import AdminToken, AuditEvent, Tenant, TenantApiKey
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TENANT_KEY = "ps-test-key-12345"  # the seeded_db tenant's legacy key
KEY_B = "ps-test-key-tenant-b"
OP_TOKEN = "op-secret-token"


def H(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


OP = H(OP_TOKEN)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", OP_TOKEN)
    monkeypatch.setattr(settings, "admin_require_scoped_tokens", False)
    monkeypatch.setattr(settings, "webhook_secret_keys", "k1:" + "1" * 40)
    monkeypatch.setattr(settings, "webhook_secret_key", "")
    monkeypatch.delenv("PS_PROVIDER_ADMIN_KEYS", raising=False)
    audit_trail.auth_failure_limiter.reset()


@pytest.fixture
async def client(seeded_db):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


def _admin(engine):
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        info=rls.bypass_info("test"),
    )


async def _add_tenant_b(engine) -> uuid.UUID:
    async with _admin(engine)() as s:
        b = Tenant(
            name="Other Corp",
            settings={},
            api_key_prefix=api_key_lookup_prefix(KEY_B),
            api_key_hash=hash_api_key(KEY_B),
        )
        s.add(b)
        await s.commit()
        return b.id


async def _token(c, scopes: list[str], key: str = TENANT_KEY, **extra) -> dict:
    name = extra.pop("name", f"t-{uuid.uuid4().hex[:8]}")
    resp = await c.post(
        "/api/v1/admin/tokens",
        headers=H(key),
        json={"name": name, "scopes": scopes, **extra},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _audit_rows(engine) -> list[AuditEvent]:
    """Audit trail rows (collector.audit_trail), every chain."""
    async with _admin(engine)() as s:
        return list(
            (
                await s.execute(
                    select(AuditEvent).order_by(AuditEvent.created_at, AuditEvent.seq)
                )
            ).scalars()
        )


async def _works(c, key: str) -> int:
    """Status of a tenant-key route (the registry) with this key."""
    return (await c.get("/api/v1/registry/assets", headers=H(key))).status_code


# ──────────────────────────────────────────────────────────────────────
# Backwards-compatible default and the scoped-only setting
# ──────────────────────────────────────────────────────────────────────


async def test_tenant_key_keeps_full_admin_access_by_default(client):
    for path in (
        "/api/v1/admin/tokens",
        "/api/v1/admin/api-keys",
        "/api/v1/admin/partner-credentials",
        "/api/v1/admin/provider-credentials",
        "/api/v1/admin/webhooks/subscriptions",
        "/api/v1/admin/token-scopes",
    ):
        resp = await client.get(path, headers=H(TENANT_KEY))
        assert resp.status_code == 200, (path, resp.text)
    # And may grant any scope.
    tok = await _token(client, list(ADMIN_SCOPES))
    assert set(tok["scopes"]) == set(ADMIN_SCOPES)


async def test_require_scoped_tokens_refuses_tenant_keys_on_admin_routes(
    client, seeded_db, monkeypatch
):
    tok = await _token(client, ["admin:provider-credentials"])
    monkeypatch.setattr(settings, "admin_require_scoped_tokens", True)

    resp = await client.post(
        "/api/v1/admin/provider-credentials/openai/rotate",
        headers=H(TENANT_KEY),
        json={"admin_key": "sk-admin-" + "x" * 30},
    )
    assert resp.status_code == 403
    assert "PS_ADMIN_REQUIRE_SCOPED_TOKENS" in resp.json()["detail"]["detail"]
    # Ingest and registry routes still take the key.
    assert await _works(client, TENANT_KEY) == 200
    # The scoped token gets in.
    resp = await client.post(
        "/api/v1/admin/provider-credentials/openai",
        headers=H(tok["token"]),
        json={"admin_key": "sk-admin-" + "x" * 30},
    )
    assert resp.status_code == 201, resp.text
    # The refusal is audited (auth.failure on the tenant's chain).
    rows = await _audit_rows(seeded_db)
    assert any(
        r.action == "auth.failure"
        and r.status_code == 403
        and r.details["reason"] == "insufficient_scope"
        and r.details["route"].endswith("/rotate")
        for r in rows
    )


# ──────────────────────────────────────────────────────────────────────
# Scopes
# ──────────────────────────────────────────────────────────────────────


async def test_scopes_allow_and_deny(client):
    webhooks = await _token(client, ["admin:webhooks"])
    reader = await _token(client, ["admin:read"])
    creds = await _token(client, ["admin:provider-credentials"])

    async def status(method, path, tok, **kw):
        return (
            await client.request(method, path, headers=H(tok["token"]), **kw)
        ).status_code

    assert await status("GET", "/api/v1/admin/webhooks/subscriptions", webhooks) == 200
    assert await status("GET", "/api/v1/admin/provider-credentials", webhooks) == 403
    assert await status("GET", "/api/v1/admin/tokens", webhooks) == 403
    assert await status("GET", "/api/v1/admin/api-keys", webhooks) == 403

    # admin:read reads everything and changes nothing.
    for path in (
        "/api/v1/admin/provider-credentials",
        "/api/v1/admin/webhooks/subscriptions",
        "/api/v1/admin/tokens",
        "/api/v1/admin/api-keys",
        "/api/v1/admin/partner-credentials",
    ):
        assert await status("GET", path, reader) == 200, path
    body = {"admin_key": "sk-admin-" + "y" * 30}
    assert (
        await status(
            "POST", "/api/v1/admin/provider-credentials/openai", reader, json=body
        )
        == 403
    )
    assert (
        await status("POST", "/api/v1/admin/api-keys", reader, json={"name": "x"})
        == 403
    )
    assert await status("POST", "/api/v1/admin/risk-suggestions/run", reader) == 403

    # The right scope writes.
    assert (
        await status(
            "POST", "/api/v1/admin/provider-credentials/openai", creds, json=body
        )
        == 201
    )
    assert (
        await status("DELETE", "/api/v1/admin/provider-credentials/openai", creds)
        == 204
    )
    # The scope check runs before body validation: no 422 leak of the route shape.
    assert await status("POST", "/api/v1/admin/api-keys", webhooks, json={}) == 403


async def test_admin_tokens_do_not_reach_tenant_key_routes(client):
    tok = await _token(client, list(ADMIN_SCOPES))
    assert await _works(client, tok["token"]) == 401
    resp = await client.post(
        "/ingest/events",
        headers=H(tok["token"]),
        json={"events": [{"vendor": "openai", "model": "gpt-4o"}]},
    )
    assert resp.status_code == 401


async def test_a_token_cannot_grant_scopes_it_lacks(client):
    minter = await _token(client, ["admin:tokens"])
    resp = await client.post(
        "/api/v1/admin/tokens",
        headers=H(minter["token"]),
        json={"name": "escalate", "scopes": ["admin:keys"]},
    )
    assert resp.status_code == 403
    assert "admin:keys" in resp.json()["detail"]["detail"]
    ok = await _token(client, ["admin:tokens"], key=minter["token"])
    assert ok["created_by"].startswith("token:")

    resp = await client.post(
        "/api/v1/admin/tokens",
        headers=H(TENANT_KEY),
        json={"name": "bad", "scopes": ["admin:everything"]},
    )
    assert resp.status_code == 400


async def test_admin_keys_token_cannot_mint_a_full_admin_ingest_key(
    client, seeded_db, monkeypatch
):
    """While PS_ADMIN_REQUIRE_SCOPED_TOKENS is off an ingest key is full
    admin, so an admin:keys token may not issue or rotate one."""
    keys = await _token(client, ["admin:keys"])
    existing = (
        await client.post(
            "/api/v1/admin/api-keys", headers=H(TENANT_KEY), json={"name": "sdk"}
        )
    ).json()

    resp = await client.post(
        "/api/v1/admin/api-keys", headers=H(keys["token"]), json={"name": "mine"}
    )
    assert resp.status_code == 403
    assert "every admin scope" in resp.json()["detail"]["detail"]
    resp = await client.post(
        f"/api/v1/admin/api-keys/{existing['id']}/rotate", headers=H(keys["token"])
    )
    assert resp.status_code == 403
    # The key was not rotated: the old secret still works.
    assert await _works(client, existing["key"]) == 200
    # Listing needs only admin:keys.
    assert (
        await client.get("/api/v1/admin/api-keys", headers=H(keys["token"]))
    ).status_code == 200
    rows = await _audit_rows(seeded_db)
    assert any(
        r.action == "auth.failure" and r.details["reason"] == "insufficient_scope"
        for r in rows
    )

    # With scoped tokens required, an ingest key has no admin access, so
    # admin:keys is enough.
    monkeypatch.setattr(settings, "admin_require_scoped_tokens", True)
    resp = await client.post(
        "/api/v1/admin/api-keys", headers=H(keys["token"]), json={"name": "mine"}
    )
    assert resp.status_code == 201, resp.text
    minted = resp.json()["key"]
    assert (
        await client.get("/api/v1/admin/api-keys", headers=H(minted))
    ).status_code == 403
    resp = await client.post(
        f"/api/v1/admin/api-keys/{existing['id']}/rotate", headers=H(keys["token"])
    )
    assert resp.status_code == 200, resp.text
    assert (
        await client.delete(
            f"/api/v1/admin/api-keys/{existing['id']}", headers=H(keys["token"])
        )
    ).status_code == 204


# ──────────────────────────────────────────────────────────────────────
# Admin tokens: storage, expiry, revoke
# ──────────────────────────────────────────────────────────────────────


async def test_admin_token_is_stored_hashed_and_shown_once(client, seeded_db):
    tok = await _token(client, ["admin:read"], name="terraform")
    plaintext = tok["token"]
    assert plaintext.startswith("ps-at-")
    assert tok["token_hint"] == plaintext[-4:]
    assert "warning" in tok

    async with _admin(seeded_db)() as s:
        row = (
            await s.execute(select(AdminToken).where(AdminToken.name == "terraform"))
        ).scalar_one()
    assert row.token_hash.startswith("$2b$")
    assert verify_api_key(plaintext, row.token_hash)
    assert row.token_prefix == api_key_lookup_prefix(plaintext)
    stored = json.dumps(
        {c.name: str(getattr(row, c.name)) for c in AdminToken.__table__.c}
    )
    assert plaintext not in stored

    for resp in (
        await client.get("/api/v1/admin/tokens", headers=H(TENANT_KEY)),
        await client.get(f"/api/v1/admin/tokens/{tok['id']}", headers=H(TENANT_KEY)),
    ):
        assert resp.status_code == 200
        assert plaintext not in resp.text
        assert "token_hash" not in resp.text and '"token"' not in resp.text


async def test_admin_token_name_conflict_is_409(client):
    await _token(client, ["admin:read"], name="ci")
    resp = await client.post(
        "/api/v1/admin/tokens",
        headers=H(TENANT_KEY),
        json={"name": "ci", "scopes": ["admin:read"]},
    )
    assert resp.status_code == 409


async def test_admin_token_expiry(client, seeded_db):
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    resp = await client.post(
        "/api/v1/admin/tokens",
        headers=H(TENANT_KEY),
        json={"name": "old", "scopes": ["admin:read"], "expires_at": past},
    )
    assert resp.status_code == 400

    soon = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    tok = await _token(client, ["admin:read"], expires_at=soon)
    assert (
        await client.get("/api/v1/admin/tokens", headers=H(tok["token"]))
    ).status_code == 200

    async with _admin(seeded_db)() as s:
        await s.execute(
            update(AdminToken)
            .where(AdminToken.id == uuid.UUID(tok["id"]))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await s.commit()
    resp = await client.get("/api/v1/admin/tokens", headers=H(tok["token"]))
    assert resp.status_code == 401
    assert resp.json()["detail"]["detail"] == "Admin token revoked or expired"
    got = await client.get(f"/api/v1/admin/tokens/{tok['id']}", headers=H(TENANT_KEY))
    assert got.json()["status"] == "expired"


async def test_admin_token_revoke_semantics(client):
    tok = await _token(client, ["admin:read"])
    path = f"/api/v1/admin/tokens/{tok['id']}"
    assert (await client.delete(path, headers=H(TENANT_KEY))).status_code == 204
    assert (await client.get(path, headers=H(tok["token"]))).status_code == 401
    got = await client.get(path, headers=H(TENANT_KEY))
    assert got.status_code == 200 and got.json()["status"] == "revoked"
    assert got.json()["revoked_at"]
    # Repeated DELETE is 204; an unknown id is 404.
    assert (await client.delete(path, headers=H(TENANT_KEY))).status_code == 204
    missing = f"/api/v1/admin/tokens/{uuid.uuid4()}"
    assert (await client.delete(missing, headers=H(TENANT_KEY))).status_code == 404
    # Listed with a status; filtered out by ?status=active.
    listed = (await client.get("/api/v1/admin/tokens", headers=H(TENANT_KEY))).json()[
        "data"
    ]
    assert [t["status"] for t in listed if t["id"] == tok["id"]] == ["revoked"]
    active = (
        await client.get("/api/v1/admin/tokens?status=active", headers=H(TENANT_KEY))
    ).json()["data"]
    assert tok["id"] not in {t["id"] for t in active}
    # The name is free again.
    await _token(client, ["admin:read"], name=tok["name"])


# ──────────────────────────────────────────────────────────────────────
# Tenant ingest keys
# ──────────────────────────────────────────────────────────────────────


async def test_legacy_key_is_adopted_and_keeps_working(client, seeded_db):
    resp = await client.get("/api/v1/admin/api-keys", headers=H(TENANT_KEY))
    assert resp.status_code == 200
    (legacy,) = resp.json()["data"]
    assert legacy["name"] == "legacy-ingest-key"
    assert legacy["origin"] == "legacy"
    assert legacy["key_hint"] is None
    async with _admin(seeded_db)() as s:
        tenant = (await s.execute(select(Tenant))).scalar_one()
    assert tenant.api_key_hash is None and tenant.api_key_prefix is None
    assert await _works(client, TENANT_KEY) == 200

    # Revoking it really revokes it: no fallback to the tenant row.
    path = f"/api/v1/admin/api-keys/{legacy['id']}"
    assert (await client.delete(path, headers=H(TENANT_KEY))).status_code == 204
    assert await _works(client, TENANT_KEY) == 401


async def test_api_key_lifecycle(client, seeded_db):
    resp = await client.post(
        "/api/v1/admin/api-keys", headers=H(TENANT_KEY), json={"name": "gateway-prod"}
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    key1 = created["key"]
    assert key1.startswith("ps-ik-") and created["key_hint"] == key1[-4:]
    assert await _works(client, key1) == 200
    resp = await client.post(
        "/ingest/events",
        headers=H(key1),
        json={"events": [{"vendor": "openai", "model": "gpt-4o"}]},
    )
    assert resp.status_code == 200, resp.text

    # Hashed only.
    async with _admin(seeded_db)() as s:
        row = (
            await s.execute(
                select(TenantApiKey).where(TenantApiKey.name == "gateway-prod")
            )
        ).scalar_one()
    assert row.key_hash.startswith("$2b$") and verify_api_key(key1, row.key_hash)
    assert key1 not in json.dumps(
        {c.name: str(getattr(row, c.name)) for c in TenantApiKey.__table__.c}
    )

    # Duplicate name.
    dup = await client.post(
        "/api/v1/admin/api-keys", headers=H(TENANT_KEY), json={"name": "gateway-prod"}
    )
    assert dup.status_code == 409

    # GET by id: metadata only.
    path = f"/api/v1/admin/api-keys/{created['id']}"
    got = await client.get(path, headers=H(TENANT_KEY))
    assert got.status_code == 200 and key1 not in got.text and '"key"' not in got.text

    # Rotate with a grace window: same id, both secrets work.
    resp = await client.post(
        path + "/rotate", headers=H(TENANT_KEY), json={"grace_period_seconds": 3600}
    )
    assert resp.status_code == 200, resp.text
    rotated = resp.json()
    key2 = rotated["key"]
    assert rotated["id"] == created["id"] and key2 != key1
    assert rotated["rotation_grace_ends_at"]
    assert await _works(client, key1) == 200
    assert await _works(client, key2) == 200

    # The window ends: only the new secret works.
    async with _admin(seeded_db)() as s:
        await s.execute(
            update(TenantApiKey)
            .where(TenantApiKey.id == uuid.UUID(created["id"]))
            .values(previous_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await s.commit()
    assert await _works(client, key1) == 401
    assert await _works(client, key2) == 200

    # Rotate without grace: the old secret stops at once.
    resp = await client.post(path + "/rotate", headers=H(TENANT_KEY))
    key3 = resp.json()["key"]
    assert resp.json()["rotation_grace_ends_at"] is None
    assert await _works(client, key2) == 401
    assert await _works(client, key3) == 200

    # Revoke; a revoked key cannot be rotated; DELETE is idempotent.
    assert (await client.delete(path, headers=H(TENANT_KEY))).status_code == 204
    assert await _works(client, key3) == 401
    assert (
        await client.post(path + "/rotate", headers=H(TENANT_KEY))
    ).status_code == 409
    assert (await client.delete(path, headers=H(TENANT_KEY))).status_code == 204
    assert (await client.get(path, headers=H(TENANT_KEY))).json()["status"] == "revoked"


async def test_revoked_key_during_grace_stops_both_secrets(client):
    created = (
        await client.post(
            "/api/v1/admin/api-keys", headers=H(TENANT_KEY), json={"name": "k"}
        )
    ).json()
    path = f"/api/v1/admin/api-keys/{created['id']}"
    new = (
        await client.post(
            path + "/rotate", headers=H(TENANT_KEY), json={"grace_period_seconds": 600}
        )
    ).json()["key"]
    await client.delete(path, headers=H(TENANT_KEY))
    assert await _works(client, created["key"]) == 401
    assert await _works(client, new) == 401


async def test_api_key_expiry(client, seeded_db):
    soon = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    created = (
        await client.post(
            "/api/v1/admin/api-keys",
            headers=H(TENANT_KEY),
            json={"name": "temp", "expires_at": soon},
        )
    ).json()
    assert await _works(client, created["key"]) == 200
    async with _admin(seeded_db)() as s:
        await s.execute(
            update(TenantApiKey)
            .where(TenantApiKey.id == uuid.UUID(created["id"]))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await s.commit()
    resp = await client.get("/api/v1/registry/assets", headers=H(created["key"]))
    assert resp.status_code == 401
    assert resp.json()["detail"]["detail"] == "API key revoked or expired"


# ──────────────────────────────────────────────────────────────────────
# Tenant isolation with RLS enforced
# ──────────────────────────────────────────────────────────────────────


async def test_tenant_isolation_under_rls(client, seeded_db):
    await _add_tenant_b(seeded_db)
    tok_a = await _token(client, ["admin:tokens", "admin:keys"])
    # Every scope, so the cross-tenant rotate below reaches the lookup.
    tok_b = await _token(client, list(ADMIN_SCOPES), key=KEY_B)
    key_a = (
        await client.post(
            "/api/v1/admin/api-keys", headers=H(TENANT_KEY), json={"name": "a"}
        )
    ).json()
    async with seeded_db.begin() as conn:
        await rls.enable(conn)

    # Under RLS, each tenant sees only its own tokens and keys.
    listed = (
        await client.get("/api/v1/admin/tokens", headers=H(tok_b["token"]))
    ).json()["data"]
    assert {t["id"] for t in listed} == {tok_b["id"]}
    for path in (
        f"/api/v1/admin/tokens/{tok_a['id']}",
        f"/api/v1/admin/api-keys/{key_a['id']}",
    ):
        assert (await client.get(path, headers=H(tok_b["token"]))).status_code == 404
        assert (await client.delete(path, headers=H(tok_b["token"]))).status_code == 404
    rotate = await client.post(
        f"/api/v1/admin/api-keys/{key_a['id']}/rotate", headers=H(tok_b["token"])
    )
    assert rotate.status_code == 404
    keys_b = (await client.get("/api/v1/admin/api-keys", headers=H(KEY_B))).json()[
        "data"
    ]
    assert key_a["id"] not in {k["id"] for k in keys_b}

    # Auth lookups still work across tenants under RLS.
    assert await _works(client, key_a["key"]) == 200
    assert (
        await client.get("/api/v1/admin/api-keys", headers=H(tok_a["token"]))
    ).status_code == 200
    # Operator routes see every tenant.
    tenants = (await client.get("/api/v1/operator/tenants", headers=OP)).json()["data"]
    assert len(tenants) == 2


# ──────────────────────────────────────────────────────────────────────
# Operator tenant routes
# ──────────────────────────────────────────────────────────────────────


async def test_tenant_routes_are_operator_only(client, monkeypatch):
    tok = await _token(client, list(ADMIN_SCOPES))
    for headers in (H(TENANT_KEY), H(tok["token"]), {}):
        assert (
            await client.get("/api/v1/operator/tenants", headers=headers)
        ).status_code == 401
        resp = await client.post(
            "/api/v1/operator/tenants", headers=headers, json={"name": "x"}
        )
        assert resp.status_code == 401
    monkeypatch.setattr(settings, "admin_token", "")
    assert (await client.get("/api/v1/operator/tenants", headers=OP)).status_code == 404


async def test_tenant_crud(client, seeded_db):
    resp = await client.post(
        "/api/v1/operator/tenants",
        headers=OP,
        json={
            "name": "Nordbank",
            "domain": "nordbank.example",
            "settings": {"retention_days": 90},
        },
    )
    assert resp.status_code == 201, resp.text
    t = resp.json()
    tid = t["id"]
    assert t["status"] == "active" and t["settings"] == {"retention_days": 90}
    assert t["links"]["erase"] == f"/api/v1/operator/tenants/{tid}?confirm={tid}"
    key = t["api_key"]["key"]
    assert await _works(client, key) == 200

    dup = await client.post(
        "/api/v1/operator/tenants", headers=OP, json={"name": "nordbank"}
    )
    assert dup.status_code == 409

    bad = await client.post(
        "/api/v1/operator/tenants",
        headers=OP,
        json={"name": "Z", "settings": {"api_key": "x"}},
    )
    assert bad.status_code == 400
    bad = await client.patch(
        f"/api/v1/operator/tenants/{tid}",
        headers=OP,
        json={"settings": {"retention_days": 0}},
    )
    assert bad.status_code == 400

    resp = await client.patch(
        f"/api/v1/operator/tenants/{tid}",
        headers=OP,
        json={
            "name": "Nordbank AB",
            "settings": {"retention_days": None, "region": "eu"},
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Nordbank AB"
    assert resp.json()["settings"] == {"region": "eu"}
    assert (await client.get(f"/api/v1/operator/tenants/{tid}", headers=OP)).json()[
        "name"
    ] == "Nordbank AB"
    assert (
        await client.get(f"/api/v1/operator/tenants/{uuid.uuid4()}", headers=OP)
    ).status_code == 404
    names = {
        x["name"]
        for x in (await client.get("/api/v1/operator/tenants", headers=OP)).json()[
            "data"
        ]
    }
    assert names == {"Test Corp", "Nordbank AB"}

    # Bootstrap a scoped admin token; it manages the new tenant only.
    resp = await client.post(
        f"/api/v1/operator/tenants/{tid}/admin-tokens",
        headers=OP,
        json={"name": "terraform", "scopes": ["admin:keys", "admin:read"]},
    )
    assert resp.status_code == 201, resp.text
    boot = resp.json()
    assert boot["created_by"] == "operator"
    keys = await client.get("/api/v1/admin/api-keys", headers=H(boot["token"]))
    assert [k["name"] for k in keys.json()["data"]] == ["default"]


async def test_disabled_tenant_is_refused_everywhere(client):
    tok = await _token(client, ["admin:read"])
    partner = (
        await client.post(
            "/api/v1/admin/partner-credentials",
            headers=H(TENANT_KEY),
            json={"partner_name": "ardoq"},
        )
    ).json()
    assert (
        await client.get("/partner/v1/assets", headers=H(partner["api_key"]))
    ).status_code == 200
    tid = (await client.get("/api/v1/operator/tenants", headers=OP)).json()["data"][0][
        "id"
    ]

    resp = await client.patch(
        f"/api/v1/operator/tenants/{tid}", headers=OP, json={"disabled": True}
    )
    assert resp.json()["status"] == "disabled" and resp.json()["disabled_at"]
    assert await _works(client, TENANT_KEY) == 403
    assert (
        await client.get("/api/v1/admin/tokens", headers=H(tok["token"]))
    ).status_code == 403
    assert (
        await client.get("/partner/v1/assets", headers=H(partner["api_key"]))
    ).status_code == 403
    # No new OAuth access tokens either.
    oauth_form = {
        "grant_type": "client_credentials",
        "client_id": partner["client_id"],
        "client_secret": partner["client_secret"],
    }
    resp = await client.post("/oauth/token", data=oauth_form)
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "invalid_client"
    disabled = (
        await client.get("/api/v1/operator/tenants?status=disabled", headers=OP)
    ).json()["data"]
    assert [x["id"] for x in disabled] == [tid]

    await client.patch(
        f"/api/v1/operator/tenants/{tid}", headers=OP, json={"disabled": False}
    )
    assert await _works(client, TENANT_KEY) == 200
    assert (
        await client.get("/api/v1/admin/tokens", headers=H(tok["token"]))
    ).status_code == 200
    assert (await client.post("/oauth/token", data=oauth_form)).status_code == 200


# ──────────────────────────────────────────────────────────────────────
# Audit
# ──────────────────────────────────────────────────────────────────────


async def test_admin_changes_are_audited_without_secrets(client, seeded_db):
    # Issuing and rotating ingest keys needs every scope while
    # PS_ADMIN_REQUIRE_SCOPED_TOKENS is off.
    tok = await _token(client, list(ADMIN_SCOPES))
    created = (
        await client.post(
            "/api/v1/admin/api-keys", headers=H(tok["token"]), json={"name": "k"}
        )
    ).json()
    rotated = (
        await client.post(
            f"/api/v1/admin/api-keys/{created['id']}/rotate", headers=H(tok["token"])
        )
    ).json()
    await client.delete(
        f"/api/v1/admin/api-keys/{created['id']}", headers=H(tok["token"])
    )
    await client.delete(f"/api/v1/admin/tokens/{tok['id']}", headers=H(TENANT_KEY))
    await client.post("/api/v1/operator/tenants", headers=OP, json={"name": "New Co"})
    # A denied call.
    await client.get("/api/v1/admin/webhooks/subscriptions", headers=H(created["key"]))

    rows = await _audit_rows(seeded_db)
    actions = [r.action for r in rows]
    for action in (
        "admin_token.create",
        "api_key.create",
        "api_key.rotate",
        "api_key.revoke",
        "admin_token.revoke",
        "tenant.create",
    ):
        assert action in actions, (action, actions)
    by_action = {r.action: r for r in rows}
    # The token's calls are attributed to its lookup prefix.
    create = by_action["api_key.create"]
    assert (create.actor_type, create.actor_id) == (
        "admin_token",
        api_key_lookup_prefix(tok["token"]),
    )
    assert (create.target_type, create.target_id) == ("tenant_api_key", created["id"])
    revoke = by_action["admin_token.revoke"]
    assert (revoke.actor_type, revoke.actor_id) == (
        "tenant_key",
        api_key_lookup_prefix(TENANT_KEY),
    )
    assert (by_action["tenant.create"].actor_type, by_action["tenant.create"].actor_id) == (
        "operator",
        "operator-api",
    )
    # The chains still verify.
    async with _admin(seeded_db)() as s:
        assert all(r.ok for r in await audit_trail.verify_all(s, None))
    dump = json.dumps(
        [(r.actor_id, r.target_id, r.details) for r in rows], default=str
    )
    for secret in (tok["token"], created["key"], rotated["key"], OP_TOKEN):
        assert secret not in dump


# ──────────────────────────────────────────────────────────────────────
# Partner credentials: GET by id, status, DELETE semantics
# ──────────────────────────────────────────────────────────────────────


async def test_partner_credential_consistency(client, seeded_db):
    await _add_tenant_b(seeded_db)
    created = (
        await client.post(
            "/api/v1/admin/partner-credentials",
            headers=H(TENANT_KEY),
            json={"partner_name": "leanix"},
        )
    ).json()
    path = f"/api/v1/admin/partner-credentials/{created['id']}"
    got = await client.get(path, headers=H(TENANT_KEY))
    assert got.status_code == 200
    assert (
        got.json()["status"] == "active"
        and got.json()["client_id"] == created["client_id"]
    )
    assert (
        created["client_secret"] not in got.text and created["api_key"] not in got.text
    )
    assert (await client.get(path, headers=H(KEY_B))).status_code == 404

    assert (await client.delete(path, headers=H(TENANT_KEY))).status_code == 204
    assert (await client.delete(path, headers=H(TENANT_KEY))).status_code == 204
    got = (await client.get(path, headers=H(TENANT_KEY))).json()
    assert got["status"] == "revoked" and got["is_active"] == "false"
    listed = (
        await client.get("/api/v1/admin/partner-credentials", headers=H(TENANT_KEY))
    ).json()["data"]
    assert [c["status"] for c in listed] == ["revoked"]
    active = (
        await client.get(
            "/api/v1/admin/partner-credentials?status=active", headers=H(TENANT_KEY)
        )
    ).json()["data"]
    assert active == []

    partners = await _token(client, ["admin:partners"])
    webhooks = await _token(client, ["admin:webhooks"])
    assert (await client.get(path, headers=H(partners["token"]))).status_code == 200
    assert (await client.get(path, headers=H(webhooks["token"]))).status_code == 403


# ──────────────────────────────────────────────────────────────────────
# Migration 022
# ──────────────────────────────────────────────────────────────────────


def _load_migration():
    pytest.importorskip("alembic")
    path = (
        Path(__file__).resolve().parents[2]
        / "db"
        / "alembic"
        / "versions"
        / "022_admin_tokens_keys.py"
    )
    spec = importlib.util.spec_from_file_location("migration_022", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_revision_chain():
    m = _load_migration()
    assert (m.revision, m.down_revision) == ("022", "021")
    assert set(m.TABLES) == {TenantApiKey.__tablename__, AdminToken.__tablename__}


async def test_migration_moves_legacy_keys_and_downgrades(client, seeded_db):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    m = _load_migration()

    def run(sync_conn, step):
        ctx = MigrationContext.configure(sync_conn)
        with Operations.context(ctx):
            step()

    issued = (
        await client.post(
            "/api/v1/admin/api-keys", headers=H(TENANT_KEY), json={"name": "n"}
        )
    ).json()
    # The tenant's legacy key is now in the table; downgrade puts it back.
    async with seeded_db.begin() as conn:
        await conn.run_sync(run, m.downgrade)
    async with _admin(seeded_db)() as s:
        tenant = (await s.execute(select(Tenant.api_key_prefix))).scalar_one()
    assert tenant == api_key_lookup_prefix(TENANT_KEY)

    async with seeded_db.begin() as conn:
        await conn.run_sync(run, m.upgrade)
    async with _admin(seeded_db)() as s:
        tenant = (await s.execute(select(Tenant))).scalar_one()
        keys = (await s.execute(select(TenantApiKey))).scalars().all()
    assert tenant.api_key_hash is None
    assert [(k.name, k.origin) for k in keys] == [("legacy-ingest-key", "legacy")]
    assert await _works(client, TENANT_KEY) == 200
    # Keys other than the one restored do not survive a downgrade.
    assert await _works(client, issued["key"]) == 401
