"""Erasure by end user and the tenant audit read API (#353) against a real
PostgreSQL database (PS_TEST_DATABASE_URL), with row-level security
enforced: two tenants send events for the same user id; tenant a erases
it; tenant b's rows, salt and audit chain are untouched, and the erasure is
on a's audit trail, readable through /partner/v1/audit."""

from __future__ import annotations

import hashlib
import hmac

from collector import rls, usage_rollups
from collector.app import app
from collector.config import settings
from db.models import (
    AIUsageEvent,
    AuditEvent,
    CallFeedback,
    McpToolUsage,
    PartnerCredential,
    RetentionAuditLog,
    TenantSecret,
    UsageRollupDaily,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from tests.test_rls_db import KEY_A, KEY_B, PARTNER_KEY_A, PARTNER_KEY_B, rls_db  # noqa: F401

USER = "0123456789abcdef0123456789abcdef"  # the gateway's 32-hex subject hash
OTHER = "fedcba9876543210fedcba9876543210"


def _head(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


async def _ingest(c: AsyncClient, key: str) -> None:
    events = [
        {"vendor": "openai", "model": "gpt-4o", "source": "gateway", "user_id": user,
         "session_id": f"s-{user[:4]}", "prompt_text": "hello", "call_id": f"call-{user[:4]}-{i}",
         "tokens_in": 10, "cost": 0.01}
        for user in (USER, OTHER)
        for i in range(3)
    ]
    resp = await c.post("/ingest/events", headers=_head(key), json={"events": events})
    assert resp.status_code == 200, resp.text
    for user in (USER, OTHER):
        resp = await c.post("/ingest/feedback", headers=_head(key),
                            json={"call_id": f"call-{user[:4]}-0", "score": 1, "user_id": user})
        assert resp.status_code == 200, resp.text
    mcp = [{"server_name": "jira", "tool_name": "create", "user_id": u} for u in (USER, OTHER)]
    mcp.append({"server_name": "jira", "tool_name": "create"})  # no user
    mcp.append({"server_name": "jira", "tool_name": "search", "user_id": USER})  # user only
    resp = await c.post("/ingest/mcp", headers=_head(key), json={"events": mcp})
    assert resp.status_code == 200, resp.text


def _none_first(row: tuple) -> tuple:
    return tuple((x is not None, "" if x is None else str(x)) for x in row)


async def _state(factory, tenant) -> dict:
    async with factory() as s:
        await rls.set_tenant(s, tenant)
        events = (await s.execute(
            select(AIUsageEvent).where(AIUsageEvent.source == "gateway")
        )).scalars().all()
        feedback = (await s.execute(
            select(CallFeedback.call_id, CallFeedback.user_id)
            .where(CallFeedback.call_id.like("call-%"))
        )).all()
        mcp = (await s.execute(
            select(McpToolUsage.tool_name, McpToolUsage.user_hash, McpToolUsage.call_count)
            .where(McpToolUsage.server_name == "jira")
        )).all()
        salt = (await s.execute(select(TenantSecret.user_id_salt))).scalar_one()
        rollups = dict((await s.execute(
            select(UsageRollupDaily.user_id_hash, func.sum(UsageRollupDaily.requests))
            .group_by(UsageRollupDaily.user_id_hash)
        )).all())
    return {
        "rollups": rollups,
        "events": sorted(
            ((e.user_id_hash, e.session_id, e.prompt_hash, e.tokens_in) for e in events),
            key=_none_first,
        ),
        "feedback": sorted((tuple(r) for r in feedback), key=_none_first),
        "mcp": sorted(tuple(r) for r in mcp),
        "salt": salt,
    }


async def test_erase_user_is_tenant_scoped_audited_and_idempotent(rls_db, monkeypatch):  # noqa: F811
    a, b = rls_db["a"], rls_db["b"]
    monkeypatch.setattr(settings, "retention_batch_size", 2)  # several batches
    plain = rls_db["plain"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await _ingest(c, KEY_A)
        await _ingest(c, KEY_B)
        # Tenant a's usage rollups (#346) hold the user's hash too.
        async with plain() as s:
            await rls.set_tenant(s, a)
            await usage_rollups.refresh_tenant(s, a)
        before_b = await _state(plain, b)
        before_a = await _state(plain, a)
        hash_a = hmac.new(before_a["salt"].encode(), USER.encode(), hashlib.sha256).hexdigest()
        assert before_a["rollups"][hash_a] == 3

        resp = await c.post("/api/v1/admin/users/erase", headers=_head(KEY_A),
                            json={"user_id": USER})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["user_id_hash"] == hash_a and body["input"] == "user_id"
        assert body["rows_erased"] == {
            "ai_usage_events": 3, "usage_rollups": 2, "call_feedback": 1, "mcp_tool_usage": 2,
        }
        assert body["audit_event_id"]

        after_a = await _state(plain, a)
        other_hash = hmac.new(
            before_a["salt"].encode(), OTHER.encode(), hashlib.sha256).hexdigest()
        # The user's events keep their usage and lose every identifier.
        assert [e for e in after_a["events"] if e[0] is None] == [(None, None, None, 10)] * 3
        assert sum(1 for e in after_a["events"] if e[0] == other_hash) == 3
        assert all(e[0] != hash_a for e in after_a["events"])
        assert after_a["feedback"] == [
            ("call-0123-0", None), ("call-fedc-0", OTHER),
        ]
        # create: the user's call folded into the no-user row; search moved.
        assert after_a["mcp"] == [
            ("create", "", 2), ("create", OTHER, 1), ("search", "", 1),
        ]
        assert after_a["salt"] == before_a["salt"]  # never rotated
        # The rollups keep the totals and lose the user's hash.
        assert hash_a not in after_a["rollups"]
        assert after_a["rollups"][None] == before_a["rollups"].get(None, 0) + 3
        assert after_a["rollups"][other_hash] == 3
        async with plain() as s:  # a later rollup run does not bring it back
            await rls.set_tenant(s, a)
            await usage_rollups.rebuild_tenant(s, a)
        rebuilt = (await _state(plain, a))["rollups"]
        assert hash_a not in rebuilt and rebuilt[other_hash] == 3
        assert await _state(plain, b) == before_b  # the other tenant untouched

        # Idempotent: nothing left, still audited.
        resp = await c.post("/api/v1/admin/users/erase", headers=_head(KEY_A),
                            json={"user_id": USER})
        assert resp.status_code == 200
        assert set(resp.json()["rows_erased"].values()) == {0}

        # Tenant b erases by the stored hash (its own salt's hash).
        hash_b = hmac.new(before_b["salt"].encode(), USER.encode(), hashlib.sha256).hexdigest()
        assert hash_b != hash_a
        resp = await c.delete(f"/api/v1/admin/users/{hash_b}/data", headers=_head(KEY_B))
        assert resp.status_code == 200, resp.text
        assert resp.json()["rows_erased"] == {"ai_usage_events": 3, "usage_rollups": 0}
        # a's hash means nothing in b: zero rows, the same answer as for an
        # id b never saw.
        resp = await c.delete(f"/api/v1/admin/users/{other_hash}/data", headers=_head(KEY_B))
        assert resp.json()["rows_erased"] == {"ai_usage_events": 0, "usage_rollups": 0}

        resp = await c.delete("/api/v1/admin/users/NOT-A-HASH/data", headers=_head(KEY_B))
        assert resp.status_code == 400
        resp = await c.post("/api/v1/admin/users/erase", headers=_head(KEY_A), json={})
        assert resp.status_code == 422

        # Audit trail: on each tenant's own chain, readable via /partner/v1/audit.
        async with rls_db["admin"]() as s:
            await s.execute(update(PartnerCredential).values(scopes=["audit:read"]))
            await s.commit()
        resp = await c.get("/partner/v1/audit", headers=_head(PARTNER_KEY_A),
                           params={"action": "user.erase"})
        assert resp.status_code == 200, resp.text
        page = resp.json()
        assert [r["target"] for r in page["data"]] == [{"type": "user", "id": hash_a}] * 2
        first = page["data"][0]
        assert first["details"] == {
            "input": "user_id",
            "rows_erased": {
                "ai_usage_events": 3, "usage_rollups": 2, "call_feedback": 1, "mcp_tool_usage": 2,
            },
        }
        assert first["tenant_id"] == str(a) and first["row_hash"] and first["prev_hash"]
        assert first["actor"]["type"] == "tenant_key"
        assert page["meta"] == {"has_more": False, "next_cursor": str(page["data"][-1]["seq"])}

        resp = await c.get("/partner/v1/audit", headers=_head(PARTNER_KEY_B),
                           params={"action": "user.erase"})
        assert [r["target"]["id"] for r in resp.json()["data"]] == [hash_b, other_hash]

    async with rls_db["admin"]() as s:
        rows = (await s.execute(
            select(RetentionAuditLog.tenant_id).where(RetentionAuditLog.action == "erase_user")
        )).scalars().all()
        chain = (await s.execute(
            select(AuditEvent.tenant_id).where(AuditEvent.action == "user.erase")
        )).scalars().all()
    assert sorted(map(str, rows)) == sorted(map(str, [a, a, b, b]))
    assert sorted(map(str, chain)) == sorted(map(str, [a, a, b, b]))


async def test_erase_without_a_salt_creates_none(rls_db):  # noqa: F811
    """A tenant that never ingested a user_id has no salt; erasing by id
    does not create one, and still erases a user_id_raw stored as sent."""
    a = rls_db["a"]
    async with rls_db["admin"]() as s:
        await s.execute(TenantSecret.__table__.delete())
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        events = [{"vendor": "openai", "model": "gpt-4o", "source": "gateway",
                   "session_id": f"s-{i}", "tokens_in": 10} for i in range(2)]
        resp = await c.post("/ingest/events", headers=_head(KEY_A), json={"events": events})
        assert resp.status_code == 200, resp.text
        async with rls_db["admin"]() as s:
            await s.execute(
                update(AIUsageEvent)
                .where(AIUsageEvent.tenant_id == a, AIUsageEvent.session_id == "s-0")
                .values(user_id_raw="someone@example.com")
            )
            await s.commit()
        resp = await c.post("/api/v1/admin/users/erase", headers=_head(KEY_A),
                            json={"user_id": "someone@example.com"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["user_id_hash"] is None
    assert resp.json()["rows_erased"] == {
        "ai_usage_events": 1, "call_feedback": 0, "mcp_tool_usage": 0,
    }
    async with rls_db["plain"]() as s:
        await rls.set_tenant(s, a)
        assert (await s.execute(select(TenantSecret))).first() is None
        rows = (await s.execute(
            select(AIUsageEvent.session_id, AIUsageEvent.user_id_raw)
            .where(AIUsageEvent.source == "gateway")
        )).all()
    assert sorted(map(tuple, rows), key=_none_first) == [(None, None), ("s-1", None)]


async def test_audit_api_pages_and_filters(rls_db):  # noqa: F811
    a = rls_db["a"]
    async with rls_db["admin"]() as s:
        await s.execute(update(PartnerCredential).values(scopes=["audit:read"]))
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        for _ in range(3):
            resp = await c.post("/api/v1/admin/users/erase", headers=_head(KEY_A),
                                json={"user_id": USER})
            assert resp.status_code == 200
        seen, cursor = [], None
        while True:
            params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
            resp = await c.get("/partner/v1/audit", headers=_head(PARTNER_KEY_A), params=params)
            assert resp.status_code == 200, resp.text
            page = resp.json()
            seen += [r["seq"] for r in page["data"]]
            cursor = page["meta"]["next_cursor"]
            if not page["meta"]["has_more"]:
                break
        assert seen == list(range(1, 5))  # the seeded row, then three erasures
        assert {r["tenant_id"] for r in page["data"]} == {str(a)}

        resp = await c.get("/partner/v1/audit", headers=_head(PARTNER_KEY_A),
                           params={"until": "2000-01-01T00:00:00Z"})
        assert resp.json() == {"data": [], "meta": {"has_more": False, "next_cursor": None}}
        resp = await c.get("/partner/v1/audit", headers=_head(PARTNER_KEY_A),
                           params={"cursor": "abc"})
        assert resp.status_code == 400
        for bad in ("9" * 19, "9" * 40, "\u0661"):  # past bigint; Arabic-Indic 1
            resp = await c.get("/partner/v1/audit", headers=_head(PARTNER_KEY_A),
                               params={"cursor": bad})
            assert resp.status_code == 400, bad

        # Without audit:read: 403.
        async with rls_db["admin"]() as s:
            await s.execute(update(PartnerCredential).values(scopes=["registry:read"]))
            await s.commit()
        resp = await c.get("/partner/v1/audit", headers=_head(PARTNER_KEY_B))
        assert resp.status_code == 403
