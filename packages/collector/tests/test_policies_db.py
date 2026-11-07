"""Central governance policies (#340) against a real PostgreSQL database
(PS_TEST_DATABASE_URL).

Covers: create/version/activate/rollback/deactivate/delete through the admin
API and their audit rows (before/after digests, in the audit export),
immutable versions (no route changes one; the database refuses UPDATE),
document validation, one policy per binding, the scopes (admin:policies,
viewer read-only, ingest keys never), the gateway pull with policy:read,
its ETag/304, inheritance (project > team > tenant per section), team and
project deletes refused while a policy is bound, migration 043's chain and
scope grants, and tenant isolation with row-level security enforced.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from collector import audit_trail, policy_doc, rls, tenant_keys
from collector.admin_auth import ADMIN_SCOPES, TENANT_KEY_SCOPES
from collector.app import app
from collector.config import settings
from db.api_keys import api_key_lookup_prefix, hash_api_key
from db.models import AuditEvent, PolicyVersion, Tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.scoped_admin_default

TENANT_KEY = "ps-test-key-12345"
KEY_B = "ps-test-key-tenant-b"
OP_TOKEN = "op-secret-token"
A = "/api/v1/admin"

ROUTE = {
    "schema_version": "route-policy.v1",
    "default_group": "cheap",
    "quality_to_group": {"draft": "cheap", "balanced": "cheap", "critical": "cheap"},
    "groups": {
        "cheap": [
            {
                "model": "mistral-small",
                "provider": "mistral",
                "input_cost_per_1k": 0.0002,
                "output_cost_per_1k": 0.0006,
            }
        ]
    },
}


def ACCESS(models: list[str], default: str = "deny") -> dict:
    return {
        "version": 1,
        "default": default,
        "groups": [{"name": "eng", "match": ["engineering"], "models": models}],
    }


def DOC(**sections) -> dict:
    return {"schema": "ps-policy.v1", "sections": sections}


def H(key: str, **extra) -> dict:
    return {"Authorization": f"Bearer {key}", **extra}


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


async def _bootstrap(c, tenant_id, scopes=None) -> str:
    resp = await c.post(
        f"/api/v1/operator/tenants/{tenant_id}/admin-tokens",
        headers=H(OP_TOKEN),
        json={"name": f"boot-{uuid.uuid4().hex[:6]}", "scopes": scopes or list(ADMIN_SCOPES)},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


async def _post(c, token, path, body, status=201) -> dict:
    resp = await c.post(A + path, headers=H(token), json=body)
    assert resp.status_code == status, resp.text
    return resp.json()


async def _policy_key(c, token) -> str:
    """An ingest key holding only policy:read (the gateway's credential)."""
    resp = await c.post(
        A + "/api-keys", headers=H(token), json={"name": "gw", "scopes": ["policy:read"]}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


async def _audit(engine, tenant_id) -> list[AuditEvent]:
    async with _admin(engine)() as s:
        return list(
            (
                await s.execute(
                    select(AuditEvent)
                    .where(AuditEvent.tenant_id == tenant_id)
                    .order_by(AuditEvent.seq)
                )
            ).scalars()
        )


@pytest.fixture
async def env(seeded_db):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        tid = await _tenant_id(seeded_db)
        yield {"c": c, "engine": seeded_db, "tid": tid, "boot": await _bootstrap(c, tid)}


# ──────────────────────────────────────────────────────────────────────
# Static checks
# ──────────────────────────────────────────────────────────────────────


def test_scopes_are_registered():
    assert "admin:policies" in ADMIN_SCOPES
    # Governance policies are never reachable with an ingest key.
    assert "admin:policies" not in TENANT_KEY_SCOPES
    assert tenant_keys.POLICY_READ in tenant_keys.KEY_SCOPES
    # policy:read does not change what makes a key "full".
    assert tenant_keys.POLICY_READ not in tenant_keys.ALL_KEY_SCOPES


def test_migration_043_chain_and_scopes():
    pytest.importorskip("alembic")
    path = (
        Path(__file__).resolve().parents[2] / "db" / "alembic" / "versions" / "043_policies.py"
    )
    spec = importlib.util.spec_from_file_location("migration_043", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.revision == "043"
    assert m.down_revision == "046"
    assert set(m.TABLES) == {"policies", "policy_versions"}
    assert set(m.PREVIOUS_ADMIN_SCOPES) | {m.NEW_ADMIN_SCOPE} == set(ADMIN_SCOPES)
    assert not hasattr(m, "PREVIOUS_KEY_SCOPES")  # policy:read is never granted
    assert tenant_keys.ALL_KEY_SCOPES | {m.NEW_KEY_SCOPE} == set(tenant_keys.KEY_SCOPES)
    # A legacy key does not get it either (issued explicitly to the gateway).
    assert m.NEW_KEY_SCOPE not in tenant_keys.LEGACY_KEY_SCOPES


async def test_migration_043_grants_admin_policies_but_never_policy_read(env):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = (
        Path(__file__).resolve().parents[2] / "db" / "alembic" / "versions" / "043_policies.py"
    )
    spec = importlib.util.spec_from_file_location("migration_043_run", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    c, engine, tid = env["c"], env["engine"], env["tid"]
    full = await _bootstrap(c, tid, list(m.PREVIOUS_ADMIN_SCOPES))
    narrow = await _bootstrap(c, tid, ["admin:read"])
    full_key = (
        await c.post(
            A + "/api-keys",
            headers=H(env["boot"]),
            json={"name": "full", "scopes": sorted(tenant_keys.ALL_KEY_SCOPES)},
        )
    ).json()["key"]
    sdk_key = await _policy_key(c, env["boot"])  # policy:read only: unchanged
    assert (await c.get(A + "/policies", headers=H(full))).status_code == 200  # admin:read
    assert (await c.post(A + "/policies", headers=H(full), json={})).status_code == 403
    assert (await c.get("/api/v1/policy/effective", headers=H(full_key))).status_code == 403

    def run(sync_conn):
        ctx = MigrationContext.configure(sync_conn)
        with Operations.context(ctx):
            sync_conn.exec_driver_sql(
                "SELECT set_config('app.rls_bypass', 'test', true)"
            )
            m._grant("admin_tokens", m.NEW_ADMIN_SCOPE, m.PREVIOUS_ADMIN_SCOPES)

    async with engine.begin() as conn:
        await conn.run_sync(run)
    assert (
        await c.post(A + "/policies", headers=H(full), json={"name": "p", "scope": "tenant"})
    ).status_code == 201
    assert (await c.post(A + "/policies", headers=H(narrow), json={})).status_code == 403
    # An existing full ingest key (e.g. in an SDK client) still cannot pull
    # the policy; only a key issued with policy:read can.
    assert (await c.get("/api/v1/policy/effective", headers=H(full_key))).status_code == 403
    assert (await c.get("/api/v1/policy/effective", headers=H(sdk_key))).status_code == 200


# ──────────────────────────────────────────────────────────────────────
# Lifecycle and audit
# ──────────────────────────────────────────────────────────────────────


async def test_create_version_activate_rollback_and_audit(env):
    c, boot, engine, tid = env["c"], env["boot"], env["engine"], env["tid"]
    p = await _post(
        c,
        boot,
        "/policies",
        {
            "name": "default",
            "scope": "tenant",
            "document": DOC(access=ACCESS(["gpt-4o-mini"])),
            "comment": "first",
            "activate": True,
        },
    )
    assert p["active_version"] == 1 and p["scope"] == "tenant"
    assert p["created_by"].startswith("token:")
    v2 = await _post(
        c,
        boot,
        f"/policies/{p['id']}/versions",
        {"document": DOC(access=ACCESS(["gpt-4o"]), route={"version": 1, "policy": ROUTE})},
    )
    assert v2["version"] == 2 and v2["sections"] == {"access": 1, "route": 1}
    # Adding a version does not activate it.
    got = (await c.get(f"{A}/policies/{p['id']}", headers=H(boot))).json()
    assert got["active_version"] == 1
    assert got["active_document"]["sections"]["access"]["groups"][0]["models"] == ["gpt-4o-mini"]

    resp = await c.post(f"{A}/policies/{p['id']}/activate", headers=H(boot), json={"version": 2})
    assert resp.status_code == 200 and resp.json()["active_version"] == 2
    # Rollback is activating an older version.
    resp = await c.post(f"{A}/policies/{p['id']}/activate", headers=H(boot), json={"version": 1})
    assert resp.json()["active_version"] == 1
    # Activating the active version again changes (and audits) nothing.
    await c.post(f"{A}/policies/{p['id']}/activate", headers=H(boot), json={"version": 1})
    resp = await c.post(f"{A}/policies/{p['id']}/activate", headers=H(boot), json={"version": 9})
    assert resp.status_code == 404

    versions = (await c.get(f"{A}/policies/{p['id']}/versions", headers=H(boot))).json()["data"]
    assert [v["version"] for v in versions] == [2, 1]
    assert "document" not in versions[0]
    full = (await c.get(f"{A}/policies/{p['id']}/versions/2", headers=H(boot))).json()
    assert full["sha256"] == policy_doc.digest(full["document"])

    rows = [r for r in await _audit(engine, tid) if r.action.startswith("policy.")]
    assert [r.action for r in rows] == [
        "policy.create",
        "policy.version_create",
        "policy.activate",
        "policy.version_create",
        "policy.activate",
        "policy.activate",
    ]
    first, to_v2, back = rows[2], rows[4], rows[5]
    assert first.details["before"] == {"version": None}
    assert first.details["after"]["version"] == 1
    assert to_v2.details["before"]["version"] == 1
    assert to_v2.details["after"] == {"version": 2, "sha256": full["sha256"]}
    assert to_v2.details["sections_changed"] == ["access", "route"]
    assert back.details["after"]["version"] == 1
    assert all(r.target_id == p["id"] for r in rows)

    # The audit export (partner API, audit:read) shows the change.
    cred = await _post(
        c, boot, "/partner-credentials", {"partner_name": "siem", "scopes": ["audit:read"]}
    )
    exported = (await c.get("/partner/v1/audit", headers=H(cred["api_key"]))).json()["data"]
    assert "policy.activate" in {e["action"] for e in exported}

    # Deactivate, then delete.
    resp = await c.post(
        f"{A}/policies/{p['id']}/activate", headers=H(boot), json={"version": None}
    )
    assert resp.json()["active_version"] is None
    assert (await c.delete(f"{A}/policies/{p['id']}", headers=H(boot))).status_code == 204
    assert (await c.get(f"{A}/policies/{p['id']}", headers=H(boot))).status_code == 404
    last = (await _audit(engine, tid))[-1]
    assert last.action == "policy.delete" and last.details["versions_deleted"] == 2


async def test_versions_are_immutable(env):
    c, boot, engine = env["c"], env["boot"], env["engine"]
    p = await _post(
        c, boot, "/policies", {"name": "p", "scope": "tenant", "document": DOC()}
    )
    async with _admin(engine)() as s:
        with pytest.raises(DBAPIError, match="immutable"):
            await s.execute(
                text("UPDATE policy_versions SET comment = 'x' WHERE policy_id = :p"),
                {"p": p["id"]},
            )
    # No route changes a version: PUT/PATCH are not served.
    for method in ("put", "patch"):
        resp = await c.request(method, f"{A}/policies/{p['id']}/versions/1", headers=H(boot))
        assert resp.status_code == 405


async def test_invalid_documents_are_refused_with_every_problem(env):
    c, boot = env["c"], env["boot"]
    bad = {
        "schema": "ps-policy.v1",
        "sections": {
            "route": {"version": 1, "policy": {"schema_version": "route-policy.v1"}},
            "access": {"version": 2},
            "rate_limits": {"version": 1},
            "bogus": {"version": 1},
        },
    }
    resp = await c.post(
        A + "/policies", headers=H(boot), json={"name": "p", "scope": "tenant", "document": bad}
    )
    assert resp.status_code == 422
    problems = resp.json()["detail"]["detail"]
    assert any("route.policy is missing" in x for x in problems)
    assert any("access.version" in x for x in problems)
    assert any("reserved for rate-limit policies" in x for x in problems)
    assert any("bogus: unknown section" in x for x in problems)
    # Nothing was created.
    assert (await c.get(A + "/policies", headers=H(boot))).json()["data"] == []
    # Binding checks.
    for body in (
        {"name": "x", "scope": "team"},
        {"name": "x", "scope": "tenant", "team_id": str(uuid.uuid4())},
        {"name": "x", "scope": "tenant", "activate": True},
        {"name": "bad name", "scope": "tenant"},
    ):
        assert (await c.post(A + "/policies", headers=H(boot), json=body)).status_code == 422
    resp = await c.post(
        A + "/policies",
        headers=H(boot),
        json={"name": "x", "scope": "team", "team_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404


async def test_one_policy_per_binding_and_unique_names(env):
    c, boot = env["c"], env["boot"]
    await _post(c, boot, "/policies", {"name": "a", "scope": "tenant"})
    await _post(c, boot, "/policies", {"name": "b", "scope": "tenant"}, 409)
    team = await _post(c, boot, "/teams", {"name": "eng"})
    await _post(c, boot, "/policies", {"name": "a", "scope": "team", "team_id": team["id"]}, 409)
    t = await _post(c, boot, "/policies", {"name": "t", "scope": "team", "team_id": team["id"]})
    resp = await c.patch(f"{A}/policies/{t['id']}", headers=H(boot), json={"name": "a"})
    assert resp.status_code == 409
    resp = await c.patch(f"{A}/policies/{t['id']}", headers=H(boot), json={"name": "team-eng"})
    assert resp.status_code == 200 and resp.json()["name"] == "team-eng"
    listed = (await c.get(A + "/policies?scope=team", headers=H(boot))).json()["data"]
    assert [p["name"] for p in listed] == ["team-eng"]


async def test_team_and_project_with_a_policy_cannot_be_deleted(env):
    c, boot = env["c"], env["boot"]
    team = await _post(c, boot, "/teams", {"name": "eng"})
    project = await _post(c, boot, "/projects", {"name": "app", "team_id": team["id"]})
    pp = await _post(
        c, boot, "/policies", {"name": "pp", "scope": "project", "project_id": project["id"]}
    )
    assert (await c.delete(f"{A}/projects/{project['id']}", headers=H(boot))).status_code == 409
    await c.delete(f"{A}/policies/{pp['id']}", headers=H(boot))
    assert (await c.delete(f"{A}/projects/{project['id']}", headers=H(boot))).status_code == 204
    tp = await _post(c, boot, "/policies", {"name": "tp", "scope": "team", "team_id": team["id"]})
    assert (await c.delete(f"{A}/teams/{team['id']}", headers=H(boot))).status_code == 409
    await c.delete(f"{A}/policies/{tp['id']}", headers=H(boot))
    assert (await c.delete(f"{A}/teams/{team['id']}", headers=H(boot))).status_code == 204


# ──────────────────────────────────────────────────────────────────────
# Scopes
# ──────────────────────────────────────────────────────────────────────


async def test_scopes_viewer_and_ingest_keys(env, monkeypatch):
    c, boot, tid = env["c"], env["boot"], env["tid"]
    reader = await _bootstrap(c, tid, ["admin:read"])
    members = await _bootstrap(c, tid, ["admin:members"])
    assert (await c.get(A + "/policies", headers=H(reader))).status_code == 200
    assert (await c.get(A + "/policy/effective", headers=H(reader))).status_code == 200
    for tok in (reader, members):
        resp = await c.post(A + "/policies", headers=H(tok), json={"name": "p", "scope": "tenant"})
        assert resp.status_code == 403
    # Even with scoped tokens off, an ingest key never reaches policy writes.
    monkeypatch.setattr(settings, "admin_require_scoped_tokens", False)
    assert (await c.get(A + "/policies", headers=H(TENANT_KEY))).status_code == 200
    resp = await c.post(
        A + "/policies", headers=H(TENANT_KEY), json={"name": "p", "scope": "tenant"}
    )
    assert resp.status_code == 403
    # The gateway pull needs policy:read; an ingest:write key is refused.
    sdk = (
        await c.post(A + "/api-keys", headers=H(boot), json={"name": "sdk"})
    ).json()["key"]
    resp = await c.get("/api/v1/policy/effective", headers=H(sdk))
    assert resp.status_code == 403
    assert "policy:read" in resp.json()["detail"]["detail"]
    assert (await c.get("/api/v1/policy/effective", headers=H(boot))).status_code == 401
    # Not even the legacy key: policy:read is only ever issued explicitly.
    assert (await c.get("/api/v1/policy/effective", headers=H(TENANT_KEY))).status_code == 403


# ──────────────────────────────────────────────────────────────────────
# Effective policy and the gateway pull
# ──────────────────────────────────────────────────────────────────────


async def test_effective_bundle_inheritance_and_etag(env):
    c, boot = env["c"], env["boot"]
    key = await _policy_key(c, boot)
    empty = await c.get("/api/v1/policy/effective", headers=H(key))
    assert empty.status_code == 200
    assert empty.json() == {
        "schema": "ps-policy-bundle.v1",
        "tenant": {"version": None, "sections": {}},
        "teams": {},
        "projects": {},
    }
    etag = empty.headers["etag"]
    assert empty.headers["cache-control"] == "no-cache"
    resp = await c.get("/api/v1/policy/effective", headers=H(key, **{"If-None-Match": etag}))
    assert resp.status_code == 304 and resp.headers["etag"] == etag

    route = {"version": 1, "policy": ROUTE}
    await _post(
        c,
        boot,
        "/policies",
        {
            "name": "default",
            "scope": "tenant",
            "document": DOC(route=route, access=ACCESS(["*"], "allow")),
            "activate": True,
        },
    )
    eng = await _post(c, boot, "/teams", {"name": "eng"})
    ops = await _post(c, boot, "/teams", {"name": "ops"})
    app_ = await _post(c, boot, "/projects", {"name": "app", "team_id": eng["id"]})
    other = await _post(c, boot, "/projects", {"name": "other", "team_id": ops["id"]})
    await _post(
        c,
        boot,
        "/policies",
        {
            "name": "team-eng",
            "scope": "team",
            "team_id": eng["id"],
            "document": DOC(access=ACCESS(["gpt-4o-mini"])),
            "activate": True,
        },
    )
    proj = await _post(
        c,
        boot,
        "/policies",
        {
            "name": "proj-app",
            "scope": "project",
            "project_id": app_["id"],
            "document": DOC(access=ACCESS(["mistral-small"])),
        },
    )
    resp = await c.get("/api/v1/policy/effective", headers=H(key, **{"If-None-Match": etag}))
    assert resp.status_code == 200 and resp.headers["etag"] != etag
    bundle = resp.json()
    assert bundle["tenant"]["version"] == "default@1"
    assert bundle["tenant"]["sections"]["route"] == route
    team = bundle["teams"][eng["id"]]
    assert team["name"] == "eng" and team["version"] == "default@1+team-eng@1"
    # The team's section replaces the tenant's whole; the route is inherited.
    assert team["sections"]["access"]["groups"][0]["models"] == ["gpt-4o-mini"]
    assert team["sections"]["route"] == route
    assert ops["id"] not in bundle["teams"]
    assert other["id"] not in bundle["projects"]
    # The project policy is not active yet: the project gets its team's.
    assert bundle["projects"][app_["id"]]["version"] == "default@1+team-eng@1"
    await c.post(f"{A}/policies/{proj['id']}/activate", headers=H(boot), json={"version": 1})
    bundle = (await c.get("/api/v1/policy/effective", headers=H(key))).json()
    p = bundle["projects"][app_["id"]]
    assert p["version"] == "default@1+team-eng@1+proj-app@1"
    assert p["team_id"] == eng["id"]
    assert p["sections"]["access"]["groups"][0]["models"] == ["mistral-small"]
    # The admin preview is the same bundle with the same ETag.
    preview = await c.get(A + "/policy/effective", headers=H(boot))
    assert preview.json() == bundle
    assert preview.headers["etag"] == (
        await c.get("/api/v1/policy/effective", headers=H(key))
    ).headers["etag"]


# ──────────────────────────────────────────────────────────────────────
# Row-level security
# ──────────────────────────────────────────────────────────────────────


async def test_tenant_isolation_under_rls(env):
    c, boot, engine = env["c"], env["boot"], env["engine"]
    async with _admin(engine)() as s:
        b = Tenant(
            name="Other Corp",
            settings={},
            api_key_prefix=api_key_lookup_prefix(KEY_B),
            api_key_hash=hash_api_key(KEY_B),
        )
        s.add(b)
        await s.commit()
        b_id = b.id
    boot_b = await _bootstrap(c, b_id)
    team_a = await _post(c, boot, "/teams", {"name": "eng"})
    pa = await _post(
        c,
        boot,
        "/policies",
        {"name": "default", "scope": "tenant", "document": DOC(), "activate": True},
    )
    key_a = await _policy_key(c, boot)
    async with engine.begin() as conn:
        await rls.enable(conn)
    try:
        # Same name, same binding kind in another tenant is fine.
        pb = await _post(
            c,
            boot_b,
            "/policies",
            {
                "name": "default",
                "scope": "tenant",
                "document": DOC(access=ACCESS(["x"])),
                "activate": True,
            },
        )
        await _post(
            c, boot_b, f"/policies/{pb['id']}/versions", {"document": DOC(), "activate": True}
        )
        listed = (await c.get(A + "/policies", headers=H(boot_b))).json()["data"]
        assert [p["id"] for p in listed] == [pb["id"]]
        assert (await c.get(f"{A}/policies/{pa['id']}", headers=H(boot_b))).status_code == 404
        resp = await c.post(
            f"{A}/policies/{pa['id']}/activate", headers=H(boot_b), json={"version": None}
        )
        assert resp.status_code == 404
        # Binding to another tenant's team is refused.
        await _post(
            c,
            boot_b,
            "/policies",
            {"name": "t", "scope": "team", "team_id": team_a["id"]},
            404,
        )
        key_b = await _policy_key(c, boot_b)
        bundle_b = (await c.get("/api/v1/policy/effective", headers=H(key_b))).json()
        assert bundle_b["tenant"]["version"] == "default@2"
        bundle_a = (await c.get("/api/v1/policy/effective", headers=H(key_a))).json()
        assert bundle_a["tenant"] == {"version": "default@1", "sections": {}}
        async with async_sessionmaker(engine, class_=AsyncSession)() as s:
            await rls.set_tenant(s, b_id)
            rows = (await s.execute(select(PolicyVersion.tenant_id))).scalars().all()
            assert rows and set(rows) == {b_id}
    finally:
        async with engine.begin() as conn:
            await rls.disable(conn)
