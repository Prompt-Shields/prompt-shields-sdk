"""LeanIX connector end to end on PostgreSQL: the connector reads the real
Partner API (in-process, with a scoped partner key issued by the admin
API) and pushes to a mocked LeanIX. No network."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from collector import leanix as lx
from collector.app import app
from db.models import AIAsset, DataFlow, Tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ADMIN = {"Authorization": "Bearer ps-test-key-12345"}  # seeded tenant key
OPERATOR = "operator-token-for-tests"
LX_TOKEN = "lx-API-TOKEN-secret-value"
HOST = "acme.leanix.net"


async def _public(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


async def _no_sleep(_s: float) -> None:
    return None


class FakeLeanIX:
    def __init__(self):
        self.ldifs: list[dict] = []

    def __call__(self, r: httpx.Request) -> httpx.Response:
        assert r.url.host == HOST
        if r.url.path == lx.TOKEN_PATH:
            return httpx.Response(200, json={"access_token": "bearer"})
        if r.url.path.endswith("/configurations/"):
            return httpx.Response(200, json={})
        if r.url.path.endswith("/synchronizationRuns"):
            self.ldifs.append(json.loads(r.content))
            return httpx.Response(200, json={"id": f"run-{len(self.ldifs)}"})
        if r.url.path.endswith("/status"):
            return httpx.Response(200, json={"status": "FINISHED"})
        return httpx.Response(404)


async def _partner_key(scopes: list[str]) -> str:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/api/v1/admin/partner-credentials",
            headers=ADMIN,
            json={"partner_name": "LeanIX connector", "scopes": scopes},
        )
        assert r.status_code == 201, r.text
        return r.json()["api_key"]


@pytest.fixture
def app_sessions(seeded_db, monkeypatch):
    """Match production: the app's sessions keep loaded rows after commit
    (the shared fixture's factory expires them, which /export trips on)."""
    from collector import app as app_module

    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(app_module, "SessionLocal", factory)
    return factory


def _settings(tenant_id, key: str) -> lx.LeanIXSettings:
    return lx.LeanIXSettings(
        leanix={
            str(tenant_id): {
                "workspace_host": HOST,
                "api_token": LX_TOKEN,
                "partner_api": {"base_url": "https://collector.test", "api_key": key},
            }
        }
    )


@pytest.mark.asyncio
async def test_round_trip_through_partner_api(app_sessions, monkeypatch):
    factory = app_sessions
    async with factory() as db:
        tenant = (await db.execute(select(Tenant))).scalar_one()
        tid = tenant.id
        asset = AIAsset(
            tenant_id=tid,
            vendor="anthropic",
            model="claude",
            use_case_name="Contract review",
            business_unit="Legal",
            discovery_source=["sdk"],
        )
        db.add(asset)
        await db.flush()
        db.add(
            DataFlow(
                tenant_id=tid,
                asset_id=asset.id,
                source_system="dms",
                destination_system="anthropic/claude",
                direction="input",
                detected_pii_types=["person_name"],
            )
        )
        await db.commit()
        asset_id = str(asset.id)

    key = await _partner_key(["export:read", "changes:read", "data-flows:read"])
    settings = _settings(tid, key)
    fake = FakeLeanIX()
    kw = {
        "settings": settings,
        "partner_transport": ASGITransport(app=app),
        "leanix_transport": httpx.MockTransport(fake),
        "resolver": _public,
        "sleep": _no_sleep,
    }

    async with factory() as db:
        out = await lx.sync_leanix(db, tid, **kw)
    assert out["status"] == "ok", out
    assert out["mode"] == "full" and out["assets"] == 1 and out["data_flows"] == 1
    ids = {(i["type"], i["id"]) for i in fake.ldifs[0]["content"]}
    assert ("ITComponent", f"promptshields:asset:{asset_id}") in ids
    assert ("Provider", "promptshields:provider:anthropic") in ids
    assert ("Organization", "promptshields:business-unit:Legal") in ids
    assert any(t == "Interface" for t, _ in ids)

    # Retire the asset; the incremental run picks it up through /changes.
    async with factory() as db:
        a = await db.get(AIAsset, asset.id)
        a.lifecycle_state = "retired"
        a.lifecycle_changed_at = datetime.now(UTC)
        await db.commit()
        out = await lx.sync_leanix(db, tid, **kw)
    assert out["status"] == "ok" and out["mode"] == "incremental", out
    itc = next(i for i in fake.ldifs[1]["content"] if i["type"] == "ITComponent")
    assert itc["id"] == f"promptshields:asset:{asset_id}"
    assert itc["data"]["lifecyclePhase"] == "endOfLife"

    # Operator status shows state and host, never the token or partner key.
    monkeypatch.setenv("PS_ADMIN_TOKEN", OPERATOR)
    conn = {
        "workspace_host": HOST,
        "api_token": LX_TOKEN,
        "partner_api": {"base_url": "https://collector.test", "api_key": key},
    }
    monkeypatch.setenv("PS_LEANIX", json.dumps({str(tid): conn}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(
            "/api/v1/admin/connectors/leanix/status",
            params={"tenant_id": str(tid)},
            headers={"Authorization": f"Bearer {OPERATOR}"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True and body["workspace_host"] == HOST
    assert body["last_status"] == "ok" and body["cursor"]
    assert LX_TOKEN not in r.text and key not in r.text


@pytest.mark.asyncio
async def test_missing_scope_is_reported_and_cursor_untouched(app_sessions):
    factory = app_sessions
    async with factory() as db:
        tid = (await db.execute(select(Tenant))).scalar_one().id
        db.add(AIAsset(tenant_id=tid, vendor="openai", discovery_source=["sdk"]))
        await db.commit()

    # export only: data flows need data-flows:read.
    key = await _partner_key(["export:read"])
    async with factory() as db:
        out = await lx.sync_leanix(
            db,
            tid,
            settings=_settings(tid, key),
            partner_transport=ASGITransport(app=app),
            leanix_transport=httpx.MockTransport(FakeLeanIX()),
            resolver=_public,
            sleep=_no_sleep,
        )
        assert out["status"] == "error"
        assert out["error"] == "partner-api: data flows returned HTTP 403"
        tenant = await db.get(Tenant, tid)
        await db.refresh(tenant)
        state = tenant.settings["connectors"]["leanix"]
        assert "cursor" not in state and state["last_status"] == "error"


@pytest.mark.asyncio
async def test_sync_endpoint_requires_operator(seeded_db, monkeypatch):
    monkeypatch.setenv("PS_ADMIN_TOKEN", OPERATOR)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/api/v1/admin/connectors/leanix/sync",
            json={"tenant_id": "00000000-0000-0000-0000-000000000000"},
            headers=ADMIN,
        )
        assert r.status_code == 401
        r = await c.post(
            "/api/v1/admin/connectors/leanix/sync",
            json={"tenant_id": "00000000-0000-0000-0000-000000000000"},
            headers={"Authorization": f"Bearer {OPERATOR}"},
        )
        assert r.status_code == 404
