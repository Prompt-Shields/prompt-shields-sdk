"""Scoped ingest keys and named operator tokens (#341), against Postgres.

Ingest keys carry ``ingest:write``, ``registry:read`` and
``registry:attest`` (collector/tenant_keys.py): keys from before scopes
existed keep all three, a new key gets ``ingest:write`` only. Operator
routes take a named operator token (``ps-ot-...``, collector/operator_auth.py)
or PS_ADMIN_TOKEN as break-glass, and every action names the caller in
the audit trail.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from collector import audit_trail, rls
from collector.app import app
from collector.config import settings
from db.audit_chain import SYSTEM_TENANT_ID
from db.models import AuditEvent, OperatorToken, Tenant, TenantApiKey
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TENANT_KEY = "ps-test-key-12345"  # the seeded_db tenant's legacy key
OP_TOKEN = "op-secret-token"
ALL_KEY_SCOPES = ["ingest:write", "registry:read", "registry:attest"]
EVENT = {"events": [{"vendor": "openai", "model": "gpt-4o"}]}
ATTEST = {"field": "owner", "state": "attested", "attested_by": "cio@example.com"}


def H(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


OP = H(OP_TOKEN)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", OP_TOKEN)
    # provider_usage_admin reads the environment.
    monkeypatch.setenv("PS_ADMIN_TOKEN", OP_TOKEN)
    monkeypatch.setattr(settings, "admin_require_scoped_tokens", False)
    audit_trail.auth_failure_limiter.reset()


@pytest.fixture
async def client(seeded_db):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


def _admin(engine):
    return async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, info=rls.bypass_info("test")
    )


async def _rows(engine, tenant_id=None) -> list[AuditEvent]:
    async with _admin(engine)() as s:
        stmt = select(AuditEvent).order_by(AuditEvent.tenant_id, AuditEvent.seq)
        if tenant_id is not None:
            stmt = stmt.where(AuditEvent.tenant_id == tenant_id)
        return list((await s.execute(stmt)).scalars())


async def _tenant_id(engine) -> uuid.UUID:
    async with _admin(engine)() as s:
        return (await s.execute(select(Tenant.id))).scalar_one()


async def _issue_key(c, name: str, scopes: list[str] | None = None) -> dict:
    body = {"name": name} if scopes is None else {"name": name, "scopes": scopes}
    resp = await c.post("/api/v1/admin/api-keys", headers=H(TENANT_KEY), json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _registry(c, key: str) -> int:
    return (await c.get("/api/v1/registry/assets", headers=H(key))).status_code


async def _ingest(c, key: str) -> int:
    return (await c.post("/ingest/events", headers=H(key), json=EVENT)).status_code


async def _attest(c, key: str) -> int:
    return (
        await c.post(
            f"/api/v1/registry/assets/{uuid.uuid4()}/attestations",
            headers=H(key),
            json=ATTEST,
        )
    ).status_code


async def _op_token(c, scopes: list[str], name: str | None = None, headers=OP) -> dict:
    resp = await c.post(
        "/api/v1/operator/tokens",
        headers=headers,
        json={"name": name or f"op-{uuid.uuid4().hex[:8]}", "scopes": scopes},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ──────────────────────────────────────────────────────────────────────
# Ingest key scopes
# ──────────────────────────────────────────────────────────────────────


async def test_new_key_is_ingest_only_and_old_key_keeps_every_scope(client, seeded_db):
    new = await _issue_key(client, "gateway")
    assert new["scopes"] == ["ingest:write"]
    assert await _ingest(client, new["key"]) == 200
    resp = await client.get("/api/v1/registry/assets", headers=H(new["key"]))
    assert resp.status_code == 403
    assert resp.json()["detail"]["detail"] == "API key lacks required scope: registry:read"
    assert await _attest(client, new["key"]) == 403
    assert (await client.get("/telemetry/loss", headers=H(new["key"]))).status_code == 403

    # The tenant's key from before scopes (moved into the table by the
    # issue above) still does everything.
    async with _admin(seeded_db)() as s:
        legacy = (
            await s.execute(select(TenantApiKey).where(TenantApiKey.origin == "legacy"))
        ).scalar_one()
    # Every scope, including policy:read (#340).
    assert sorted(legacy.scopes) == sorted([*ALL_KEY_SCOPES, "policy:read"])
    assert await _registry(client, TENANT_KEY) == 200
    assert await _ingest(client, TENANT_KEY) == 200
    assert await _attest(client, TENANT_KEY) == 404  # past the scope check

    # The refusals are audited on the tenant's chain, by key prefix.
    rows = [
        r
        for r in await _rows(seeded_db, await _tenant_id(seeded_db))
        if r.action == "auth.failure"
    ]
    assert rows and all(r.details["reason"] == "insufficient_scope" for r in rows)
    assert {r.details["realm"] for r in rows} == {"ingest_key"}
    assert rows[0].actor_type == "tenant_key"


async def test_key_on_the_tenant_row_keeps_every_scope(client):
    # Never moved into tenant_api_keys: the fallback holds every scope.
    assert await _registry(client, TENANT_KEY) == 200
    assert await _attest(client, TENANT_KEY) == 404


async def test_scopes_are_enforced_per_route(client):
    reader = await _issue_key(client, "dashboard", ["registry:read"])
    assert await _registry(client, reader["key"]) == 200
    assert await _ingest(client, reader["key"]) == 403
    assert await _attest(client, reader["key"]) == 403
    attester = await _issue_key(client, "grc", ["registry:read", "registry:attest"])
    assert attester["scopes"] == ["registry:read", "registry:attest"]
    assert await _attest(client, attester["key"]) == 404
    # Rotation keeps the scopes.
    rotated = (
        await client.post(
            f"/api/v1/admin/api-keys/{reader['id']}/rotate", headers=H(TENANT_KEY)
        )
    ).json()
    assert rotated["scopes"] == ["registry:read"]
    assert await _registry(client, rotated["key"]) == 200
    assert await _ingest(client, rotated["key"]) == 403


async def test_unknown_or_empty_scopes_are_refused(client):
    for scopes in (["registry:write"], []):
        resp = await client.post(
            "/api/v1/admin/api-keys",
            headers=H(TENANT_KEY),
            json={"name": "x", "scopes": scopes},
        )
        assert resp.status_code in (400, 422), resp.text
    resp = await client.post(
        "/api/v1/operator/tenants",
        headers=OP,
        json={"name": "Bad", "api_key_scopes": ["admin:keys"]},
    )
    assert resp.status_code == 400


async def test_narrowed_key_is_not_an_admin(client, seeded_db):
    new = await _issue_key(client, "sdk")
    resp = await client.get("/api/v1/admin/api-keys", headers=H(new["key"]))
    assert resp.status_code == 403
    full = await _issue_key(client, "full", ALL_KEY_SCOPES)
    assert (
        await client.get("/api/v1/admin/api-keys", headers=H(full["key"]))
    ).status_code == 200
    rows = await _rows(seeded_db, await _tenant_id(seeded_db))
    assert any(
        r.action == "auth.failure" and r.details["realm"] == "tenant_admin" for r in rows
    )


async def test_admin_keys_token_may_issue_and_rotate_narrowed_keys(client):
    tok = (
        await client.post(
            "/api/v1/admin/tokens",
            headers=H(TENANT_KEY),
            json={"name": "keys-only", "scopes": ["admin:keys"]},
        )
    ).json()["token"]
    resp = await client.post(
        "/api/v1/admin/api-keys", headers=H(tok), json={"name": "gw"}
    )
    assert resp.status_code == 201, resp.text
    narrowed = resp.json()
    resp = await client.post(
        f"/api/v1/admin/api-keys/{narrowed['id']}/rotate", headers=H(tok)
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        "/api/v1/admin/api-keys",
        headers=H(tok),
        json={"name": "full", "scopes": ALL_KEY_SCOPES},
    )
    assert resp.status_code == 403


async def test_operator_tenant_key_defaults_to_ingest_only(client):
    resp = await client.post("/api/v1/operator/tenants", headers=OP, json={"name": "N"})
    assert resp.status_code == 201, resp.text
    key = resp.json()["api_key"]
    assert key["scopes"] == ["ingest:write"]
    assert await _registry(client, key["key"]) == 403
    assert await _ingest(client, key["key"]) == 200


async def test_new_rows_default_to_ingest_only_in_the_database(seeded_db):
    tenant_id = await _tenant_id(seeded_db)
    async with _admin(seeded_db)() as s:
        await s.execute(
            text(
                "INSERT INTO tenant_api_keys (id, tenant_id, name, key_prefix, key_hash,"
                " origin, created_at) VALUES (gen_random_uuid(), :t, 'raw', 'p', 'h',"
                " 'api', now())"
            ),
            {"t": tenant_id},
        )
        await s.commit()
        scopes = (
            await s.execute(text("SELECT scopes FROM tenant_api_keys WHERE name = 'raw'"))
        ).scalar_one()
    assert scopes == ["ingest:write"]


# ──────────────────────────────────────────────────────────────────────
# Named operator tokens
# ──────────────────────────────────────────────────────────────────────


async def test_operator_actions_are_attributed_to_the_named_token(client, seeded_db):
    tok = await _op_token(
        client, ["operator:tenants", "operator:retention"], name="ci-provisioner"
    )
    assert tok["token"].startswith("ps-ot-") and tok["created_by"] == "operator"
    assert "token" not in (
        await client.get(f"/api/v1/operator/tokens/{tok['id']}", headers=OP)
    ).json()

    resp = await client.post(
        "/api/v1/operator/tenants", headers=H(tok["token"]), json={"name": "Nordbank"}
    )
    assert resp.status_code == 201, resp.text
    tid = uuid.UUID(resp.json()["id"])
    resp = await client.put(
        f"/api/v1/operator/tenants/{tid}/retention",
        headers=H(tok["token"]),
        json={"retention_days": 30},
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/v1/operator/tenants/{tid}/admin-tokens",
        headers=H(tok["token"]),
        json={"name": "tf", "scopes": ["admin:read"]},
    )
    assert resp.status_code == 201 and resp.json()["created_by"] == (
        "operator_token:ci-provisioner"
    )

    tenant_rows = await _rows(seeded_db, tid)
    acted = {r.action: (r.actor_type, r.actor_id) for r in tenant_rows}
    for action in ("tenant.create", "admin_token.create"):
        assert acted[action] == ("operator_token", "ci-provisioner"), action
    assert any(
        r.actor_type == "operator_token" and r.actor_id == "ci-provisioner"
        for r in tenant_rows
        if r.action.startswith("tenant.retention") or r.action.startswith("retention")
    )

    system = await _rows(seeded_db, SYSTEM_TENANT_ID)
    issued = [r for r in system if r.action == "operator_token.issue"]
    assert [(r.actor_type, r.target_id, r.details["name"]) for r in issued] == [
        ("operator", tok["id"], "ci-provisioner")
    ]
    assert issued[0].details["scopes"] == ["operator:tenants", "operator:retention"]
    access = [r for r in system if r.action == "operator.access"]
    assert access and {(r.actor_type, r.actor_id) for r in access} == {
        ("operator_token", "ci-provisioner")
    }
    # The break-glass token's every use stands out.
    glass = [r for r in system if r.action == "operator.break_glass"]
    assert glass and all(r.actor_type == "operator" for r in glass)
    assert {r.details["route"] for r in glass} >= {"/api/v1/operator/tokens"}
    # No secret anywhere in the trail.
    dump = repr([(r.details, r.actor_id) for r in tenant_rows + system])
    assert tok["token"] not in dump and OP_TOKEN not in dump


async def test_named_token_scopes_are_enforced(client, seeded_db):
    reader = await _op_token(client, ["operator:read"])
    assert (
        await client.get("/api/v1/operator/tenants", headers=H(reader["token"]))
    ).status_code == 200
    resp = await client.post(
        "/api/v1/operator/tenants", headers=H(reader["token"]), json={"name": "X"}
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["detail"] == "Token lacks required scope: operator:tenants"
    tenant_id = await _tenant_id(seeded_db)
    resp = await client.delete(
        f"/api/v1/operator/tenants/{tenant_id}",
        headers=H(reader["token"]),
        params={"confirm": str(tenant_id)},
    )
    assert resp.status_code == 403
    # Operator-only admin routes take operator:integrations.
    resp = await client.post(
        "/api/v1/admin/provider-usage/pull",
        headers=H(reader["token"]),
        json={"tenant_id": str(tenant_id)},
    )
    assert resp.status_code == 403
    integ = await _op_token(client, ["operator:integrations"])
    resp = await client.post(
        "/api/v1/admin/provider-usage/pull",
        headers=H(integ["token"]),
        json={"tenant_id": str(tenant_id)},
    )
    assert resp.status_code == 200, resp.text
    # Tenant credentials are never operators.
    assert (
        await client.get("/api/v1/operator/tenants", headers=H(TENANT_KEY))
    ).status_code == 401
    failures = [
        r
        for r in await _rows(seeded_db, SYSTEM_TENANT_ID)
        if r.action == "auth.failure" and r.details["reason"] == "insufficient_scope"
    ]
    assert failures and {r.details["realm"] for r in failures} == {"operator"}


async def test_a_token_grants_only_scopes_it_holds(client):
    minter = await _op_token(client, ["operator:tokens", "operator:read"])
    resp = await client.post(
        "/api/v1/operator/tokens",
        headers=H(minter["token"]),
        json={"name": "escalate", "scopes": ["operator:erase"]},
    )
    assert resp.status_code == 403
    child = await _op_token(client, ["operator:read"], headers=H(minter["token"]))
    assert child["created_by"] == f"operator_token:{minter['name']}"
    # Rotating hands out the token's scopes, so the same rule applies.
    eraser = await _op_token(client, ["operator:erase"])
    resp = await client.post(
        f"/api/v1/operator/tokens/{eraser['id']}/rotate", headers=H(minter["token"])
    )
    assert resp.status_code == 403
    resp = await client.post(
        "/api/v1/operator/tokens",
        headers=OP,
        json={"name": "bad", "scopes": ["admin:keys"]},
    )
    assert resp.status_code == 400


async def test_rotate_revoke_expire_and_name_reuse(client, seeded_db):
    tok = await _op_token(client, ["operator:read"], name="rotating")
    path = f"/api/v1/operator/tokens/{tok['id']}"
    resp = await client.post(path + "/rotate", headers=OP)
    assert resp.status_code == 200, resp.text
    new = resp.json()
    assert new["id"] == tok["id"] and new["rotated_at"] and new["token"] != tok["token"]
    assert (
        await client.get("/api/v1/operator/tenants", headers=H(tok["token"]))
    ).status_code == 401
    assert (
        await client.get("/api/v1/operator/tenants", headers=H(new["token"]))
    ).status_code == 200

    assert (await client.delete(path, headers=OP)).status_code == 204
    assert (await client.delete(path, headers=OP)).status_code == 204
    assert (
        await client.get("/api/v1/operator/tenants", headers=H(new["token"]))
    ).status_code == 401
    assert (await client.get(path, headers=OP)).json()["status"] == "revoked"
    assert (await client.post(path + "/rotate", headers=OP)).status_code == 409
    # A revoked name is never reused, so audit rows name one token for good.
    resp = await client.post(
        "/api/v1/operator/tokens",
        headers=OP,
        json={"name": "rotating", "scopes": ["operator:read"]},
    )
    assert resp.status_code == 409

    short = await _op_token(client, ["operator:read"])
    async with _admin(seeded_db)() as s:
        await s.execute(
            update(OperatorToken)
            .where(OperatorToken.id == uuid.UUID(short["id"]))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await s.commit()
    assert (
        await client.get("/api/v1/operator/tenants", headers=H(short["token"]))
    ).status_code == 401

    listed = (await client.get("/api/v1/operator/tokens", headers=OP)).json()["data"]
    assert {t["status"] for t in listed} == {"revoked", "expired"}
    active = (
        await client.get("/api/v1/operator/tokens?status=active", headers=OP)
    ).json()["data"]
    assert active == []

    system = await _rows(seeded_db, SYSTEM_TENANT_ID)
    actions = [r.action for r in system if r.action.startswith("operator_token.")]
    assert actions.count("operator_token.rotate") == 1
    assert actions.count("operator_token.revoke") == 1

    async with _admin(seeded_db)() as s:
        row = (
            await s.execute(select(OperatorToken).where(OperatorToken.name == "rotating"))
        ).scalar_one()
    assert row.token_hash.startswith("$2b$") and new["token"] not in repr(
        {c.name: getattr(row, c.name) for c in OperatorToken.__table__.c}
    )


async def test_scopes_and_operator_tokens_under_rls(client, seeded_db):
    new = await _issue_key(client, "gw")
    op = await _op_token(client, ["operator:tenants"])
    async with seeded_db.begin() as conn:
        await rls.enable(conn)
    assert await _registry(client, new["key"]) == 403
    assert await _ingest(client, new["key"]) == 200
    assert await _registry(client, TENANT_KEY) == 200
    tenants = (
        await client.get("/api/v1/operator/tenants", headers=H(op["token"]))
    ).json()["data"]
    assert len(tenants) == 1


async def test_named_tokens_work_without_the_break_glass_token(client, monkeypatch):
    tok = await _op_token(client, ["operator:read"])
    monkeypatch.setattr(settings, "admin_token", "")
    monkeypatch.delenv("PS_ADMIN_TOKEN")
    assert (
        await client.get("/api/v1/operator/tenants", headers=H(tok["token"]))
    ).status_code == 200
    # No operator token: the surface is absent, as before.
    assert (await client.get("/api/v1/operator/tenants", headers=OP)).status_code == 404
    assert (await client.get("/api/v1/operator/tenants")).status_code == 404
    assert (
        await client.get("/api/v1/operator/tenants", headers=H("ps-ot-unknown"))
    ).status_code == 401
