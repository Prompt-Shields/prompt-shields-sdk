"""Kubernetes operations surface without a database: /readyz failures,
/metrics (PS_COLLECTOR_METRICS_ENABLED) and the background worker switch
(PS_COLLECTOR_RUN_WORKERS)."""

import asyncio

import pytest
from collector import app as app_module
from collector import metrics, workers
from collector.app import app
from collector.config import settings
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _requests(route: str, method: str, status: str) -> float:
    value = metrics.registry.get_sample_value(
        "ps_collector_requests_total",
        {"route": route, "method": method, "status": status},
    )
    return value or 0.0


# ── /readyz ─────────────────────────────────────────────────────────────


async def test_readyz_503_when_the_database_is_unreachable(monkeypatch):
    dead = create_async_engine("postgresql+asyncpg://ps_user:x@127.0.0.1:1/none")
    monkeypatch.setattr(app_module, "engine", dead)
    try:
        async with _client() as c:
            resp = await c.get("/readyz")
    finally:
        await dead.dispose()
    assert resp.status_code == 503
    assert resp.json() == {"status": "unavailable"}


async def test_readyz_503_when_the_check_times_out(monkeypatch):
    async def hang():
        await asyncio.sleep(10)

    monkeypatch.setattr(app_module, "_ping_db", hang)
    monkeypatch.setattr(settings, "readyz_timeout_seconds", 0.05)
    async with _client() as c:
        resp = await c.get("/readyz")
    assert resp.status_code == 503


async def test_liveness_does_not_touch_the_database(monkeypatch):
    dead = create_async_engine("postgresql+asyncpg://ps_user:x@127.0.0.1:1/none")
    monkeypatch.setattr(app_module, "engine", dead)
    try:
        async with _client() as c:
            resp = await c.get("/partner/v1/health")
    finally:
        await dead.dispose()
    assert resp.status_code == 200


def test_probe_and_metrics_routes_are_not_in_the_openapi_schema():
    paths = app.openapi()["paths"]
    assert "/readyz" not in paths and "/metrics" not in paths


# ── /metrics ────────────────────────────────────────────────────────────


async def test_metrics_404_and_nothing_recorded_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "collector_metrics_enabled", False)
    before = _requests("/partner/v1/health", "GET", "200")
    async with _client() as c:
        assert (await c.get("/partner/v1/health")).status_code == 200
        assert (await c.get("/metrics")).status_code == 404
    assert _requests("/partner/v1/health", "GET", "200") == before


async def test_metrics_count_requests_by_route_template(monkeypatch):
    monkeypatch.setattr(settings, "collector_metrics_enabled", True)
    monkeypatch.setattr(settings, "collector_metrics_token", "")
    health = _requests("/partner/v1/health", "GET", "200")
    asset = _requests("/partner/v1/assets/{asset_id}", "GET", "401")
    unmatched = _requests("unmatched", "GET", "404")
    async with _client() as c:
        await c.get("/partner/v1/health")
        await c.get("/partner/v1/health")
        # No credentials: 401 before any database access.
        await c.get("/partner/v1/assets/00000000-0000-0000-0000-000000000001")
        await c.get("/no/such/path/123")
        resp = await c.get("/metrics")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert _requests("/partner/v1/health", "GET", "200") == health + 2
    assert _requests("/partner/v1/assets/{asset_id}", "GET", "401") == asset + 1
    assert _requests("unmatched", "GET", "404") == unmatched + 1
    body = resp.text
    assert "ps_collector_request_duration_seconds_bucket" in body
    assert 'route="/partner/v1/assets/{asset_id}"' in body
    # Concrete paths never become labels.
    assert "00000000-0000-0000-0000-000000000001" not in body
    assert "/no/such/path" not in body
    # /metrics does not count itself.
    assert 'route="/metrics"' not in body


async def test_metrics_unknown_methods_share_one_label(monkeypatch):
    monkeypatch.setattr(settings, "collector_metrics_enabled", True)
    monkeypatch.setattr(settings, "collector_metrics_token", "")
    before = _requests("/partner/v1/health", "OTHER", "405")
    async with _client() as c:
        await c.request("FOO123", "/partner/v1/health")
        await c.request("BAR456", "/partner/v1/health")
        body = (await c.get("/metrics")).text
    assert _requests("/partner/v1/health", "OTHER", "405") == before + 2
    assert "FOO123" not in body and "BAR456" not in body


async def test_metrics_token_is_required_when_set(monkeypatch):
    monkeypatch.setattr(settings, "collector_metrics_enabled", True)
    monkeypatch.setattr(settings, "collector_metrics_token", "s3cret-token")
    async with _client() as c:
        missing = await c.get("/metrics")
        wrong = await c.get("/metrics", headers={"Authorization": "Bearer nope"})
        ok = await c.get("/metrics", headers={"Authorization": "Bearer s3cret-token"})
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert wrong.status_code == 401
    assert ok.status_code == 200


def test_record_ingest_ignores_zero_counts():
    labels = {"endpoint": "events", "outcome": "accepted"}
    before = metrics.registry.get_sample_value("ps_collector_ingest_events_total", labels) or 0
    metrics.record_ingest("events", "accepted", 0)
    metrics.record_ingest("events", "accepted", 3)
    after = metrics.registry.get_sample_value("ps_collector_ingest_events_total", labels)
    assert after == before + 3


# ── background workers ──────────────────────────────────────────────────


@pytest.fixture
def fake_loops(monkeypatch):
    """Replace both loops with ones that record that they ran."""
    started: list[str] = []

    async def webhook_loop(factory, interval):
        started.append("webhooks")
        await asyncio.Event().wait()

    async def usage_loop(factory, interval, days):
        started.append("provider_usage")
        await asyncio.Event().wait()

    from collector import provider_usage_admin, webhooks

    monkeypatch.setattr(webhooks, "run_worker", webhook_loop)
    monkeypatch.setattr(provider_usage_admin, "run_schedule", usage_loop)
    monkeypatch.setattr(settings, "webhook_worker_interval_seconds", 5.0)
    monkeypatch.setattr(settings, "provider_usage_pull_interval_seconds", 86400.0)
    return started


async def test_start_workers_runs_each_configured_loop(fake_loops):
    tasks = workers.start_workers(object())
    await asyncio.sleep(0)
    assert sorted(fake_loops) == ["provider_usage", "webhooks"]
    await workers.stop_workers(tasks)
    assert all(t.cancelled() for t in tasks)


async def test_start_workers_with_nothing_configured(monkeypatch):
    monkeypatch.setattr(settings, "webhook_worker_interval_seconds", 0)
    monkeypatch.setattr(settings, "provider_usage_pull_interval_seconds", 0)
    assert workers.start_workers(object()) == []


@pytest.mark.parametrize("run_workers, expected", [(True, 2), (False, 0)])
async def test_api_lifespan_honours_run_workers(
    fake_loops, monkeypatch, run_workers, expected
):
    monkeypatch.setattr(settings, "collector_run_workers", run_workers)
    async with app_module.lifespan(app):
        await asyncio.sleep(0)
        assert len(fake_loops) == expected


async def test_worker_process_exits_when_nothing_is_configured(monkeypatch, capsys):
    monkeypatch.setattr(settings, "webhook_worker_interval_seconds", 0)
    monkeypatch.setattr(settings, "provider_usage_pull_interval_seconds", 0)
    monkeypatch.setattr(settings, "rls_required", False)
    assert await workers._run() == 2
    assert "No worker enabled" in capsys.readouterr().err


async def test_worker_process_runs_loops_until_sigterm(fake_loops, monkeypatch):
    import os
    import signal

    monkeypatch.setattr(settings, "rls_required", False)
    # The worker process ignores PS_COLLECTOR_RUN_WORKERS (it only gates the API).
    monkeypatch.setattr(settings, "collector_run_workers", False)
    run = asyncio.create_task(workers._run())
    for _ in range(50):
        if len(fake_loops) == 2:
            break
        await asyncio.sleep(0.01)
    assert sorted(fake_loops) == ["provider_usage", "webhooks"]
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(run, 5) == 0
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.remove_signal_handler(sig)
