"""SCIM 2.0 provisioning (#339) against a real PostgreSQL database
(PS_TEST_DATABASE_URL), with payloads from the RFC 7643 / RFC 7644 examples
and the shapes Okta and Microsoft Entra ID send.

Covers: SCIM token issuance (owner only), authentication and scopes (a SCIM
token reaches /scim/v2 only, admin tokens and ingest keys do not), Users
create / list with filter / get / replace / patch / delete, Groups create /
patch members / replace / delete, deactivation revoking the principal's
tokens, partner credentials and SSO sessions at once, group membership
driving team membership and (through idp_group_roles) the tenant-wide
role, owner protections, audit rows, and tenant isolation under RLS.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from collector import audit_trail, rls
from collector.admin_auth import ADMIN_SCOPES, issue_token
from collector.app import app
from collector.config import settings
from db.models import AdminToken, AuditEvent, Membership
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests.test_rbac_db import (
    OP_TOKEN,
    TENANT_KEY,
    A,
    H,
    _add_tenant_b,
    _admin,
    _bootstrap,
    _member,
    _partner_for,
    _post,
    _tenant_id,
    _token_for,
)

pytestmark = pytest.mark.scoped_admin_default

S = "/scim/v2"
SCIM_JSON = {"Content-Type": "application/scim+json"}
USER = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


def SH(token: str) -> dict:
    return {**H(token), **SCIM_JSON}


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", OP_TOKEN)
    audit_trail.auth_failure_limiter.reset()


async def _scim_token(c, boot, scopes=None, status=201) -> dict:
    body = {"name": f"idp-{uuid.uuid4().hex[:6]}"}
    if scopes is not None:
        body["scopes"] = scopes
    return await _post(c, boot, "/scim-tokens", body, status)


@pytest.fixture
async def env(seeded_db):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://t") as c:
        tid = await _tenant_id(seeded_db)
        boot = await _bootstrap(c, tid)
        scim = (await _scim_token(c, boot))["token"]
        yield {"c": c, "engine": seeded_db, "tid": tid, "boot": boot, "scim": scim}


def bjensen(**over) -> dict:
    """RFC 7643 §8.2 (full user representation), trimmed."""
    body = {
        "schemas": [USER],
        "externalId": "701984",
        "userName": "bjensen@example.com",
        "name": {
            "formatted": "Ms. Barbara J Jensen, III",
            "familyName": "Jensen",
            "givenName": "Barbara",
        },
        "displayName": "Babs Jensen",
        "emails": [
            {"value": "bjensen@example.com", "type": "work", "primary": True},
            {"value": "babs@jensen.org", "type": "home"},
        ],
        "active": True,
    }
    body.update(over)
    return body


async def _create_user(c, scim, **over) -> dict:
    resp = await c.post(f"{S}/Users", headers=SH(scim), json=bjensen(**over))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_group(c, scim, name="Tour Guides", members=()) -> dict:
    body = {"schemas": [GROUP], "displayName": name, "externalId": f"ext-{name}"}
    if members:
        body["members"] = [{"value": m} for m in members]
    resp = await c.post(f"{S}/Groups", headers=SH(scim), json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _patch(c, scim, path, ops, status=200):
    resp = await c.patch(path, headers=SH(scim), json={"schemas": [PATCH], "Operations": ops})
    assert resp.status_code == status, resp.text
    return resp.json() if resp.content else None


async def _audit(engine, tid, action=None) -> list[AuditEvent]:
    async with _admin(engine)() as s:
        stmt = select(AuditEvent).where(AuditEvent.tenant_id == tid)
        if action:
            stmt = stmt.where(AuditEvent.action == action)
        return list((await s.execute(stmt.order_by(AuditEvent.seq))).scalars())


async def _tenant_role(engine, pid) -> Membership | None:
    async with _admin(engine)() as s:
        return (
            await s.execute(
                select(Membership).where(
                    Membership.principal_id == uuid.UUID(pid), Membership.team_id.is_(None)
                )
            )
        ).scalar_one_or_none()


# ──────────────────────────────────────────────────────────────────────
# Tokens and authentication
# ──────────────────────────────────────────────────────────────────────


async def test_scim_tokens_need_an_owner_and_are_hashed(env):
    c, boot, engine, tid = env["c"], env["boot"], env["engine"], env["tid"]
    admin_pid = await _member(c, boot, "admin")
    admin_tok = (await _token_for(c, boot, admin_pid))["token"]
    await _scim_token(c, admin_tok, status=403)
    issued = await _scim_token(c, boot, scopes=["scim:groups"])
    assert issued["token"].startswith("ps-scim-") and issued["scopes"] == ["scim:groups"]
    listed = (await c.get(f"{A}/scim-tokens", headers=H(boot))).json()["data"]
    assert all("token" not in t for t in listed) and len(listed) == 2
    assert (await c.get(f"{A}/scim-tokens", headers=H(admin_tok))).status_code == 200
    await _scim_token(c, boot, scopes=["admin:read"], status=422)
    # Revoke: 204, the token stops working, a repeat is 204.
    resp = await c.delete(f"{A}/scim-tokens/{issued['id']}", headers=H(boot))
    assert resp.status_code == 204
    assert (await c.get(f"{S}/Groups", headers=H(issued["token"]))).status_code == 401
    assert (await c.delete(f"{A}/scim-tokens/{issued['id']}", headers=H(boot))).status_code == 204
    actions = [e.action for e in await _audit(engine, tid) if e.action.startswith("scim_token")]
    assert actions == ["scim_token.create", "scim_token.create", "scim_token.revoke"]


async def test_scim_auth_is_separate_from_other_credentials(env):
    c, boot, scim = env["c"], env["boot"], env["scim"]
    for headers in ({}, H("ps-scim-wrong"), H(boot), H(TENANT_KEY), H("garbage")):
        resp = await c.get(f"{S}/Users", headers=headers)
        assert resp.status_code == 401, headers
        body = resp.json()
        assert body["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
        assert body["status"] == "401"
        assert resp.headers["content-type"].startswith("application/scim+json")
    # A SCIM token does not reach the admin or partner API.
    assert (await c.get(f"{A}/principals", headers=H(scim))).status_code in (401, 403)
    assert (await c.get("/partner/v1/teams", headers=H(scim))).status_code == 401
    # Scopes: a groups-only token cannot touch users.
    groups_only = (await _scim_token(c, boot, scopes=["scim:groups"]))["token"]
    assert (await c.get(f"{S}/Users", headers=H(groups_only))).status_code == 403
    assert (await c.get(f"{S}/Groups", headers=H(groups_only))).status_code == 200


async def test_expired_scim_token_is_refused(env):
    from collector.scim import issue_token as issue_scim

    c, engine, tid = env["c"], env["engine"], env["tid"]
    async with _admin(engine)() as s:
        _, plaintext = await issue_scim(
            s,
            tid,
            name="old",
            scopes=["scim:users"],
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
            created_by="operator",
        )
        await s.commit()
    assert (await c.get(f"{S}/Users", headers=H(plaintext))).status_code == 401


async def test_service_provider_config(env):
    resp = await env["c"].get(f"{S}/ServiceProviderConfig", headers=H(env["scim"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["patch"]["supported"] is True and body["bulk"]["supported"] is False


# ──────────────────────────────────────────────────────────────────────
# Users
# ──────────────────────────────────────────────────────────────────────


async def test_user_lifecycle(env):
    c, scim, boot, engine, tid = env["c"], env["scim"], env["boot"], env["engine"], env["tid"]
    u = await _create_user(c, scim)
    assert u["userName"] == "bjensen@example.com" and u["externalId"] == "701984"
    assert u["displayName"] == "Babs Jensen" and u["active"] is True
    assert u["emails"] == [{"value": "bjensen@example.com", "primary": True, "type": "work"}]
    assert u["meta"]["resourceType"] == "User"
    assert u["meta"]["location"].endswith(f"/scim/v2/Users/{u['id']}")
    # The admin API sees the same principal.
    p = (await c.get(f"{A}/principals/{u['id']}", headers=H(boot))).json()
    assert p["kind"] == "user" and p["external_id"] == "701984"
    # Uniqueness: userName (case-insensitive) and externalId.
    for over in ({"userName": "BJENSEN@example.com", "externalId": "x"}, {"userName": "other"}):
        resp = await c.post(f"{S}/Users", headers=SH(scim), json=bjensen(**over))
        assert resp.status_code == 409 and resp.json()["scimType"] == "uniqueness"
    resp = await c.post(f"{S}/Users", headers=SH(scim), json={"schemas": [USER]})
    assert resp.status_code == 400 and resp.json()["scimType"] == "invalidValue"
    # List with filters (RFC 7644 §3.4.2.2), attribute names case-insensitive.
    await _create_user(c, scim, userName="jsmith", externalId="702000", emails=[])
    for f, n in (
        ('userName eq "bjensen@example.com"', 1),
        ('USERNAME eq "BJensen@Example.com"', 1),
        ('externalId eq "702000"', 1),
        ('emails.value eq "bjensen@example.com"', 1),
        (f'id eq "{u["id"]}"', 1),
        ('userName eq "nobody"', 0),
    ):
        body = (await c.get(f"{S}/Users", headers=H(scim), params={"filter": f})).json()
        assert body["totalResults"] == n, f
        assert body["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:ListResponse"]
    resp = await c.get(f"{S}/Users", headers=H(scim), params={"filter": 'title co "x"'})
    assert resp.status_code == 400 and resp.json()["scimType"] == "invalidFilter"
    page = (await c.get(f"{S}/Users", headers=H(scim), params={"startIndex": 2, "count": 1})).json()
    assert page["totalResults"] == 2 and page["itemsPerPage"] == 1 and page["startIndex"] == 2
    # PUT replaces.
    resp = await c.put(
        f"{S}/Users/{u['id']}",
        headers=SH(scim),
        json=bjensen(displayName="Barbara Jensen", emails=[{"value": "bj@example.com"}]),
    )
    assert resp.status_code == 200 and resp.json()["displayName"] == "Barbara Jensen"
    assert resp.json()["emails"][0]["value"] == "bj@example.com"
    # PATCH (Entra ID shape): replace userName and an emails filter path.
    out = await _patch(
        c,
        scim,
        f"{S}/Users/{u['id']}",
        [
            {"op": "Replace", "path": "userName", "value": "barbara@example.com"},
            {"op": "Add", "path": 'emails[type eq "work"].value', "value": "b@example.com"},
            {"op": "Replace", "path": "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User:manager", "value": "x"},
        ],
    )
    assert out["userName"] == "barbara@example.com" and out["emails"][0]["value"] == "b@example.com"
    assert (await c.get(f"{S}/Users/{uuid.uuid4()}", headers=H(scim))).status_code == 404
    assert (await c.get(f"{S}/Users/not-a-uuid", headers=H(scim))).status_code == 404
    # Service accounts are invisible to SCIM.
    sa = await _post(c, boot, "/principals", {"kind": "service_account"})
    assert (await c.get(f"{S}/Users/{sa['id']}", headers=H(scim))).status_code == 404
    # DELETE removes it.
    assert (await c.delete(f"{S}/Users/{u['id']}", headers=H(scim))).status_code == 204
    assert (await c.get(f"{S}/Users/{u['id']}", headers=H(scim))).status_code == 404
    actions = [e for e in await _audit(engine, tid) if e.actor_type == "scim_token"]
    assert {e.action for e in actions} >= {"principal.create", "principal.update", "principal.delete"}
    for e in actions:
        assert e.details["source"] == "scim"
        # Never an email, user name or display name in the audit trail.
        assert "example.com" not in str(e.details) and "Jensen" not in str(e.details)


@pytest.mark.parametrize(
    "ops",
    [
        # Microsoft Entra ID: string booleans.
        [{"op": "Replace", "path": "active", "value": "False"}],
        # Okta: no path, an object value.
        [{"op": "replace", "value": {"active": False}}],
    ],
    ids=["entra", "okta"],
)
async def test_deactivation_revokes_tokens_credentials_and_sessions(env, ops):
    c, scim, boot, engine, tid = env["c"], env["scim"], env["boot"], env["engine"], env["tid"]
    u = await _create_user(c, scim)
    await _post(c, boot, "/memberships", {"principal_id": u["id"], "role": "admin"})
    tok = (await _token_for(c, boot, u["id"], scopes=["admin:read", "admin:tokens"]))["token"]
    cred = await _partner_for(c, boot, u["id"], scopes=["members:read"])
    # An SSO session for the principal, and an unbound token it minted.
    async with _admin(engine)() as s:
        sess_row, session = await issue_token(
            s,
            tid,
            name="sso-x",
            scopes=["admin:read", "admin:tokens"],
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            created_by="sso",
            principal_id=uuid.UUID(u["id"]),
        )
        await s.commit()
        sess_id = sess_row.id
    child = await _post(c, session, "/tokens", {"name": "child", "scopes": ["admin:read"]})
    assert child["principal_id"] is None
    for t in (tok, session, child["token"]):
        assert (await c.get(f"{A}/principals", headers=H(t))).status_code == 200
    assert (await c.get("/partner/v1/teams", headers=H(cred["api_key"]))).status_code == 200

    out = await _patch(c, scim, f"{S}/Users/{u['id']}", ops)
    assert out["active"] is False
    # Effective on the very next request (well within 60 s).
    for t in (tok, session, child["token"]):
        assert (await c.get(f"{A}/principals", headers=H(t))).status_code == 401
    assert (await c.get("/partner/v1/teams", headers=H(cred["api_key"]))).status_code == 401
    async with _admin(engine)() as s:
        rows = {r.id: r for r in (await s.execute(select(AdminToken))).scalars()}
        assert rows[sess_id].revoked_at is not None
        assert rows[uuid.UUID(child["id"])].revoked_at is not None
    [disable] = await _audit(engine, tid, "principal.disable")
    assert disable.actor_type == "scim_token"
    assert str(sess_id) in disable.details["revoked_token_ids"]
    # Re-activating restores the (unrevoked) admin token and credential, not
    # the revoked session.
    out = await _patch(c, scim, f"{S}/Users/{u['id']}", [{"op": "replace", "path": "active", "value": True}])
    assert out["active"] is True
    assert (await c.get(f"{A}/principals", headers=H(tok))).status_code == 200
    assert (await c.get(f"{A}/principals", headers=H(session))).status_code == 401


async def test_owner_protections(env):
    c, scim, boot = env["c"], env["scim"], env["boot"]
    owner = await _create_user(c, scim)
    await _post(c, boot, "/memberships", {"principal_id": owner["id"], "role": "owner"})
    deactivate = [{"op": "replace", "path": "active", "value": False}]
    # The last enabled owner cannot be disabled.
    resp = await c.patch(
        f"{S}/Users/{owner['id']}", headers=SH(scim), json={"schemas": [PATCH], "Operations": deactivate}
    )
    assert resp.status_code == 409
    # With another owner it can be; but SCIM cannot re-enable, re-identify or delete it.
    second = await _create_user(c, scim, userName="second", externalId="2")
    await _post(c, boot, "/memberships", {"principal_id": second["id"], "role": "owner"})
    await _patch(c, scim, f"{S}/Users/{owner['id']}", deactivate)
    await _patch(
        c, scim, f"{S}/Users/{owner['id']}", [{"op": "replace", "path": "active", "value": True}], 403
    )
    await _patch(
        c, scim, f"{S}/Users/{second['id']}", [{"op": "replace", "path": "externalId", "value": "evil"}], 403
    )
    assert (await c.delete(f"{S}/Users/{second['id']}", headers=H(scim))).status_code == 403
    # Roles in a SCIM payload are ignored: nothing grants owner.
    u = await _create_user(c, scim, userName="r", externalId="r", roles=[{"value": "owner"}])
    p = (await c.get(f"{A}/principals/{u['id']}", headers=H(boot))).json()
    assert p["memberships"] == []


# ──────────────────────────────────────────────────────────────────────
# Groups
# ──────────────────────────────────────────────────────────────────────


async def test_group_membership_drives_team_and_tenant_roles(env):
    c, scim, boot, engine, tid = env["c"], env["scim"], env["boot"], env["engine"], env["tid"]
    babs = await _create_user(c, scim)
    mandy = await _create_user(c, scim, userName="mandy", externalId="m")
    g = await _create_group(c, scim, members=[babs["id"]])
    assert g["displayName"] == "Tour Guides" and [m["value"] for m in g["members"]] == [babs["id"]]
    team = (await c.get(f"{A}/teams/{g['id']}", headers=H(boot))).json()
    assert team["name"] == "Tour Guides"
    # Default group role: a viewer team membership, no tenant role.
    p = (await c.get(f"{A}/principals/{babs['id']}", headers=H(boot))).json()
    assert [(m["team_id"], m["role"], m["created_by"]) for m in p["memberships"]] == [
        (g["id"], "viewer", "scim")
    ]
    assert await _tenant_role(engine, babs["id"]) is None
    # The group carries the developer tenant role and member role.
    resp = await c.put(
        f"{A}/idp-group-roles/{g['id']}",
        headers=H(boot),
        json={"member_role": "developer", "tenant_role": "developer"},
    )
    assert resp.status_code == 200, resp.text
    assert (await _tenant_role(engine, babs["id"])).role == "developer"
    # RFC 7644 §3.5.2.1: add a member.
    await _patch(
        c,
        scim,
        f"{S}/Groups/{g['id']}",
        [{"op": "add", "path": "members", "value": [{"display": "Mandy", "value": mandy["id"]}]}],
    )
    assert (await _tenant_role(engine, mandy["id"])).role == "developer"
    user = (await c.get(f"{S}/Users/{mandy['id']}", headers=H(scim))).json()
    assert user["groups"] == [{"value": g["id"], "display": "Tour Guides", "type": "direct"}]
    # A second group with a higher tenant role wins; leaving it falls back.
    admins = await _create_group(c, scim, name="Admins", members=[mandy["id"]])
    await c.put(f"{A}/idp-group-roles/{admins['id']}", headers=H(boot), json={"tenant_role": "admin"})
    assert (await _tenant_role(engine, mandy["id"])).role == "admin"
    # RFC 7644 §3.5.2.2: remove a member by filter.
    await _patch(
        c, scim, f"{S}/Groups/{admins['id']}", [{"op": "remove", "path": f'members[value eq "{mandy["id"]}"]'}]
    )
    assert (await _tenant_role(engine, mandy["id"])).role == "developer"
    # Entra ID: remove with a value list.
    await _patch(
        c,
        scim,
        f"{S}/Groups/{g['id']}",
        [{"op": "Remove", "path": "members", "value": [{"value": mandy["id"]}]}],
    )
    assert await _tenant_role(engine, mandy["id"]) is None
    # RFC 7644 §3.5.2.3: replace members (and the display name, no path).
    out = await _patch(
        c,
        scim,
        f"{S}/Groups/{g['id']}",
        [
            {"op": "replace", "path": "members", "value": [{"value": mandy["id"]}]},
            {"op": "replace", "value": {"displayName": "Guides"}},
        ],
    )
    assert out["displayName"] == "Guides" and [m["value"] for m in out["members"]] == [mandy["id"]]
    assert await _tenant_role(engine, babs["id"]) is None
    # Filter, excludedAttributes, PUT.
    body = (
        await c.get(
            f"{S}/Groups",
            headers=H(scim),
            params={"filter": 'displayName eq "Guides"', "excludedAttributes": "members"},
        )
    ).json()
    assert body["totalResults"] == 1 and "members" not in body["Resources"][0]
    resp = await c.put(
        f"{S}/Groups/{g['id']}",
        headers=SH(scim),
        json={"schemas": [GROUP], "displayName": "Guides", "members": [{"value": babs["id"]}]},
    )
    assert [m["value"] for m in resp.json()["members"]] == [babs["id"]]
    assert (await _tenant_role(engine, babs["id"])).role == "developer"
    # Resetting the group's roles applies at once.
    assert (await c.delete(f"{A}/idp-group-roles/{g['id']}", headers=H(boot))).status_code == 204
    assert await _tenant_role(engine, babs["id"]) is None
    p = (await c.get(f"{A}/principals/{babs['id']}", headers=H(boot))).json()
    assert [m["role"] for m in p["memberships"]] == ["viewer"]
    # Deleting the group removes its memberships.
    assert (await c.delete(f"{S}/Groups/{g['id']}", headers=H(scim))).status_code == 204
    assert (await c.get(f"{S}/Groups/{g['id']}", headers=H(scim))).status_code == 404
    assert (await c.get(f"{A}/principals/{babs['id']}", headers=H(boot))).json()["memberships"] == []
    scim_actions = {e.action for e in await _audit(engine, tid) if e.actor_type == "scim_token"}
    assert scim_actions >= {
        "team.create",
        "team.update",
        "team.delete",
        "membership.add",
        "membership.remove",
        "role.change",
    }


async def test_manual_tenant_roles_are_never_touched_by_sync(env):
    c, scim, boot, engine = env["c"], env["scim"], env["boot"], env["engine"]
    u = await _create_user(c, scim)
    await _post(c, boot, "/memberships", {"principal_id": u["id"], "role": "auditor"})
    g = await _create_group(c, scim)
    await c.put(f"{A}/idp-group-roles/{g['id']}", headers=H(boot), json={"tenant_role": "admin"})
    await _patch(c, scim, f"{S}/Groups/{g['id']}", [{"op": "add", "path": "members", "value": [{"value": u["id"]}]}])
    m = await _tenant_role(engine, u["id"])
    assert m.role == "auditor" and m.created_by != "scim"
    await _patch(c, scim, f"{S}/Groups/{g['id']}", [{"op": "remove", "path": "members"}])
    assert (await _tenant_role(engine, u["id"])).role == "auditor"


async def test_group_roles_never_owner_and_need_the_role_scopes(env):
    c, scim, boot = env["c"], env["scim"], env["boot"]
    g = await _create_group(c, scim)
    for body in ({"tenant_role": "owner"}, {"member_role": "owner"}, {"tenant_role": "root"}):
        resp = await c.put(f"{A}/idp-group-roles/{g['id']}", headers=H(boot), json=body)
        assert resp.status_code == 422, body
    # A token with admin:members only cannot map a group to developer
    # (it does not hold the role's admin scopes).
    narrow = (await _post(c, boot, "/tokens", {"name": "n", "scopes": ["admin:members"]}))["token"]
    resp = await c.put(f"{A}/idp-group-roles/{g['id']}", headers=H(narrow), json={"tenant_role": "developer"})
    assert resp.status_code == 403
    resp = await c.put(f"{A}/idp-group-roles/{uuid.uuid4()}", headers=H(boot), json={})
    assert resp.status_code == 404
    listed = (await c.get(f"{A}/idp-group-roles", headers=H(boot))).json()["data"]
    assert listed == []


async def test_group_errors(env):
    c, scim, boot = env["c"], env["scim"], env["boot"]
    g = await _create_group(c, scim)
    resp = await c.post(f"{S}/Groups", headers=SH(scim), json={"schemas": [GROUP], "displayName": "Tour Guides"})
    assert resp.status_code == 409 and resp.json()["scimType"] == "uniqueness"
    resp = await c.post(
        f"{S}/Groups", headers=SH(scim), json={"schemas": [GROUP], "displayName": "x", "members": [{"value": str(uuid.uuid4())}]}
    )
    assert resp.status_code == 400
    await _patch(c, scim, f"{S}/Groups/{g['id']}", [{"op": "add", "path": "owner", "value": "x"}], 400)
    resp = await c.patch(f"{S}/Groups/{g['id']}", headers=SH(scim), json={"Operations": []})
    assert resp.status_code == 400
    # A team with projects cannot be deleted.
    await _post(c, boot, "/projects", {"name": "p1", "team_id": g["id"]})
    assert (await c.delete(f"{S}/Groups/{g['id']}", headers=H(scim))).status_code == 409


async def test_tenants_are_isolated_under_rls(env):
    c, scim, boot, engine = env["c"], env["scim"], env["boot"], env["engine"]
    b = await _add_tenant_b(engine)
    boot_b = await _bootstrap(c, b)
    scim_b = (await _scim_token(c, boot_b))["token"]
    u = await _create_user(c, scim)
    g = await _create_group(c, scim, members=[u["id"]])
    async with engine.begin() as conn:
        await rls.enable(conn)
    try:
        # B can reuse A's userName, externalId and group name.
        u_b = await _create_user(c, scim_b)
        g_b = await _create_group(c, scim_b)
        for path in ("Users", "Groups"):
            ids = {r["id"] for r in (await c.get(f"{S}/{path}", headers=H(scim_b))).json()["Resources"]}
            assert ids == {u_b["id"] if path == "Users" else g_b["id"]}
        assert (await c.get(f"{S}/Users/{u['id']}", headers=H(scim_b))).status_code == 404
        assert (await c.get(f"{S}/Groups/{g['id']}", headers=H(scim_b))).status_code == 404
        await _patch(c, scim_b, f"{S}/Users/{u['id']}", [{"op": "replace", "path": "active", "value": False}], 404)
        assert (await c.delete(f"{S}/Groups/{g['id']}", headers=H(scim_b))).status_code == 404
        # Nor put A's user in B's group.
        await _patch(
            c, scim_b, f"{S}/Groups/{g_b['id']}", [{"op": "add", "path": "members", "value": [{"value": u["id"]}]}], 400
        )
        # A is untouched.
        assert (await c.get(f"{S}/Users/{u['id']}", headers=H(scim))).json()["active"] is True
        assert (await c.get(f"{A}/scim-tokens", headers=H(boot_b))).json()["data"][0]["id"]
        ids_a = {t["id"] for t in (await c.get(f"{A}/scim-tokens", headers=H(boot))).json()["data"]}
        ids_b = {t["id"] for t in (await c.get(f"{A}/scim-tokens", headers=H(boot_b))).json()["data"]}
        assert not ids_a & ids_b
    finally:
        async with engine.begin() as conn:
            await rls.disable(conn)


def test_scim_token_scopes_are_documented():
    from collector.scim import SCIM_SCOPES

    assert set(SCIM_SCOPES) == {"scim:users", "scim:groups"}
    assert ADMIN_SCOPES  # admin scopes unchanged by SCIM
