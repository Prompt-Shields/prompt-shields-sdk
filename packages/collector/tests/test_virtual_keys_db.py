"""Gateway virtual keys and credential pools (#343) against a real
PostgreSQL database (PS_TEST_DATABASE_URL).

Covers: the admin routes (the key shown once, hashed at rest, never in a
list, read, audit row or log line), scopes (admin tokens only; resolution
needs an ingest key holding virtual-keys:resolve, never granted
implicitly), resolution (binding, the project's current team, the pool's
active credentials decrypted in order, a disabled or expired credential
reported, revoked / expired keys and disabled principals inactive), the
decrypted-credential cache (one audit row per decrypt, rotation picked up
at once), principal deletion revoking its keys, team deletion refused while
a key is bound, tenant isolation with row-level security enforced and the
migration's scope backfill.
"""

from __future__ import annotations

import importlib.util
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from collector import audit_trail, kms, rls, tenant_keys
from collector import virtual_keys as vk
from collector.admin_auth import ADMIN_SCOPES, TENANT_KEY_SCOPES
from collector.app import app
from collector.config import settings
from collector.rbac import ROLES
from db.api_keys import api_key_lookup_prefix, hash_api_key
from db.models import AuditEvent, Tenant, VirtualKey
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.scoped_admin_default

OP_TOKEN = "op-secret-token"
KEY_B = "ps-test-key-tenant-b"
K1 = "1" * 40
OPENAI_1 = "sk-proj-POOL-secret-0001"
OPENAI_2 = "sk-proj-POOL-secret-0002"
ANTHROPIC_1 = "sk-ant-POOL-secret-0003"
ROTATED = "sk-proj-ROTATED-secret-0004"
A = "/api/v1/admin"
BASE = A + "/virtual-keys"
RESOLVE = "/api/v1/virtual-keys/resolve"


def H(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", OP_TOKEN)
    monkeypatch.setattr(settings, "credential_kek_provider", "local")
    monkeypatch.setattr(settings, "credential_kek_keys", f"k1:{K1}")
    monkeypatch.setattr(settings, "virtual_key_credential_cache_seconds", 300)
    audit_trail.auth_failure_limiter.reset()
    kms.reset()
    vk.credential_cache().clear()
    yield
    vk.credential_cache().clear()
    kms.reset()


def _admin(engine):
    return async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, info=rls.bypass_info("test")
    )


async def _tenant_id(engine, name="Test Corp") -> uuid.UUID:
    async with _admin(engine)() as s:
        return (await s.execute(select(Tenant.id).where(Tenant.name == name))).scalar_one()


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


async def _token(c, tenant_id, scopes=None) -> str:
    resp = await c.post(
        f"/api/v1/operator/tenants/{tenant_id}/admin-tokens",
        headers=H(OP_TOKEN),
        json={"name": f"t-{uuid.uuid4().hex[:6]}", "scopes": scopes or list(ADMIN_SCOPES)},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


async def _post(c, tok, path, body, status=201) -> dict:
    resp = await c.post(path, headers=H(tok), json=body)
    assert resp.status_code == status, resp.text
    return resp.json()


async def _cred(c, tok, provider, label, key) -> str:
    out = await _post(
        c, tok, A + "/inference-credentials", {"provider": provider, "label": label, "api_key": key}
    )
    return out["id"]


async def _resolver_key(c, tok, scopes=("virtual-keys:resolve",)) -> str:
    out = await _post(c, tok, A + "/api-keys", {"name": f"gw-{uuid.uuid4().hex[:4]}", "scopes": list(scopes)})
    return out["key"]


async def _resolve(c, ingest_key, key, status=200) -> dict:
    resp = await c.post(RESOLVE, headers=H(ingest_key), json={"key": key})
    assert resp.status_code == status, resp.text
    if status == 200:
        assert resp.headers["cache-control"] == "no-store"
    return resp.json()


async def _uses(f) -> int:
    async with f() as s:
        return (
            await s.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "inference_credential.use")
            )
        ).scalar_one()


async def _all_text(f) -> str:
    async with f() as s:
        parts = [
            *(await s.execute(text("SELECT row_to_json(a)::text FROM audit_events a"))).scalars(),
            *(
                await s.execute(text("SELECT row_to_json(a)::text FROM partner_audit_log a"))
            ).scalars(),
            *(await s.execute(text("SELECT row_to_json(v)::text FROM virtual_keys v"))).scalars(),
        ]
        return "\n".join(parts)


@pytest.fixture
async def env(seeded_db):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        tid = await _tenant_id(seeded_db)
        tok = await _token(c, tid)
        team = await _post(c, tok, A + "/teams", {"name": "payments"})
        project = await _post(c, tok, A + "/projects", {"team_id": team["id"], "name": "checkout"})
        svc = await _post(c, tok, A + "/principals", {"kind": "service_account", "display_name": "svc"})
        creds = {
            "o1": await _cred(c, tok, "openai", "one", OPENAI_1),
            "o2": await _cred(c, tok, "openai", "two", OPENAI_2),
            "a1": await _cred(c, tok, "anthropic", "one", ANTHROPIC_1),
        }
        yield {
            "c": c,
            "engine": seeded_db,
            "tid": tid,
            "tok": tok,
            "f": _admin(seeded_db),
            "team": team,
            "project": project,
            "svc": svc,
            "creds": creds,
            "resolver": await _resolver_key(c, tok),
        }


async def _issue(env, status=201, **body) -> dict:
    payload = {
        "name": "checkout-svc",
        "credential_ids": [env["creds"]["o1"], env["creds"]["o2"], env["creds"]["a1"]],
        **body,
    }
    return await _post(env["c"], env["tok"], BASE, payload, status=status)


# ── admin routes ──────────────────────────────────────────────────────


async def test_issue_shows_the_key_once_and_stores_only_its_hash(env, caplog):
    caplog.set_level(logging.DEBUG)
    c, tok, f = env["c"], env["tok"], env["f"]
    out = await _issue(
        env, project_id=env["project"]["id"], principal_id=env["svc"]["id"], allowed_models=["gpt-4o*"]
    )
    key = out["key"]
    assert key.startswith(vk.KEY_PREFIX) and len(key) > 40
    assert out["key_hint"] == key[-4:]
    # A project carries its team.
    assert out["team_id"] == env["team"]["id"]
    assert out["project_id"] == env["project"]["id"]
    assert [p["id"] for p in out["credentials"]] == [
        env["creds"]["o1"],
        env["creds"]["o2"],
        env["creds"]["a1"],
    ]
    assert out["status"] == "active" and out["strategy"] == "round_robin"
    one = (await c.get(f"{BASE}/{out['id']}", headers=H(tok))).json()
    listed = (await c.get(BASE, headers=H(tok))).json()["data"]
    assert "key" not in one and "key" not in listed[0]
    assert one == {k: v for k, v in out.items() if k not in ("key", "warning")}
    async with f() as s:
        row = (await s.execute(select(VirtualKey))).scalar_one()
    assert row.key_hash == vk.hash_key(key) and key not in row.key_hash
    stored = await _all_text(f)
    for secret in (key, OPENAI_1, OPENAI_2, ANTHROPIC_1):
        assert secret not in stored
        assert secret not in caplog.text
    async with f() as s:
        actions = [
            r.action
            for r in (await s.execute(select(AuditEvent).order_by(AuditEvent.seq))).scalars()
        ]
    assert "virtual_key.create" in actions


async def test_update_revoke_delete_and_validation(env):
    c, tok = env["c"], env["tok"]
    out = await _issue(env)
    one = f"{BASE}/{out['id']}"
    resp = await c.patch(
        one,
        headers=H(tok),
        json={
            "name": "renamed",
            "credential_ids": [env["creds"]["o2"]],
            "strategy": "least_recently_limited",
            "allowed_models": ["gpt-4o-mini", "gpt-4o-mini"],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "renamed" and body["strategy"] == "least_recently_limited"
    assert body["allowed_models"] == ["gpt-4o-mini"]
    assert [p["id"] for p in body["credentials"]] == [env["creds"]["o2"]]
    # The binding is fixed.
    resp = await c.patch(one, headers=H(tok), json={"team_id": env["team"]["id"]})
    assert resp.status_code == 422
    # Validation.
    bad = [
        {"name": "bad name!"},
        {"credential_ids": []},
        {"credential_ids": [str(uuid.uuid4())]},
        {"strategy": "random"},
        {"allowed_models": []},
        {"allowed_models": ["has space"]},
        {"expires_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()},
    ]
    for patch in bad:
        resp = await c.patch(one, headers=H(tok), json=patch)
        assert resp.status_code == 400, (patch, resp.text)
    other_team = await _post(c, tok, A + "/teams", {"name": "other"})
    await _issue(
        env,
        name="x",
        team_id=other_team["id"],
        project_id=env["project"]["id"],
        status=400,
    )
    await _issue(env, name="renamed", status=409)
    # Revoke, then delete.
    resp = await c.post(one + "/revoke", headers=H(tok))
    assert resp.status_code == 200 and resp.json()["status"] == "revoked"
    assert (await c.delete(one, headers=H(tok))).status_code == 204
    assert (await c.get(one, headers=H(tok))).status_code == 404


async def test_scopes_admin_tokens_only_and_resolve_never_implicit(env):
    c, tid = env["c"], env["tid"]
    assert "admin:virtual-keys" in ADMIN_SCOPES
    assert "admin:virtual-keys" not in TENANT_KEY_SCOPES
    assert "admin:virtual-keys" in ROLES["admin"][1] and "admin:virtual-keys" in ROLES["owner"][1]
    assert "admin:virtual-keys" not in ROLES["developer"][1]
    assert tenant_keys.VIRTUAL_KEYS_RESOLVE not in tenant_keys.ALL_KEY_SCOPES
    assert tenant_keys.VIRTUAL_KEYS_RESOLVE not in tenant_keys.LEGACY_KEY_SCOPES
    narrow = await _token(c, tid, ["admin:inference-credentials"])
    assert (await c.get(BASE, headers=H(narrow))).status_code == 403
    reader = await _token(c, tid, ["admin:read"])
    assert (await c.get(BASE, headers=H(reader))).status_code == 200
    assert (await c.post(BASE, headers=H(reader), json={})).status_code == 403
    # The seeded legacy ingest key cannot reach the admin routes or resolve.
    assert (await c.get(BASE, headers=H("ps-test-key-12345"))).status_code == 403
    out = await _issue(env)
    resp = await c.post(RESOLVE, headers=H("ps-test-key-12345"), json={"key": out["key"]})
    assert resp.status_code == 403
    assert "virtual-keys:resolve" in resp.json()["detail"]["detail"]
    policy_only = await _resolver_key(c, env["tok"], scopes=("policy:read",))
    resp = await c.post(RESOLVE, headers=H(policy_only), json={"key": out["key"]})
    assert resp.status_code == 403
    # A 422 never echoes the submitted key.
    resp = await c.post(RESOLVE, headers=H(env["resolver"]), json={"key": out["key"], "x": 1})
    assert resp.status_code == 422 and out["key"] not in resp.text
    resp = await c.post(RESOLVE, headers=H(env["resolver"]), json={"kee": out["key"]})
    assert resp.status_code == 422 and out["key"] not in resp.text


async def test_only_vault_and_virtual_key_admins_mint_resolver_keys(env, monkeypatch):
    """An ingest key with virtual-keys:resolve returns decrypted inference
    keys, so admin:keys alone (or an ingest key while scoped tokens are
    off) cannot issue or rotate one."""
    c, tid = env["c"], env["tid"]
    body = {"name": "gw-x", "scopes": ["virtual-keys:resolve"]}
    keys_only = await _token(c, tid, ["admin:keys"])
    resp = await c.post(A + "/api-keys", headers=H(keys_only), json=body)
    assert resp.status_code == 403, resp.text
    assert "admin:inference-credentials" in resp.json()["detail"]["detail"]
    for held in (["admin:keys", "admin:virtual-keys"], ["admin:keys", "admin:inference-credentials"]):
        tok = await _token(c, tid, held)
        assert (await c.post(A + "/api-keys", headers=H(tok), json=body)).status_code == 403
    # Rotating the gateway's resolver key returns a new secret: same rule.
    row_id = (
        await _post(c, env["tok"], A + "/api-keys", {"name": "gw-r", "scopes": body["scopes"]})
    )["id"]
    resp = await c.post(A + f"/api-keys/{row_id}/rotate", headers=H(keys_only))
    assert resp.status_code == 403
    enough = await _token(
        c, tid, ["admin:keys", "admin:virtual-keys", "admin:inference-credentials"]
    )
    assert (await c.post(A + "/api-keys", headers=H(enough), json=body)).status_code == 201
    assert (await c.post(A + f"/api-keys/{row_id}/rotate", headers=H(enough))).status_code == 200
    # Other scopes are unaffected.
    plain = {"name": "sdk", "scopes": ["ingest:write", "policy:read"]}
    assert (await c.post(A + "/api-keys", headers=H(keys_only), json=plain)).status_code == 201
    # A full legacy ingest key, admitted while scoped tokens are off, is refused.
    monkeypatch.setattr(settings, "admin_require_scoped_tokens", False)
    resp = await c.post(
        A + "/api-keys", headers=H("ps-test-key-12345"), json={**body, "name": "gw-y"}
    )
    assert resp.status_code == 403
    assert "needs an admin token" in resp.json()["detail"]["detail"]


# ── resolution ────────────────────────────────────────────────────────


async def test_resolve_returns_binding_and_decrypted_pool(env):
    c, f = env["c"], env["f"]
    out = await _issue(
        env, project_id=env["project"]["id"], principal_id=env["svc"]["id"], allowed_models=["gpt-*"]
    )
    res = await _resolve(c, env["resolver"], out["key"])
    assert res["active"] is True and res["cache_ttl_seconds"] == 30
    assert res["virtual_key"] == {
        "id": out["id"],
        "name": "checkout-svc",
        "team_id": env["team"]["id"],
        "project_id": env["project"]["id"],
        "principal_id": env["svc"]["id"],
        "user_id": f"principal:{env['svc']['id']}",
        "allowed_models": ["gpt-*"],
        "strategy": "round_robin",
        "expires_at": None,
    }
    assert [(x["provider"], x["api_key"]) for x in res["credentials"]] == [
        ("openai", OPENAI_1),
        ("openai", OPENAI_2),
        ("anthropic", ANTHROPIC_1),
    ]
    assert res["credentials"][0]["key_fingerprint"] == vk.cv.key_fingerprint(OPENAI_1)
    assert res["unavailable_credentials"] == []
    # One audited decrypt per credential; the cache serves the next resolve.
    assert await _uses(f) == 3
    await _resolve(c, env["resolver"], out["key"])
    assert await _uses(f) == 3
    async with f() as s:
        row = (await s.execute(select(VirtualKey))).scalar_one()
    assert row.last_resolved_at is not None
    # Rotation is picked up at once (the cache is keyed to the ciphertext).
    o1 = env["creds"]["o1"]
    resp = await c.post(
        f"{A}/inference-credentials/{o1}/rotate", headers=H(env["tok"]), json={"api_key": ROTATED}
    )
    assert resp.status_code == 200
    res = await _resolve(c, env["resolver"], out["key"])
    assert res["credentials"][0]["api_key"] == ROTATED
    assert await _uses(f) == 4
    # A disabled credential is reported, not served.
    resp = await c.patch(
        f"{A}/inference-credentials/{o1}", headers=H(env["tok"]), json={"disabled": True}
    )
    assert resp.status_code == 200
    res = await _resolve(c, env["resolver"], out["key"])
    assert [x["api_key"] for x in res["credentials"]] == [OPENAI_2, ANTHROPIC_1]
    assert res["unavailable_credentials"] == [{"id": o1, "reason": "disabled"}]
    # Deleting a credential removes it from the pool.
    assert (
        await c.delete(f"{A}/inference-credentials/{o1}", headers=H(env["tok"]))
    ).status_code == 204
    res = await _resolve(c, env["resolver"], out["key"])
    assert len(res["credentials"]) == 2 and res["unavailable_credentials"] == []
    # The project moves to another team: attribution follows.
    other = await _post(c, env["tok"], A + "/teams", {"name": "platform"})
    resp = await c.patch(
        f"{A}/projects/{env['project']['id']}",
        headers=H(env["tok"]),
        json={"team_id": other["id"]},
    )
    assert resp.status_code == 200, resp.text
    res = await _resolve(c, env["resolver"], out["key"])
    assert res["virtual_key"]["team_id"] == other["id"]


async def test_inactive_keys(env):
    c, tok = env["c"], env["tok"]
    assert await _resolve(c, env["resolver"], "ps-vk-unknown") == {
        "active": False,
        "reason": "not_found",
        "cache_ttl_seconds": 30,
    }
    assert (await _resolve(c, env["resolver"], "sk-not-a-virtual-key"))["reason"] == "not_found"
    revoked = await _issue(env, name="revoked")
    await c.post(f"{BASE}/{revoked['id']}/revoke", headers=H(tok))
    assert (await _resolve(c, env["resolver"], revoked["key"]))["reason"] == "revoked"
    expiring = await _issue(env, name="expiring")
    async with env["f"]() as s:
        await s.execute(
            update(VirtualKey)
            .where(VirtualKey.id == uuid.UUID(expiring["id"]))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await s.commit()
    assert (await _resolve(c, env["resolver"], expiring["key"]))["reason"] == "expired"
    bound = await _issue(env, name="bound", principal_id=env["svc"]["id"])
    pid = env["svc"]["id"]
    resp = await c.patch(f"{A}/principals/{pid}", headers=H(tok), json={"disabled": True})
    assert resp.status_code == 200, resp.text
    res = await _resolve(c, env["resolver"], bound["key"])
    assert res == {"active": False, "reason": "principal_disabled", "cache_ttl_seconds": 30}
    # Re-enabled, the key works again (disabling suspends, it does not revoke).
    await c.patch(f"{A}/principals/{pid}", headers=H(tok), json={"disabled": False})
    assert (await _resolve(c, env["resolver"], bound["key"]))["active"] is True
    # A disabled principal cannot be bound.
    await c.patch(f"{A}/principals/{pid}", headers=H(tok), json={"disabled": True})
    await _issue(env, name="late", principal_id=pid, status=400)


async def test_deleting_a_principal_revokes_its_keys_and_teams_need_keys_gone(env):
    c, tok, f = env["c"], env["tok"], env["f"]
    bound = await _issue(env, name="bound", principal_id=env["svc"]["id"])
    assert (await c.delete(f"{A}/principals/{env['svc']['id']}", headers=H(tok))).status_code == 204
    one = (await c.get(f"{BASE}/{bound['id']}", headers=H(tok))).json()
    assert one["status"] == "revoked" and one["principal_id"] is None
    assert (await _resolve(c, env["resolver"], bound["key"]))["reason"] == "revoked"
    async with f() as s:
        audit = (
            await s.execute(select(AuditEvent).where(AuditEvent.action == "principal.delete"))
        ).scalar_one()
    assert audit.details["revoked_virtual_key_ids"] == [bound["id"]]

    on_project = await _issue(env, name="on-project", project_id=env["project"]["id"])
    resp = await c.delete(f"{A}/projects/{env['project']['id']}", headers=H(tok))
    assert resp.status_code == 409 and "virtual keys" in resp.text
    await c.delete(f"{BASE}/{on_project['id']}", headers=H(tok))
    assert (await c.delete(f"{A}/projects/{env['project']['id']}", headers=H(tok))).status_code == 204
    await _issue(env, name="on-team", team_id=env["team"]["id"])
    resp = await c.delete(f"{A}/teams/{env['team']['id']}", headers=H(tok))
    assert resp.status_code == 409 and "virtual keys" in resp.text


async def test_disabling_the_creator_revokes_keys_its_tokens_created(env):
    c, tid, tok = env["c"], env["tid"], env["tok"]
    person = await _post(c, tok, A + "/principals", {"kind": "user", "email": "dev@test.com"})
    await _post(
        c, tok, A + "/memberships", {"principal_id": person["id"], "role": "admin"}
    )
    resp = await c.post(
        f"{A}/tokens",
        headers=H(tok),
        json={"name": "person-token", "scopes": ["admin:virtual-keys", "admin:read"], "principal_id": person["id"]},
    )
    assert resp.status_code == 201, resp.text
    person_tok = resp.json()["token"]
    made = await _post(
        c,
        person_tok,
        BASE,
        {"name": "made-by-person", "credential_ids": [env["creds"]["o1"]]},
    )
    assert made["created_by"].startswith("token:")
    resp = await c.patch(f"{A}/principals/{person['id']}", headers=H(tok), json={"disabled": True})
    assert resp.status_code == 200, resp.text
    assert (await c.get(f"{BASE}/{made['id']}", headers=H(tok))).json()["status"] == "revoked"
    assert tid


# ── tenant isolation ──────────────────────────────────────────────────


async def test_tenants_cannot_reach_each_others_keys_under_rls(env):
    c, engine = env["c"], env["engine"]
    b = await _add_tenant_b(engine)
    tok_b = await _token(c, b)
    a_key = await _issue(env)
    async with engine.begin() as conn:
        await rls.enable(conn)
    try:
        resolver_b = await _resolver_key(c, tok_b)
        cred_b = await _cred(c, tok_b, "openai", "b", "sk-proj-TENANT-B-0001")
        # B cannot pool A's credentials, read A's key or resolve it.
        resp = await c.post(
            BASE,
            headers=H(tok_b),
            json={"name": "steal", "credential_ids": [env["creds"]["o1"]]},
        )
        assert resp.status_code == 400
        assert (await c.get(f"{BASE}/{a_key['id']}", headers=H(tok_b))).status_code == 404
        assert (await c.post(f"{BASE}/{a_key['id']}/revoke", headers=H(tok_b))).status_code == 404
        assert (await _resolve(c, resolver_b, a_key["key"]))["reason"] == "not_found"
        b_key = await _post(c, tok_b, BASE, {"name": "own", "credential_ids": [cred_b]})
        res = await _resolve(c, resolver_b, b_key["key"])
        assert [x["api_key"] for x in res["credentials"]] == ["sk-proj-TENANT-B-0001"]
        # And A's resolver cannot resolve B's key.
        assert (await _resolve(c, env["resolver"], b_key["key"]))["reason"] == "not_found"
        assert (await _resolve(c, env["resolver"], a_key["key"]))["active"] is True
    finally:
        async with engine.begin() as conn:
            await rls.disable(conn)


# ── migration ─────────────────────────────────────────────────────────


def _load_migration_059():
    pytest.importorskip("alembic")
    path = (
        Path(__file__).resolve().parents[2] / "db" / "alembic" / "versions" / "059_virtual_keys.py"
    )
    spec = importlib.util.spec_from_file_location("migration_059", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_059_covers_every_scope():
    m = _load_migration_059()
    assert m.NEW_SCOPE == "admin:virtual-keys"
    assert set(m.PREVIOUS_SCOPES) | {m.NEW_SCOPE} == set(ADMIN_SCOPES)
    assert m.NEW_KEY_SCOPE == tenant_keys.VIRTUAL_KEYS_RESOLVE
    assert {m.KEYS, m.POOL} <= set(rls_tables())


def rls_tables():
    from db.rls import TENANT_TABLES

    return TENANT_TABLES
