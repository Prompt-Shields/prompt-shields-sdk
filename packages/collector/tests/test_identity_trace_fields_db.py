"""Gateway identity and trace fields (#334): user_id, user_group, trace_id,
retry_count, attempts and the stream flags are stored on ai_usage_events
instead of landing in ignored_fields; user_id always as a per-tenant salted
HMAC, and as sent too only if the tenant set store_raw_user_ids; the gateway's
route_sensitivity_* names land in sensitivity_level / sensitivity_source.

GATEWAY_EVENT mirrors the event a current gateway (ps-telemetry.ts) sends for
an OIDC-authenticated, streamed request that succeeded after one retry.
"""

import asyncio
import hashlib
import hmac
import importlib.util
from pathlib import Path

import pytest
from collector import audit_trail, user_identity
from collector.app import app
from collector.config import settings
from collector.ingest import IDENTITY_TRACE_FIELDS, IngestEvent
from db.api_keys import api_key_lookup_prefix, hash_api_key
from db.models import AIUsageEvent, Tenant, TenantSecret
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

KEY = "ps-test-key-12345"
KEY_B = "ps-test-key-tenant-b"
OP_TOKEN = "op-secret-token"
HEADERS = {"Authorization": f"Bearer {KEY}"}
OP = {"Authorization": f"Bearer {OP_TOKEN}"}
SUBJECT = "auth0|5f7c8ec7c33c6c004bbafe82"

ATTEMPTS = [
    {
        "attempt": 1,
        "provider": "openai",
        "model": "gpt-4o",
        "status": 503,
        "latency_ms": 120,
        "error_class": "server_error",
    },
    {"attempt": 2, "provider": "openai", "model": "gpt-4o", "status": 200, "latency_ms": 840},
]

GATEWAY_EVENT = {
    "vendor": "openai",
    "model": "gpt-4o",
    "source": "gateway",
    "tokens_in": 12,
    "tokens_out": 30,
    "route_mode": "gateway",
    "route_sensitivity_level": "confidential",
    "route_sensitivity_source": "pii_escalated",
    "stream": True,
    "stream_truncated": True,
    "stream_cancelled": False,
    "user_id": SUBJECT,
    "user_group": "eng-staff",
    "trace_id": "trace-7f3a",
    "retry_count": 1,
    "attempts": ATTEMPTS,
    "fallback_chain": ["server_error"],
    "call_id": "6f1c2b8e-0d7e-4b8f-9a51-0f2a4c3d9e10",
}


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", OP_TOKEN)
    monkeypatch.setattr(settings, "admin_require_scoped_tokens", False)
    audit_trail.auth_failure_limiter.reset()


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _ingest(events, headers=HEADERS):
    async with _client() as c:
        resp = await c.post("/ingest/events", headers=headers, json={"events": events})
        loss = (await c.get("/telemetry/loss", headers=headers)).json()
    return resp, loss


def _session(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)()


async def _events(engine):
    async with _session(engine) as s:
        return (await s.execute(select(AIUsageEvent))).scalars().all()


async def _tenant(engine, name=None) -> Tenant:
    async with _session(engine) as s:
        stmt = select(Tenant)
        if name:
            stmt = stmt.where(Tenant.name == name)
        return (await s.execute(stmt)).scalars().first()


async def _set_settings(engine, tenant_id, values):
    async with _session(engine) as s:
        await s.execute(update(Tenant).where(Tenant.id == tenant_id).values(settings=values))
        await s.commit()


async def _add_tenant_b(engine):
    async with _session(engine) as s:
        b = Tenant(
            name="Other Corp",
            settings={},
            api_key_prefix=api_key_lookup_prefix(KEY_B),
            api_key_hash=hash_api_key(KEY_B),
        )
        s.add(b)
        await s.commit()
        return b.id


async def _salt(engine, tenant_id=None):
    async with _session(engine) as s:
        if tenant_id is None:
            tenant_id = (await s.execute(select(Tenant.id))).scalars().first()
        return (
            await s.execute(
                select(TenantSecret.user_id_salt).where(TenantSecret.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()


def _hmac(salt, value):
    return hmac.new(salt.encode(), value.encode(), hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_gateway_event_round_trip(seeded_db):
    assert await _salt(seeded_db) is None
    resp, loss = await _ingest([GATEWAY_EVENT])
    assert resp.status_code == 200, resp.text

    salt = await _salt(seeded_db)
    assert len(salt) == 64
    # The salt is not a tenant setting.
    assert "user_id_salt" not in ((await _tenant(seeded_db)).settings or {})
    (ev,) = await _events(seeded_db)
    assert ev.user_id_hash == _hmac(salt, SUBJECT)
    assert ev.user_id_raw is None
    assert ev.user_group == "eng-staff"
    assert ev.trace_id == "trace-7f3a"
    assert ev.retry_count == 1
    assert ev.attempts == ATTEMPTS
    assert ev.stream_truncated is True and ev.stream_cancelled is False
    # Gateway sensitivity lands in the canonical columns.
    assert (ev.sensitivity_level, ev.sensitivity_source) == ("confidential", "pii_escalated")

    # The raw subject is stored nowhere on the row.
    row = {c.name: getattr(ev, c.name) for c in AIUsageEvent.__table__.columns}
    assert SUBJECT not in repr(row)

    # Nothing a current gateway sends is dropped any more, except "stream",
    # which the collector has never stored.
    (stats,) = loss["clients"]
    assert set(stats["ignored_fields"]) <= {"stream"}


@pytest.mark.asyncio
async def test_salt_is_stable_and_per_tenant(seeded_db):
    tenant_b = await _add_tenant_b(seeded_db)
    for headers in (HEADERS, HEADERS, {"Authorization": f"Bearer {KEY_B}"}):
        resp, _ = await _ingest([GATEWAY_EVENT], headers)
        assert resp.status_code == 200, resp.text

    a = await _tenant(seeded_db)
    b = await _tenant(seeded_db, "Other Corp")
    assert b.id == tenant_b
    salt_a, salt_b = await _salt(seeded_db, a.id), await _salt(seeded_db, b.id)
    assert salt_a != salt_b
    events = await _events(seeded_db)
    by_tenant = {}
    for ev in events:
        by_tenant.setdefault(ev.tenant_id, set()).add(ev.user_id_hash)
    # One user, one hash per tenant; different across tenants.
    assert by_tenant[a.id] == {_hmac(salt_a, SUBJECT)}
    assert by_tenant[b.id] == {_hmac(salt_b, SUBJECT)}


@pytest.mark.asyncio
async def test_no_salt_without_a_user_id(seeded_db):
    event = {k: v for k, v in GATEWAY_EVENT.items() if k != "user_id"}
    resp, _ = await _ingest([event])
    assert resp.status_code == 200, resp.text
    assert await _salt(seeded_db) is None
    (ev,) = await _events(seeded_db)
    assert ev.user_id_hash is None and ev.user_id_raw is None
    assert ev.user_group == "eng-staff"


@pytest.mark.asyncio
async def test_store_raw_user_ids(seeded_db):
    tenant = await _tenant(seeded_db)
    await _set_settings(seeded_db, tenant.id, {"store_raw_user_ids": True})
    resp, _ = await _ingest([GATEWAY_EVENT])
    assert resp.status_code == 200, resp.text
    (ev,) = await _events(seeded_db)
    assert ev.user_id_raw == SUBJECT
    # The hash is stored as well: it is the join key for per-user usage and
    # erasure, whatever the setting.
    assert ev.user_id_hash == _hmac(await _salt(seeded_db), SUBJECT)

    # Turning raw storage off keeps the same hash for the same user.
    await _set_settings(seeded_db, tenant.id, {"store_raw_user_ids": False})
    resp, _ = await _ingest([GATEWAY_EVENT])
    assert resp.status_code == 200, resp.text
    events = await _events(seeded_db)
    assert {e.user_id_hash for e in events} == {ev.user_id_hash}
    assert sorted(str(e.user_id_raw) for e in events) == sorted(["None", SUBJECT])


@pytest.mark.asyncio
async def test_store_raw_must_be_true_not_truthy(seeded_db):
    tenant = await _tenant(seeded_db)
    await _set_settings(seeded_db, tenant.id, {"store_raw_user_ids": "true"})
    resp, _ = await _ingest([GATEWAY_EVENT])
    assert resp.status_code == 200, resp.text
    (ev,) = await _events(seeded_db)
    assert ev.user_id_raw is None and ev.user_id_hash is not None


@pytest.mark.asyncio
async def test_settings_rewrites_never_touch_the_salt(seeded_db):
    """Retention, connectors and the operator API rewrite tenants.settings
    whole; the salt is not in it, so hashes stay continuous."""
    tenant = await _tenant(seeded_db)
    resp, _ = await _ingest([GATEWAY_EVENT])
    assert resp.status_code == 200, resp.text
    salt = await _salt(seeded_db)
    # A stale whole-document write, and a planted settings key.
    await _set_settings(seeded_db, tenant.id, {"retention_days": 90})
    await _set_settings(seeded_db, tenant.id, {"user_id_salt": "attacker-known"})
    resp, _ = await _ingest([GATEWAY_EVENT])
    assert resp.status_code == 200, resp.text
    assert await _salt(seeded_db) == salt
    assert {e.user_id_hash for e in await _events(seeded_db)} == {_hmac(salt, SUBJECT)}


@pytest.mark.asyncio
async def test_concurrent_first_use_agrees_on_one_salt(seeded_db):
    tenant_id = (await _tenant(seeded_db)).id
    s1, s2 = _session(seeded_db), _session(seeded_db)
    try:
        p1 = await user_identity.policy_for(s1, tenant_id)
        # s2's INSERT waits on s1's uncommitted key, then reads s1's salt.
        task = asyncio.create_task(user_identity.policy_for(s2, tenant_id))
        await asyncio.sleep(0.2)
        assert not task.done()
        await s1.commit()
        p2 = await asyncio.wait_for(task, 10)
        await s2.commit()
    finally:
        await s1.close()
        await s2.close()
    assert p1.salt == p2.salt
    assert await _salt(seeded_db, tenant_id) == p1.salt


@pytest.mark.asyncio
async def test_concurrent_settings_write_cannot_drop_a_new_salt(seeded_db):
    """The race a settings-stored salt had: a writer reads settings, ingest
    creates the salt, the writer saves its stale copy. The salt survives."""
    tenant_id = (await _tenant(seeded_db)).id
    writer, ingest = _session(seeded_db), _session(seeded_db)
    try:
        stale = dict(
            (
                await writer.execute(select(Tenant.settings).where(Tenant.id == tenant_id))
            ).scalar_one()
            or {}
        )
        p = await user_identity.policy_for(ingest, tenant_id)
        await ingest.commit()
        await writer.execute(
            update(Tenant)
            .where(Tenant.id == tenant_id)
            .values(settings={**stale, "retention_days": 30})
        )
        await writer.commit()
    finally:
        await writer.close()
        await ingest.close()
    assert await _salt(seeded_db, tenant_id) == p.salt
    async with _session(seeded_db) as s:
        assert (await user_identity.policy_for(s, tenant_id)).salt == p.salt


@pytest.mark.asyncio
async def test_canonical_sensitivity_wins_over_alias(seeded_db):
    event = {**GATEWAY_EVENT, "sensitivity_level": "public", "sensitivity_source": "declared"}
    resp, loss = await _ingest([event])
    assert resp.status_code == 200, resp.text
    (ev,) = await _events(seeded_db)
    assert (ev.sensitivity_level, ev.sensitivity_source) == ("public", "declared")
    (stats,) = loss["clients"]
    assert not {k for k in stats["ignored_fields"] if k.startswith("route_sensitivity")}


@pytest.mark.asyncio
async def test_malformed_fields_are_dropped_and_counted(seeded_db):
    bad = {
        "vendor": "openai",
        "model": "gpt-4o",
        "user_id": "x" * 257,
        "user_group": "has space",
        "trace_id": "not a token!",
        "retry_count": -1,
        "attempts": [{"attempt": 1, "latency_ms": 5, "error_class": "Upstream said: boom"}],
        "stream_truncated": "true",
        "stream_cancelled": 1,
        "route_sensitivity_source": "guessed",
    }
    resp, loss = await _ingest([bad, {**GATEWAY_EVENT, "user_id": "\x00evil"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["rejected"] == 0

    (stats,) = loss["clients"]
    invalid = {k: n for k, n in stats["ignored_fields"].items() if k.endswith(":invalid")}
    assert invalid == {
        **{f"{f}:invalid": 1 for f in IDENTITY_TRACE_FIELDS if f != "user_id"},
        "user_id:invalid": 2,
        "sensitivity_source:invalid": 1,
    }
    events = await _events(seeded_db)
    dropped = next(e for e in events if e.trace_id is None)
    assert all(
        getattr(dropped, f) is None
        for f in ("user_id_hash", "user_id_raw", "user_group", "retry_count", "attempts",
                  "stream_truncated", "stream_cancelled", "sensitivity_source")
    )
    nul = next(e for e in events if e.trace_id)
    assert nul.user_id_hash is None and nul.user_group == "eng-staff"
    # Neither batch event carried a usable user_id: no salt was made.
    assert await _salt(seeded_db) is None


def test_field_validation():
    def ev(**kw):
        return IngestEvent(vendor="v", **kw)

    assert ev(user_id="  u-1 ").user_id == "u-1"
    for bad in ("", " ", "x" * 257, 42, ["u"], True):
        assert ev(user_id=bad).user_id is None, bad
    assert ev(user_group="eng.staff:eu-1").user_group == "eng.staff:eu-1"
    for bad in ("a b", "x" * 65, "", 3):
        assert ev(user_group=bad).user_group is None, bad
    assert ev(trace_id="a1b2-c3").trace_id == "a1b2-c3"
    for bad in ("", "x" * 129, "a b", 5):
        assert ev(trace_id=bad).trace_id is None, bad
    assert ev(retry_count=0).retry_count == 0
    assert ev(retry_count=3.0).retry_count == 3
    for bad in (-1, 1.5, "2", True, 2**31):
        assert ev(retry_count=bad).retry_count is None, bad
    for flag in (True, False):
        assert ev(stream_truncated=flag).stream_truncated is flag
        assert ev(stream_cancelled=flag).stream_cancelled is flag
    for bad in ("true", 1, 0, None):
        assert ev(stream_truncated=bad).stream_truncated is None

    # Unknown keys in a record are left out; known ones validated.
    rec = {"attempt": 1, "latency_ms": 10, "status": 429, "error_class": "rate_limited",
           "provider": "openai", "model": "gpt-4o", "message": "Rate limit hit for org-123"}
    assert ev(attempts=[rec]).attempts == [{k: v for k, v in rec.items() if k != "message"}]
    assert ev(attempts=[{"attempt": 1, "latency_ms": 0}]).attempts == [
        {"attempt": 1, "latency_ms": 0}
    ]
    assert ev(attempts=[]).attempts == []
    for model in ("us.anthropic.claude-3-5-sonnet-20240620-v1:0", "models/gemini-1.5-pro",
                  "meta-llama/Llama-3-8b@main", "llama3:8b"):
        rec = {"attempt": 1, "latency_ms": 1, "provider": "azure-openai", "model": model}
        assert ev(attempts=[rec]).attempts == [rec], model
    for bad in (
        "x",
        [1],
        [{"latency_ms": 1}],
        [{"attempt": 0, "latency_ms": 1}],
        [{"attempt": 1}],
        [{"attempt": 1, "latency_ms": 1, "status": 99}],
        [{"attempt": 1, "latency_ms": 1, "status": "200"}],
        [{"attempt": 1, "latency_ms": 1, "provider": "x" * 129}],
        [{"attempt": 1, "latency_ms": 1, "model": 5}],
        # Free text never fits: provider and model are identifier tokens.
        [{"attempt": 1, "latency_ms": 1, "provider": "Upstream said: boom"}],
        [{"attempt": 1, "latency_ms": 1, "model": "Rate limit hit for org-123"}],
        [{"attempt": 1, "latency_ms": 1, "model": "gpt-4o\n"}],
        [{"attempt": 1, "latency_ms": 1, "error_class": "Bad Gateway"}],
        [{"attempt": i + 1, "latency_ms": 1} for i in range(33)],
    ):
        assert ev(attempts=bad).attempts is None, bad

    # Aliases.
    e = ev(route_sensitivity_level="secret", route_sensitivity_source="DECLARED")
    assert (e.sensitivity_level, e.sensitivity_source) == ("secret", "declared")
    e = ev(sensitivity_level=None, route_sensitivity_level="secret")
    assert e.sensitivity_level == "secret"


def test_user_id_policy_columns():
    raw = user_identity.UserIdPolicy(salt="s", store_raw=True)
    assert raw.columns("u") == {"user_id_hash": _hmac("s", "u"), "user_id_raw": "u"}
    hashed = user_identity.UserIdPolicy(salt="s")
    assert hashed.columns("u") == {"user_id_hash": _hmac("s", "u"), "user_id_raw": None}
    assert hashed.columns(None) == {"user_id_hash": None, "user_id_raw": None}
    assert raw.columns(None) == {"user_id_hash": None, "user_id_raw": None}


# ──────────────────────────────────────────────────────────────────────
# The salt never leaves the database through the operator tenants API
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_operator_api_never_returns_or_changes_the_salt(seeded_db):
    resp, _ = await _ingest([GATEWAY_EVENT])
    assert resp.status_code == 200, resp.text
    tenant = await _tenant(seeded_db)
    salt = await _salt(seeded_db)
    tid = str(tenant.id)

    async with _client() as c:
        listed = (await c.get("/api/v1/operator/tenants", headers=OP)).json()["data"]
        one = (await c.get(f"/api/v1/operator/tenants/{tid}", headers=OP)).json()
        assert salt not in repr(listed) and salt not in repr(one)
        assert "user_id_salt" not in one["settings"]

        for value in ("attacker-known", None):
            bad = await c.patch(
                f"/api/v1/operator/tenants/{tid}",
                headers=OP,
                json={"settings": {"user_id_salt": value}},
            )
            assert bad.status_code == 400, bad.text
        bad = await c.post(
            "/api/v1/operator/tenants",
            headers=OP,
            json={"name": "Z", "settings": {"user_id_salt": "x"}},
        )
        assert bad.status_code == 400
        bad = await c.patch(
            f"/api/v1/operator/tenants/{tid}",
            headers=OP,
            json={"settings": {"store_raw_user_ids": "yes"}},
        )
        assert bad.status_code == 400

        ok = await c.patch(
            f"/api/v1/operator/tenants/{tid}",
            headers=OP,
            json={"settings": {"store_raw_user_ids": True}},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["settings"]["store_raw_user_ids"] is True
        assert salt not in ok.text

    # Merging other settings keeps the stored salt.
    assert await _salt(seeded_db) == salt


# ──────────────────────────────────────────────────────────────────────
# Migration 035
# ──────────────────────────────────────────────────────────────────────

MIGRATION_COLUMNS = (
    "user_id_hash",
    "user_id_raw",
    "user_group",
    "trace_id",
    "retry_count",
    "attempts",
    "stream_truncated",
    "stream_cancelled",
)


def _load_migration():
    pytest.importorskip("alembic")
    versions = Path(__file__).resolve().parents[2] / "db" / "alembic" / "versions"
    (path,) = versions.glob("*_usage_event_identity_trace.py")
    spec = importlib.util.spec_from_file_location("migration_identity_trace", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_matches_the_model():
    m = _load_migration()
    assert (m.revision, m.down_revision) == ("035", "034")
    assert tuple(name for name, _ in m.IDENTITY_TRACE_COLUMNS) == MIGRATION_COLUMNS
    assert set(MIGRATION_COLUMNS) <= set(AIUsageEvent.__table__.columns.keys())
    model_indexes = {i.name for i in AIUsageEvent.__table__.indexes}
    assert {name for name, _ in m.INDEXES} <= model_indexes
    assert m.SECRETS_TABLE == TenantSecret.__tablename__


def _columns(sync_conn, table):
    return {c["name"]: c for c in inspect(sync_conn).get_columns(table)}


def _indexes(sync_conn, table):
    return {i["name"] for i in inspect(sync_conn).get_indexes(table)}


def _tables(sync_conn):
    return set(inspect(sync_conn).get_table_names())


@pytest.mark.asyncio
async def test_migration_downgrade_and_upgrade(seeded_db):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    m = _load_migration()

    def run(sync_conn, step):
        ctx = MigrationContext.configure(sync_conn)
        with Operations.context(ctx):
            step()

    async with seeded_db.begin() as conn:
        await conn.run_sync(run, m.downgrade)
        cols = await conn.run_sync(_columns, "ai_usage_events")
        indexes = await conn.run_sync(_indexes, "ai_usage_events")
        tables = await conn.run_sync(_tables)
    assert "tenant_secrets" not in tables
    assert not set(MIGRATION_COLUMNS) & set(cols)
    assert not {name for name, _ in m.INDEXES} & indexes
    # 035 sits on 034: its downgrade leaves est_tokens_saved.
    assert "est_tokens_saved" in cols

    async with seeded_db.begin() as conn:
        await conn.run_sync(run, m.upgrade)
        cols = await conn.run_sync(_columns, "ai_usage_events")
        indexes = await conn.run_sync(_indexes, "ai_usage_events")
        secret_cols = await conn.run_sync(_columns, "tenant_secrets")
        policies = (
            await conn.exec_driver_sql(
                "SELECT policyname FROM pg_policies WHERE tablename = 'tenant_secrets'"
            )
        ).scalars().all()
    assert set(secret_cols) == {"tenant_id", "user_id_salt", "created_at"}
    assert not secret_cols["user_id_salt"]["nullable"]
    assert policies == ["tenant_isolation"]
    assert all(cols[f]["nullable"] for f in MIGRATION_COLUMNS)
    assert {name for name, _ in m.INDEXES} <= indexes

    resp, _ = await _ingest([GATEWAY_EVENT])
    assert resp.status_code == 200, resp.text
    (ev,) = await _events(seeded_db)
    assert ev.trace_id == "trace-7f3a" and ev.user_id_hash
    assert ev.user_id_hash == _hmac(await _salt(seeded_db), SUBJECT)
