"""Gateway security-event ingest and query (#353) against a real PostgreSQL
database (PS_TEST_DATABASE_URL) with row-level security enforced: the
gateway's OTLP JSON logs and forwarded JSON lines are stored for the ingest
key's tenant only, read back through /partner/v1/security-events with the
``security-events:read`` scope, purged with the tenant's retention period,
and the migration goes down and up."""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from collector import partner_rate_limit, retention, rls, tenant_limits
from collector import security_events as se
from collector.app import app
from collector.config import settings
from collector.partner_rate_limit import (
    FixedWindowRateLimiter,
    get_ingest_rate_limiter,
    get_security_events_rate_limiter,
)
from db.models import GatewaySecurityEvent, PartnerCredential
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, select, update

from tests.test_rls_db import (  # noqa: F401 - fixture and keys
    KEY_A,
    KEY_B,
    PARTNER_KEY_A,
    PARTNER_KEY_B,
    rls_db,
)


def _head(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _event(event_id: str, name: str = "injection.detected", **over) -> dict:
    """A v1 event as the gateway writes it."""
    event = {
        "@timestamp": "2026-11-06T12:00:00.000Z",
        "ps.schema_version": "1.0.0",
        "event.id": event_id,
        "event.name": name,
        "event.kind": "alert",
        "event.category": ["intrusion_detection"],
        "event.outcome": "failure",
        "log.logger": "ps.security",
        "service.name": "prompt-shields-gateway",
        "vendor_product": "Prompt Shields Gateway",
        "severity": "high",
        "action": "blocked",
        "signature": "azure.shieldPrompt",
        "ps.request_id": "req-1",
        "http.route": "/v1/chat/completions",
        "http.request.method": "POST",
        "ps.tenant": "label-from-the-gateway",
        "ps.business_unit": "Retail",
        "gen_ai.provider.name": "openai",
        "gen_ai.request.model": "gpt-4o",
        "ps.guardrail.hook_id": "input_guardrail",
        "ps.guardrail.check_id": "azure.shieldPrompt",
        "ps.guardrail.stage": "input",
        "ps.guardrail.verdict": "deny",
    }
    event.update(over)
    return event


def _attrs(event: dict) -> list[dict]:
    out = []
    for k, v in event.items():
        if k in ("@timestamp", "service.name"):
            continue
        if isinstance(v, bool):
            value = {"boolValue": v}
        elif isinstance(v, int):
            value = {"intValue": str(v)}
        elif isinstance(v, list):
            value = {"arrayValue": {"values": [{"stringValue": x} for x in v]}}
        else:
            value = {"stringValue": v}
        out.append({"key": k, "value": value})
    return out


def _log_record(event: dict, *, body: bool = True) -> dict:
    """Mirrors gateway/src/middlewares/psSecurityEvents/otlp.ts."""
    ts = datetime.fromisoformat(event["@timestamp"])
    record = {
        "timeUnixNano": f"{int(ts.timestamp() * 1000)}000000",
        "severityNumber": 17,
        "severityText": event.get("severity"),
        "eventName": event.get("event.name"),
        "attributes": _attrs(event),
    }
    if body:
        record["body"] = {"stringValue": json.dumps(event)}
    return record


def _otlp(records: list[dict]) -> dict:
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "prompt-shields-gateway"}}
                    ]
                },
                "scopeLogs": [
                    {"scope": {"name": "ps.security", "version": "1.0.0"}, "logRecords": records}
                ],
            }
        ]
    }


async def _rows(factory, tenant) -> list[GatewaySecurityEvent]:
    async with factory() as s:
        await rls.set_tenant(s, tenant)
        return list(
            (
                await s.execute(
                    select(GatewaySecurityEvent)
                    .where(GatewaySecurityEvent.event_id.not_like("seed-%"))
                    .order_by(GatewaySecurityEvent.event_id)
                )
            ).scalars()
        )


async def test_otlp_logs_from_the_gateway_are_stored_for_the_key_tenant(rls_db):  # noqa: F811
    a, b = rls_db["a"], rls_db["b"]
    denial = _event(
        "e-2",
        "policy.denied",
        severity="medium",
        signature="model_not_allowed",
        **{
            "event.category": ["iam"],
            "ps.policy.reason": "model_not_allowed",
            "user.hash": "0123456789abcdef0123456789abcdef",
            "http.response.status_code": 403,
        },
    )
    other_log = {"timeUnixNano": "1", "body": {"stringValue": "GET /healthz 200"}}
    payload = _otlp(
        [_log_record(_event("e-1")), _log_record(denial, body=False), other_log]
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/ingest/otel/v1/logs", json=payload)
        assert r.status_code == 401
        r = await c.post(
            "/ingest/otel/v1/logs",
            content=b"\x0a\x00",
            headers={**_head(KEY_A), "Content-Type": "application/x-protobuf"},
        )
        assert r.status_code == 415
        r = await c.post("/ingest/otel/v1/logs", json=payload, headers=_head(KEY_A))
        assert r.status_code == 200, r.text
        assert r.json() == {
            "partialSuccess": {
                "rejectedLogRecords": 1,
                "errorMessage": "log records that are not Prompt Shields security events",
            }
        }
        # Resent by the exporter: stored once.
        r = await c.post("/ingest/otel/v1/logs", json=payload, headers=_head(KEY_A))
        assert r.status_code == 200

    first, second = await _rows(rls_db["plain"], a)
    assert await _rows(rls_db["plain"], b) == []
    assert first.name == "injection.detected" and first.tenant_id == a
    assert first.occurred_at == datetime(2026, 11, 6, 12, tzinfo=UTC)
    assert (first.severity, first.action, first.signature) == (
        "high", "blocked", "azure.shieldPrompt"
    )
    assert (first.route, first.method, first.provider, first.model) == (
        "/v1/chat/completions", "POST", "openai", "gpt-4o"
    )
    assert first.business_unit == "Retail" and first.schema_version == "1.0.0"
    assert first.attributes["ps.guardrail.verdict"] == "deny"
    assert first.attributes["event.category"] == ["intrusion_detection"]
    # The tenant is the key's: the gateway's label is not stored.
    assert "ps.tenant" not in first.attributes
    assert "service.name" not in first.attributes
    # From attributes only (no body): the record time, int and array values.
    assert second.name == "policy.denied" and second.status_code == 403
    assert second.user_hash == "0123456789abcdef0123456789abcdef"
    # Salted like ai_usage_events.user_id_hash (erasure by stored hash).
    assert second.user_id_hash and len(second.user_id_hash) == 64
    assert first.user_id_hash is None
    assert "user_id_hash" not in se.event_fields(second)
    assert second.occurred_at == datetime(2026, 11, 6, 12, tzinfo=UTC)
    assert second.attributes["event.category"] == ["iam"]
    assert second.attributes["ps.policy.reason"] == "model_not_allowed"


async def test_forwarded_lines_validation_and_duplicates(rls_db):  # noqa: F811
    b = rls_db["b"]
    lines = [
        json.dumps(_event("n-1")),
        "not json",
        json.dumps(_event("n-2", **{"log.logger": "app"})),
        json.dumps({k: v for k, v in _event("n-3").items() if k != "@timestamp"}),
        json.dumps(_event("bad id with spaces")),
        json.dumps(_event("n-4", "NotDotted")),
        json.dumps(_event("n-5", **{"signature": "x" * 300, "ps.big": "y" * 300})),
        json.dumps(_event("n-1")),
    ]
    body = ("\n".join(lines) + "\n").encode()
    head = {**_head(KEY_B), "Content-Type": "application/x-ndjson"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/ingest/security-events", content=body, headers=head)
        assert r.status_code == 200, r.text
        out = r.json()
        assert (out["accepted"], out["duplicates"], out["rejected"]) == (2, 1, 5)
        assert [e["index"] for e in out["errors"]] == [1, 2, 3, 4, 5]
        assert out["errors"][1]["reason"] == "log.logger must be ps.security"
        # Values never come back in errors.
        assert "spaces" not in r.text

        r = await c.post(
            "/ingest/security-events",
            json={"events": [_event("n-1"), _event("n-6")]},
            headers=_head(KEY_B),
        )
        assert r.json()["accepted"] == 1 and r.json()["duplicates"] == 1
        r = await c.post(
            "/ingest/security-events", content=b"x", headers={**_head(KEY_B), "Content-Type": "text/plain"}
        )
        assert r.status_code == 415
        r = await c.post("/ingest/security-events", json={"x": 1}, headers=_head(KEY_B))
        assert r.status_code == 400
        too_many = "\n".join(json.dumps(_event(f"m-{i}")) for i in range(se.MAX_EVENTS + 1))
        r = await c.post("/ingest/security-events", content=too_many.encode(), headers=head)
        assert r.status_code == 413

    rows = await _rows(rls_db["plain"], b)
    assert [r.event_id for r in rows] == ["n-1", "n-5", "n-6"]
    big = rows[1]
    assert big.signature is None and "ps.big" not in big.attributes


async def test_ingest_rate_limit_is_per_tenant_and_apart_from_events(
    rls_db, monkeypatch  # noqa: F811
):
    """Both security-event ingest routes share the tenant's
    PS_SECURITY_EVENTS_RATE_LIMIT_PER_MINUTE bucket (429 past it, with
    Retry-After), apart from /ingest/events and from other tenants; a
    tenant override applies."""
    monkeypatch.setattr(settings, "security_events_rate_limit_per_minute", 2)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 1)
    monkeypatch.setattr(
        partner_rate_limit, "time", SimpleNamespace(time=lambda: 1_700_000_030.0)
    )
    tenant_limits.invalidate()
    fresh, events = FixedWindowRateLimiter(), FixedWindowRateLimiter()
    app.dependency_overrides[get_security_events_rate_limiter] = lambda: fresh
    app.dependency_overrides[get_ingest_rate_limiter] = lambda: events
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            first = await c.post(
                "/ingest/security-events", json=[_event("rl-1")], headers=_head(KEY_A)
            )
            second = await c.post(
                "/ingest/otel/v1/logs", json=_otlp([_log_record(_event("rl-2"))]),
                headers=_head(KEY_A),
            )
            blocked = await c.post(
                "/ingest/security-events", json=[_event("rl-3")], headers=_head(KEY_A)
            )
            blocked_otlp = await c.post(
                "/ingest/otel/v1/logs", json=_otlp([_log_record(_event("rl-4"))]),
                headers=_head(KEY_A),
            )
            other_tenant = await c.post(
                "/ingest/security-events", json=[_event("rl-5")], headers=_head(KEY_B)
            )
            sdk = await c.post("/ingest/events", json={"events": []}, headers=_head(KEY_A))
            assert [first.status_code, second.status_code] == [200, 200], second.text
            assert first.headers["X-RateLimit-Limit"] == "2"
            assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) > 0
            assert blocked_otlp.status_code == 429
            assert other_tenant.status_code == 200
            assert sdk.status_code == 200

            monkeypatch.setattr(settings, "security_events_rate_limit_per_minute", 0)
            codes = [
                (
                    await c.post(
                        "/ingest/security-events", json=[], headers=_head(KEY_A)
                    )
                ).status_code
                for _ in range(3)
            ]
            assert codes == [200] * 3
    finally:
        app.dependency_overrides.pop(get_security_events_rate_limiter, None)
        app.dependency_overrides.pop(get_ingest_rate_limiter, None)
        tenant_limits.invalidate()
    assert [r.event_id for r in await _rows(rls_db["plain"], rls_db["a"])] == ["rl-1", "rl-2"]


async def test_query_api_scope_filters_and_paging(rls_db):  # noqa: F811
    events = [
        _event("q-1"),
        _event("q-2", "auth.failure", severity="medium", action="failure"),
        _event("q-3", **{"@timestamp": "2026-11-06T20:00:00Z"}),
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        for ev in events:  # separate requests: distinct arrival times
            r = await c.post("/ingest/security-events", json=[ev], headers=_head(KEY_A))
            assert r.json()["accepted"] == 1
        await c.post("/ingest/security-events", json=[_event("q-b")], headers=_head(KEY_B))

        r = await c.get("/partner/v1/security-events", headers=_head(PARTNER_KEY_A))
        assert r.status_code == 403  # registry:read does not grant it
        async with rls_db["admin"]() as s:
            await s.execute(
                update(PartnerCredential).values(scopes=["security-events:read"])
            )
            await s.commit()

        h = _head(PARTNER_KEY_A)
        r = await c.get(
            "/partner/v1/security-events", params={"name": "injection.detected"}, headers=h
        )
        assert r.status_code == 200, r.text
        page = r.json()
        assert [e["event"]["event.id"] for e in page["data"]] == ["q-1", "q-3"]
        first = page["data"][0]
        assert first["event"]["@timestamp"] == "2026-11-06T12:00:00Z"
        assert first["event"]["ps.guardrail.check_id"] == "azure.shieldPrompt"
        assert first["id"] and first["received_at"]
        assert page["meta"]["has_more"] is False

        got, cursor = [], None
        while True:
            params = {"limit": 1, **({"cursor": cursor} if cursor else {})}
            page = (await c.get("/partner/v1/security-events", params=params, headers=h)).json()
            got += [e["event"]["event.id"] for e in page["data"]]
            cursor = page["meta"]["next_cursor"]
            if not page["meta"]["has_more"]:
                break
        # Arrival order, the seed row first; never tenant b's events.
        assert got == ["seed-a", "q-1", "q-2", "q-3"]

        r = await c.get(
            "/partner/v1/security-events",
            params={"since": "2026-11-06T18:00:00Z", "severity": "high", "action": "blocked"},
            headers=h,
        )
        assert [e["event"]["event.id"] for e in r.json()["data"]] == ["q-3"]
        r = await c.get(
            "/partner/v1/security-events",
            params={"until": "2026-11-06T18:00:00Z", "name": "auth.failure"},
            headers=h,
        )
        assert [e["event"]["event.id"] for e in r.json()["data"]] == ["q-2"]
        r = await c.get("/partner/v1/security-events", params={"cursor": "!!"}, headers=h)
        assert r.status_code == 400


async def test_retention_purges_old_security_events(rls_db):  # noqa: F811
    a = rls_db["a"]
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/ingest/security-events",
            json=[_event("old", **{"@timestamp": old}), _event("new")],
            headers=_head(KEY_A),
        )
        assert r.json()["accepted"] == 2
    async with rls_db["plain"]() as s:
        await rls.set_tenant(s, a)
        result = await retention.purge_tenant_events(s, a)
        await s.commit()
    assert result.security_events_deleted == 1
    assert [r.event_id for r in await _rows(rls_db["plain"], a)] == ["new"]


async def test_future_dated_events_are_clamped_at_ingest(rls_db):  # noqa: F811
    """An @timestamp more than MAX_CLOCK_SKEW ahead of receipt is stored as
    receipt + MAX_CLOCK_SKEW, the sent value kept in attributes; a small
    skew is stored as sent."""
    a = rls_db["a"]
    before = datetime.now(UTC)
    far = (before + timedelta(days=3650)).isoformat()
    near = (before + timedelta(minutes=1)).replace(microsecond=0)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/ingest/security-events",
            json=[
                _event("far", **{"@timestamp": far}),
                _event("near", **{"@timestamp": near.isoformat()}),
            ],
            headers=_head(KEY_A),
        )
        assert r.json()["accepted"] == 2
        otlp = await c.post(
            "/ingest/otel/v1/logs",
            json=_otlp([_log_record(_event("far-otlp", **{"@timestamp": far}), body=False)]),
            headers=_head(KEY_A),
        )
        assert otlp.status_code == 200 and otlp.json() == {}, otlp.text
    rows = {r.event_id: r for r in await _rows(rls_db["plain"], a)}
    for event_id in ("far", "far-otlp"):
        row = rows[event_id]
        assert row.occurred_at == row.received_at + se.MAX_CLOCK_SKEW
        assert datetime.fromisoformat(row.attributes["ps.original_timestamp"]) > (
            before + timedelta(days=3000)
        )
    assert rows["near"].occurred_at == near
    assert "ps.original_timestamp" not in rows["near"].attributes


async def test_retention_purges_future_dated_events_by_arrival(rls_db):  # noqa: F811
    """A row stored with a future occurred_at (before the ingest clamp) is
    purged once it arrived before the retention cutoff."""
    a = rls_db["a"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/ingest/security-events",
            json=[_event("future"), _event("recent")],
            headers=_head(KEY_A),
        )
        assert r.json()["accepted"] == 2
    long_ago = datetime.now(UTC) - timedelta(days=400)
    async with rls_db["admin"]() as s:
        await s.execute(
            update(GatewaySecurityEvent)
            .where(GatewaySecurityEvent.event_id == "future")
            .values(
                occurred_at=datetime.now(UTC) + timedelta(days=3650), received_at=long_ago
            )
        )
        await s.commit()
    async with rls_db["plain"]() as s:
        await rls.set_tenant(s, a)
        dry = await retention.purge_tenant_events(s, a, dry_run=True)
        result = await retention.purge_tenant_events(s, a)
        await s.commit()
    assert dry.security_events_deleted == 1
    assert result.security_events_deleted == 1
    assert "future" not in [r.event_id for r in await _rows(rls_db["plain"], a)]


# ──────────────────────────────────────────────────────────────────────
# Migration
# ──────────────────────────────────────────────────────────────────────


def _load_migration():
    pytest.importorskip("alembic")
    versions = Path(__file__).resolve().parents[2] / "db" / "alembic" / "versions"
    (path,) = versions.glob("*_gateway_security_events.py")
    spec = importlib.util.spec_from_file_location("migration_security_events", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_matches_the_model():
    m = _load_migration()
    assert m.TABLE == GatewaySecurityEvent.__tablename__
    assert m.revision == "042"


async def test_migration_downgrade_and_upgrade(seeded_db):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    m = _load_migration()

    def run(sync_conn, step):
        ctx = MigrationContext.configure(sync_conn)
        with Operations.context(ctx):
            step()

    def describe(sync_conn):
        insp = inspect(sync_conn)
        if m.TABLE not in insp.get_table_names():
            return None
        return (
            {c["name"]: c["nullable"] for c in insp.get_columns(m.TABLE)},
            {i["name"] for i in insp.get_indexes(m.TABLE)},
            {u["name"] for u in insp.get_unique_constraints(m.TABLE)},
        )

    async with seeded_db.begin() as conn:
        model = await conn.run_sync(describe)
        await conn.run_sync(run, m.downgrade)
        assert await conn.run_sync(describe) is None
    async with seeded_db.begin() as conn:
        await conn.run_sync(run, m.upgrade)
        migrated = await conn.run_sync(describe)
        policies = (
            await conn.exec_driver_sql(
                f"SELECT policyname FROM pg_policies WHERE tablename = '{m.TABLE}'"
            )
        ).scalars().all()
    assert migrated == model
    assert policies == ["tenant_isolation"]
