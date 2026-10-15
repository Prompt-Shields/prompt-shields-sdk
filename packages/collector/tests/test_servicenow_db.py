"""ServiceNow connector end to end on PostgreSQL: the connector reads the
real Partner API (in-process, with a scoped partner key issued by the
admin API) and pushes to a mocked ServiceNow IRE API. Also checks, for
both connectors, that a partner key belonging to another tenant is
refused. No network."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import httpx
import pytest
from collector import leanix as lx
from collector import servicenow as sn
from collector.app import app
from db.models import AIAsset, Tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ADMIN = {"Authorization": "Bearer ps-test-key-12345"}  # seeded tenant key
OPERATOR = "operator-token-for-tests"
CLIENT_SECRET = "sn-CLIENT-SECRET-value"
HOST = "acme.service-now.com"


async def _public(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


async def _no_sleep(_s: float) -> None:
    return None


class FakeServiceNow:
    def __init__(self):
        self.payloads: list[dict] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, r: httpx.Request) -> httpx.Response:
        self.requests.append(r)
        assert r.url.host == "93.184.216.34" and r.headers["host"] == HOST
        if r.url.path == sn.TOKEN_PATH:
            return httpx.Response(200, json={"access_token": "bearer"})
        if r.url.path == sn.IRE_PATH:
            payload = json.loads(r.content)
            self.payloads.append(payload)
            items = [
                {
                    "className": i["className"],
                    "operation": "INSERT",
                    "sysId": hashlib.md5(
                        i["values"]["correlation_id"].encode()
                    ).hexdigest(),
                }
                for i in payload["items"]
            ]
            return httpx.Response(200, json={"result": {"items": items}})
        return httpx.Response(404)


async def _partner_key(scopes: list[str]) -> str:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/api/v1/admin/partner-credentials",
            headers=ADMIN,
            json={"partner_name": "ServiceNow connector", "scopes": scopes},
        )
        assert r.status_code == 201, r.text
        return r.json()["api_key"]


@pytest.fixture
def app_sessions(seeded_db):
    """Match production: the app's sessions keep loaded rows after commit."""
    from collector import app as app_module

    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    app_module.SessionLocal = factory
    return factory


def _conn(key: str) -> dict:
    return {
        "instance_host": HOST,
        "oauth_client_id": "ps-connector",
        "oauth_client_secret": CLIENT_SECRET,
        "partner_api": {"base_url": "https://collector.test", "api_key": key},
    }


def _kw(tenant_id, key: str, fake: FakeServiceNow) -> dict:
    return {
        "settings": sn.ServiceNowSettings(servicenow={str(tenant_id): _conn(key)}),
        "partner_transport": ASGITransport(app=app),
        "servicenow_transport": httpx.MockTransport(fake),
        "resolver": _public,
        "sleep": _no_sleep,
    }


@pytest.mark.asyncio
async def test_round_trip_through_partner_api(app_sessions, monkeypatch):
    factory = app_sessions
    async with factory() as db:
        tid = (await db.execute(select(Tenant))).scalar_one().id
        asset = AIAsset(
            tenant_id=tid,
            vendor="anthropic",
            model="claude",
            use_case_name="Contract review",
            discovery_source=["sdk"],
        )
        db.add(asset)
        await db.commit()
        asset_id = str(asset.id)

    # No data-flows:read needed: this slice does not map flows.
    key = await _partner_key(["export:read", "changes:read"])
    fake = FakeServiceNow()
    async with factory() as db:
        out = await sn.sync_servicenow(db, tid, **_kw(tid, key, fake))
    assert out["status"] == "ok", out
    assert out["mode"] == "full" and out["assets"] == 1 and out["relations"] == 1
    refs = {i["values"]["correlation_id"] for i in fake.payloads[0]["items"]}
    assert refs == {
        f"promptshields:asset:{asset_id}",
        "promptshields:provider:anthropic",
    }

    # Retire the asset; the incremental run picks it up through /changes.
    async with factory() as db:
        a = await db.get(AIAsset, asset.id)
        a.lifecycle_state = "retired"
        a.lifecycle_changed_at = datetime.now(UTC)
        await db.commit()
        out = await sn.sync_servicenow(db, tid, **_kw(tid, key, fake))
    assert out["status"] == "ok" and out["mode"] == "incremental", out
    ci = fake.payloads[1]["items"][0]
    assert ci["values"]["correlation_id"] == f"promptshields:asset:{asset_id}"
    assert ci["values"]["install_status"] == "7"

    # Operator status shows state, host and method, never a secret.
    monkeypatch.setenv("PS_ADMIN_TOKEN", OPERATOR)
    monkeypatch.setenv("PS_SERVICENOW", json.dumps({str(tid): _conn(key)}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(
            "/api/v1/admin/connectors/servicenow/status",
            params={"tenant_id": str(tid)},
            headers={"Authorization": f"Bearer {OPERATOR}"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True and body["instance_host"] == HOST
    assert body["auth_method"] == "oauth_client_credentials"
    assert body["last_status"] == "ok" and body["cursor"]
    assert CLIENT_SECRET not in r.text and key not in r.text


@pytest.mark.asyncio
async def test_partner_key_of_another_tenant_is_refused(app_sessions):
    """The operator pairs tenant B with tenant A's partner key: both
    connectors refuse before writing anything or moving B's cursor."""
    factory = app_sessions
    async with factory() as db:
        tenant_a = (await db.execute(select(Tenant))).scalar_one().id
        db.add(AIAsset(tenant_id=tenant_a, vendor="openai", discovery_source=["sdk"]))
        tenant_b = Tenant(name="Other Corp", domain="other.com", settings={})
        db.add(tenant_b)
        await db.commit()
        tenant_b = tenant_b.id

    key_a = await _partner_key(["export:read", "changes:read", "data-flows:read"])

    fake = FakeServiceNow()
    async with factory() as db:
        out = await sn.sync_servicenow(db, tenant_b, **_kw(tenant_b, key_a, fake))
    assert out["status"] == "error"
    assert "does not belong to the configured tenant" in out["error"]
    assert fake.requests == []

    lx_requests: list[httpx.Request] = []
    async with factory() as db:
        out = await lx.sync_leanix(
            db,
            tenant_b,
            settings=lx.LeanIXSettings(
                leanix={
                    str(tenant_b): {
                        "workspace_host": "acme.leanix.net",
                        "api_token": "lx-token",
                        "partner_api": {
                            "base_url": "https://collector.test",
                            "api_key": key_a,
                        },
                    }
                }
            ),
            partner_transport=ASGITransport(app=app),
            leanix_transport=httpx.MockTransport(
                lambda r: lx_requests.append(r) or httpx.Response(500)
            ),
            resolver=_public,
            sleep=_no_sleep,
        )
    assert out["status"] == "error"
    assert "does not belong to the configured tenant" in out["error"]
    assert lx_requests == []

    async with factory() as db:
        b = await db.get(Tenant, tenant_b)
        await db.refresh(b)
        for name in ("servicenow", "leanix"):
            state = b.settings["connectors"][name]
            assert "cursor" not in state and state["last_status"] == "error"


@pytest.mark.asyncio
async def test_sync_endpoint_requires_operator(seeded_db, monkeypatch):
    monkeypatch.setenv("PS_ADMIN_TOKEN", OPERATOR)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        body = {"tenant_id": "00000000-0000-0000-0000-000000000000"}
        r = await c.post(
            "/api/v1/admin/connectors/servicenow/sync", json=body, headers=ADMIN
        )
        assert r.status_code == 401
        r = await c.post(
            "/api/v1/admin/connectors/servicenow/sync",
            json=body,
            headers={"Authorization": f"Bearer {OPERATOR}"},
        )
        assert r.status_code == 404
