"""Usage rollups and the usage time series (#346) against PostgreSQL
(PS_TEST_DATABASE_URL).

Covers: hourly and daily rollups equal to the raw events to the token,
incremental refresh (new and late events), rollups surviving the raw
purge, the rebuild, asset merges, rollup retention, and the Partner API
series and export (grouping, paging, windows, scopes, tenant isolation).
Row-level security is covered in test_rls_db.py.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from collector import retention, usage_rollups
from collector.app import app
from collector.config import settings
from collector.partner_auth import hash_secret, issue_access_token
from db.models import (
    AIAsset,
    AIUsageEvent,
    PartnerAuditLog,
    PartnerCredential,
    Tenant,
    UsageRollupDaily,
    UsageRollupHourly,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

D = Decimal
SERIES = "/partner/v1/usage/series"
EXPORT = "/partner/v1/usage/series/export"
# Midday yesterday and the day before (UTC): every event is in the past and
# the hours used around them stay inside their own day.
TODAY = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=1)
YESTERDAY = TODAY - timedelta(days=1)


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 0)


def _ev(tenant, asset, at, **fields):
    return AIUsageEvent(
        tenant_id=tenant, asset_id=asset, timestamp=at, source="gateway", **fields
    )


@pytest.fixture
async def env(seeded_db):
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        a = (await s.execute(select(Tenant))).scalar_one()
        b = Tenant(name="B", settings={})
        s.add(b)
        await s.flush()
        support = AIAsset(tenant_id=a.id, vendor="openai", model="gpt-4o",
                          use_case_name="support", discovery_source=["sdk"])
        search = AIAsset(tenant_id=a.id, vendor="mistral", model="small",
                         use_case_name="search", discovery_source=["sdk"])
        other = AIAsset(tenant_id=b.id, vendor="openai", model="gpt-4o",
                        use_case_name="support", discovery_source=["sdk"])
        s.add_all([support, search, other])
        await s.flush()
        attempts_ok = [{"attempt": 1, "latency_ms": 10, "status": 200}]
        attempts_err = [
            {"attempt": 1, "latency_ms": 10, "status": 200},
            {"attempt": 2, "latency_ms": 10, "status": 503, "error_class": "server_error"},
        ]
        s.add_all([
            _ev(a.id, support.id, TODAY, tokens_in=100, tokens_out=50, tokens_cached=20,
                tokens_cache_write=5, tokens_reasoning=7, cost=D("0.010"),
                served_model="gpt-4o-2024-08-06", requested_model="auto",
                served_provider="openai", api_surface="chat", user_id_hash="h-alice",
                cache_status="miss", attempts=attempts_ok),
            _ev(a.id, support.id, TODAY + timedelta(minutes=30), tokens_in=10, tokens_out=5,
                cost=D("0.002"), served_provider="openai", requested_model="gpt-4o",
                api_surface="chat", user_id_hash="h-alice", cache_status="hit",
                cache_saved_cost=D("0.004"), attempts=attempts_err),
            _ev(a.id, search.id, TODAY + timedelta(hours=1), tokens_in=1, tokens_out=2,
                served_model="small", served_provider="mistral", api_surface="responses",
                user_id_hash="h-bob"),
            # No cost, no user, no model.
            _ev(a.id, search.id, YESTERDAY, tokens_in=3),
            _ev(a.id, support.id, YESTERDAY + timedelta(hours=3), tokens_in=4, cost=D("1.5"),
                served_model="gpt-4o", served_provider="openai", user_id_hash="h-bob"),
            # Tenant B never shows in tenant A's rollups or series.
            _ev(b.id, other.id, TODAY, tokens_in=999, cost=D("9.0"), user_id_hash="h-alice"),
        ])

        def partner(tenant, name, scopes):
            return PartnerCredential(tenant_id=tenant, partner_name=name,
                                     client_id=f"{name}-client",
                                     client_secret_hash=hash_secret("x"),
                                     scopes=list(scopes), is_active="true")

        pa = partner(a.id, "pa", ["usage:read", "usage:export"])
        pb = partner(b.id, "pb", ["usage:read"])
        s.add_all([pa, pb])
        await s.commit()

    def bearer(tenant, p):
        token, _ = issue_access_token(tenant, p.id, p.client_id, p.scopes)
        return {"Authorization": f"Bearer {token}"}

    return {
        "factory": factory,
        "a": a.id,
        "b": b.id,
        "support": support.id,
        "search": search.id,
        "h_a": bearer(a.id, pa),
        "h_b": bearer(b.id, pb),
    }


async def _refresh(env, tenant=None):
    async with env["factory"]() as db:
        return await usage_rollups.refresh_tenant(db, tenant or env["a"])


async def _raw_daily(db, tenant):
    """Per UTC day straight from the raw events: the acceptance check."""
    return {
        r.day: (r.requests, r.tin, r.tout, r.tcached, r.tcw, r.treason, r.cost)
        for r in (
            await db.execute(
                text(
                    "SELECT date_trunc('day', timestamp, 'UTC') AS day, count(*) AS requests, "
                    "coalesce(sum(tokens_in),0) AS tin, coalesce(sum(tokens_out),0) AS tout, "
                    "coalesce(sum(tokens_cached),0) AS tcached, "
                    "coalesce(sum(tokens_cache_write),0) AS tcw, "
                    "coalesce(sum(tokens_reasoning),0) AS treason, "
                    "coalesce(sum(cost),0) AS cost FROM ai_usage_events "
                    "WHERE tenant_id = :t GROUP BY 1"
                ),
                {"t": tenant},
            )
        ).all()
    }


async def _rollup_daily(db, tenant):
    t = UsageRollupDaily
    rows = (
        await db.execute(
            select(
                t.bucket_start,
                func.sum(t.requests), func.sum(t.tokens_in), func.sum(t.tokens_out),
                func.sum(t.tokens_cached), func.sum(t.tokens_cache_write),
                func.sum(t.tokens_reasoning), func.sum(t.cost),
            )
            .where(t.tenant_id == tenant)
            .group_by(t.bucket_start)
        )
    ).all()
    return {r[0]: tuple(r[1:]) for r in rows}


async def test_daily_rollups_equal_raw_events_to_the_token(env):
    result = await _refresh(env)
    assert not result.skipped
    assert result.hours == 4 and result.days == 2
    async with env["factory"]() as db:
        raw = await _raw_daily(db, env["a"])
        assert await _rollup_daily(db, env["a"]) == raw
        assert set(raw) == {YESTERDAY.replace(hour=0), TODAY.replace(hour=0)}
        # Hourly rows sum to the same, and tenant B is its own.
        hourly = (
            await db.execute(
                select(func.sum(UsageRollupHourly.tokens_in)).where(
                    UsageRollupHourly.tenant_id == env["a"]
                )
            )
        ).scalar_one()
        assert hourly == 100 + 10 + 1 + 3 + 4
    await _refresh(env, env["b"])
    async with env["factory"]() as db:
        assert await _rollup_daily(db, env["b"]) == await _raw_daily(db, env["b"])


async def test_dimensions_and_derived_counts(env):
    await _refresh(env)
    async with env["factory"]() as db:
        rows = (
            await db.execute(
                select(UsageRollupHourly).where(
                    UsageRollupHourly.tenant_id == env["a"],
                    UsageRollupHourly.bucket_start == TODAY,
                )
            )
        ).scalars().all()
    by_model = {r.model: r for r in rows}
    # Served model wins; the requested one stands in when none was served.
    assert set(by_model) == {"gpt-4o-2024-08-06", "gpt-4o"}
    first, second = by_model["gpt-4o-2024-08-06"], by_model["gpt-4o"]
    assert (first.provider, first.api_surface, first.user_id_hash) == ("openai", "chat", "h-alice")
    assert first.asset_id == env["support"]
    assert (first.cache_lookups, first.cache_hits, first.error_requests) == (1, 0, 0)
    assert (second.cache_lookups, second.cache_hits, second.error_requests) == (1, 1, 1)
    assert second.cache_saved_cost == D("0.004")
    assert first.attempts_reported == second.attempts_reported == 1


async def _age_ingested(env):
    """Move every stored event's ingested_at out of the refresh overlap."""
    async with env["factory"]() as db:
        await db.execute(
            update(AIUsageEvent).values(ingested_at=func.now() - timedelta(hours=1))
        )
        await db.commit()


async def test_incremental_refresh_picks_up_new_and_late_events(env):
    await _refresh(env)
    await _age_ingested(env)
    async with env["factory"]() as db:
        db.add_all([
            _ev(env["a"], env["support"], TODAY + timedelta(minutes=5), tokens_in=1000),
            # Late: ingested now for a day the rollup already covered.
            _ev(env["a"], env["search"], YESTERDAY - timedelta(hours=2), tokens_out=77),
        ])
        await db.commit()
    result = await _refresh(env)
    # Only the touched hours (and their days) were recomputed.
    assert (result.hours, result.days) == (2, 2)
    async with env["factory"]() as db:
        assert await _rollup_daily(db, env["a"]) == await _raw_daily(db, env["a"])
    # Nothing new: only the overlap's own hours again.
    again = await _refresh(env)
    assert again.hours <= 2
    await _age_ingested(env)
    assert (await _refresh(env)).hours == 0


async def test_rollups_survive_the_raw_purge(env):
    await _refresh(env)
    async with env["factory"]() as db:
        before = await _rollup_daily(db, env["a"])
        # Shorten retention so yesterday's events are purged.
        await db.execute(
            update(Tenant).where(Tenant.id == env["a"]).values(settings={"retention_days": 1})
        )
        await db.commit()
    async with env["factory"]() as db:
        purged = await retention.purge_tenant_events(db, env["a"], now=TODAY + timedelta(hours=6))
        await db.commit()
    assert purged.events_deleted == 2
    assert purged.usage_rollups_deleted == 0
    # A refresh after the purge never shrinks the rollups of purged hours.
    await _refresh(env)
    async with env["factory"]() as db:
        assert await _rollup_daily(db, env["a"]) == before


async def test_rollup_retention_is_separate_and_purged(env, monkeypatch):
    await _refresh(env)
    monkeypatch.setattr(settings, "usage_rollup_retention_days", 400)
    later = TODAY.replace(hour=0) + timedelta(days=400)
    async with env["factory"]() as db:
        dry = await retention.purge_tenant_events(db, env["a"], now=later, dry_run=True)
        # The earlier day's 2 hourly + 2 daily rows are past 400 days.
        assert dry.usage_rollups_deleted == 4
        done = await retention.purge_tenant_events(db, env["a"], now=later)
        assert done.usage_rollups_deleted == 4
    # Never shorter than raw retention.
    monkeypatch.setattr(settings, "usage_rollup_retention_days", 10)
    assert retention.rollup_retention_days({"retention_days": 90}) == 90


async def test_rebuild_drops_buckets_whose_events_are_gone(env):
    await _refresh(env)
    async with env["factory"]() as db:
        await db.execute(delete(AIUsageEvent).where(AIUsageEvent.asset_id == env["search"]))
        await db.commit()
    async with env["factory"]() as db:
        result = await usage_rollups.rebuild_tenant(db, env["a"])
    assert not result.skipped
    async with env["factory"]() as db:
        assert await _rollup_daily(db, env["a"]) == await _raw_daily(db, env["a"])
        left = (
            await db.execute(
                select(func.count()).where(UsageRollupHourly.asset_id == env["search"])
            )
        ).scalar_one()
        assert left == 0


async def test_a_concurrent_run_skips_the_tenant(env):
    async with env["factory"]() as holder:
        assert await usage_rollups._try_lock(holder, env["a"])
        result = await _refresh(env)
        assert result.skipped
        await holder.rollback()
    assert not (await _refresh(env)).skipped


async def test_a_catch_up_commits_in_steps_and_yields_the_lock(env, monkeypatch):
    # One day per transaction: the two days of tenant A take two steps.
    monkeypatch.setattr(usage_rollups, "BATCH_DAYS", 1)
    result = await _refresh(env)
    assert (result.hours, result.days, result.skipped) == (4, 2, False)
    async with env["factory"]() as db:
        assert await _rollup_daily(db, env["a"]) == await _raw_daily(db, env["a"])
        await db.execute(delete(UsageRollupHourly))
        await db.execute(delete(UsageRollupDaily))
        await db.execute(text("DELETE FROM usage_rollup_state"))
        await db.commit()

    # Another run takes the lock between the steps: this one stops without
    # moving the watermark, so the next run does the whole catch-up again.
    real = usage_rollups._try_lock
    calls = 0

    async def lock_once(db, tenant_id):
        nonlocal calls
        calls += 1
        return calls == 1 and await real(db, tenant_id)

    monkeypatch.setattr(usage_rollups, "_try_lock", lock_once)
    assert (await _refresh(env)).skipped
    async with env["factory"]() as db:
        assert await usage_rollups.watermark(db, env["a"]) is None
    monkeypatch.setattr(usage_rollups, "_try_lock", real)
    assert (await _refresh(env)).hours == 4
    async with env["factory"]() as db:
        assert await _rollup_daily(db, env["a"]) == await _raw_daily(db, env["a"])


def test_day_batches_keep_whole_days():
    hours = [YESTERDAY, YESTERDAY + timedelta(hours=1), TODAY, TODAY + timedelta(hours=3)]
    assert usage_rollups._day_batches(hours) == [hours]
    assert usage_rollups._day_batches([]) == []


async def test_asset_merge_repoints_rollups(env):
    from collector import asset_merge

    await _refresh(env)
    async with env["factory"]() as db:
        await asset_merge._repoint(db, UsageRollupDaily, env["a"], env["search"], env["support"])
        await db.commit()
        n = (
            await db.execute(
                select(func.count()).where(UsageRollupDaily.asset_id == env["search"])
            )
        ).scalar_one()
    assert n == 0


# ── Partner API ─────────────────────────────────────────────────────────


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_series_by_user_day(env):
    await _refresh(env)
    params = {"group_by": "user", "granularity": "day",
              "from": (YESTERDAY - timedelta(days=1)).isoformat()}
    async with _client() as c:
        r = await c.get(SERIES, params=params, headers=env["h_a"])
    assert r.status_code == 200, r.text
    body = r.json()
    points = {(p["bucket_start"][:10], p["key"]): p for p in body["data"]}
    today, yday = TODAY.strftime("%Y-%m-%d"), YESTERDAY.strftime("%Y-%m-%d")
    assert set(points) == {(yday, None), (yday, "h-bob"), (today, "h-alice"), (today, "h-bob")}
    alice = points[(today, "h-alice")]
    assert alice["requests"] == 2
    assert alice["tokens_in"] == 110 and alice["tokens_cached"] == 20
    assert alice["cost_usd"] == pytest.approx(0.012)
    assert alice["cache_hit_rate"] == 0.5
    assert alice["cache_saved_usd"] == pytest.approx(0.004)
    assert alice["error_rate"] == 0.5
    no_user = points[(yday, None)]
    assert no_user["cost_usd"] is None and no_user["error_rate"] is None
    assert body["meta"]["group_by"] == "user" and body["meta"]["has_more"] is False
    assert body["meta"]["rolled_up_at"] is not None
    # Raw ids and the salt never appear.
    assert "user_id_raw" not in r.text and "salt" not in r.text
    async with env["factory"]() as db:
        audit = (
            await db.execute(
                select(PartnerAuditLog).where(PartnerAuditLog.endpoint == SERIES)
            )
        ).scalars().all()
    assert len(audit) == 1


@pytest.mark.parametrize(
    ("group_by", "keys"),
    [
        ("project", {"support", "search"}),
        ("model", {"gpt-4o-2024-08-06", "gpt-4o", "small", None}),
        ("provider", {"openai", "mistral", None}),
        ("api_surface", {"chat", "responses", None}),
        ("none", {None}),
    ],
)
async def test_series_group_by(env, group_by, keys):
    await _refresh(env)
    params = {"group_by": group_by, "granularity": "week",
              "from": (TODAY - timedelta(days=14)).isoformat()}
    async with _client() as c:
        r = await c.get(SERIES, params=params, headers=env["h_a"])
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert {p["key"] for p in data} == keys
    assert sum(p["requests"] for p in data) == 5
    for p in data:
        start = datetime.fromisoformat(p["bucket_start"])
        assert start.weekday() == 0 and start.hour == 0


async def test_series_by_asset_hourly_with_paging(env):
    await _refresh(env)
    params = {"group_by": "asset", "granularity": "hour", "limit": 2,
              "from": (YESTERDAY - timedelta(hours=1)).isoformat()}
    seen = []
    async with _client() as c:
        cursor = None
        for _ in range(5):
            q = dict(params, **({"cursor": cursor} if cursor else {}))
            r = await c.get(SERIES, params=q, headers=env["h_a"])
            assert r.status_code == 200, r.text
            seen += r.json()["data"]
            cursor = r.json()["meta"]["next_cursor"]
            if not cursor:
                break
        bad = await c.get(SERIES, params={"cursor": "nope!"}, headers=env["h_a"])
    assert bad.status_code == 400
    assert len(seen) == 4
    assert [p["bucket_start"] for p in seen] == sorted(p["bucket_start"] for p in seen)
    assert {p["key"] for p in seen} == {str(env["support"]), str(env["search"])}


async def test_series_tenant_isolation_and_windows(env):
    await _refresh(env)
    await _refresh(env, env["b"])
    async with _client() as c:
        b = await c.get(SERIES, params={"group_by": "user"}, headers=env["h_b"])
        backwards = await c.get(
            SERIES, params={"from": TODAY.isoformat(), "to": YESTERDAY.isoformat()},
            headers=env["h_a"],
        )
        too_long = await c.get(
            SERIES,
            params={"granularity": "hour", "from": (TODAY - timedelta(days=100)).isoformat()},
            headers=env["h_a"],
        )
        bad_group = await c.get(SERIES, params={"group_by": "team"}, headers=env["h_a"])
    assert b.status_code == 200
    assert [(p["key"], p["tokens_in"]) for p in b.json()["data"]] == [("h-alice", 999)]
    assert backwards.status_code == 400 and too_long.status_code == 400
    assert bad_group.status_code == 422


async def test_series_is_clamped_to_rollup_retention(env, monkeypatch):
    await _refresh(env)
    monkeypatch.setattr(settings, "usage_rollup_retention_days", 0)
    async with env["factory"]() as db:
        await db.execute(
            update(Tenant).where(Tenant.id == env["a"]).values(settings={"retention_days": 1})
        )
        await db.commit()
    async with _client() as c:
        r = await c.get(
            SERIES, params={"from": (TODAY - timedelta(days=5)).isoformat()}, headers=env["h_a"]
        )
    assert r.status_code == 200
    start = datetime.fromisoformat(r.json()["meta"]["from"])
    assert start >= datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)


async def test_export_csv_and_jsonl(env):
    await _refresh(env)
    params = {"group_by": "provider", "granularity": "day",
              "from": (YESTERDAY - timedelta(days=1)).isoformat()}
    async with _client() as c:
        as_csv = await c.get(EXPORT, params=params, headers=env["h_a"])
        as_jsonl = await c.get(EXPORT, params=dict(params, format="jsonl"), headers=env["h_a"])
        no_scope = await c.get(EXPORT, params=params, headers=env["h_b"])
    assert as_csv.status_code == 200, as_csv.text
    assert as_csv.headers["content-type"].startswith("text/csv")
    assert "attachment" in as_csv.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(as_csv.text)))
    assert tuple(rows[0]) == usage_rollups.SERIES_COLUMNS
    assert sum(int(r["requests"]) for r in rows) == 5
    lines = [json.loads(x) for x in as_jsonl.text.splitlines()]
    assert len(lines) == len(rows)
    assert {x["key"] for x in lines} == {"openai", "mistral", None}
    assert no_scope.status_code == 403


async def test_export_neutralises_formulas():
    rows = [dict.fromkeys(usage_rollups.SERIES_COLUMNS, 0) | {"key": "=cmd()", "cost_usd": None}]
    out = usage_rollups.serialize(rows, "csv")
    assert "'=cmd()" in out
    assert out.splitlines()[1].split(",")[usage_rollups.SERIES_COLUMNS.index("cost_usd")] == ""


def test_window_rounding_and_cursor():
    now = datetime(2026, 11, 6, 15, 30, tzinfo=UTC)  # a Friday
    w = usage_rollups.resolve_window("week", None, None, {}, now)
    assert w.start.weekday() == 0 and w.end == datetime(2026, 11, 9, tzinfo=UTC)
    w = usage_rollups.resolve_window("hour", None, None, {}, now)
    assert (w.start, w.end) == (
        datetime(2026, 11, 4, 15, tzinfo=UTC), datetime(2026, 11, 6, 16, tzinfo=UTC)
    )
    with pytest.raises(ValueError):
        usage_rollups.resolve_window("day", now, now - timedelta(days=1), {}, now)
    assert usage_rollups.decode_cursor(usage_rollups.encode_cursor(42)) == 42
    assert usage_rollups.decode_cursor(None) == 0
    with pytest.raises(ValueError):
        usage_rollups.decode_cursor(usage_rollups.encode_cursor(1)[:-1] + "@")
