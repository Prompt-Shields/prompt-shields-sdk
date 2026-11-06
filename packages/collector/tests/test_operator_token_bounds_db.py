"""Operator tokens are bounded by the caller's own token (#364), against Postgres.

A named operator token holding ``operator:tokens`` cannot outlive or
disrupt a broader token: a token it issues or rotates expires no later than
itself (an omitted or later expiry is capped, as for tenant admin tokens),
and it cannot rotate or revoke a token holding a scope it lacks (403, with
an ``outcome=failure`` audit row). PS_ADMIN_TOKEN (break-glass) is not
bounded.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from collector import audit_trail, rls
from collector.app import app
from collector.config import settings
from db.audit_chain import SYSTEM_TENANT_ID
from db.models import AuditEvent, OperatorToken
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

OP_TOKEN = "op-secret-token"
TOKENS = "/api/v1/operator/tokens"


def H(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


OP = H(OP_TOKEN)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", OP_TOKEN)
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


async def _system_rows(engine, prefix: str = "operator_token.") -> list[AuditEvent]:
    async with _admin(engine)() as s:
        rows = (
            await s.execute(
                select(AuditEvent)
                .where(AuditEvent.tenant_id == SYSTEM_TENANT_ID)
                .order_by(AuditEvent.seq)
            )
        ).scalars()
        return [r for r in rows if r.action.startswith(prefix)]


async def _issue(c, scopes, *, headers=OP, expires_at=None, name=None):
    body = {"name": name or f"op-{uuid.uuid4().hex[:8]}", "scopes": scopes}
    if expires_at is not None:
        body["expires_at"] = expires_at.isoformat()
    return await c.post(TOKENS, headers=headers, json=body)


async def _token(c, scopes, **kw) -> dict:
    resp = await _issue(c, scopes, **kw)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


async def test_issued_token_expires_no_later_than_the_caller(client, seeded_db):
    caller_exp = datetime.now(UTC) + timedelta(hours=1)
    minter = await _token(
        client, ["operator:tokens", "operator:read"], expires_at=caller_exp
    )
    caller_exp = _ts(minter["expires_at"])
    m = H(minter["token"])

    # Omitted (never-expiring) and later expiries are capped to the caller's.
    unbounded = await _token(client, ["operator:read"], headers=m)
    assert _ts(unbounded["expires_at"]) == caller_exp
    later = await _token(
        client,
        ["operator:read"],
        headers=m,
        expires_at=caller_exp + timedelta(days=30),
    )
    assert _ts(later["expires_at"]) == caller_exp
    # A shorter expiry is kept as asked.
    shorter_exp = caller_exp - timedelta(minutes=10)
    shorter = await _token(client, ["operator:read"], headers=m, expires_at=shorter_exp)
    assert _ts(shorter["expires_at"]) == shorter_exp

    issued = {
        r.details["name"]: r
        for r in await _system_rows(seeded_db)
        if r.action == "operator_token.issue"
    }
    assert issued[unbounded["name"]].details.get("expiry_capped") is True
    assert issued[later["name"]].details.get("expiry_capped") is True
    assert "expiry_capped" not in issued[shorter["name"]].details
    assert issued[shorter["name"]].actor_id == minter["name"]


async def test_break_glass_and_non_expiring_callers_are_not_bounded(client):
    # PS_ADMIN_TOKEN issues a token that never expires.
    forever = await _token(client, ["operator:tokens", "operator:read"])
    assert forever["expires_at"] is None
    # A named token without an expiry may do the same.
    child = await _token(client, ["operator:read"], headers=H(forever["token"]))
    assert child["expires_at"] is None
    far = datetime.now(UTC) + timedelta(days=365)
    grandchild = await _token(
        client, ["operator:read"], headers=H(forever["token"]), expires_at=far
    )
    assert _ts(grandchild["expires_at"]) == far


async def test_rotation_caps_the_expiry_to_the_caller(client, seeded_db):
    caller = await _token(
        client,
        ["operator:tokens", "operator:read"],
        expires_at=datetime.now(UTC) + timedelta(hours=2),
    )
    caller_exp = _ts(caller["expires_at"])
    # Issued by break-glass: never expires.
    target = await _token(client, ["operator:read"])
    assert target["expires_at"] is None

    resp = await client.post(f"{TOKENS}/{target['id']}/rotate", headers=H(caller["token"]))
    assert resp.status_code == 200, resp.text
    assert _ts(resp.json()["expires_at"]) == caller_exp
    async with _admin(seeded_db)() as s:
        stored = (
            await s.execute(
                select(OperatorToken.expires_at).where(
                    OperatorToken.id == uuid.UUID(target["id"])
                )
            )
        ).scalar_one()
    assert stored == caller_exp

    # A token that already expires sooner keeps its expiry.
    sooner = await _token(
        client,
        ["operator:read"],
        expires_at=caller_exp - timedelta(minutes=30),
    )
    resp = await client.post(f"{TOKENS}/{sooner['id']}/rotate", headers=H(caller["token"]))
    assert resp.status_code == 200, resp.text
    assert resp.json()["expires_at"] == sooner["expires_at"]

    rotated = {
        r.target_id: r
        for r in await _system_rows(seeded_db)
        if r.action == "operator_token.rotate"
    }
    assert rotated[target["id"]].details.get("expiry_capped") is True
    assert "expiry_capped" not in rotated[sooner["id"]].details

    # Break-glass rotation leaves the expiry alone (unbounded).
    forever = await _token(client, ["operator:read"])
    resp = await client.post(f"{TOKENS}/{forever['id']}/rotate", headers=OP)
    assert resp.status_code == 200 and resp.json()["expires_at"] is None


async def test_revoke_and_rotate_need_every_scope_of_the_target(client, seeded_db):
    narrow = await _token(client, ["operator:tokens", "operator:read"])
    n = H(narrow["token"])
    broad = await _token(client, ["operator:tokens", "operator:erase", "operator:read"])
    path = f"{TOKENS}/{broad['id']}"

    resp = await client.delete(path, headers=n)
    assert resp.status_code == 403
    assert "operator:erase" in resp.json()["detail"]["detail"]
    resp = await client.post(path + "/rotate", headers=n)
    assert resp.status_code == 403
    # The broad token still works, with its original secret.
    got = await client.get("/api/v1/operator/tenants", headers=H(broad["token"]))
    assert got.status_code == 200
    assert (await client.get(path, headers=OP)).json()["status"] == "active"

    # A token whose scopes the caller holds can be rotated and revoked.
    peer = await _token(client, ["operator:read"], headers=n)
    peer_path = f"{TOKENS}/{peer['id']}"
    assert (await client.post(peer_path + "/rotate", headers=n)).status_code == 200
    assert (await client.delete(peer_path, headers=n)).status_code == 204
    # The broader token can revoke the narrower one; break-glass anything.
    assert (
        await client.delete(f"{TOKENS}/{narrow['id']}", headers=H(broad["token"]))
    ).status_code == 204
    assert (await client.delete(path, headers=OP)).status_code == 204

    refused = [
        r for r in await _system_rows(seeded_db) if r.outcome == audit_trail.FAILURE
    ]
    by_action = {r.action: r for r in refused}
    assert set(by_action) == {"operator_token.revoke", "operator_token.rotate"}
    for row in refused:
        assert row.status_code == 403
        assert row.actor_type == "operator_token" and row.actor_id == narrow["name"]
        assert row.target_id == broad["id"]
        assert row.details["name"] == broad["name"]
        assert row.details["reason"] == "scope_not_held"
        assert row.details["scopes"] == ["operator:erase"]
    revoked = [
        r
        for r in await _system_rows(seeded_db)
        if r.action == "operator_token.revoke" and r.outcome == audit_trail.SUCCESS
    ]
    assert {r.target_id for r in revoked} == {peer["id"], narrow["id"], broad["id"]}


async def test_refused_grant_is_audited(client, seeded_db):
    minter = await _token(client, ["operator:tokens", "operator:read"])
    resp = await _issue(
        client, ["operator:read", "operator:erase"], headers=H(minter["token"]), name="esc"
    )
    assert resp.status_code == 403
    rows = [
        r
        for r in await _system_rows(seeded_db)
        if r.action == "operator_token.issue" and r.outcome == audit_trail.FAILURE
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row.status_code == 403 and row.target_id is None
    assert row.actor_id == minter["name"]
    assert row.details == {
        "name": "esc",
        "reason": "scope_not_held",
        "scopes": ["operator:erase"],
    }
    async with _admin(seeded_db)() as s:
        assert (
            await s.execute(select(OperatorToken).where(OperatorToken.name == "esc"))
        ).first() is None


async def test_expired_caller_cannot_issue(client, seeded_db):
    minter = await _token(
        client,
        ["operator:tokens", "operator:read"],
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    async with _admin(seeded_db)() as s:
        await s.execute(
            update(OperatorToken)
            .where(OperatorToken.id == uuid.UUID(minter["id"]))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await s.commit()
    resp = await _issue(client, ["operator:read"], headers=H(minter["token"]))
    assert resp.status_code == 401


async def test_refusals_and_caps_under_rls(client, seeded_db):
    narrow = await _token(
        client,
        ["operator:tokens", "operator:read"],
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    broad = await _token(client, ["operator:tokens", "operator:erase"])
    async with seeded_db.begin() as conn:
        await rls.enable(conn)
    n = H(narrow["token"])
    child = await _token(client, ["operator:read"], headers=n)
    assert child["expires_at"] == narrow["expires_at"]
    assert (await client.delete(f"{TOKENS}/{broad['id']}", headers=n)).status_code == 403
    refused = [
        r for r in await _system_rows(seeded_db) if r.outcome == audit_trail.FAILURE
    ]
    assert [(r.action, r.target_id) for r in refused] == [
        ("operator_token.revoke", broad["id"])
    ]
