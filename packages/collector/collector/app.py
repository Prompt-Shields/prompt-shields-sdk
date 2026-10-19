import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from collector import rls
from collector.config import settings
from collector.embeddings import get_embedder
from collector.metrics import MetricsMiddleware, metrics_endpoint
from collector.workers import start_workers, stop_workers

logger = logging.getLogger(__name__)

engine = create_async_engine(settings.database_url)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_embedder()  # fail fast on a bad PS_EMBEDDINGS_* configuration
    if settings.rls_required:
        async with engine.connect() as conn:
            await rls.check_required(conn)
    # Background loops (collector/workers.py); off here with
    # PS_COLLECTOR_RUN_WORKERS=false when `python -m collector.workers` or
    # CronJobs run them instead.
    tasks = start_workers(SessionLocal) if settings.collector_run_workers else []
    yield
    await stop_workers(tasks)
    await engine.dispose()


app = FastAPI(
    title="Prompt Shields Collector",
    description=(
        "Internal ingestion + registry plus the read-only Partner API "
        "consumed by EA tools (Ardoq, LeanIX, ServiceNow)."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def db_session_middleware(request: Request, call_next):
    async with SessionLocal() as session:
        request.state.db = session
        response = await call_next(request)
        return response


# Prometheus metrics (PS_COLLECTOR_METRICS_ENABLED); outermost, so it times
# the whole request. A no-op while metrics are off.
app.add_middleware(MetricsMiddleware)
app.add_api_route("/metrics", metrics_endpoint, methods=["GET"], include_in_schema=False)


async def _ping_db() -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


@app.get("/readyz", include_in_schema=False)
async def readyz():
    """Readiness: the database answers SELECT 1 within
    PS_READYZ_TIMEOUT_SECONDS. Liveness stays /partner/v1/health, which never
    touches the database (a database outage must not restart every pod)."""
    try:
        await asyncio.wait_for(_ping_db(), settings.readyz_timeout_seconds)
    except Exception as exc:  # noqa: BLE001 - any failure means not ready
        logger.warning("readiness check failed: %s", type(exc).__name__)
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ready"}


# Internal collector + registry surface
from collector.ingest import router as ingest_router
from collector.lifecycle import router as lifecycle_router
from collector.loss_accounting import router as loss_accounting_router
from collector.otel_ingest import router as otel_ingest_router
from collector.registry import router as registry_router
from collector.risk_suggestions import router as risk_suggestions_router

app.include_router(ingest_router)
app.include_router(registry_router)
app.include_router(lifecycle_router)
app.include_router(otel_ingest_router)
app.include_router(loss_accounting_router)
app.include_router(risk_suggestions_router)

# Partner API surface (read-only, OAuth + API key)
from collector.oauth import router as oauth_router
from collector.partner_admin import router as partner_admin_router
from collector.partner_registry import router as partner_registry_router

app.include_router(oauth_router)
app.include_router(partner_admin_router)
app.include_router(partner_registry_router)

# Partner change webhooks: tenant admin management (#103)
from collector.webhook_admin import router as webhook_admin_router

app.include_router(webhook_admin_router)

# Human attestation: tenant registry and Partner API routes (#98)
from collector.attestation_api import partner_router as attestation_partner_router
from collector.attestation_api import tenant_router as attestation_tenant_router

app.include_router(attestation_tenant_router)
app.include_router(attestation_partner_router)

# Platform signal: provider org usage pull (admin trigger)
from collector.provider_usage_admin import router as provider_usage_router

app.include_router(provider_usage_router)

# Provider admin credentials and key fingerprint links (#93)
from collector.provider_credentials_api import (
    partner_router as provider_credentials_partner_router,
)
from collector.provider_credentials_api import (
    tenant_router as provider_credentials_tenant_router,
)

app.include_router(provider_credentials_tenant_router)
app.include_router(provider_credentials_partner_router)

# Platform signal: cloud AI inventory (admin trigger)
from collector.cloud_inventory import router as cloud_inventory_router

app.include_router(cloud_inventory_router)

# EA-tool connectors: push the registry to LeanIX / ServiceNow (operator trigger)
from collector.leanix import router as leanix_router
from collector.servicenow import router as servicenow_router

app.include_router(leanix_router)
app.include_router(servicenow_router)

# Operator surface: retention purge and tenant erasure (PS_ADMIN_TOKEN)
from collector.operator_admin import router as operator_admin_router

app.include_router(operator_admin_router)
