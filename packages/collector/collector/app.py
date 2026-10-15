from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from collector import rls
from collector.config import settings
from collector.embeddings import get_embedder

engine = create_async_engine(settings.database_url)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_embedder()  # fail fast on a bad PS_EMBEDDINGS_* configuration
    if settings.rls_required:
        async with engine.connect() as conn:
            await rls.check_required(conn)
    yield
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


# Internal collector + registry surface
from collector.ingest import router as ingest_router  # noqa: E402
from collector.lifecycle import router as lifecycle_router
from collector.loss_accounting import router as loss_accounting_router  # noqa: E402
from collector.otel_ingest import router as otel_ingest_router  # noqa: E402
from collector.registry import router as registry_router  # noqa: E402

app.include_router(ingest_router)
app.include_router(registry_router)
app.include_router(lifecycle_router)
app.include_router(otel_ingest_router)
app.include_router(loss_accounting_router)

# Partner API surface (read-only, OAuth + API key)
from collector.oauth import router as oauth_router  # noqa: E402
from collector.partner_admin import router as partner_admin_router  # noqa: E402
from collector.partner_registry import router as partner_registry_router  # noqa: E402

app.include_router(oauth_router)
app.include_router(partner_admin_router)
app.include_router(partner_registry_router)

# Platform signal: provider org usage pull (admin trigger)
from collector.provider_usage_admin import router as provider_usage_router  # noqa: E402

app.include_router(provider_usage_router)

# Operator surface: retention purge and tenant erasure (PS_ADMIN_TOKEN)
from collector.operator_admin import router as operator_admin_router  # noqa: E402

app.include_router(operator_admin_router)
