"""Load test for the collector's ingest path, POST /ingest/events (#82).

It starts the real collector app under uvicorn on a free local port, against
a Postgres database you name, and seeds its own tenants there. Then it sends
event batches from many concurrent clients and reports:

  - throughput (requests/s, accepted events/s)
  - latency percentiles (p50/p90/p95/p99/max), for all requests and for 200s
  - status codes, and the Retry-After values the 429s carried
  - per-tenant rate-limit enforcement: per tenant and per minute window, the
    accepted requests never exceed PS_RATE_LIMIT_PER_MINUTE
  - that the database holds exactly the accepted events, and that loss
    accounting counted the rate-limited ones

The clients deliberately ignore Retry-After, so the limiter is under pressure.
The SDK's side of a 429 (back off, re-buffer, never block the caller) is
covered by tests/test_sdk_rate_limit_e2e_db.py.

Run it from the repository root, against a scratch database. Tables are
created if missing; the tenants it seeds, and every row they own, are deleted
when the run ends (``--keep-data`` keeps them). It refuses a database URL whose
host is not local unless ``--allow-remote-database`` is given, so it cannot be
pointed at a shared database by accident:

    PYTHONPATH=packages:packages/collector python -m benchmarks.ingest_load \\
        --database-url postgresql+asyncpg://ps_user:ps_local_dev@localhost:5432/ps_load \\
        --tenants 4 --concurrency 32 --requests 2000 --events-per-request 10 \\
        --rate-limit 300

`--rate-limit 0` turns the limit off to measure raw ingest throughput. `--json`
prints the report as JSON. The exit status is 1 when a check fails.
tests/test_ingest_load_smoke_db.py runs a small version of this in CI.

Every ingest request authenticates with bcrypt (collector/auth.py), so a
run's throughput is mostly bounded by the collector's CPU, and the load
driver shares that CPU here. Treat the numbers as a regression baseline on
one machine, not as a production capacity figure.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import json
import socket
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field

import httpx

MODELS = ("gpt-4o", "gpt-4o-mini", "claude-sonnet-4", "gemini-2.5-pro")


@dataclass
class LoadConfig:
    database_url: str
    tenants: int = 4
    concurrency: int = 32
    requests: int = 2000
    events_per_request: int = 10
    rate_limit: int = 300  # PS_RATE_LIMIT_PER_MINUTE for the run; 0 = off
    timeout: float = 30.0
    keep_data: bool = False  # keep the seeded tenants and their rows
    allow_remote_database: bool = False


@dataclass
class Sample:
    tenant: int
    status: int  # 0 = transport error
    latency_ms: float
    window: str | None = None  # X-RateLimit-Reset: identifies the minute window
    retry_after: str | None = None


@dataclass
class LoadReport:
    config: LoadConfig
    duration_s: float
    samples: list[Sample]
    stored_events: dict[int, int] = field(default_factory=dict)
    rejected_events: dict[int, int] = field(default_factory=dict)

    @property
    def status_counts(self) -> dict[int, int]:
        return dict(sorted(Counter(s.status for s in self.samples).items()))

    def accepted(self, tenant: int | None = None) -> int:
        return sum(
            1 for s in self.samples
            if s.status == 200 and (tenant is None or s.tenant == tenant)
        )

    def rate_limited(self, tenant: int | None = None) -> int:
        return sum(
            1 for s in self.samples
            if s.status == 429 and (tenant is None or s.tenant == tenant)
        )

    def latency(self, status: int | None = None) -> dict[str, float]:
        values = sorted(
            s.latency_ms for s in self.samples if status is None or s.status == status
        )
        if not values:
            return {}
        return {
            "p50": percentile(values, 50),
            "p90": percentile(values, 90),
            "p95": percentile(values, 95),
            "p99": percentile(values, 99),
            "max": values[-1],
        }

    def accepted_per_window(self) -> dict[tuple[int, str], int]:
        counts: dict[tuple[int, str], int] = defaultdict(int)
        for s in self.samples:
            if s.status == 200:
                counts[(s.tenant, s.window or "?")] += 1
        return dict(counts)

    def failures(self) -> list[str]:
        """Checks that must hold; empty when the run passed."""
        cfg = self.config
        problems = []
        unexpected = {
            code: n for code, n in self.status_counts.items() if code not in (200, 429)
        }
        if unexpected:
            problems.append(f"unexpected status codes: {unexpected}")
        if cfg.rate_limit > 0:
            for (tenant, window), n in sorted(self.accepted_per_window().items()):
                if n > cfg.rate_limit:
                    problems.append(
                        f"tenant {tenant} window {window}: {n} accepted > "
                        f"limit {cfg.rate_limit}"
                    )
            bad_retry = [
                s.retry_after for s in self.samples
                if s.status == 429
                and not (s.retry_after or "").isdigit()
            ]
            if bad_retry:
                problems.append(f"429 without a numeric Retry-After: {bad_retry[:5]}")
        elif self.rate_limited():
            problems.append(f"{self.rate_limited()} 429s with the limit off")
        for tenant in range(cfg.tenants):
            want = self.accepted(tenant) * cfg.events_per_request
            if self.stored_events.get(tenant, 0) != want:
                problems.append(
                    f"tenant {tenant}: {self.stored_events.get(tenant, 0)} events "
                    f"stored, {want} accepted"
                )
            want = self.rate_limited(tenant) * cfg.events_per_request
            if self.rejected_events.get(tenant, 0) != want:
                problems.append(
                    f"tenant {tenant}: {self.rejected_events.get(tenant, 0)} events "
                    f"counted rate_limited, {want} refused"
                )
        return problems

    def as_dict(self) -> dict:
        cfg = self.config
        accepted = self.accepted()
        return {
            "config": {
                "tenants": cfg.tenants,
                "concurrency": cfg.concurrency,
                "requests": cfg.requests,
                "events_per_request": cfg.events_per_request,
                "rate_limit_per_minute": cfg.rate_limit,
            },
            "duration_s": round(self.duration_s, 3),
            "requests_per_s": round(len(self.samples) / self.duration_s, 1),
            "accepted_events_per_s": round(
                accepted * cfg.events_per_request / self.duration_s, 1
            ),
            "status_counts": self.status_counts,
            "latency_ms": {k: round(v, 1) for k, v in self.latency().items()},
            "latency_ms_200": {k: round(v, 1) for k, v in self.latency(200).items()},
            "retry_after_values": sorted(
                {s.retry_after for s in self.samples if s.retry_after}, key=int
            ) if cfg.rate_limit > 0 else [],
            "per_tenant": {
                t: {
                    "accepted": self.accepted(t),
                    "rate_limited": self.rate_limited(t),
                    "stored_events": self.stored_events.get(t, 0),
                    "rejected_events": self.rejected_events.get(t, 0),
                }
                for t in range(cfg.tenants)
            },
            "failures": self.failures(),
        }


def percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile of an ascending list."""
    rank = max(1, -(-len(sorted_values) * pct // 100))  # ceil
    return sorted_values[int(rank) - 1]


LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}


def is_local_database(database_url: str) -> bool:
    """True when the URL's host is this machine (or a unix socket)."""
    from sqlalchemy.engine import make_url

    url = make_url(database_url)
    hosts = [url.host or ""]
    query_host = url.query.get("host")
    if query_host:
        hosts += [query_host] if isinstance(query_host, str) else list(query_host)
    return all(h in LOCAL_HOSTS or h.startswith("/") for h in hosts)


# --- collector under test -----------------------------------------------------


@contextlib.contextmanager
def collector_server(
    database_url: str, rate_limit: int | None = None, limiter=None
) -> Iterator[str]:
    """Serve the collector app on 127.0.0.1 from a thread with its own event
    loop and database engine; yields the base URL.

    The ingest limiter is ``limiter``, else a fresh one. ``rate_limit`` overrides
    PS_RATE_LIMIT_PER_MINUTE while the server runs (None keeps it). The
    lifespan (background workers) does not run.
    """
    import uvicorn
    from collector import app as app_module
    from collector.config import settings
    from collector.partner_rate_limit import (
        FixedWindowRateLimiter,
        get_ingest_rate_limiter,
    )
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    # Connections bind to the loop that opens them: the engine is only ever
    # used by the server thread, so its pool lives on that thread's loop.
    engine = create_async_engine(database_url)
    saved = (app_module.engine, app_module.SessionLocal, settings.rate_limit_per_minute)
    app_module.engine = engine
    app_module.SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    if rate_limit is not None:
        settings.rate_limit_per_minute = rate_limit
    limiter = limiter if limiter is not None else FixedWindowRateLimiter()
    app_module.app.dependency_overrides[get_ingest_rate_limiter] = lambda: limiter

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app_module.app, lifespan="off", log_level="warning", access_log=False
        )
    )

    async def serve() -> None:
        try:
            await server.serve(sockets=[sock])
        finally:
            await engine.dispose()

    thread = threading.Thread(
        target=lambda: asyncio.run(serve()), name="collector-under-test", daemon=True
    )
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("collector did not start")
            time.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        app_module.app.dependency_overrides.pop(get_ingest_rate_limiter, None)
        app_module.engine, app_module.SessionLocal, settings.rate_limit_per_minute = saved


# --- database: seed and verify ------------------------------------------------


def _bypass_sessions(database_url: str):
    from collector import rls
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    engine = create_async_engine(database_url)
    return engine, async_sessionmaker(
        engine, class_=AsyncSession, info=rls.bypass_info("ingest-load-test")
    )


async def seed_tenants(database_url: str, n: int, *, create_schema: bool = True):
    """Create ``n`` tenants with fresh ingest keys; returns [(id, key)]."""
    from db.api_keys import api_key_lookup_prefix, hash_api_key
    from db.models import Base, Tenant

    engine, sessions = _bypass_sessions(database_url)
    try:
        if create_schema:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        tenants = []
        async with sessions() as session:
            for i in range(n):
                key = f"ps-load-{uuid.uuid4().hex}"
                tenant = Tenant(
                    name=f"Load test tenant {i}",
                    domain=f"load-{i}.test",
                    settings={},
                    api_key_prefix=api_key_lookup_prefix(key),
                    api_key_hash=hash_api_key(key),
                )
                session.add(tenant)
                await session.flush()
                tenants.append((tenant.id, key))
            await session.commit()
        return tenants
    finally:
        await engine.dispose()


async def delete_tenants(database_url: str, tenant_ids: list) -> None:
    """Delete the given tenants and every row that references them."""
    from db.models import Base, Tenant
    from sqlalchemy import delete

    if not tenant_ids:
        return
    engine, sessions = _bypass_sessions(database_url)
    try:
        async with sessions() as session:
            for table in reversed(Base.metadata.sorted_tables):
                if table.name != Tenant.__tablename__ and "tenant_id" in table.c:
                    await session.execute(
                        delete(table).where(table.c.tenant_id.in_(tenant_ids))
                    )
            await session.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))
            await session.commit()
    finally:
        await engine.dispose()


async def count_events(database_url: str, tenant_ids: list) -> tuple[dict, dict]:
    """(stored ai_usage_events, events counted rate_limited) per tenant index."""
    from db.models import AIUsageEvent, TelemetryClientStats
    from sqlalchemy import func, select

    engine, sessions = _bypass_sessions(database_url)
    stored, rejected = {}, {}
    try:
        async with sessions() as session:
            for i, tenant_id in enumerate(tenant_ids):
                stored[i] = await session.scalar(
                    select(func.count()).where(AIUsageEvent.tenant_id == tenant_id)
                )
                rows = await session.scalars(
                    select(TelemetryClientStats.collector_rejected).where(
                        TelemetryClientStats.tenant_id == tenant_id
                    )
                )
                rejected[i] = sum(int(r.get("rate_limited", 0)) for r in rows)
    finally:
        await engine.dispose()
    return stored, rejected


# --- load driver --------------------------------------------------------------


def _batch(n: int, seq: int, client_id: str | None = None) -> dict:
    batch: dict = {
        "events": [
            {
                "vendor": "openai",
                "model": MODELS[(seq + i) % len(MODELS)],
                "use_case_name": "load-test",
                "business_unit": "Load",
                "source": "sdk",
                "tokens_in": 100 + i,
                "tokens_out": 50,
                "latency_ms": 200,
            }
            for i in range(n)
        ]
    }
    if client_id is not None:
        # As the SDK does: loss accounting keeps one row per client, so
        # clients without an id would all contend for one row lock.
        batch["metadata"] = {"client_id": client_id, "seq": seq + 1, "sdk": "loadtest"}
    return batch


async def drive(base_url: str, keys: list[str], cfg: LoadConfig) -> tuple[list[Sample], float]:
    """Send cfg.requests batches from cfg.concurrency clients, round-robin
    over the tenant keys. Returns the samples and the wall time."""
    samples: list[Sample] = []
    counter = itertools.count()
    limits = httpx.Limits(
        max_connections=cfg.concurrency, max_keepalive_connections=cfg.concurrency
    )

    async with httpx.AsyncClient(
        base_url=base_url, limits=limits, timeout=cfg.timeout
    ) as client:

        async def worker() -> None:
            client_id = uuid.uuid4().hex
            while (seq := next(counter)) < cfg.requests:
                tenant = seq % len(keys)
                start = time.perf_counter()
                try:
                    resp = await client.post(
                        "/ingest/events",
                        json=_batch(cfg.events_per_request, seq, client_id),
                        headers={"Authorization": f"Bearer {keys[tenant]}"},
                    )
                except httpx.HTTPError:
                    samples.append(
                        Sample(tenant, 0, (time.perf_counter() - start) * 1000)
                    )
                    continue
                samples.append(
                    Sample(
                        tenant,
                        resp.status_code,
                        (time.perf_counter() - start) * 1000,
                        window=resp.headers.get("X-RateLimit-Reset"),
                        retry_after=resp.headers.get("Retry-After"),
                    )
                )

        start = time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(cfg.concurrency)))
        return samples, time.perf_counter() - start


def run(cfg: LoadConfig) -> LoadReport:
    """Seed tenants, serve the collector, drive the load, verify the database,
    then delete the seeded tenants (unless cfg.keep_data)."""
    if not cfg.allow_remote_database and not is_local_database(cfg.database_url):
        raise ValueError(
            "refusing a non-local database: the load test seeds tenants and "
            "writes events; pass allow_remote_database / --allow-remote-database "
            "if this really is a scratch database"
        )
    tenants = asyncio.run(seed_tenants(cfg.database_url, cfg.tenants))
    tenant_ids = [t for t, _ in tenants]
    try:
        with collector_server(cfg.database_url, rate_limit=cfg.rate_limit) as base_url:
            samples, duration = asyncio.run(
                drive(base_url, [k for _, k in tenants], cfg)
            )
        stored, rejected = asyncio.run(count_events(cfg.database_url, tenant_ids))
    finally:
        if not cfg.keep_data:
            asyncio.run(delete_tenants(cfg.database_url, tenant_ids))
    return LoadReport(cfg, duration, samples, stored, rejected)


def format_report(report: LoadReport) -> str:
    d = report.as_dict()
    lines = [
        "Collector ingest load test (/ingest/events)",
        "  config: " + ", ".join(f"{k}={v}" for k, v in d["config"].items()),
        (
            f"  duration: {d['duration_s']}s  requests/s: {d['requests_per_s']}  "
            f"accepted events/s: {d['accepted_events_per_s']}"
        ),
        f"  status codes: {d['status_counts']}",
        "  latency ms (all): "
        + "  ".join(f"{k}={v}" for k, v in d["latency_ms"].items()),
        "  latency ms (200): "
        + "  ".join(f"{k}={v}" for k, v in d["latency_ms_200"].items()),
    ]
    if d["retry_after_values"]:
        lines.append(f"  Retry-After values (s): {d['retry_after_values']}")
    for t, row in d["per_tenant"].items():
        lines.append(f"  tenant {t}: " + ", ".join(f"{k}={v}" for k, v in row.items()))
    if d["failures"]:
        lines.append("  checks: FAIL")
        lines.extend(f"    {problem}" for problem in d["failures"])
    else:
        lines.append("  checks: PASS")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--database-url", required=True,
                   help="postgresql+asyncpg:// URL of a scratch database")
    p.add_argument("--tenants", type=int, default=4)
    p.add_argument("--concurrency", type=int, default=32)
    p.add_argument("--requests", type=int, default=2000)
    p.add_argument("--events-per-request", type=int, default=10)
    p.add_argument("--rate-limit", type=int, default=300,
                   help="PS_RATE_LIMIT_PER_MINUTE for the run (0 = off)")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    p.add_argument("--keep-data", action="store_true",
                   help="keep the seeded tenants and their events after the run")
    p.add_argument("--allow-remote-database", action="store_true",
                   help="allow a database host other than localhost/unix socket")
    args = p.parse_args(argv)
    if not args.allow_remote_database and not is_local_database(args.database_url):
        p.error("--database-url is not local; pass --allow-remote-database if it "
                "really is a scratch database")
    cfg = LoadConfig(
        database_url=args.database_url,
        tenants=args.tenants,
        concurrency=args.concurrency,
        requests=args.requests,
        events_per_request=args.events_per_request,
        rate_limit=args.rate_limit,
        keep_data=args.keep_data,
        allow_remote_database=args.allow_remote_database,
    )
    report = run(cfg)
    print(json.dumps(report.as_dict(), indent=2) if args.json else format_report(report))
    return 1 if report.failures() else 0


if __name__ == "__main__":
    sys.exit(main())
