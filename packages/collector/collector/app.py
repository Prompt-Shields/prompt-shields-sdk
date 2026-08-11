from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from collector.config import settings

engine = create_async_engine(settings.database_url)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
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
from collector.registry import router as registry_router  # noqa: E402

app.include_router(ingest_router)
app.include_router(registry_router)

# Partner API surface (read-only, OAuth + API key)
from collector.oauth import router as oauth_router  # noqa: E402
from collector.partner_admin import router as partner_admin_router  # noqa: E402
from collector.partner_registry import router as partner_registry_router  # noqa: E402

app.include_router(oauth_router)
app.include_router(partner_admin_router)
app.include_router(partner_registry_router)
