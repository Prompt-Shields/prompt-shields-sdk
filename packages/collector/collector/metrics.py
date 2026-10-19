"""Prometheus metrics for the collector (GET /metrics).

Opt-in with PS_COLLECTOR_METRICS_ENABLED=true; /metrics answers 404 while it
is off. PS_COLLECTOR_METRICS_TOKEN, when set, is required as a bearer token,
like the gateway's PS_METRICS_TOKEN.

Labels are low-cardinality on purpose: the route *template*
(``/partner/v1/assets/{asset_id}``, never the concrete path), the method and
the status code; ingest counts carry the endpoint and an outcome. No tenant,
asset or request content reaches a label.

The registry is per process: with several uvicorn workers or replicas, each
one is scraped separately (Prometheus sums them).
"""

from __future__ import annotations

import hmac
import time

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from collector.config import settings

registry = CollectorRegistry()

requests_total = Counter(
    "ps_collector_requests",
    "HTTP requests handled, by route template, method and status.",
    ["route", "method", "status"],
    registry=registry,
)
request_duration = Histogram(
    "ps_collector_request_duration_seconds",
    "HTTP request duration, by route template and method.",
    ["route", "method"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
    registry=registry,
)
ingest_events_total = Counter(
    "ps_collector_ingest_events",
    "Events (SDK/gateway) or GenAI spans (OTel) received on ingest, by "
    "endpoint and outcome: accepted, rate_limited, write_failed, rejected.",
    ["endpoint", "outcome"],
    registry=registry,
)
assets_created_total = Counter(
    "ps_collector_assets_created",
    "Registry assets created by ingest.",
    ["endpoint"],
    registry=registry,
)

UNMATCHED = "unmatched"


def record_ingest(endpoint: str, outcome: str, count: int) -> None:
    if count > 0:
        ingest_events_total.labels(endpoint=endpoint, outcome=outcome).inc(count)


def record_assets_created(endpoint: str, count: int) -> None:
    if count > 0:
        assets_created_total.labels(endpoint=endpoint).inc(count)


def _route_template(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else UNMATCHED


class MetricsMiddleware:
    """Pure ASGI middleware (streams are not buffered). Does nothing while
    PS_COLLECTOR_METRICS_ENABLED is off."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or not settings.collector_metrics_enabled
            or scope.get("path") == "/metrics"
        ):
            await self.app(scope, receive, send)
            return

        status = 500
        start = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = _route_template(scope)
            method = scope.get("method", "")
            requests_total.labels(route=route, method=method, status=str(status)).inc()
            request_duration.labels(route=route, method=method).observe(
                time.perf_counter() - start
            )


async def metrics_endpoint(request: Request) -> Response:
    if not settings.collector_metrics_enabled:
        return Response(status_code=404)
    token = settings.collector_metrics_token
    if token:
        given = request.headers.get("authorization", "")
        if not hmac.compare_digest(given.encode(), f"Bearer {token}".encode()):
            return Response(
                status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
