"""Principals, teams, projects and built-in roles (#338) against a real
PostgreSQL database (PS_TEST_DATABASE_URL).

Covers: the admin CRUD routes and their audit rows, the role scope sets, a
viewer getting 403 on every write route (admin and partner), an auditor
reading audit:read only, tokens and partner credentials issued for a
principal (intersection at issue time and at every request, demotion and
disabling), owner protections and escalation checks, unbound credentials
working unchanged, the partner /teams and /projects routes, and tenant
isolation with row-level security enforced.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from collector import audit_trail, rbac, rls
from collector.admin_auth import ADMIN_SCOPES, TENANT_KEY_SCOPES
from collector.app import app
from collector.config import Settings, settings
from collector.partner_scopes import SCOPES
from db.api_keys import api_key_lookup_prefix, hash_api_key
from db.models import AuditEvent, PartnerCredential, Tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# PS_ADMIN_REQUIRE_SCOPED_TOKENS at its real default (tests/conftest.py).
pytestmark = pytest.mark.scoped_admin_default

TENANT_KEY = "ps-test-key-12345"  # the seeded_db tenant's legacy key
KEY_B = "ps-test-key-tenant-b"
OP_TOKEN = "op-secret-token"
A = "/api/v1/admin"


def H(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


OP = H(OP_TOKEN)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", OP_TOKEN)
    audit_trail.auth_failure_limiter.reset()


def _admin(engine):
    return async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, info=rls.bypass_info("test")
    )


async def _tenant_id(engine, name: str = "Test Corp") -> uuid.UUID:
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


async def _bootstrap(c, tenant_id, scopes=None) -> str:
    """An unbound token holding every admin scope, from the operator."""
    resp = await c.post(
        f"/api/v1/operator/tenants/{tenant_id}/admin-tokens",
        headers=OP,
        json={"name": f"boot-{uuid.uuid4().hex[:6]}", "scopes": scopes or list(ADMIN_SCOPES)},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


async def _post(c, token, path, body, status=201) -> dict:
    resp = await c.post(A + path, headers=H(token), json=body)
    assert resp.status_code == status, resp.text
    return resp.json()


async def _member(c, token, role: str, kind: str = "user") -> str:
    """A principal with a tenant-wide ``role``; returns its id."""
    p = await _post(c, token, "/principals", {"kind": kind, "display_name": role})
    await _post(c, token, "/memberships", {"principal_id": p["id"], "role": role})
    return p["id"]


async def _token_for(c, token, principal_id, scopes=None, status=201) -> dict:
    return await _post(
        c,
        token,
        "/tokens",
        {
            "name": f"t-{uuid.uuid4().hex[:8]}",
            "scopes": scopes or list(ADMIN_SCOPES),
            "principal_id": principal_id,
        },
        status,
    )


async def _partner_for(c, token, principal_id, **extra) -> dict:
    return await _post(
        c,
        token,
        "/partner-credentials",
        {"partner_name": "bi-tool", "principal_id": principal_id, **extra},
    )


@pytest.fixture
async def env(seeded_db):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        tid = await _tenant_id(seeded_db)
        yield {"c": c, "engine": seeded_db, "tid": tid, "boot": await _bootstrap(c, tid)}


def _write_routes(prefix: str) -> list[tuple[str, str]]:
    """Every non-GET operation the app serves under ``prefix``."""
    return [
        (method.upper(), path)
        for path, item in app.openapi()["paths"].items()
        if path.startswith(prefix)
        for method in ("post", "put", "patch", "delete")
        if method in item
    ]


def _operator_only() -> set[tuple[str, str]]:
    """/api/v1/admin operations documented as operator surfaces
    (PS_ADMIN_TOKEN, no tenant credential)."""
    import yaml

    spec = yaml.safe_load(
        (Path(__file__).resolve().parents[3] / "docs" / "openapi.yaml").read_text()
    )
    return {
        (method.upper(), path)
        for path, item in spec["paths"].items()
        if path.startswith("/api/v1/admin/")
        for method, op in item.items()
        if isinstance(op, dict) and op.get("security") == [{"OperatorToken": []}]
    }


def _fill(path: str) -> str:
    """Path parameters -> a fresh UUID (auth runs before validation)."""
    while "{" in path:
        start = path.index("{")
        path = path[:start] + str(uuid.uuid4()) + path[path.index("}") + 1 :]
    return path


# ──────────────────────────────────────────────────────────────────────
# Static checks
# ──────────────────────────────────────────────────────────────────────


def test_scoped_tokens_are_required_by_default(monkeypatch):
    monkeypatch.delenv("PS_ADMIN_REQUIRE_SCOPED_TOKENS", raising=False)
    assert Settings().admin_require_scoped_tokens is True


def test_roles_are_named_sets_of_existing_scopes():
    from collector.rbac_admin import Role

    known = set(ADMIN_SCOPES) | set(SCOPES)
    assert list(rbac.ROLES) == ["owner", "admin", "developer", "viewer", "auditor", "billing"]
    assert set(Role.__args__) == set(rbac.ROLES)
    for name, (_, scopes) in rbac.ROLES.items():
        assert scopes <= known, name
    assert set(rbac._ALL_ADMIN) == set(ADMIN_SCOPES)
    assert rbac.ROLES["owner"][1] == known
    # A viewer writes nothing: admin:read and partner read scopes only.
    viewer = rbac.ROLES["viewer"][1]
    assert viewer & set(ADMIN_SCOPES) == {"admin:read"}
    assert all(s.endswith(":read") for s in viewer)
    assert rbac.ROLES["auditor"][1] == {"audit:read"}
    # Member management is never reachable with an ingest key.
    assert "admin:members" not in TENANT_KEY_SCOPES
    assert TENANT_KEY_SCOPES < set(ADMIN_SCOPES)


def test_migration_revision_chain():
    pytest.importorskip("alembic")
    path = (
        Path(__file__).resolve().parents[2]
        / "db"
        / "alembic"
        / "versions"
        / "039_rbac_principals.py"
    )
    spec = importlib.util.spec_from_file_location("migration_039", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.revision == "039"
    assert m.down_revision == "040"
    assert set(m.TABLES) == {"principals", "teams", "projects", "memberships"}
    # Full-admin tokens stay full admin (like 038 did for admin:privacy).
    assert m.NEW_SCOPE == "admin:members"
    assert set(m.PREVIOUS_SCOPES) | {m.NEW_SCOPE} == set(ADMIN_SCOPES)


# ──────────────────────────────────────────────────────────────────────
# Defaults and backwards compatibility
# ──────────────────────────────────────────────────────────────────────


async def test_tenant_keys_are_refused_on_admin_routes_by_default(env):
    c = env["c"]
    for path in ("/tokens", "/principals", "/teams"):
        resp = await c.get(A + path, headers=H(TENANT_KEY))
        assert resp.status_code == 403, (path, resp.text)
        assert "PS_ADMIN_REQUIRE_SCOPED_TOKENS" in resp.json()["detail"]["detail"]
    # Ingest and registry routes are unchanged.
    assert (await c.get("/api/v1/registry/assets", headers=H(TENANT_KEY))).status_code == 200


async def test_ingest_keys_never_reach_member_management(env, monkeypatch):
    monkeypatch.setattr(settings, "admin_require_scoped_tokens", False)
    c = env["c"]
    assert (await c.get(A + "/tokens", headers=H(TENANT_KEY))).status_code == 200
    # admin:read reads every admin resource; changing members needs
    # admin:members, which no ingest key holds or can grant.
    assert (await c.get(A + "/principals", headers=H(TENANT_KEY))).status_code == 200
    resp = await c.post(A + "/principals", headers=H(TENANT_KEY), json={"kind": "user"})
    assert resp.status_code == 403
    resp = await c.post(
        A + "/tokens",
        headers=H(TENANT_KEY),
        json={"name": "x", "scopes": ["admin:members"]},
    )
    assert resp.status_code == 403


async def test_unbound_tokens_and_credentials_work_unchanged(env):
    c, boot = env["c"], env["boot"]
    tok = await _post(c, boot, "/tokens", {"name": "ci", "scopes": ["admin:read"]})
    assert tok["principal_id"] is None
    assert tok["scopes"] == ["admin:read"]
    assert (await c.get(A + "/teams", headers=H(tok["token"]))).status_code == 200
    cred = await _post(
        c, boot, "/partner-credentials", {"partner_name": "ardoq", "scopes": ["registry:read"]}
    )
    assert cred["principal_id"] is None
    assert cred["scopes"] == ["registry:read"]
    resp = await c.get("/partner/v1/assets", headers=H(cred["api_key"]))
    assert resp.status_code == 200, resp.text


# ──────────────────────────────────────────────────────────────────────
# CRUD and audit
# ──────────────────────────────────────────────────────────────────────


async def test_crud_and_hash_chained_audit(env):
    c, boot, engine = env["c"], env["boot"], env["engine"]
    p = await _post(
        c,
        boot,
        "/principals",
        {"kind": "user", "external_id": "oidc|42", "email": "ana@example.com", "display_name": "Ana"},
    )
    assert p["status"] == "active" and p["email"] == "ana@example.com"
    await _post(c, boot, "/principals", {"kind": "user", "external_id": "oidc|42"}, 409)
    svc = await _post(c, boot, "/principals", {"kind": "service_account"})

    team = await _post(c, boot, "/teams", {"name": "Payments", "description": "card team"})
    await _post(c, boot, "/teams", {"name": "Payments"}, 409)
    proj = await _post(c, boot, "/projects", {"team_id": team["id"], "name": "checkout"})
    assert proj["team_id"] == team["id"]

    tenant_wide = await _post(c, boot, "/memberships", {"principal_id": p["id"], "role": "admin"})
    assert tenant_wide["team_id"] is None
    in_team = await _post(
        c, boot, "/memberships", {"principal_id": p["id"], "role": "developer", "team_id": team["id"]}
    )
    in_proj = await _post(
        c, boot, "/memberships", {"principal_id": svc["id"], "role": "viewer", "project_id": proj["id"]}
    )
    assert in_proj["team_id"] == team["id"]  # taken from the project
    await _post(c, boot, "/memberships", {"principal_id": p["id"], "role": "viewer"}, 409)

    resp = await c.patch(
        f"{A}/memberships/{in_team['id']}", headers=H(boot), json={"role": "viewer"}
    )
    assert resp.status_code == 200 and resp.json()["role"] == "viewer"

    got = (await c.get(f"{A}/principals/{p['id']}", headers=H(boot))).json()
    assert {m["id"] for m in got["memberships"]} == {tenant_wide["id"], in_team["id"]}
    listed = (await c.get(f"{A}/memberships?team_id={team['id']}", headers=H(boot))).json()
    assert {m["id"] for m in listed["data"]} == {in_team["id"], in_proj["id"]}
    projects = (await c.get(f"{A}/projects?team_id={team['id']}", headers=H(boot))).json()
    assert [x["name"] for x in projects["data"]] == ["checkout"]

    resp = await c.patch(f"{A}/principals/{p['id']}", headers=H(boot), json={"display_name": "Ana B"})
    assert resp.status_code == 200 and resp.json()["display_name"] == "Ana B"
    resp = await c.patch(f"{A}/teams/{team['id']}", headers=H(boot), json={"name": "Pay"})
    assert resp.status_code == 200 and resp.json()["name"] == "Pay"

    # A team with projects is not deleted; its project, then the team, are.
    assert (await c.delete(f"{A}/teams/{team['id']}", headers=H(boot))).status_code == 409
    assert (await c.delete(f"{A}/projects/{proj['id']}", headers=H(boot))).status_code == 204
    assert (await c.get(f"{A}/memberships/{in_proj['id']}", headers=H(boot))).status_code == 404
    assert (await c.delete(f"{A}/teams/{team['id']}", headers=H(boot))).status_code == 204
    assert (await c.delete(f"{A}/memberships/{tenant_wide['id']}", headers=H(boot))).status_code == 204
    assert (await c.delete(f"{A}/principals/{svc['id']}", headers=H(boot))).status_code == 204
    assert (await c.get(f"{A}/principals/{svc['id']}", headers=H(boot))).status_code == 404

    async with _admin(engine)() as s:
        rows = (
            await s.execute(
                select(AuditEvent)
                .where(AuditEvent.tenant_id == env["tid"])
                .order_by(AuditEvent.seq)
            )
        ).scalars().all()
        assert (await audit_trail.verify_chain(s, env["tid"])).ok
    actions = [r.action for r in rows]
    for action in (
        "principal.create",
        "principal.update",
        "principal.delete",
        "team.create",
        "team.update",
        "team.delete",
        "project.create",
        "project.delete",
        "membership.add",
        "role.change",
        "membership.remove",
    ):
        assert action in actions, action
    change = next(r for r in rows if r.action == "role.change")
    assert change.details["role"] == "viewer"
    assert change.details["previous_role"] == "developer"
    assert all(r.actor_type == "admin_token" for r in rows if r.action.startswith("membership"))
    # No personal data on the trail.
    assert "ana@example.com" not in str([r.details for r in rows])
    assert "Ana" not in str([r.details for r in rows])


async def test_roles_route_lists_scopes(env):
    resp = await env["c"].get(A + "/roles", headers=H(env["boot"]))
    assert resp.status_code == 200
    roles = {r["name"]: r for r in resp.json()["data"]}
    assert roles["viewer"]["admin_scopes"] == ["admin:read"]
    assert roles["auditor"] == {
        "name": "auditor",
        "description": rbac.ROLES["auditor"][0],
        "admin_scopes": [],
        "partner_scopes": ["audit:read"],
    }


# ──────────────────────────────────────────────────────────────────────
# Viewer and auditor
# ──────────────────────────────────────────────────────────────────────


async def test_viewer_gets_403_on_every_write_route(env):
    c, boot = env["c"], env["boot"]
    viewer = await _member(c, boot, "viewer")
    tok = await _token_for(c, boot, viewer)
    assert tok["scopes"] == ["admin:read"]  # the role's share of what was asked
    cred = await _partner_for(c, boot, viewer)
    assert set(cred["scopes"]) == rbac.ROLES["viewer"][1] & set(SCOPES)

    admin_writes = _write_routes("/api/v1/admin/")
    operator_only = _operator_only()
    partner_writes = _write_routes("/partner/v1/")
    assert len(admin_writes) > 30 and len(partner_writes) > 5
    for method, path in admin_writes:
        resp = await c.request(method, _fill(path), headers=H(tok["token"]), json={})
        if (method, path) in operator_only:
            # Operator routes under /api/v1/admin take PS_ADMIN_TOKEN only.
            assert resp.status_code in (401, 403, 404), (method, path, resp.text)
        else:
            assert resp.status_code == 403, (method, path, resp.text)
    for method, path in partner_writes:
        resp = await c.request(method, _fill(path), headers=H(cred["api_key"]), json={})
        assert resp.status_code == 403, (method, path, resp.text)

    # Reads work.
    for path in ("/principals", "/teams", "/tokens", "/partner-credentials", "/roles"):
        assert (await c.get(A + path, headers=H(tok["token"]))).status_code == 200, path
    for path in ("/partner/v1/assets", "/partner/v1/teams", "/partner/v1/projects"):
        assert (await c.get(path, headers=H(cred["api_key"]))).status_code == 200, path
    # But not the audit trail.
    resp = await c.get("/partner/v1/siem/audit", headers=H(cred["api_key"]))
    assert resp.status_code == 403


async def test_auditor_reads_audit_only(env):
    c, boot = env["c"], env["boot"]
    auditor = await _member(c, boot, "auditor")
    # No admin scope in the role: no admin token.
    await _token_for(c, boot, auditor, status=400)
    cred = await _partner_for(c, boot, auditor, scopes=["registry:read", "audit:read"])
    assert cred["scopes"] == ["audit:read"]
    key = H(cred["api_key"])
    resp = await c.get("/partner/v1/siem/audit", headers=key)
    assert resp.status_code == 200, resp.text
    for path in ("/partner/v1/assets", "/partner/v1/teams", "/partner/v1/siem/calls"):
        assert (await c.get(path, headers=key)).status_code == 403, path

    # OAuth tokens carry the same scope set.
    async with _admin(env["engine"])() as s:
        row = await s.get(PartnerCredential, uuid.UUID(cred["id"]))
        client_id = row.client_id
    resp = await c.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": cred["client_secret"],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["scope"] == "audit:read"
    jwt = H(resp.json()["access_token"])
    assert (await c.get("/partner/v1/siem/audit", headers=jwt)).status_code == 200
    assert (await c.get("/partner/v1/assets", headers=jwt)).status_code == 403


# ──────────────────────────────────────────────────────────────────────
# Inheritance follows the principal
# ──────────────────────────────────────────────────────────────────────


async def test_demotion_and_disabling_take_effect_at_once(env):
    c, boot = env["c"], env["boot"]
    pid = await _member(c, boot, "admin")
    tok = await _token_for(c, boot, pid, scopes=["admin:read", "admin:members"])
    cred = await _partner_for(c, boot, pid, scopes=["assets:read", "webhooks:manage"])
    t, k = H(tok["token"]), H(cred["api_key"])
    assert (await c.post(A + "/teams", headers=t, json={"name": "Ops"})).status_code == 201

    membership = (await c.get(f"{A}/memberships?principal_id={pid}", headers=H(boot))).json()
    mid = membership["data"][0]["id"]
    resp = await c.patch(f"{A}/memberships/{mid}", headers=H(boot), json={"role": "viewer"})
    assert resp.status_code == 200
    assert (await c.post(A + "/teams", headers=t, json={"name": "Ops2"})).status_code == 403
    assert (await c.get(A + "/teams", headers=t)).status_code == 200
    assert (await c.get("/partner/v1/assets", headers=k)).status_code == 200
    resp = await c.get("/api/v1/admin/webhooks/subscriptions", headers=t)
    assert resp.status_code == 200  # admin:read still

    # Team or project roles grant nothing on tenant-wide routes.
    assert (await c.delete(f"{A}/memberships/{mid}", headers=H(boot))).status_code == 204
    team = (await c.get(A + "/teams", headers=H(boot))).json()["data"][0]
    await _post(c, boot, "/memberships", {"principal_id": pid, "role": "admin", "team_id": team["id"]})
    assert (await c.get(A + "/teams", headers=t)).status_code == 403
    assert (await c.get("/partner/v1/assets", headers=k)).status_code == 403

    resp = await c.patch(f"{A}/principals/{pid}", headers=H(boot), json={"disabled": True})
    assert resp.status_code == 200 and resp.json()["status"] == "disabled"
    assert (await c.get(A + "/teams", headers=t)).status_code == 401
    assert (await c.get("/partner/v1/assets", headers=k)).status_code == 401
    # Nothing new is issued for a disabled principal.
    await _token_for(c, boot, pid, status=400)


async def test_deleting_a_principal_revokes_its_credentials(env):
    c, boot = env["c"], env["boot"]
    pid = await _member(c, boot, "developer")
    tok = await _token_for(c, boot, pid, scopes=["admin:read"])
    cred = await _partner_for(c, boot, pid)
    assert (await c.delete(f"{A}/principals/{pid}", headers=H(boot))).status_code == 204
    assert (await c.get(A + "/teams", headers=H(tok["token"]))).status_code == 401
    assert (await c.get("/partner/v1/assets", headers=H(cred["api_key"]))).status_code == 401
    got = (await c.get(f"{A}/tokens/{tok['id']}", headers=H(boot))).json()
    assert got["status"] == "revoked" and got["principal_id"] is None
    got = (await c.get(f"{A}/partner-credentials/{cred['id']}", headers=H(boot))).json()
    assert got["status"] == "revoked" and got["principal_id"] is None


# ──────────────────────────────────────────────────────────────────────
# Escalation and owners
# ──────────────────────────────────────────────────────────────────────


async def test_no_escalation_through_memberships(env):
    c, boot = env["c"], env["boot"]
    limited = await _post(c, boot, "/tokens", {"name": "hr", "scopes": ["admin:members"]})
    lt = limited["token"]
    pid = (await _post(c, lt, "/principals", {"kind": "user"}))["id"]
    await _post(c, lt, "/memberships", {"principal_id": pid, "role": "viewer"}, 403)
    await _post(c, lt, "/memberships", {"principal_id": pid, "role": "admin"}, 403)
    await _post(c, lt, "/memberships", {"principal_id": pid, "role": "auditor"})
    # A token cannot be issued past the caller's own scopes either.
    await _post(
        c, lt, "/tokens", {"name": "x", "scopes": ["admin:read"], "principal_id": pid}, 403
    )


async def test_owner_role_is_protected(env):
    c, boot = env["c"], env["boot"]
    owner = await _member(c, boot, "owner")
    admin = await _member(c, boot, "admin")
    owner_raw = (await _token_for(c, boot, owner))["token"]
    admin_raw = (await _token_for(c, boot, admin))["token"]
    owner_tok, admin_tok = H(owner_raw), H(admin_raw)
    owner_m = (await c.get(f"{A}/memberships?principal_id={owner}", headers=H(boot))).json()
    owner_mid = owner_m["data"][0]["id"]

    # An admin holds every scope but cannot grant or touch the owner role.
    newbie = (await _post(c, admin_raw, "/principals", {"kind": "user"}))["id"]
    await _post(c, admin_raw, "/memberships", {"principal_id": newbie, "role": "owner"}, 403)
    await _post(c, admin_raw, "/memberships", {"principal_id": newbie, "role": "admin"})
    resp = await c.delete(f"{A}/memberships/{owner_mid}", headers=admin_tok)
    assert resp.status_code == 403
    resp = await c.patch(f"{A}/principals/{owner}", headers=admin_tok, json={"disabled": True})
    assert resp.status_code == 403
    assert (await c.delete(f"{A}/principals/{owner}", headers=admin_tok)).status_code == 403

    # The last owner stays.
    resp = await c.patch(f"{A}/memberships/{owner_mid}", headers=owner_tok, json={"role": "admin"})
    assert resp.status_code == 409
    resp = await c.patch(f"{A}/principals/{owner}", headers=owner_tok, json={"disabled": True})
    assert resp.status_code == 409

    # An owner makes another owner; then the first can step down.
    newbie_m = (await c.get(f"{A}/memberships?principal_id={newbie}", headers=H(boot))).json()
    resp = await c.patch(
        f"{A}/memberships/{newbie_m['data'][0]['id']}", headers=owner_tok, json={"role": "owner"}
    )
    assert resp.status_code == 200
    resp = await c.patch(f"{A}/memberships/{owner_mid}", headers=owner_tok, json={"role": "admin"})
    assert resp.status_code == 200
    # Now an admin, the former owner's token no longer manages owners.
    resp = await c.patch(f"{A}/principals/{newbie}", headers=owner_tok, json={"disabled": True})
    assert resp.status_code == 403


async def test_admin_cannot_mint_an_owner_capable_token(env):
    """An admin holds every admin scope, but a token holding every admin
    scope that is unbound, or issued for an owner, can manage owners; only
    an owner may issue one, or an admin could make itself owner."""
    c, boot = env["c"], env["boot"]
    owner = await _member(c, boot, "owner")
    admin = await _member(c, boot, "admin")
    admin_raw = (await _token_for(c, boot, admin))["token"]
    owner_raw = (await _token_for(c, boot, owner))["token"]
    every = list(ADMIN_SCOPES)
    await _post(c, admin_raw, "/tokens", {"name": "unbound", "scopes": every}, 403)
    await _token_for(c, admin_raw, owner, status=403)
    # Narrower tokens, or full tokens for a non-owner, are fine.
    await _post(c, admin_raw, "/tokens", {"name": "ci", "scopes": every[:-1]})
    await _token_for(c, admin_raw, admin)
    await _token_for(c, admin_raw, owner, scopes=["admin:read"])
    # An owner (or the operator's unbound bootstrap token) can.
    await _post(c, owner_raw, "/tokens", {"name": "unbound2", "scopes": every})
    await _token_for(c, owner_raw, owner)


async def test_narrow_member_token_cannot_reenable_an_admin(env):
    """Disabling, re-enabling (which revives its credentials) or changing
    the external_id of a principal needs what granting its roles would."""
    c, boot = env["c"], env["boot"]
    admin = await _member(c, boot, "admin")
    resp = await c.patch(f"{A}/principals/{admin}", headers=H(boot), json={"disabled": True})
    assert resp.status_code == 200
    lt = (await _post(c, boot, "/tokens", {"name": "hr", "scopes": ["admin:members"]}))["token"]
    for body in ({"disabled": False}, {"external_id": "idp|attacker"}):
        resp = await c.patch(f"{A}/principals/{admin}", headers=H(lt), json=body)
        assert resp.status_code == 403, body
    # Display fields stay editable.
    resp = await c.patch(f"{A}/principals/{admin}", headers=H(lt), json={"display_name": "Al"})
    assert resp.status_code == 200
    auditor = (await _post(c, lt, "/principals", {"kind": "user"}))["id"]
    await _post(c, lt, "/memberships", {"principal_id": auditor, "role": "auditor"})
    resp = await c.patch(f"{A}/principals/{auditor}", headers=H(lt), json={"disabled": True})
    assert resp.status_code == 200


async def test_principal_of_another_tenant_cannot_be_bound_without_rls(env):
    """The tenant check on principal_id is explicit, not only row-level
    security (which is opt-in)."""
    c, boot, engine = env["c"], env["boot"], env["engine"]
    b = await _add_tenant_b(engine)
    boot_b = await _bootstrap(c, b)
    p_a = await _member(c, boot, "admin")
    await _token_for(c, boot_b, p_a, scopes=["admin:read"], status=400)
    await _post(
        c,
        boot_b,
        "/partner-credentials",
        {"partner_name": "x", "principal_id": p_a, "scopes": ["members:read"]},
        400,
    )


# ──────────────────────────────────────────────────────────────────────
# Partner read routes and tenant isolation
# ──────────────────────────────────────────────────────────────────────


async def test_partner_teams_and_projects(env):
    c, boot = env["c"], env["boot"]
    team = await _post(c, boot, "/teams", {"name": "Data"})
    await _post(c, boot, "/teams", {"name": "Apps"})
    await _post(c, boot, "/projects", {"team_id": team["id"], "name": "warehouse"})
    cred = await _post(
        c, boot, "/partner-credentials", {"partner_name": "leanix", "scopes": ["members:read"]}
    )
    key = H(cred["api_key"])
    teams = (await c.get("/partner/v1/teams", headers=key)).json()["data"]
    assert [t["name"] for t in teams] == ["Apps", "Data"]
    projects = (await c.get(f"/partner/v1/projects?team_id={team['id']}", headers=key)).json()
    assert [p["name"] for p in projects["data"]] == ["warehouse"]
    legacy = await _post(
        c, boot, "/partner-credentials", {"partner_name": "old", "scopes": ["registry:read"]}
    )
    resp = await c.get("/partner/v1/teams", headers=H(legacy["api_key"]))
    assert resp.status_code == 403  # members:read is not part of registry:read


async def test_tenants_cannot_see_each_others_members_under_rls(env):
    c, boot, engine = env["c"], env["boot"], env["engine"]
    b = await _add_tenant_b(engine)
    boot_b = await _bootstrap(c, b)
    p_a = await _member(c, boot, "viewer")
    team_a = await _post(c, boot, "/teams", {"name": "Shared name"})
    async with engine.begin() as conn:
        await rls.enable(conn)
    try:
        # Same team name in another tenant is fine.
        team_b = await _post(c, boot_b, "/teams", {"name": "Shared name"})
        p_b = await _member(c, boot_b, "admin")
        for path in ("/principals", "/teams", "/memberships", "/projects"):
            data = (await c.get(A + path, headers=H(boot_b))).json()["data"]
            ids = {d["id"] for d in data}
            assert p_a not in ids and team_a["id"] not in ids, path
        assert {d["id"] for d in (await c.get(A + "/teams", headers=H(boot_b))).json()["data"]} == {
            team_b["id"]
        }
        assert (await c.get(f"{A}/principals/{p_a}", headers=H(boot_b))).status_code == 404
        assert (await c.get(f"{A}/teams/{team_a['id']}", headers=H(boot_b))).status_code == 404
        # Nor act on them: grant, issue for, or delete.
        await _post(c, boot_b, "/memberships", {"principal_id": p_a, "role": "viewer"}, 404)
        await _token_for(c, boot_b, p_a, status=400)
        assert (await c.delete(f"{A}/principals/{p_a}", headers=H(boot_b))).status_code == 404
        cred_b = await _post(
            c, boot_b, "/partner-credentials", {"partner_name": "x", "scopes": ["members:read"]}
        )
        teams = (await c.get("/partner/v1/teams", headers=H(cred_b["api_key"]))).json()["data"]
        assert [t["id"] for t in teams] == [team_b["id"]]
        # A token for tenant B's principal works under RLS.
        tok_b = await _token_for(c, boot_b, p_b, scopes=["admin:read"])
        assert (await c.get(A + "/principals", headers=H(tok_b["token"]))).status_code == 200
    finally:
        async with engine.begin() as conn:
            await rls.disable(conn)
