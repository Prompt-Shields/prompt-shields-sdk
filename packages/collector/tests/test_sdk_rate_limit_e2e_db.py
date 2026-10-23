"""SDK <-> collector end to end when the collector rate limits (#82).

The real collector serves on a local port (benchmarks.ingest_load's
collector_server) against the test database, with PS_RATE_LIMIT_PER_MINUTE=3.
The real SDK client sends its telemetry there over HTTP; only the LLM
provider is faked. Checked:

  - the model call always returns, and never waits out Retry-After
  - after a 429 the SDK sends nothing until Retry-After has passed (one 429,
    not a retry storm), keeping the refused and newer events buffered
  - once the back-off ends, the buffer is delivered: nothing is lost

Both clocks are controlled: the collector limiter's (pinned 10s before a
minute boundary, so Retry-After is exactly 10) and the SDK sender's monotonic
clock (advanced past the back-off without sleeping).
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from benchmarks.ingest_load import _bypass_sessions, collector_server, seed_tenants
from collector import partner_rate_limit
from collector.partner_rate_limit import FixedWindowRateLimiter
from db.models import AIUsageEvent, TelemetryClientStats
from sqlalchemy import func, select

# The SDK is a sibling package in this repository, not a collector dependency.
SDK_ROOT = Path(__file__).resolve().parents[2] / "sdk"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from prompt_shields import telemetry as sdk_telemetry
from prompt_shields.async_client import AsyncShieldsClient
from prompt_shields.client import ShieldsClient

LIMIT = 3
SERVER_NOW = 1_700_000_030.0  # 50s into its minute: Retry-After is 10
RETRY_AFTER = 10
# A call that waited out Retry-After would take at least RETRY_AFTER seconds.
CALL_BUDGET_S = 2.0


def _response(model):
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=5),
        choices=[],
    )


class _FakeOpenAI:
    def __init__(self):
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model, messages, **kwargs):
        self.calls += 1
        return _response(model)


class _FakeAsyncOpenAI(_FakeOpenAI):
    async def _create(self, *, model, messages, **kwargs):
        self.calls += 1
        return _response(model)


class _Client(ShieldsClient):
    def _make_upstream(self, api_key, **provider_kwargs):
        return _FakeOpenAI()


class _AsyncClient(AsyncShieldsClient):
    def _make_upstream(self, api_key, **provider_kwargs):
        return _FakeAsyncOpenAI()


@pytest.fixture
def clocks(monkeypatch):
    """Pinned collector limiter clock and an advanceable SDK monotonic clock."""
    server = [SERVER_NOW]
    sdk_offset = [0.0]
    real_monotonic = time.monotonic
    monkeypatch.setattr(
        partner_rate_limit, "time", SimpleNamespace(time=lambda: server[0])
    )
    monkeypatch.setattr(
        sdk_telemetry,
        "time",
        SimpleNamespace(monotonic=lambda: real_monotonic() + sdk_offset[0]),
    )

    def advance(seconds: float) -> None:
        server[0] += seconds
        sdk_offset[0] += seconds

    return advance


async def _db_counts(database_url, tenant_id):
    engine, sessions = _bypass_sessions(database_url)
    try:
        async with sessions() as session:
            stored = await session.scalar(
                select(func.count()).where(AIUsageEvent.tenant_id == tenant_id)
            )
            rows = (
                await session.scalars(
                    select(TelemetryClientStats).where(
                        TelemetryClientStats.tenant_id == tenant_id
                    )
                )
            ).all()
            return stored, [
                {
                    "received_events": r.received_events,
                    "received_batches": r.received_batches,
                    "sdk_enqueued_total": r.sdk_enqueued_total,
                    "collector_rejected": dict(r.collector_rejected),
                }
                for r in rows
            ]
    finally:
        await engine.dispose()


def _call(client, i):
    start = time.perf_counter()
    response = client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": f"hello {i}"}]
    )
    return response, time.perf_counter() - start


def test_sdk_backs_off_on_429_without_blocking_and_loses_nothing(
    test_database_url, clocks
):
    [(tenant_id, key)] = asyncio.run(seed_tenants(test_database_url, 1))
    limiter = FixedWindowRateLimiter()
    with collector_server(test_database_url, rate_limit=LIMIT, limiter=limiter) as url:
        client = _Client(api_key="sk-test", ps_api_key=key, ps_collector_url=url)
        try:
            results = [_call(client, i) for i in range(10)]

            # Every model call returned its response, none waited on the 429.
            assert client._upstream.calls == 10
            assert all(r.model == "gpt-4o" for r, _ in results)
            assert max(elapsed for _, elapsed in results) < CALL_BUDGET_S

            stats = client.telemetry_stats()["collector"]
            assert stats["sent"] == LIMIT
            # One 429: calls 5-10 sent nothing while backing off.
            assert stats["rate_limited_responses"] == 1
            assert stats["buffered"] == 10 - LIMIT
            assert stats["dropped_total"] == 0
            assert RETRY_AFTER - 1 < stats["retry_after_seconds"] <= RETRY_AFTER
            # The collector saw exactly 4 requests from this tenant.
            assert sum(
                n for (k, _), n in limiter._counts.items() if k == tenant_id
            ) == LIMIT + 1

            stored, rows = asyncio.run(_db_counts(test_database_url, tenant_id))
            assert stored == LIMIT
            assert [r["collector_rejected"] for r in rows] == [{"rate_limited": 1}]

            # Retry-After has passed and the collector's window rolled over:
            # the next call delivers the whole backlog in one batch.
            clocks(RETRY_AFTER + 1)
            _, elapsed = _call(client, 10)
            assert elapsed < CALL_BUDGET_S

            stats = client.telemetry_stats()["collector"]
            assert stats["sent"] == 11
            assert stats["buffered"] == 0
            assert stats["dropped_total"] == 0
            assert stats["rate_limited_responses"] == 1
        finally:
            client.close()

    stored, rows = asyncio.run(_db_counts(test_database_url, tenant_id))
    assert stored == 11
    assert len(rows) == 1
    assert rows[0]["received_events"] == 11
    assert rows[0]["received_batches"] == LIMIT + 1
    assert rows[0]["sdk_enqueued_total"] == 11
    assert rows[0]["collector_rejected"] == {"rate_limited": 1}


async def test_async_sdk_backs_off_on_429_without_blocking(test_database_url, clocks):
    [(tenant_id, key)] = await seed_tenants(test_database_url, 1)
    with collector_server(test_database_url, rate_limit=LIMIT) as url:
        client = _AsyncClient(api_key="sk-test", ps_api_key=key, ps_collector_url=url)
        try:
            elapsed = []
            for i in range(8):
                start = time.perf_counter()
                response = await client.chat.completions.create(
                    model="gpt-4o", messages=[{"role": "user", "content": f"hi {i}"}]
                )
                elapsed.append(time.perf_counter() - start)
                assert response.model == "gpt-4o"

            assert client._upstream.calls == 8
            assert max(elapsed) < CALL_BUDGET_S
            stats = client.telemetry_stats()["collector"]
            assert stats["sent"] == LIMIT
            assert stats["rate_limited_responses"] == 1
            assert stats["buffered"] == 8 - LIMIT
            assert stats["dropped_total"] == 0

            clocks(RETRY_AFTER + 1)
            await client.chat.completions.create(
                model="gpt-4o", messages=[{"role": "user", "content": "again"}]
            )
            stats = client.telemetry_stats()["collector"]
            assert stats["sent"] == 9
            assert stats["buffered"] == 0
        finally:
            await client.aclose()

    stored, _ = await _db_counts(test_database_url, tenant_id)
    assert stored == 9
