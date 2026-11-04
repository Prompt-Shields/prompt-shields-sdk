"""Smoke run of the ingest load test (benchmarks/ingest_load.py, #82).

A few dozen requests against the real collector on a local port and the test
database: the per-tenant limit holds under concurrency, 429s carry
Retry-After, and the database holds exactly the accepted events. The full
load test is run by hand; see the module docstring.
"""

import asyncio

import pytest
from benchmarks.ingest_load import (
    LoadConfig,
    LoadReport,
    Sample,
    _bypass_sessions,
    delete_tenants,
    is_local_database,
    percentile,
    run,
    seed_tenants,
)
from db.models import Tenant
from sqlalchemy import func, select


def test_percentile_nearest_rank():
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 50) == 50
    assert percentile(values, 99) == 99
    assert percentile(values, 100) == 100
    assert percentile([7.0], 95) == 7


def test_report_flags_a_window_over_the_limit():
    cfg = LoadConfig(database_url="", tenants=1, events_per_request=1, rate_limit=2)
    samples = [Sample(0, 200, 1.0, window="60") for _ in range(3)]
    report = LoadReport(cfg, 1.0, samples, stored_events={0: 3}, rejected_events={0: 0})
    assert report.failures() == ["tenant 0 window 60: 3 accepted > limit 2"]


@pytest.mark.parametrize(
    "url, local",
    [
        ("postgresql+asyncpg://u:p@localhost:5432/db", True),
        ("postgresql+asyncpg://u:p@127.0.0.1/db", True),
        ("postgresql+asyncpg://u:p@[::1]:5432/db", True),
        ("postgresql+asyncpg://u:p@/db?host=/var/run/postgresql", True),
        ("postgresql+asyncpg://u:p@db.prod.example.com:5432/db", False),
        ("postgresql+asyncpg://u:p@localhost/db?host=db.prod.example.com", False),
    ],
)
def test_is_local_database(url, local):
    assert is_local_database(url) is local


def test_run_refuses_a_remote_database():
    cfg = LoadConfig(database_url="postgresql+asyncpg://u:p@db.example.com/db")
    with pytest.raises(ValueError, match="non-local"):
        run(cfg)


def test_delete_tenants_removes_seeded_tenants(test_database_url):
    async def scenario():
        ids = [t for t, _ in await seed_tenants(test_database_url, 2)]
        await delete_tenants(test_database_url, ids)
        engine, sessions = _bypass_sessions(test_database_url)
        try:
            async with sessions() as session:
                return await session.scalar(
                    select(func.count()).where(Tenant.id.in_(ids))
                )
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == 0


def test_ingest_load_smoke_enforces_limit_per_tenant(test_database_url):
    cfg = LoadConfig(
        database_url=test_database_url,
        tenants=2,
        concurrency=8,
        requests=24,
        events_per_request=2,
        rate_limit=5,
    )
    report = run(cfg)
    d = report.as_dict()

    assert report.failures() == [], d
    # 12 requests per tenant against 5 a minute: some are always refused,
    # even when the run straddles a minute boundary.
    assert report.rate_limited() > 0
    assert report.accepted() >= 2 * cfg.rate_limit
    assert set(d["status_counts"]) <= {200, 429}
    assert set(d["latency_ms"]) == {"p50", "p90", "p95", "p99", "max"}
    assert all(int(v) >= 1 for v in d["retry_after_values"])
