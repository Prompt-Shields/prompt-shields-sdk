import os
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from db.models import Base, Tenant

# Overridable so a host that has remapped the database port (PS_DB_PORT) can
# still run the integration tests without editing a tracked file.
TEST_DB_URL = os.environ.get(
    "PS_TEST_DATABASE_URL",
    "postgresql+asyncpg://ps_user:ps_local_dev@localhost:5432/prompt_shields_test",
)


@pytest_asyncio.fixture
async def seeded_db():
    engine = create_async_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession)
    async with session_factory() as session:
        tenant = Tenant(
            name="Test Corp",
            domain="test.com",
            settings={"api_key": "ps-test-key-12345"}
        )
        session.add(tenant)
        await session.commit()

    from collector import app as app_module
    app_module.engine = engine
    app_module.SessionLocal = session_factory

    yield engine

    await engine.dispose()
