"""Erasure by end user (#353) on SQLite: the batching, the MCP merge's
portable path and the argument checks. test_user_erasure_db.py covers
Postgres with row-level security and the routes."""

from __future__ import annotations

from datetime import timedelta

import pytest
from collector import audit_trail, user_erasure, user_identity
from db.models import (
    AIUsageEvent,
    AuditEvent,
    CallFeedback,
    GatewaySecurityEvent,
    McpToolUsage,
    TenantSecret,
)
from sqlalchemy import select

from tests.test_retention import (  # noqa: F401 - fixture and helpers
    NOW,
    add_events,
    make_asset,
    make_tenant,
    session_factory,
)

ACTOR = audit_trail.system_actor("test")
USER = "0123456789abcdef0123456789abcdef"


def _mcp(tenant_id, tool: str, user_hash: str, calls: int, age: int = 0) -> McpToolUsage:
    return McpToolUsage(
        tenant_id=tenant_id, source="gateway", server_name="jira", tool_name=tool,
        user_hash=user_hash, call_count=calls, error_count=1, latency_ms_total=10 * calls,
        latency_count=calls, first_seen=NOW - timedelta(days=age), last_seen=NOW,
    )


async def test_erase_user_merges_mcp_rows_and_nulls_ids(session_factory):  # noqa: F811
    async with session_factory() as db:
        a = await make_tenant(db, "a")
        b = await make_tenant(db, "b")
        db.add_all([
            TenantSecret(tenant_id=a, user_id_salt="salt-a"),
            _mcp(a, "create", USER, 2, age=5),
            _mcp(a, "create", "", 3),
            _mcp(a, "search", USER, 1),
            _mcp(b, "create", USER, 4),
            CallFeedback(tenant_id=a, call_id="c1", score=1, source="sdk", user_id=USER),
            CallFeedback(tenant_id=b, call_id="c1", score=1, source="sdk", user_id=USER),
            GatewaySecurityEvent(tenant_id=a, event_id="e1", name="policy.denied",
                                 occurred_at=NOW, user_hash=USER),
            GatewaySecurityEvent(tenant_id=b, event_id="e1", name="policy.denied",
                                 occurred_at=NOW, user_hash=USER),
        ])
        await db.commit()
        asset = await make_asset(db, a)
        await add_events(db, a, asset, 1, 2, 3)

        events = (await db.execute(select(AIUsageEvent))).scalars().all()
        hashed = user_identity.hash_user_id("salt-a", USER)
        for e in events:
            e.user_id_hash, e.session_id, e.prompt_hash = hashed, "s", "p"
        await db.commit()

        result = await user_erasure.erase_user(db, a, user_id=USER, actor=ACTOR, batch_size=1)
        assert result.user_id_hash == hashed
        assert result.rows_erased == {
            "ai_usage_events": 3, "usage_rollups": 0, "call_feedback": 1, "mcp_tool_usage": 2,
            "gateway_security_events": 1,
        }
        assert result.audit_event_id is not None

        rows = (await db.execute(
            select(McpToolUsage.tenant_id, McpToolUsage.tool_name, McpToolUsage.user_hash,
                   McpToolUsage.call_count, McpToolUsage.error_count,
                   McpToolUsage.latency_count, McpToolUsage.first_seen)
            .order_by(McpToolUsage.tool_name)
        )).all()
        by_tenant = {(r[0], r[1]): r[2:] for r in rows}
        create = by_tenant[(a, "create")]
        assert create[:4] == ("", 5, 2, 5)
        assert create[4].replace(tzinfo=None) == (NOW - timedelta(days=5)).replace(tzinfo=None)
        assert by_tenant[(a, "search")][:2] == ("", 1)
        assert by_tenant[(b, "create")][:2] == (USER, 4)  # other tenant untouched

        feedback = dict((await db.execute(
            select(CallFeedback.tenant_id, CallFeedback.user_id))).all())
        assert feedback == {a: None, b: USER}
        events = (await db.execute(
            select(AIUsageEvent).execution_options(populate_existing=True)
        )).scalars().all()
        assert {(e.user_id_hash, e.session_id, e.prompt_hash) for e in events} == {
            (None, None, None)
        }
        action = (await db.execute(select(AuditEvent.action))).scalars().all()
        assert action == ["user.erase"]


async def test_erase_user_needs_exactly_one_identifier(session_factory):  # noqa: F811
    async with session_factory() as db:
        a = await make_tenant(db, "a")
        with pytest.raises(ValueError):
            await user_erasure.erase_user(db, a, actor=ACTOR)
        with pytest.raises(ValueError):
            await user_erasure.erase_user(
                db, a, user_id="x", user_id_hash="0" * 64, actor=ACTOR
            )
