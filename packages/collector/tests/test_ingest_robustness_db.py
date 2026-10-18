"""Ingest must not 500 or lose a batch on data the collector can't store as
sent (PS_TEST_DATABASE_URL):

- several assets sharing the SDK's merge key (e.g. one platform_signal asset
  per provider API key for the same model) used to raise
  ``MultipleResultsFound`` in ``dedup.find_or_create_asset``;
- concurrent ingests for a new merge key used to create duplicate assets;
- ``cost`` beyond ``Numeric(10, 6)`` and token/latency counts beyond
  ``INTEGER`` used to fail the insert;
- an upper-case ``api_key_fingerprint`` was dropped as invalid.
"""

import asyncio
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from collector import provider_usage as pu
from collector.app import app
from collector.dedup import find_or_create_asset
from db.models import AIAsset, AIUsageEvent, Tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

HEADERS = {"Authorization": "Bearer ps-test-key-12345"}
DAY = datetime(2026, 10, 1, tzinfo=UTC)


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _ingest(events):
    async with _client() as c:
        resp = await c.post("/ingest/events", headers=HEADERS, json={"events": events})
        loss = (await c.get("/telemetry/loss", headers=HEADERS)).json()
    return resp, loss


def _factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _tenant_id(engine):
    async with _factory(engine)() as s:
        return (await s.execute(select(Tenant.id))).scalar_one()


async def _rows(engine):
    async with _factory(engine)() as s:
        events = (await s.execute(select(AIUsageEvent))).scalars().all()
        assets = (await s.execute(select(AIAsset))).scalars().all()
    return events, assets


def _invalid(loss):
    (stats,) = loss["clients"]
    return {k: v for k, v in stats["ignored_fields"].items() if k.endswith(":invalid")}


def _usage(key_id, start):
    return pu.UsageRecord(
        provider="openai",
        endpoint="completions",
        bucket_start=start,
        bucket_end=start + timedelta(days=1),
        project_id="proj_a",
        api_key_id=key_id,
        model="gpt-4o",
        tokens_in=10,
        tokens_out=5,
        requests=1,
    )


# --- duplicate merge keys --------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_ingest_with_two_platform_signal_assets_for_one_model(seeded_db):
    tenant_id = await _tenant_id(seeded_db)
    # The usage puller keeps one asset per (project, key, model): two keys
    # calling gpt-4o give two openai/gpt-4o assets by design.
    async with _factory(seeded_db)() as db:
        await pu.store_usage(
            db,
            tenant_id,
            [_usage("key_b", DAY + timedelta(days=1)), _usage("key_a", DAY)],
        )
        await db.commit()

    sdk = {"vendor": "openai", "model": "gpt-4o", "source": "sdk", "tokens_in": 3}
    for _ in range(2):  # deterministic: both land on the same asset
        resp, _loss = await _ingest([sdk])
        assert resp.status_code == 200, resp.text
        assert resp.json()["assets_created"] == 0

    events, assets = await _rows(seeded_db)
    assert len(assets) == 2
    # The oldest is the one created first, not the one whose (backdated)
    # first_seen is earliest (dedup.OLDEST_FIRST).
    oldest = min(assets, key=lambda a: (a.created_at, a.id))
    sdk_events = [e for e in events if e.source == "sdk"]
    assert len(sdk_events) == 2
    assert {e.asset_id for e in sdk_events} == {oldest.id}
    assert "sdk" in oldest.discovery_source


@pytest.mark.asyncio
async def test_event_matching_several_sdk_assets(seeded_db):
    # Two SDK use cases on one model; an event without a use case matches
    # both (the merge key ignores fields the event doesn't send).
    base = {"vendor": "openai", "model": "gpt-4o", "source": "sdk"}
    resp, _ = await _ingest(
        [{**base, "use_case_name": "a"}, {**base, "use_case_name": "b"}]
    )
    assert resp.json()["assets_created"] == 2

    for _ in range(2):
        resp, _ = await _ingest([{**base, "session_id": "no-use-case"}])
        assert resp.status_code == 200, resp.text
        assert resp.json()["assets_created"] == 0

    events, assets = await _rows(seeded_db)
    assert len(assets) == 2
    # Deterministic: both land on the same (oldest) asset.
    hits = {e.asset_id for e in events if e.session_id == "no-use-case"}
    assert hits == {min(assets, key=lambda a: (a.first_seen, a.id)).id}


@pytest.mark.asyncio
async def test_concurrent_ingest_does_not_duplicate_new_asset(seeded_db):
    tenant_id = await _tenant_id(seeded_db)
    factory = _factory(seeded_db)
    kwargs = {
        "tenant_id": tenant_id,
        "vendor": "openai",
        "model": "gpt-4.1",
        "use_case_name": "race",
        "business_unit": None,
        "calling_service": None,
        "source": "sdk",
    }
    async with factory() as first, factory() as second:
        a1, new1 = await find_or_create_asset(db=first, **kwargs)
        await first.flush()
        assert new1

        # The second transaction must wait for the first to commit, then
        # see its row instead of inserting a duplicate.
        task = asyncio.create_task(find_or_create_asset(db=second, **kwargs))
        await asyncio.sleep(0.3)
        await first.commit()
        a2, new2 = await asyncio.wait_for(task, timeout=10)
        await second.commit()

    assert not new2
    assert a2.id == a1.id
    _, assets = await _rows(seeded_db)
    assert len(assets) == 1


@pytest.mark.asyncio
async def test_concurrent_batches_in_different_orders_do_not_deadlock(
    seeded_db, monkeypatch
):
    # Each batch updates (or creates) the same assets, in its own order.
    # Without locking them up front in one order, two such batches can wait
    # on each other's rows and Postgres aborts one with a deadlock (500).
    from collector.config import settings

    monkeypatch.setattr(settings, "rate_limit_per_minute", 0)
    keys = [{"vendor": "openai", "model": f"m{i}", "source": "sdk"} for i in range(6)]
    rng = random.Random(195)

    async def post(events):
        async with _client() as c:
            return await c.post(
                "/ingest/events", headers=HEADERS, json={"events": events}
            )

    created = 0
    for _ in range(6):  # the first round creates the assets, later ones update
        batches = [rng.sample(keys, len(keys)) for _ in range(4)]
        resps = await asyncio.gather(*(post(b) for b in batches))
        assert [r.status_code for r in resps] == [200] * 4, [r.text for r in resps]
        created += sum(r.json()["assets_created"] for r in resps)

    assert created == len(keys)
    events, assets = await _rows(seeded_db)
    assert len(assets) == len(keys)
    assert len(events) == 6 * 4 * len(keys)


@pytest.mark.asyncio
async def test_batch_of_many_new_keys_holds_bounded_advisory_locks(seeded_db):
    # One advisory lock per new key would let a single large batch fill
    # Postgres's shared lock table for every tenant. Locks are per bucket.
    from collector.dedup import ASSET_LOCK_BUCKETS, AssetKey, lock_asset_keys
    from sqlalchemy import text

    tenant_id = await _tenant_id(seeded_db)
    keys = [AssetKey("openai", f"m{i}") for i in range(500)]
    async with _factory(seeded_db)() as db:
        await lock_asset_keys(db, tenant_id, keys)
        held = (
            await db.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
                    " AND pid = pg_backend_pid()"
                )
            )
        ).scalar_one()
        await db.rollback()
    assert 1 < held <= ASSET_LOCK_BUCKETS

    resp, _ = await _ingest(
        [{"vendor": "openai", "model": f"m{i}", "source": "sdk"} for i in range(500)]
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["assets_created"] == 500


# --- cost and count bounds -------------------------------------------------


@pytest.mark.asyncio
async def test_out_of_range_cost_is_dropped_not_the_batch(seeded_db):
    base = {"vendor": "openai", "model": "gpt-4o", "source": "sdk"}
    resp, loss = await _ingest(
        [
            {**base, "cost": 10000, "session_id": "too-big"},
            {**base, "cost": 9999.9999999, "session_id": "rounds-over"},
            {**base, "cost": -0.01, "session_id": "negative"},
            {**base, "cost": 0.0123, "session_id": "ok"},
            {**base, "cost": 9999.999999, "session_id": "max"},
        ]
    )
    assert resp.status_code == 200, resp.text
    assert _invalid(loss) == {"cost:invalid": 3}

    events, _ = await _rows(seeded_db)
    cost = {e.session_id: e.cost for e in events}
    assert cost == {
        "too-big": None,
        "rounds-over": None,
        "negative": None,
        "ok": Decimal("0.012300"),
        "max": Decimal("9999.999999"),
    }


def test_lenient_cost_rejects_non_finite_and_wrong_types():
    from collector.ingest import IngestEvent

    for value in (float("inf"), float("nan"), -1, 1e4, 10**400, "0.5", True):
        assert IngestEvent(vendor="v", cost=value).cost is None
    assert IngestEvent(vendor="v", cost=0).cost == 0
    assert IngestEvent(vendor="v", cost=None).cost is None


@pytest.mark.asyncio
async def test_out_of_range_core_counts_are_dropped_not_the_batch(seeded_db):
    base = {"vendor": "openai", "model": "gpt-4o", "source": "sdk"}
    resp, loss = await _ingest(
        [
            {**base, "tokens_in": 2**31, "tokens_out": -1, "latency_ms": 2**40},
            {**base, "tokens_in": 7, "tokens_out": 8, "latency_ms": 9},
        ]
    )
    assert resp.status_code == 200, resp.text
    assert _invalid(loss) == {
        "tokens_in:invalid": 1,
        "tokens_out:invalid": 1,
        "latency_ms:invalid": 1,
    }
    events, _ = await _rows(seeded_db)
    counts = {(e.tokens_in, e.tokens_out, e.latency_ms) for e in events}
    assert counts == {(None, None, None), (7, 8, 9)}


# --- fingerprint normalisation ---------------------------------------------


@pytest.mark.asyncio
async def test_uppercase_fingerprint_is_normalised(seeded_db):
    event = {
        "vendor": "openai",
        "model": "gpt-4o",
        "source": "sdk",
        "api_key_fingerprint": " 0123456789ABCDEF ",
    }
    resp, loss = await _ingest([event])
    assert resp.status_code == 200, resp.text
    assert _invalid(loss) == {}
    (ev,), _ = await _rows(seeded_db)
    assert ev.api_key_fingerprint == "0123456789abcdef"


def test_fingerprint_still_rejects_non_hex():
    from collector.ingest import IngestEvent

    for value in ("sk-proj-abcdef0123456789", "0123456789abcdeg", "0123", 12):
        assert (
            IngestEvent(vendor="v", api_key_fingerprint=value).api_key_fingerprint
            is None
        )
