"""Admin API SSO (#339) end to end against a fake IdP and a real PostgreSQL
database (PS_TEST_DATABASE_URL).

Covers: owner-only configuration, the login redirect (PKCE S256, state,
nonce, browser binding cookie), the callback (single-use state, cookie
check, code exchange with the verifier and, for a confidential client, the
sealed secret), mapping the ID token subject to a principal, the session
token's scopes coming from the principal's roles and its lifetime, refusals
(unknown subject, disabled principal, no admin role, bad nonce, replay,
another browser) with audit rows, and tenant isolation under RLS.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import yaml
from collector import audit_trail, oidc, rls
from collector.admin_auth import ADMIN_SCOPES
from collector.app import app
from collector.config import settings
from db.models import AdminToken, AuditEvent, OidcLoginState, TenantOidcConfig
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests.fake_idp import CLIENT_ID, ISSUER, FakeIdp
from tests.test_rbac_db import (
    OP_TOKEN,
    A,
    H,
    _add_tenant_b,
    _admin,
    _bootstrap,
    _member,
    _post,
    _tenant_id,
)

pytestmark = pytest.mark.scoped_admin_default

REDIRECT = "https://collector.example.com/api/v1/sso/callback"


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", OP_TOKEN)
    audit_trail.auth_failure_limiter.reset()


@pytest.fixture
def idp(monkeypatch):
    fake = FakeIdp()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
async def env(seeded_db, idp):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://t") as c:
        tid = await _tenant_id(seeded_db)
        boot = await _bootstrap(c, tid)
        yield {"c": c, "engine": seeded_db, "tid": tid, "boot": boot, "idp": idp}


def _cfg(**over) -> dict:
    body = {"issuer": ISSUER, "client_id": CLIENT_ID, "redirect_uri": REDIRECT}
    body.update(over)
    return body


async def _configure(c, token, **over) -> dict:
    resp = await c.put(f"{A}/sso/oidc", headers=H(token), json=_cfg(**over))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _user(c, boot, role: str | None, external_id: str) -> str:
    p = await _post(c, boot, "/principals", {"kind": "user", "external_id": external_id})
    if role:
        await _post(c, boot, "/memberships", {"principal_id": p["id"], "role": role})
    return p["id"]


async def _login(c, tid, idp) -> tuple[dict, str]:
    """Start a login; returns (authorize params, state)."""
    resp = await c.get(f"/api/v1/sso/{tid}/login")
    assert resp.status_code == 302, resp.text
    params = idp.capture_authorize(resp.headers["location"])
    cookie = resp.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "samesite=lax" in cookie.lower()
    assert "Path=/api/v1/sso" in cookie
    return params, params["state"]


async def _callback(c, state, code="good-code", **kw):
    return await c.get("/api/v1/sso/callback", params={"state": state, "code": code}, **kw)


async def _audit(engine, tid, action: str) -> list[AuditEvent]:
    async with _admin(engine)() as s:
        return list(
            (
                await s.execute(
                    select(AuditEvent)
                    .where(AuditEvent.tenant_id == tid, AuditEvent.action == action)
                    .order_by(AuditEvent.seq)
                )
            ).scalars()
        )


async def test_full_login_issues_a_session_bound_to_the_principal(env):
    c, boot, tid, idp, engine = env["c"], env["boot"], env["tid"], env["idp"], env["engine"]
    cfg = await _configure(c, boot, session_ttl_seconds=900)
    assert cfg["client_secret_set"] is False
    assert cfg["algorithms"] == list(oidc.DEFAULT_ALGORITHMS)
    assert cfg["login_path"] == f"/api/v1/sso/{tid}/login"
    pid = await _user(c, boot, "viewer", "user-1")
    params, state = await _login(c, tid, idp)
    assert params["response_type"] == "code"
    assert params["client_id"] == CLIENT_ID
    assert params["redirect_uri"] == REDIRECT
    assert params["code_challenge_method"] == "S256"
    assert "openid" in params["scope"].split()
    assert params["nonce"] and params["state"]
    resp = await _callback(c, state)
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    spec = yaml.safe_load((Path(__file__).resolve().parents[3] / "docs" / "openapi.yaml").read_text())
    assert set(body) == set(spec["components"]["schemas"]["SsoSession"]["required"])
    assert body["principal_id"] == pid
    assert body["roles"] == ["viewer"]
    assert body["scopes"] == ["admin:read"]  # the viewer role's admin scopes
    assert body["expires_in"] == 900
    # A public client: client_id in the form, the verifier matched (the fake
    # IdP refuses a wrong one), no client secret.
    sent = idp.token_requests[-1]
    assert sent["client_id"] == CLIENT_ID and sent["code_verifier"] and sent["_authorization"] is None
    # The session works for reads, not writes.
    session = body["token"]
    assert (await c.get(f"{A}/principals", headers=H(session))).status_code == 200
    resp = await c.post(f"{A}/teams", headers=H(session), json={"name": "x"})
    assert resp.status_code == 403
    # Stored as an admin token issued for the principal, created by sso.
    async with _admin(engine)() as s:
        row = (await s.execute(select(AdminToken).where(AdminToken.id == uuid.UUID(body["token_id"])))).scalar_one()
        assert row.principal_id == uuid.UUID(pid) and row.created_by == "sso"
        assert 890 <= (row.expires_at - row.created_at).total_seconds() <= 900
        assert (await s.execute(select(OidcLoginState))).first() is None  # used up
    [login] = await _audit(engine, tid, "sso.login")
    assert login.outcome == "success" and login.actor_type == "principal"
    assert login.actor_id == pid and login.details["scopes"] == ["admin:read"]
    # Demoting the principal narrows the live session at once.
    resp = await c.get(f"{A}/principals/{pid}", headers=H(boot))
    m = resp.json()["memberships"][0]
    await c.patch(f"{A}/memberships/{m['id']}", headers=H(boot), json={"role": "auditor"})
    assert (await c.get(f"{A}/principals", headers=H(session))).status_code == 403


async def test_admin_role_gets_admin_scopes_and_owner_can_manage_owners(env):
    c, boot, tid, idp = env["c"], env["boot"], env["tid"], env["idp"]
    await _configure(c, boot)
    await _user(c, boot, "owner", "user-1")
    _, state = await _login(c, tid, idp)
    body = (await _callback(c, state)).json()
    assert body["scopes"] == list(ADMIN_SCOPES)
    # An owner's session can grant the owner role.
    other = await _user(c, boot, None, "user-2")
    await _post(c, body["token"], "/memberships", {"principal_id": other, "role": "owner"})


async def test_subject_claim_is_configurable(env):
    c, boot, tid, idp = env["c"], env["boot"], env["tid"], env["idp"]
    await _configure(c, boot, subject_claim="oid")
    await _user(c, boot, "admin", "object-id-7")
    idp.id_token_claims = {"oid": "object-id-7", "sub": "pairwise-xyz"}
    _, state = await _login(c, tid, idp)
    assert (await _callback(c, state)).status_code == 200


@pytest.mark.parametrize(
    ("setup", "status", "reason"),
    [
        ("unknown", 403, "unknown_principal"),
        ("disabled", 403, "principal_disabled"),
        ("no_role", 403, "no_admin_roles"),
        ("service_account", 403, "unknown_principal"),
        ("bad_nonce", 401, "invalid_id_token"),
        ("wrong_aud", 401, "invalid_id_token"),
        ("bad_code", 502, "token_exchange_failed"),
    ],
)
async def test_refusals_are_audited(env, setup, status, reason):
    c, boot, tid, idp, engine = env["c"], env["boot"], env["tid"], env["idp"], env["engine"]
    await _configure(c, boot)
    code = "good-code"
    if setup == "disabled":
        pid = await _user(c, boot, "admin", "user-1")
        await c.patch(f"{A}/principals/{pid}", headers=H(boot), json={"disabled": True})
    elif setup == "no_role":
        await _user(c, boot, "auditor", "user-1")  # audit:read only: no admin scope
    elif setup == "service_account":
        p = await _post(c, boot, "/principals", {"kind": "service_account", "external_id": "user-1"})
        await _post(c, boot, "/memberships", {"principal_id": p["id"], "role": "admin"})
    else:
        await _user(c, boot, "admin", "user-1" if setup != "unknown" else "someone-else")
    _, state = await _login(c, tid, idp)
    if setup == "bad_nonce":
        idp.nonce = "not-the-nonce"
    elif setup == "wrong_aud":
        idp.id_token_claims = {"aud": "another-client"}
    elif setup == "bad_code":
        code = "stolen-code"
    resp = await _callback(c, state, code=code)
    assert resp.status_code == status, resp.text
    assert "token" not in resp.json()
    [row] = await _audit(engine, tid, "sso.login")
    assert row.outcome == "failure" and row.details["reason"] == reason
    async with _admin(engine)() as s:
        assert (await s.execute(select(AdminToken).where(AdminToken.created_by == "sso"))).first() is None


async def test_state_is_single_use_and_bound_to_the_browser(env):
    c, boot, tid, idp = env["c"], env["boot"], env["tid"], env["idp"]
    await _configure(c, boot)
    await _user(c, boot, "admin", "user-1")
    _, state = await _login(c, tid, idp)
    # Another browser (no cookie) cannot finish this login, and spends it.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://t") as other:
        resp = await _callback(other, state)
        assert resp.status_code == 400
    assert (await _callback(c, state)).status_code == 400
    # A fresh login works once, then the state is gone.
    _, state = await _login(c, tid, idp)
    assert (await _callback(c, state)).status_code == 200
    assert (await _callback(c, state)).status_code == 400
    assert (await _callback(c, "made-up")).status_code == 400
    assert (await c.get("/api/v1/sso/callback")).status_code == 400


async def test_idp_error_and_missing_config(env):
    c, boot, tid, idp = env["c"], env["boot"], env["tid"], env["idp"]
    assert (await c.get(f"/api/v1/sso/{tid}/login")).status_code == 404
    assert (await c.get(f"/api/v1/sso/{uuid.uuid4()}/login")).status_code == 404
    await _configure(c, boot, enabled=False)
    assert (await c.get(f"/api/v1/sso/{tid}/login")).status_code == 404
    await _configure(c, boot)
    _, state = await _login(c, tid, idp)
    resp = await c.get("/api/v1/sso/callback", params={"state": state, "error": "access_denied"})
    assert resp.status_code == 400
    idp.discovery_overrides = {"issuer": "https://evil.example.com"}
    oidc.cache.clear()
    assert (await c.get(f"/api/v1/sso/{tid}/login")).status_code == 502


async def test_confidential_client_secret_is_sealed_and_sent(env, monkeypatch):
    c, boot, tid, idp, engine = env["c"], env["boot"], env["tid"], env["idp"], env["engine"]
    resp = await c.put(f"{A}/sso/oidc", headers=H(boot), json=_cfg(client_secret="s3cret-value"))
    assert resp.status_code == 400  # no keyring
    monkeypatch.setattr(settings, "webhook_secret_keys", "k1:" + "x" * 32)
    cfg = await _configure(c, boot, client_secret="s3cret-value")
    assert cfg["client_secret_set"] is True and "client_secret" not in cfg
    async with _admin(engine)() as s:
        row = (await s.execute(select(TenantOidcConfig))).scalar_one()
        assert "s3cret" not in row.client_secret_ciphertext
    # Omitted on a later PUT: kept.
    cfg = await _configure(c, boot, session_ttl_seconds=600)
    assert cfg["client_secret_set"] is True
    await _user(c, boot, "admin", "user-1")
    _, state = await _login(c, tid, idp)
    assert (await _callback(c, state)).status_code == 200
    sent = idp.token_requests[-1]
    assert sent["_authorization"].startswith("Basic ") and "client_id" not in sent
    [update, *_] = await _audit(engine, tid, "sso.oidc.update")
    assert "s3cret" not in str(update.details)
    # null clears it.
    cfg = await _configure(c, boot, client_secret=None)
    assert cfg["client_secret_set"] is False


async def test_configuration_needs_an_owner_and_valid_values(env):
    c, boot, engine = env["c"], env["boot"], env["engine"]
    admin_pid = await _member(c, boot, "admin")
    admin_tok = (
        await _post(
            c,
            boot,
            "/tokens",
            {"name": "adm", "scopes": list(ADMIN_SCOPES), "principal_id": admin_pid},
        )
    )["token"]
    resp = await c.put(f"{A}/sso/oidc", headers=H(admin_tok), json=_cfg())
    assert resp.status_code == 403
    await _configure(c, boot)
    assert (await c.get(f"{A}/sso/oidc", headers=H(admin_tok))).status_code == 200
    assert (await c.delete(f"{A}/sso/oidc", headers=H(admin_tok))).status_code == 403
    for bad in (
        {"issuer": "http://idp.example.com"},
        {"redirect_uri": "http://collector/cb"},
        {"algorithms": ["HS256"]},
        {"algorithms": ["none"]},
        {"session_ttl_seconds": 60},
        {"session_ttl_seconds": 99999},
        {"subject_claim": "a b"},
        {"unknown": 1},
    ):
        resp = await c.put(f"{A}/sso/oidc", headers=H(boot), json=_cfg(**bad))
        assert resp.status_code in (400, 422), bad
    assert (await c.delete(f"{A}/sso/oidc", headers=H(boot))).status_code == 204
    assert (await c.get(f"{A}/sso/oidc", headers=H(boot))).status_code == 404
    tid = env["tid"]
    assert [e.action for e in await _audit(engine, tid, "sso.oidc.delete")] == ["sso.oidc.delete"]


async def test_pending_logins_are_capped(env, monkeypatch):
    c, boot, tid = env["c"], env["boot"], env["tid"]
    from collector import sso

    monkeypatch.setattr(sso, "MAX_PENDING_LOGINS", 3)
    await _configure(c, boot)
    for _ in range(3):
        assert (await c.get(f"/api/v1/sso/{tid}/login")).status_code == 302
    assert (await c.get(f"/api/v1/sso/{tid}/login")).status_code == 429


async def test_tenants_are_isolated_under_rls(env):
    c, boot, tid, idp, engine = env["c"], env["boot"], env["tid"], env["idp"], env["engine"]
    b = await _add_tenant_b(engine)
    boot_b = await _bootstrap(c, b)
    await _configure(c, boot)
    await _user(c, boot, "admin", "user-1")
    async with engine.begin() as conn:
        await rls.enable(conn)
    try:
        # Tenant B sees no SSO config, and its same-subject principal is its own.
        assert (await c.get(f"{A}/sso/oidc", headers=H(boot_b))).status_code == 404
        pid_b = await _user(c, boot_b, "viewer", "user-1")
        await _configure(c, boot_b)
        _, state = await _login(c, b, idp)
        body = (await _callback(c, state)).json()
        assert body["principal_id"] == pid_b and body["scopes"] == ["admin:read"]
        # A's login still maps to A's principal.
        _, state = await _login(c, tid, idp)
        body_a = (await _callback(c, state)).json()
        assert body_a["principal_id"] != pid_b and body_a["roles"] == ["admin"]
        # B's session cannot read A's principals.
        ids = {p["id"] for p in (await c.get(f"{A}/principals", headers=H(body["token"]))).json()["data"]}
        assert body_a["principal_id"] not in ids
    finally:
        async with engine.begin() as conn:
            await rls.disable(conn)
