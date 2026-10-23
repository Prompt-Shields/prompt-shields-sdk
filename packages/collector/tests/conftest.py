import os

import pytest
import pytest_asyncio
from db.api_keys import api_key_lookup_prefix, hash_api_key
from db.models import Base, Tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Overridable so a host that has remapped the database port (PS_DB_PORT) can
# still run the integration tests without editing a tracked file.
TEST_DB_URL = os.environ.get(
    "PS_TEST_DATABASE_URL",
    "postgresql+asyncpg://ps_user:ps_local_dev@localhost:5432/prompt_shields_test",
)


@pytest.fixture
def test_database_url() -> str:
    """For tests that manage their own engine (e.g. a server on another loop)."""
    return TEST_DB_URL


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
            settings={},
            api_key_prefix=api_key_lookup_prefix("ps-test-key-12345"),
            api_key_hash=hash_api_key("ps-test-key-12345"),
        )
        session.add(tenant)
        await session.commit()

    # Patch the app's engine to use test DB
    from collector import app as app_module
    app_module.engine = engine
    app_module.SessionLocal = session_factory

    yield engine

    await engine.dispose()
