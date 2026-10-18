"""Scheduled provider usage pull (#93): every tenant with credentials, each
under its own RLS context. Against a real PostgreSQL database
(PS_TEST_DATABASE_URL); vendor HTTP is mocked."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from collector import provider_credentials as pc
from collector import provider_usage as pu
from collector import provider_usage_admin as pua
from collector import rls
from collector.config import Settings, settings
from db.models import AIAsset, Tenant
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

K1 = "1" * 40
STORED_KEY = "sk-admin-STORED-schedule-0001"
ENV_KEY = "sk-admin-ENV-schedule-0002"
NOW = datetime(2026, 10, 4, tzinfo=UTC)
DAY1 = int(datetime(2026, 10, 2, tzinfo=UTC).timestamp())


@pytest.fixture(autouse=True)
def _keyring(monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret_keys", f"k1:{K1}")
    monkeypatch.setattr(settings, "webhook_secret_key", "")
    monkeypatch.delenv("PS_PROVIDER_ADMIN_KEYS", raising=False)


def _handler(seen: list[str]):
    """OpenAI usage: one key/model per admin key, so rows show whose key ran."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.openai.com"
        auth = request.headers["authorization"]
        seen.append(auth)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [], "has_more": False})
        key_id = "key_stored" if STORED_KEY in auth else "key_env"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "start_time": DAY1,
                        "end_time": DAY1 + 86400,
                        "results": [
                            {
                                "input_tokens": 10,
                                "output_tokens": 5,
                                "num_model_requests": 1,
                                "project_id": "proj",
                                "api_key_id": key_id,
                                "model": "gpt-4o-mini",
                            }
                        ],
                    }
                ],
                "has_more": False,
            },
        )

    return handler


async def _no_sleep(_s: float) -> None:
    return None


async def _tenants(engine):
    """Seeded tenant (stored key), one with an env key, one with nothing."""
    admin = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        info=rls.bypass_info("test-seed"),
    )
    async with admin() as s:
        stored = (await s.execute(select(Tenant))).scalar_one()
        env = Tenant(name="Env", settings={})
        idle = Tenant(name="Idle", settings={})
        s.add_all([env, idle])
        await s.flush()
        await pc.create_credential(s, stored.id, "openai", STORED_KEY)
        await s.commit()
        return stored.id, env.id, idle.id


def _settings(env_id, *extra):
    keys = {str(env_id): {"openai": ENV_KEY}}
    for raw in extra:
        keys[raw] = {"openai": "sk-admin-not-a-tenant-0003"}
    return pu.ProviderUsageSettings(provider_admin_keys=keys)


async def test_pulls_each_tenant_with_credentials_under_rls(seeded_db):
    stored_id, env_id, idle_id = await _tenants(seeded_db)
    async with seeded_db.begin() as conn:
        await rls.enable(conn)
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)

    seen: list[str] = []
    summary = await pua.pull_all_tenants(
        factory,
        days=3,
        # An env entry for a tenant that does not exist, and one that is not
        # a uuid, are skipped.
        settings=_settings(env_id, str(uuid.uuid4()), "not-a-uuid"),
        transport=httpx.MockTransport(_handler(seen)),
        sleep=_no_sleep,
        now=NOW,
    )

    assert set(summary["tenants"]) == {str(stored_id), str(env_id)}
    assert summary["start"] == "2026-10-01T00:00:00+00:00"
    stored = summary["tenants"][str(stored_id)]["providers"]["openai"]
    env = summary["tenants"][str(env_id)]["providers"]["openai"]
    assert (stored["status"], stored["credential_source"]) == ("ok", "stored")
    assert (env["status"], env["credential_source"]) == ("ok", "env")
    assert {f"Bearer {STORED_KEY}", f"Bearer {ENV_KEY}"} == set(seen)

    # Rows landed on the right tenant, visible only under its context.
    plain = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with plain() as s:
        assert (await s.execute(text("SELECT id FROM ai_assets"))).all() == []
        for tenant_id, key_id in ((stored_id, "key_stored"), (env_id, "key_env")):
            await rls.set_tenant(s, tenant_id)
            assets = (await s.execute(select(AIAsset))).scalars().all()
            assert [key_id in a.calling_service for a in assets] == [True]
            await s.commit()
        await rls.set_tenant(s, idle_id)
        assert (await s.execute(select(AIAsset))).scalars().all() == []


async def test_a_failing_tenant_does_not_stop_the_others(seeded_db, monkeypatch):
    stored_id, env_id, _ = await _tenants(seeded_db)
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    real_pull = pu.pull_provider_usage

    async def flaky(db, tenant_id, **kwargs):
        if tenant_id == stored_id:
            raise RuntimeError(f"boom {STORED_KEY}")
        return await real_pull(db, tenant_id, **kwargs)

    monkeypatch.setattr(pua, "pull_provider_usage", flaky)
    summary = await pua.pull_all_tenants(
        factory,
        days=1,
        settings=_settings(env_id),
        transport=httpx.MockTransport(_handler([])),
        sleep=_no_sleep,
        now=NOW,
    )
    assert summary["tenants"][str(stored_id)] == {
        "tenant_id": str(stored_id),
        "error": "RuntimeError",
    }
    assert summary["tenants"][str(env_id)]["providers"]["openai"]["status"] == "ok"
    # The exception text (which here carries a key) is not reported.
    assert STORED_KEY not in str(summary)


async def test_nothing_to_pull(seeded_db):
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    summary = await pua.pull_all_tenants(
        factory, days=3, settings=pu.ProviderUsageSettings(), now=NOW
    )
    assert summary["tenants"] == {}


async def test_days_bounds(seeded_db):
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    for days in (0, 91):
        with pytest.raises(ValueError):
            await pua.pull_all_tenants(factory, days=days)


async def test_run_schedule_pulls_then_waits_the_interval(seeded_db, monkeypatch):
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    calls: list[int] = []
    waits: list[float] = []

    async def fake_pull(session_factory, *, days, list_factory, **kwargs):
        calls.append(days)
        if len(calls) == 1:
            raise RuntimeError("first run fails; the loop keeps going")
        return {"tenants": {}}

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(pua, "pull_all_tenants", fake_pull)
    with pytest.raises(asyncio.CancelledError):
        await pua.run_schedule(factory, 86400, 3, sleep=sleep)
    assert calls == [3, 3]
    assert waits == [86400, 86400]


async def test_lifespan_starts_the_schedule_only_when_configured(
    seeded_db, monkeypatch
):
    from collector.app import app

    started: list[tuple] = []

    async def fake_schedule(session_factory, interval, days):
        started.append((interval, days))
        await asyncio.Event().wait()

    monkeypatch.setattr(pua, "run_schedule", fake_schedule)
    monkeypatch.setattr(settings, "webhook_worker_interval_seconds", 0)

    monkeypatch.setattr(settings, "provider_usage_pull_interval_seconds", 0)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
    assert started == []

    monkeypatch.setattr(settings, "provider_usage_pull_interval_seconds", 86400)
    monkeypatch.setattr(settings, "provider_usage_pull_days", 2)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
    assert started == [(86400, 2)]


def test_settings_default_off_and_bounds():
    assert Settings().provider_usage_pull_interval_seconds == 0
    assert Settings().provider_usage_pull_days == 3
    assert (
        Settings(
            provider_usage_pull_interval_seconds=86400
        ).provider_usage_pull_interval_seconds
        == 86400
    )
    for bad in (
        {"provider_usage_pull_interval_seconds": 60},
        {"provider_usage_pull_days": 0},
    ):
        with pytest.raises(ValidationError):
            Settings(**bad)


def test_cli_arguments(monkeypatch, capsys):
    seen: list[tuple] = []

    async def fake_all(days, loop, interval):
        seen.append(("all", days, loop, interval))
        return {"tenants": {}}

    async def fake_one(tenant, days, providers):
        seen.append(("one", tenant, days, providers))
        return {}

    monkeypatch.setattr(pua, "_main_all", fake_all)
    monkeypatch.setattr(pua, "_main", fake_one)
    tenant = uuid.uuid4()
    pua.main(["--all-tenants"])
    pua.main(["--all-tenants", "--loop", "--interval", "3600", "--days", "5"])
    pua.main(["--tenant", str(tenant)])
    assert seen == [
        ("all", 3, False, 86400.0),
        ("all", 5, True, 3600.0),
        ("one", tenant, 7, None),
    ]
    for argv in (
        [],
        ["--tenant", str(tenant), "--all-tenants"],
        ["--tenant", str(tenant), "--loop"],
        ["--all-tenants", "--provider", "openai"],
        ["--all-tenants", "--days", "0"],
    ):
        with pytest.raises(SystemExit):
            pua.main(argv)


async def test_concurrent_run_is_skipped_while_another_holds_the_lock(seeded_db):
    """Two replicas starting a run at once: the second skips it, so tenants'
    admin keys are not used twice and the upserts do not race."""
    _, env_id, _ = await _tenants(seeded_db)
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    seen: list[str] = []
    async with factory() as other:
        await other.execute(
            text("SELECT pg_advisory_xact_lock(:k)"), {"k": pua.SCHEDULE_LOCK_KEY}
        )
        summary = await pua.pull_all_tenants(
            factory,
            days=1,
            settings=_settings(env_id),
            transport=httpx.MockTransport(_handler(seen)),
            sleep=_no_sleep,
            now=NOW,
        )
        assert summary["tenants"] == {}
        assert "skipped" in summary
        assert seen == []
    # Released with the other transaction: the next run pulls.
    summary = await pua.pull_all_tenants(
        factory,
        days=1,
        settings=_settings(env_id),
        transport=httpx.MockTransport(_handler(seen)),
        sleep=_no_sleep,
        now=NOW,
    )
    assert "skipped" not in summary
    assert summary["tenants"][str(env_id)]["providers"]["openai"]["status"] == "ok"
    assert seen
