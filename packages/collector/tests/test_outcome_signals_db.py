"""Implicit outcome signals (#44): the SDK's (#249) and gateway's
``outcome_signals`` / ``outcome_signals_version`` are stored on
ai_usage_events (known class names only) instead of landing in
ignored_fields, and counted per class in the Partner API usage summary.
"""

import ast
import importlib.util
from pathlib import Path

import pytest
from collector.app import app
from collector.ingest import OUTCOME_SIGNAL_CLASSES, IngestEvent
from db.models import AIUsageEvent
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

HEADERS = {"Authorization": "Bearer ps-test-key-12345"}
SDK_EVENT = {
    "vendor": "openai",
    "model": "gpt-4o",
    "source": "sdk",
    "tokens_in": 10,
    "tokens_out": 20,
    "outcome_signals": ["max_tokens", "regenerate"],
    "outcome_signals_version": 1,
}
REPO = Path(__file__).resolve().parents[3]


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _ingest(events):
    async with _client() as c:
        resp = await c.post("/ingest/events", headers=HEADERS, json={"events": events})
        loss = (await c.get("/telemetry/loss", headers=HEADERS)).json()
    return resp, loss


async def _events(engine):
    async with async_sessionmaker(engine, class_=AsyncSession)() as s:
        return (await s.execute(select(AIUsageEvent))).scalars().all()


def _sdk_signal_classes() -> set[str]:
    """SIGNAL_CLASSES of packages/sdk/prompt_shields/outcome_signals.py, read
    with ast (the collector does not ship the SDK)."""
    tree = ast.parse((REPO / "packages/sdk/prompt_shields/outcome_signals.py").read_text())
    consts, members = {}, None
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)):
            continue
        name = node.targets[0].id
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            consts[name] = node.value.value
        if name == "SIGNAL_CLASSES":
            members = [n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)]
    assert members, "SIGNAL_CLASSES not found"
    return {consts[m] for m in members if m in consts}


def test_classes_match_the_sdk():
    assert set(OUTCOME_SIGNAL_CLASSES) == _sdk_signal_classes()
    assert list(OUTCOME_SIGNAL_CLASSES) == sorted(OUTCOME_SIGNAL_CLASSES)


def test_validation():
    def parse(value, version=1):
        ev = IngestEvent(vendor="v", outcome_signals=value, outcome_signals_version=version)
        return ev.outcome_signals, ev.outcome_signals_version

    assert parse([]) == ([], 1)
    assert parse(["retry", "fallback", "retry"]) == (["fallback", "retry"], 1)
    assert parse(list(OUTCOME_SIGNAL_CLASSES)) == (list(OUTCOME_SIGNAL_CLASSES), 1)
    # A newer SDK's class is left out; the known ones are kept.
    assert parse(["max_tokens", "future_class"]) == (["max_tokens"], 1)
    for bad in (
        "max_tokens",
        {"max_tokens": True},
        [1],
        ["Max_Tokens"],
        ["max tokens"],
        ["I cannot help with that"],
        ["x" * 65],
        ["retry"] * 33,
    ):
        assert parse(bad) == (None, None), bad
    # The version is kept only alongside classes, and only in range.
    assert parse(["retry"], 0) == (["retry"], None)
    assert parse(["retry"], "1") == (["retry"], None)
    assert parse(["retry"], 1001) == (["retry"], None)
    assert parse(None, 1) == (None, None)


@pytest.mark.asyncio
async def test_signals_round_trip_and_are_not_ignored(seeded_db):
    resp, loss = await _ingest([SDK_EVENT, {**SDK_EVENT, "outcome_signals": []}])
    assert resp.status_code == 200, resp.text

    stored = sorted((e.outcome_signals for e in await _events(seeded_db)), key=len)
    assert stored == [[], ["max_tokens", "regenerate"]]
    assert {e.outcome_signals_version for e in await _events(seeded_db)} == {1}
    (stats,) = loss["clients"]
    assert not stats["ignored_fields"]


@pytest.mark.asyncio
async def test_old_event_stores_sql_null(seeded_db):
    resp, _ = await _ingest([{"vendor": "openai", "model": "gpt-4o"}])
    assert resp.status_code == 200, resp.text
    async with async_sessionmaker(seeded_db, class_=AsyncSession)() as s:
        n = await s.scalar(
            select(func.count()).where(
                AIUsageEvent.outcome_signals.is_(None),
                AIUsageEvent.outcome_signals_version.is_(None),
            )
        )
    assert n == 1


@pytest.mark.asyncio
async def test_bad_signals_cost_only_that_field(seeded_db):
    bad = {**SDK_EVENT, "outcome_signals": ["refusal", "the model said: no"]}
    resp, loss = await _ingest([bad])
    assert resp.status_code == 200, resp.text
    (ev,) = await _events(seeded_db)
    assert ev.outcome_signals is None and ev.outcome_signals_version is None
    assert ev.tokens_out == 20
    (stats,) = loss["clients"]
    assert stats["ignored_fields"] == {
        "outcome_signals:invalid": 1,
        "outcome_signals_version:invalid": 1,
    }


@pytest.mark.asyncio
async def test_usage_summary_counts_signals(seeded_db):
    events = [
        SDK_EVENT,
        {**SDK_EVENT, "outcome_signals": ["max_tokens"]},
        {**SDK_EVENT, "outcome_signals": []},
        {**SDK_EVENT, "outcome_signals": None},  # not reported
    ]
    resp, _ = await _ingest(events)
    assert resp.status_code == 200, resp.text

    from collector.partner_auth import hash_secret, issue_access_token
    from db.models import PartnerCredential, Tenant

    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        tenant = (await s.execute(select(Tenant))).scalar_one()
        asset_id = (await s.execute(select(AIUsageEvent.asset_id))).scalars().first()
        cred = PartnerCredential(
            tenant_id=tenant.id,
            partner_name="p",
            client_id="p-client",
            client_secret_hash=hash_secret("x"),
            scopes=["usage:read"],
        )
        s.add(cred)
        await s.commit()
        token, _ = issue_access_token(tenant.id, cred.id, "p-client", ["usage:read"])

    async with _client() as c:
        r = await c.get(
            f"/partner/v1/assets/{asset_id}/usage-summary",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome_signals_reported"] == 3
    counts = body["outcome_signal_counts"]
    assert set(counts) == set(OUTCOME_SIGNAL_CLASSES)
    assert counts["max_tokens"] == 2
    assert counts["regenerate"] == 1
    assert sum(counts.values()) == 3


# ──────────────────────────────────────────────────────────────────────
# Migration (outcome signals)
# ──────────────────────────────────────────────────────────────────────


def _load_migration():
    pytest.importorskip("alembic")
    (path,) = (REPO / "packages/db/alembic/versions").glob("*_outcome_signals.py")
    spec = importlib.util.spec_from_file_location("migration_outcome_signals", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _columns(sync_conn, table):
    return {c["name"]: c for c in inspect(sync_conn).get_columns(table)}


@pytest.mark.asyncio
async def test_migration_downgrade_and_upgrade(seeded_db):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    m = _load_migration()
    names = [n for n, _ in m.OUTCOME_COLUMNS]
    assert names == ["outcome_signals", "outcome_signals_version"]

    def run(sync_conn, step):
        ctx = MigrationContext.configure(sync_conn)
        with Operations.context(ctx):
            step()

    async with seeded_db.begin() as conn:
        await conn.run_sync(run, m.downgrade)
        cols = await conn.run_sync(_columns, "ai_usage_events")
    assert not set(names) & set(cols)

    async with seeded_db.begin() as conn:
        await conn.run_sync(run, m.upgrade)
        cols = await conn.run_sync(_columns, "ai_usage_events")
    assert all(cols[n]["nullable"] for n in names)

    resp, _ = await _ingest([SDK_EVENT])
    assert resp.status_code == 200, resp.text
