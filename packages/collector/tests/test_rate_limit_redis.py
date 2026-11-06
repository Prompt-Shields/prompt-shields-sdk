"""Redis-backed rate limiter (#345): several workers share one limit.

Runs against a real Redis: PS_TEST_REDIS_URL when set (CI starts a Redis
service), else a throwaway ``redis-server`` on a free port when the binary
is installed. Skipped when neither is available or the redis client is not
installed.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import time
import uuid
from types import SimpleNamespace

import pytest
from collector import metrics, partner_rate_limit
from collector.config import settings
from collector.partner_rate_limit import (
    FixedWindowRateLimiter,
    RedisRateLimiter,
)
from collector.partner_scopes import RATE_LIMIT_TIERS

redis_asyncio = pytest.importorskip("redis.asyncio")

NOW = 1_800_000_030.0  # mid-window, so a test never straddles a minute


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def redis_url():
    url = os.environ.get("PS_TEST_REDIS_URL")
    if url:
        yield url
        return
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.skip("no PS_TEST_REDIS_URL and no redis-server binary")
    port = _free_port()
    proc = subprocess.Popen(
        [binary, "--port", str(port), "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    else:
        proc.kill()
        pytest.skip("redis-server did not start")
    yield f"redis://127.0.0.1:{port}/0"
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture
def prefix() -> str:
    # A fresh key space per test, so tests never see each other's counts.
    return f"ps:test:{uuid.uuid4().hex}"


@pytest.fixture
def pinned_clock(monkeypatch):
    monkeypatch.setattr(partner_rate_limit, "time", SimpleNamespace(time=lambda: NOW))


def _workers(n: int, redis_url: str, prefix: str, namespace: str = "partner"):
    """n limiters with their own connections, like n uvicorn workers."""
    return [
        RedisRateLimiter(namespace, url=redis_url, prefix=prefix, timeout=2.0)
        for _ in range(n)
    ]


async def _close(limiters):
    for lim in limiters:
        await lim.aclose()


async def test_four_workers_share_one_basic_tier_limit(redis_url, prefix, pinned_clock):
    """With Redis and 4 workers, a 120 rpm partner tier admits 120/min total."""
    limit = RATE_LIMIT_TIERS["basic"]
    assert limit == 120
    workers = _workers(4, redis_url, prefix)
    partner = uuid.uuid4()
    try:
        # 75 requests per worker, all at once: 300 against a limit of 120.
        results = await asyncio.gather(
            *(w.acheck(partner, limit) for w in workers for _ in range(75))
        )
    finally:
        await _close(workers)
    assert sum(r.allowed for r in results) == 120
    denied = [r for r in results if not r.allowed]
    assert len(denied) == 180
    assert all(r.remaining == 0 and r.retry_after == 30 for r in denied)
    assert {r.limit for r in results} == {120}
    assert {r.reset_at for r in results} == {(int(NOW) // 60 + 1) * 60}


async def test_in_process_limiters_multiply_the_limit(pinned_clock):
    """The problem the Redis limiter fixes: 4 in-process workers admit 4x."""
    workers = [FixedWindowRateLimiter() for _ in range(4)]
    partner = uuid.uuid4()
    results = [await w.acheck(partner, 120) for w in workers for _ in range(75)]
    assert sum(r.allowed for r in results) == 4 * 75


def test_four_worker_processes_share_one_limit(redis_url, prefix):
    """Same as above with 4 real OS processes, as uvicorn --workers 4 runs."""
    script = textwrap.dedent(
        """
        import asyncio, sys
        from types import SimpleNamespace
        from collector import partner_rate_limit
        from collector.partner_rate_limit import RedisRateLimiter

        partner_rate_limit.time = SimpleNamespace(time=lambda: float(sys.argv[3]))

        async def main():
            lim = RedisRateLimiter("partner", url=sys.argv[1], prefix=sys.argv[2], timeout=5)
            allowed = 0
            for _ in range(60):
                allowed += (await lim.acheck("partner-1", 120)).allowed
            await lim.aclose()
            print(allowed)

        asyncio.run(main())
        """
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, redis_url, prefix, str(NOW)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for _ in range(4)
    ]
    counts = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        counts.append(int(out.strip()))
    # 240 requests across 4 processes, 120 admitted in total.
    assert sum(counts) == 120


async def test_window_rolls_over(redis_url, prefix, monkeypatch):
    now = [NOW]
    monkeypatch.setattr(partner_rate_limit, "time", SimpleNamespace(time=lambda: now[0]))
    (lim,) = _workers(1, redis_url, prefix)
    try:
        assert (await lim.acheck("t", 1)).allowed
        assert not (await lim.acheck("t", 1)).allowed
        now[0] += 60
        assert (await lim.acheck("t", 1)).allowed
        ttl = await lim._get_client().ttl(lim.redis_key("t", int(now[0]) // 60))
        assert 0 < ttl <= 120
    finally:
        await lim.aclose()


async def test_keys_and_namespaces_are_separate(redis_url, prefix, pinned_clock):
    ingest, kong = _workers(1, redis_url, prefix, "ingest") + _workers(
        1, redis_url, prefix, "kong"
    )
    tenant = uuid.uuid4()
    try:
        assert (await ingest.acheck(tenant, 1)).allowed
        # Another endpoint's limiter and another tenant count apart.
        assert (await kong.acheck(tenant, 1)).allowed
        assert (await ingest.acheck(uuid.uuid4(), 1)).allowed
        assert not (await ingest.acheck(tenant, 1)).allowed
        # Tuple keys whose parts would join to the same string differ.
        assert (await ingest.acheck(("user", tenant, "a:b"), 1)).allowed
        assert (await ingest.acheck(("user", f"{tenant}:a", "b"), 1)).allowed
        # Tenant and user ids never reach Redis in the clear.
        keys = [k.decode() async for k in ingest._get_client().scan_iter(f"{prefix}:*")]
        assert keys and not any(str(tenant) in k for k in keys)
    finally:
        await _close([ingest, kong])


async def test_unreachable_redis_falls_back_to_in_process(pinned_clock, caplog):
    lim = RedisRateLimiter(
        "ingest", url=f"redis://127.0.0.1:{_free_port()}/0", timeout=0.2
    )
    before = metrics.rate_limit_store_errors_total.labels(limiter="ingest")._value.get()
    try:
        results = [await lim.acheck("t", 2) for _ in range(3)]
    finally:
        await lim.aclose()
    # Still limited, per process, and the outage is visible.
    assert [r.allowed for r in results] == [True, True, False]
    after = metrics.rate_limit_store_errors_total.labels(limiter="ingest")._value.get()
    assert after - before == 3
    assert sum("Rate limit store unavailable" in r.message for r in caplog.records) == 1


async def test_store_failure_skips_redis_for_a_while(pinned_clock, monkeypatch):
    """After one failure the limiter counts in-process without calling Redis
    until the retry interval passes, so a blackholed Redis does not add the
    timeout to every request."""
    clock = [1000.0]
    monkeypatch.setattr(partner_rate_limit, "monotonic", lambda: clock[0])
    calls = []

    class _Script:
        def __init__(self, fail):
            self.fail = fail

        async def __call__(self, keys, args):
            calls.append(keys[0])
            if self.fail[0]:
                raise ConnectionError("down")
            return 1

    fail = [True]
    client = SimpleNamespace(register_script=lambda _src: _Script(fail))
    lim = RedisRateLimiter("ingest", client=client, timeout=0.2)
    before = metrics.rate_limit_store_errors_total.labels(limiter="ingest")._value.get()
    results = [await lim.acheck("t", 2) for _ in range(3)]
    assert [r.allowed for r in results] == [True, True, False]
    assert len(calls) == 1
    after = metrics.rate_limit_store_errors_total.labels(limiter="ingest")._value.get()
    assert after - before == 3
    # Once the interval has passed, Redis is tried again.
    fail[0] = False
    clock[0] += partner_rate_limit._STORE_RETRY_SECONDS + 0.1
    assert (await lim.acheck("t", 2)).allowed
    assert len(calls) == 2


def test_backend_follows_settings(monkeypatch, redis_url):
    monkeypatch.setattr(settings, "collector_redis_url", "")
    assert isinstance(partner_rate_limit._make("partner"), FixedWindowRateLimiter)
    monkeypatch.setattr(settings, "collector_redis_url", redis_url)
    monkeypatch.setattr(settings, "collector_redis_key_prefix", "ps:x")
    lim = partner_rate_limit._make("ingest")
    assert isinstance(lim, RedisRateLimiter)
    assert lim.redis_key("t", 5).startswith("ps:x:ingest:")
    with pytest.raises(ValueError):
        RedisRateLimiter("partner")
