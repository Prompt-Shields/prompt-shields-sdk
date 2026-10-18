"""Per-source lifecycle thresholds and owner-initiated retirement (#100),
against a real PostgreSQL database (PS_TEST_DATABASE_URL) with row-level
security enforced, through the full app: sweep endpoint, attestation
routes, ingest, the registry, the Partner API delta feed and the webhook
outbox."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from collector import rls
from collector.app import app
from collector.partner_auth import hash_secret
from db.api_keys import api_key_lookup_prefix, hash_api_key
from db.models import (
    AIAsset,
    PartnerCredential,
    Tenant,
    WebhookDelivery,
    WebhookSubscription,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

KEY_A = {"Authorization": "Bearer ps-test-key-12345"}  # the seeded_db tenant
KEY_B = "ps-test-key-lifecycle-b"
PARTNER_KEY = "ps-pk-lifecycle-partner-a"
PARTNER = {"Authorization": f"Bearer {PARTNER_KEY}"}
EVENT = {"vendor": "openai", "model": "gpt-4o", "use_case_name": "screening"}


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def env(seeded_db):
    """Tenant A with a partner subscribed to every asset event, tenant B,
    RLS enforced. Assets are added per test through `admin`."""
    admin = async_sessionmaker(
        seeded_db,
        class_=AsyncSession,
        expire_on_commit=False,
        info=rls.bypass_info("test-seed"),
    )
    async with admin() as s:
        a = (await s.execute(select(Tenant.id))).scalar_one()
        b = Tenant(
            name="Other Corp",
            settings={},
            api_key_prefix=api_key_lookup_prefix(KEY_B),
            api_key_hash=hash_api_key(KEY_B),
        )
        partner = PartnerCredential(
            tenant_id=a,
            partner_name="ea-tool",
            client_id="lifecycle-client",
            client_secret_hash=hash_secret("x"),
            api_key_hash=hash_secret(PARTNER_KEY),
            scopes=["registry:read"],
            is_active="true",
        )
        s.add_all([b, partner])
        await s.flush()
        s.add(
            WebhookSubscription(
                tenant_id=a,
                partner_id=partner.id,
                url="https://hooks.example/lifecycle",
                event_types=["asset.created", "asset.changed", "asset.retired"],
                secret_ciphertext="unused",
                is_active=True,
            )
        )
        await s.commit()
        b = b.id
    async with seeded_db.begin() as conn:
        await rls.enable(conn)
    return {"admin": admin, "a": a, "b": b}


async def _add_assets(admin, tenant_id, specs) -> dict[str, uuid.UUID]:
    """specs: {name: (discovery_source, days since last seen)}."""
    now = datetime.now(UTC)
    ids = {}
    async with admin() as s:
        for name, (sources, age) in specs.items():
            a = AIAsset(
                tenant_id=tenant_id,
                vendor="openai",
                model=name,
                discovery_source=sources,
                last_seen=now - timedelta(days=age),
            )
            s.add(a)
            await s.flush()
            ids[name] = a.id
        await s.commit()
    return ids


async def _states(admin) -> dict[str, str]:
    async with admin() as s:
        rows = (await s.execute(select(AIAsset.model, AIAsset.lifecycle_state))).all()
    return dict(rows)


async def _deliveries(admin) -> list[tuple[str, list | None]]:
    async with admin() as s:
        rows = (
            await s.execute(select(WebhookDelivery).order_by(WebhookDelivery.created_at))
        ).scalars().all()
    return [(d.event_type, d.payload["data"].get("changed_fields")) for d in rows]


async def test_sweep_uses_per_source_thresholds(env):
    await _add_assets(
        env["admin"],
        env["a"],
        {
            "sdk-20d": (["sdk"], 20),  # runtime sources: global 30 / 90
            "sdk-40d": (["sdk"], 40),
            "scan-20d": (["code_scan"], 20),  # code_scan stale after 90
            "scan-100d": (["code_scan"], 100),
            "platform-40d": (["platform_signal"], 40),  # retired after 30
            "sdk+scan-100d": (["sdk", "code_scan"], 100),  # most lenient: scan
            "sdk+scan-200d": (["sdk", "code_scan"], 200),
            "custom-40d": (["custom"], 40),  # global default 30 / 90
            "gateway-5d": (["gateway"], 5),
        },
    )
    await _add_assets(env["admin"], env["b"], {"b-sdk-40d": (["sdk"], 40)})

    async with _client() as c:
        r = await c.post("/api/v1/admin/asset-lifecycle/sweep", headers=KEY_A)
        assert r.status_code == 200, r.text
        body = r.json()
        again = (await c.post("/api/v1/admin/asset-lifecycle/sweep", headers=KEY_A)).json()

    assert (body["marked_stale"], body["marked_retired"]) == (4, 2)
    assert body["stale_after_days"] == 30 and body["retire_after_days"] == 90
    assert body["source_thresholds"]["code_scan"] == {
        "stale_after_days": 90,
        "retire_after_days": 180,
    }
    assert (again["marked_stale"], again["marked_retired"], again["reactivated"]) == (0, 0, 0)

    assert await _states(env["admin"]) == {
        "sdk-20d": "active",
        "sdk-40d": "stale",
        "scan-20d": "active",
        "scan-100d": "stale",
        "platform-40d": "retired",
        "sdk+scan-100d": "stale",
        "sdk+scan-200d": "retired",
        "custom-40d": "stale",
        "gateway-5d": "active",
        "b-sdk-40d": "active",  # other tenant: untouched (RLS + tenant scope)
    }
    events = await _deliveries(env["admin"])
    assert events.count(("asset.retired", None)) == 2
    assert events.count(("asset.changed", ["lifecycle_state"])) == 4


async def test_source_thresholds_from_settings_and_explicit_override(env, monkeypatch):
    from collector.config import settings

    monkeypatch.setattr(settings, "asset_lifecycle_source_thresholds", "sdk=45:60")
    await _add_assets(env["admin"], env["a"], {"sdk-40d": (["sdk"], 40)})
    async with _client() as c:
        r = await c.post("/api/v1/admin/asset-lifecycle/sweep", headers=KEY_A)
        assert r.json()["source_thresholds"]["sdk"]["stale_after_days"] == 45
        assert await _states(env["admin"]) == {"sdk-40d": "active"}

        # Explicit days apply to every source.
        r = await c.post(
            "/api/v1/admin/asset-lifecycle/sweep?stale_after_days=10&retire_after_days=30",
            headers=KEY_A,
        )
        assert r.json()["source_thresholds"] == {}
        assert r.json()["marked_retired"] == 1 and r.json()["marked_stale"] == 0


async def test_owner_retire_in_use_signal_and_unretire(env):
    async with _client() as c:
        await c.post("/ingest/events", headers=KEY_A, json={"events": [EVENT]})
        asset = (await c.get("/api/v1/registry/assets", headers=KEY_A)).json()["data"][0]
        url = f"/api/v1/registry/assets/{asset['id']}/attestations"
        since = datetime.now(UTC).isoformat()

        # Owner retires an asset that is still active by telemetry.
        r = await c.post(
            url,
            headers=KEY_A,
            json={"field": "asset", "state": "retired", "attested_by": "owner@test.com",
                  "comment": "decommissioned"},
        )
        assert r.status_code == 201, r.text
        got = (await c.get(f"/api/v1/registry/assets/{asset['id']}", headers=KEY_A)).json()
        assert got["lifecycle_state"] == "retired"
        assert got["retired_by"] == "owner@test.com"
        assert got["lifecycle_changed_at"] is not None
        assert got["retired_in_use_at"] is None

        changes = (
            await c.get("/partner/v1/changes", headers=PARTNER, params={"since": since})
        ).json()["data"]
        assert changes[0]["lifecycle_change"] == "retired"
        assert changes[0]["asset"]["retired_by"] == "owner@test.com"

        # The sweep does not reactivate it, although it was just seen.
        r = await c.post("/api/v1/admin/asset-lifecycle/sweep", headers=KEY_A)
        assert r.json()["reactivated"] == 0

        # A new sighting raises "still in use" once instead of reactivating.
        for _ in range(2):
            await c.post("/ingest/events", headers=KEY_A, json={"events": [EVENT]})
        got = (await c.get(f"/api/v1/registry/assets/{asset['id']}", headers=KEY_A)).json()
        assert got["lifecycle_state"] == "retired"
        assert got["retired_in_use_at"] is not None
        changes = (
            await c.get("/partner/v1/changes", headers=PARTNER, params={"since": since})
        ).json()["data"]
        assert changes[0]["lifecycle_change"] == "retired_in_use"

        # Retiring again acknowledges it (no second asset.retired).
        await c.post(
            url,
            headers=KEY_A,
            json={"field": "asset", "state": "retired", "attested_by": "owner@test.com"},
        )
        got = (await c.get(f"/api/v1/registry/assets/{asset['id']}", headers=KEY_A)).json()
        assert got["retired_in_use_at"] is None

        # Retiring another field does not touch the lifecycle.
        await c.post(
            url,
            headers=KEY_A,
            json={"field": "owner", "state": "attested", "attested_by": "owner@test.com"},
        )
        got = (await c.get(f"/api/v1/registry/assets/{asset['id']}", headers=KEY_A)).json()
        assert got["retired_by"] == "owner@test.com"

        # Un-retire: back to the state last_seen calls for (active).
        await c.post(
            url,
            headers=KEY_A,
            json={"field": "asset", "state": "attested", "attested_by": "owner@test.com"},
        )
        got = (await c.get(f"/api/v1/registry/assets/{asset['id']}", headers=KEY_A)).json()
        assert got["lifecycle_state"] == "active"
        assert got["retired_by"] is None

        retired = (
            await c.get("/api/v1/registry/assets?lifecycle_state=retired", headers=KEY_A)
        ).json()["data"]
        assert retired == []

    async with env["admin"]() as s:
        payloads = [
            d.payload
            for d in (await s.execute(select(WebhookDelivery))).scalars()
            if d.event_type == "asset.retired"
        ]
    assert len(payloads) == 1
    assert payloads[0]["data"]["asset"]["retired_by_owner"] is True
    assert "retired_by" not in payloads[0]["data"]["asset"]

    events = [e for e in await _deliveries(env["admin"]) if e[0] != "asset.attested"]
    assert events == [
        ("asset.created", None),
        ("asset.retired", None),
        ("asset.changed", ["retired_in_use_at"]),
        ("asset.changed", ["lifecycle_state"]),
    ]


async def test_owner_unretire_of_long_unseen_asset_stays_retired(env):
    ids = await _add_assets(env["admin"], env["a"], {"old": (["sdk"], 120)})
    url = f"/api/v1/registry/assets/{ids['old']}/attestations"
    async with _client() as c:
        await c.post(
            url,
            headers=KEY_A,
            json={"field": "asset", "state": "retired", "attested_by": "o@test.com"},
        )
        await c.post(
            url,
            headers=KEY_A,
            json={"field": "asset", "state": "unreviewed", "attested_by": "o@test.com"},
        )
        got = (await c.get(f"/api/v1/registry/assets/{ids['old']}", headers=KEY_A)).json()
    assert got["lifecycle_state"] == "retired"  # by telemetry now
    assert got["retired_by"] is None


async def test_disputing_owner_retired_asset_does_not_unretire(env):
    ids = await _add_assets(env["admin"], env["a"], {"m": (["sdk"], 1)})
    url = f"/api/v1/registry/assets/{ids['m']}/attestations"
    async with _client() as c:
        await c.post(
            url,
            headers=KEY_A,
            json={"field": "asset", "state": "retired", "attested_by": "o@test.com"},
        )
        r = await c.post(
            url,
            headers=KEY_A,
            json={"field": "asset", "state": "disputed", "attested_by": "o@test.com",
                  "comment": "model name is wrong"},
        )
        assert r.status_code == 201, r.text
        got = (await c.get(f"/api/v1/registry/assets/{ids['m']}", headers=KEY_A)).json()
    assert got["lifecycle_state"] == "retired"
    assert got["retired_by"] == "o@test.com"


async def test_concurrent_sightings_raise_in_use_once(env):
    """Two ingests that see an owner-retired asset at the same time: the
    second waits for the first's row lock and raises nothing."""
    import asyncio

    from collector.dedup import find_or_create_asset
    from collector.webhooks import AssetChanges

    async with env["admin"]() as s:
        s.add(
            AIAsset(
                tenant_id=env["a"],
                vendor="openai",
                model="gpt-4o",
                discovery_source=["sdk"],
                confidence="high",
                last_seen=datetime.now(UTC),
                lifecycle_state="retired",
                retired_by="o@test.com",
            )
        )
        await s.commit()

    async def sight(session) -> int:
        changes = AssetChanges()
        asset, is_new = await find_or_create_asset(
            session, env["a"], "openai", "gpt-4o", None, None, None, "sdk"
        )
        changes.record(asset, is_new)
        await session.flush()
        return await changes.enqueue(session, env["a"])

    async with env["admin"]() as s1, env["admin"]() as s2:
        first = await sight(s1)  # holds the row lock until commit
        second = asyncio.create_task(sight(s2))
        await asyncio.sleep(0.3)
        assert not second.done()  # blocked on the lock
        await s1.commit()
        queued_second = await second
        await s2.commit()

    assert (first, queued_second) == (1, 0)
    events = await _deliveries(env["admin"])
    assert events == [("asset.changed", ["retired_in_use_at"])]
