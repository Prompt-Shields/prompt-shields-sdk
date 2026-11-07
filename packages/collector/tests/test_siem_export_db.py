"""SIEM connectors (#353) on PostgreSQL: the push job for Splunk HEC, the
webhook and syslog against recorded receivers, cursors per connector and
kind, partial failure, an unexpected failure in an all-tenants run, a run
under row-level security, and the operator routes. No network."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from collector import rls
from collector import siem_export as sx
from collector.app import app
from db.models import GatewaySecurityEvent, Tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.test_rls_db import rls_db  # noqa: F401 - fixture
from tests.test_sentinel_export import _no_sleep, _public
from tests.test_sentinel_export_db import ADMIN, OPERATOR, _seed
from tests.test_siem_export import HEC_TOKEN, HOOK_SECRET, Recorder

HEC = {"url": "https://splunk.example.com:8088", "token": HEC_TOKEN}
HOOK = {"url": "https://hooks.example.com/in/SECRET-PATH", "signing_secret": HOOK_SECRET}


@pytest.fixture
def factory(seeded_db):
    from collector import app as app_module

    f = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    app_module.SessionLocal = f
    return f


async def _seed_security(factory, tid, n: int = 2, *, received: datetime | None = None):
    received = received or datetime.now(UTC) - timedelta(hours=1)
    async with factory() as db:
        await rls.set_tenant(db, tid)
        for i in range(n):
            db.add(
                GatewaySecurityEvent(
                    tenant_id=tid,
                    event_id=f"sec-{i}",
                    name="pii.detected",
                    occurred_at=received - timedelta(seconds=5),
                    received_at=received + timedelta(seconds=i),
                    severity="medium",
                    action="allowed",
                    signature="EMAIL_ADDRESS",
                    attributes={"ps.pii.category": "EMAIL_ADDRESS", "ps.pii.count": 2},
                )
            )
        await db.commit()


def _settings(tid, **connectors) -> sx.SiemSettings:
    return sx.SiemSettings(siem_export={str(tid): connectors})


def _kw(rec: Recorder, **over) -> dict:
    return {
        "transport": httpx.MockTransport(rec),
        "resolver": _public,
        "sleep": _no_sleep,
        "now": datetime.now(UTC),
        **over,
    }


async def _state(factory, tid, connector) -> dict:
    async with factory() as db:
        return (await db.get(Tenant, tid)).settings["connectors"][sx.state_key(connector)]


def _hec_events(rec: Recorder) -> list[dict]:
    return [json.loads(line) for r in rec.requests for line in r.content.splitlines()]


async def test_splunk_push_sends_every_kind_once(factory, caplog):
    caplog.set_level(logging.DEBUG)
    tid, other = await _seed(factory)
    await _seed_security(factory, tid)
    await _seed_security(factory, other, 1)
    rec = Recorder()
    async with factory() as db:
        out = await sx.export_siem(
            db, tid, "splunk_hec", settings=_settings(tid, splunk_hec=HEC), **_kw(rec)
        )
    assert out["status"] == "ok", out
    assert (out["calls"], out["audit"], out["security"], out["requests"]) == (3, 2, 2, 3)
    events = _hec_events(rec)
    by_type: dict[str, list] = {}
    for e in events:
        by_type.setdefault(e["sourcetype"], []).append(e["event"])
    assert [c["PsCallId"] for c in by_type["prompt_shields:call"]] == [
        "call-0", "call-1", "call-2"
    ]
    assert [a["PsAuditSeq"] for a in by_type["prompt_shields:audit"]] == [1, 2]
    sec = by_type["prompt_shields:security"]
    assert [s["event.id"] for s in sec] == ["sec-0", "sec-1"]
    assert sec[0]["ps.pii.count"] == 2 and sec[0]["ps.tenant_id"] == str(tid)
    assert {c["PsTenantId"] for c in by_type["prompt_shields:call"]} == {str(tid)}
    assert "secret-prompt-hash" not in json.dumps(events)

    state = await _state(factory, tid, "splunk_hec")
    assert state["audit_seq"] == 2 and state["calls_cursor"] and state["security_cursor"]
    assert state["last_status"] == "ok"
    assert HEC_TOKEN not in json.dumps(state) and HEC_TOKEN not in caplog.text

    # Nothing new: nothing sent. Each connector keeps its own cursors.
    rec2 = Recorder()
    async with factory() as db:
        out = await sx.export_siem(
            db, tid, "splunk_hec", settings=_settings(tid, splunk_hec=HEC), **_kw(rec2)
        )
    assert out["requests"] == 0 and rec2.requests == []
    async with factory() as db:
        out = await sx.export_siem(
            db, tid, "webhook", settings=_settings(tid, webhook=HOOK), **_kw(rec2)
        )
    assert (out["calls"], out["audit"], out["security"]) == (3, 2, 2)


async def test_security_events_wait_for_the_settle_time(factory):
    tid, _ = await _seed(factory, n_calls=0, recent=0)
    await _seed_security(factory, tid, 1, received=datetime.now(UTC) - timedelta(seconds=10))
    rec = Recorder()
    cfg = _settings(tid, splunk_hec={**HEC, "include": ["security"]})
    async with factory() as db:
        out = await sx.export_siem(db, tid, "splunk_hec", settings=cfg, **_kw(rec))
    assert out["security"] == 0
    later = _kw(rec, now=datetime.now(UTC) + timedelta(minutes=10))
    async with factory() as db:
        out = await sx.export_siem(db, tid, "splunk_hec", settings=cfg, **later)
    assert out["security"] == 1


async def test_failed_batch_keeps_progress(factory):
    tid, _ = await _seed(factory, n_calls=4, recent=0)
    rec = Recorder([200, 400])
    cfg = _settings(tid, splunk_hec={**HEC, "batch_records": 2, "include": ["calls"]})
    async with factory() as db:
        out = await sx.export_siem(db, tid, "splunk_hec", settings=cfg, **_kw(rec))
    assert out["status"] == "error" and "HTTP 400" in out["error"] and out["calls"] == 2
    assert HEC_TOKEN not in out["error"]
    state = await _state(factory, tid, "splunk_hec")
    assert state["last_status"] == "error" and state["calls_cursor"]

    rec2 = Recorder()
    async with factory() as db:
        out = await sx.export_siem(db, tid, "splunk_hec", settings=cfg, **_kw(rec2))
    assert out["status"] == "ok"
    assert [e["event"]["PsCallId"] for e in _hec_events(rec2)] == ["call-2", "call-3"]


async def test_webhook_and_syslog_jobs(factory):
    tid, _ = await _seed(factory, n_calls=1, recent=0)
    await _seed_security(factory, tid, 1)
    rec = Recorder()
    async with factory() as db:
        out = await sx.export_siem(
            db, tid, "webhook", settings=_settings(tid, webhook=HOOK), **_kw(rec)
        )
    assert out["status"] == "ok" and out["requests"] == 3
    kinds = [json.loads(r.content)["kind"] for r in rec.requests]
    assert kinds == ["calls", "audit", "security"]
    assert all(r.url.path == "/in/SECRET-PATH" for r in rec.requests)
    assert all("prompt-shields-signature" in r.headers for r in rec.requests)

    written: list[bytes] = []

    class Writer:
        def write(self, data):
            written.append(data)

        async def drain(self):
            return None

        def close(self):
            return None

        async def wait_closed(self):
            return None

    async def opener(address, port, context, server_hostname):
        assert (address, port, server_hostname) == ("93.184.216.34", 6514, "siem.example.com")
        return None, Writer()

    async with factory() as db:
        out = await sx.export_siem(
            db,
            tid,
            "syslog",
            settings=_settings(tid, syslog={"host": "siem.example.com"}),
            **_kw(Recorder(), opener=opener),
        )
    assert out["status"] == "ok" and (out["calls"], out["audit"], out["security"]) == (1, 2, 1)
    frames = b"".join(written)
    assert frames.count(b" CEF:0|") == 4
    assert b" SecurityEvent - CEF:0|Prompt Shields|Prompt Shields Gateway|1.0|pii.detected|" in frames


async def test_not_configured_bad_config_and_unexpected_failure(factory, monkeypatch):
    tid, other = await _seed(factory, n_calls=1, recent=0)
    rec = Recorder()
    async with factory() as db:
        out = await sx.export_siem(
            db, other, "syslog", settings=_settings(tid, splunk_hec=HEC), **_kw(rec)
        )
    assert out["status"] == "not_configured"
    async with factory() as db:
        out = await sx.export_siem(
            db, UUID(int=1), "syslog", settings=_settings(tid), **_kw(rec)
        )
    assert out["status"] == "tenant_not_found"
    monkeypatch.setenv("PS_SIEM_EXPORT", "{not json")
    async with factory() as db:
        out = await sx.export_siem(db, tid, "webhook", **_kw(rec))
    assert out["status"] == "error" and "PS_SIEM_EXPORT" in out["error"]

    async def boom(*args, **kwargs):
        raise RuntimeError(f"upstream said {HEC_TOKEN}")

    monkeypatch.delenv("PS_SIEM_EXPORT")
    monkeypatch.setattr(sx, "load_calls", boom)
    settings = sx.SiemSettings(
        siem_export={str(tid): {"splunk_hec": HEC}, str(other): {"webhook": HOOK}}
    )
    results = await sx.export_all_tenants(factory, settings=settings, **_kw(rec))
    by_tenant = {r["tenant_id"]: r for r in results}
    assert by_tenant[str(tid)]["error"] == "unexpected error (RuntimeError)"
    assert by_tenant[str(other)]["status"] == "error"
    assert HEC_TOKEN not in json.dumps(results)
    assert "2 runs, 0 ok" in sx.summarize(results)
    state = await _state(factory, tid, "splunk_hec")
    assert state["last_error"] == "unexpected error (RuntimeError)"


async def test_job_under_row_level_security(rls_db):  # noqa: F811
    a, b = rls_db["a"], rls_db["b"]
    rec = Recorder()
    async with rls_db["plain"]() as db:
        await rls.set_tenant(db, a)
        out = await sx.export_siem(
            db,
            a,
            "splunk_hec",
            settings=_settings(a, splunk_hec={**HEC, "include": ["security"]}),
            **_kw(rec, now=datetime.now(UTC) + timedelta(hours=1)),
        )
    assert out["status"] == "ok" and out["security"] == 1
    (event,) = _hec_events(rec)
    assert event["event"]["event.id"] == "seed-a" and event["event"]["ps.tenant_id"] == str(a)
    assert str(b) not in json.dumps(event)


async def test_operator_routes(factory, monkeypatch):
    tid, _ = await _seed(factory, n_calls=0, recent=0)
    monkeypatch.setenv("PS_ADMIN_TOKEN", OPERATOR)
    monkeypatch.setenv(
        "PS_SIEM_EXPORT",
        json.dumps({str(tid): {"splunk_hec": HEC, "webhook": {**HOOK, "bearer_token": "b-TOK"}}}),
    )
    op = {"Authorization": f"Bearer {OPERATOR}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/api/v1/admin/connectors/splunk-hec/sync", json={"tenant_id": str(tid)}, headers=ADMIN
        )
        assert r.status_code == 401
        r = await c.post(
            "/api/v1/admin/connectors/webhook/sync",
            json={"tenant_id": "00000000-0000-0000-0000-000000000000"},
            headers=op,
        )
        assert r.status_code == 404
        r = await c.post(
            "/api/v1/admin/connectors/syslog/sync", json={"tenant_id": str(tid)}, headers=op
        )
        assert r.status_code == 200 and r.json()["status"] == "not_configured"
        statuses = {}
        for slug in ("splunk-hec", "webhook", "syslog"):
            r = await c.get(
                f"/api/v1/admin/connectors/{slug}/status",
                params={"tenant_id": str(tid)},
                headers=op,
            )
            assert r.status_code == 200, r.text
            statuses[slug] = r.json()
            for secret in (HEC_TOKEN, HOOK_SECRET, "b-TOK", "SECRET-PATH"):
                assert secret not in r.text
    assert statuses["splunk-hec"]["host"] == "splunk.example.com"
    assert statuses["splunk-hec"]["port"] == 8088
    assert statuses["webhook"] == {
        **statuses["webhook"],
        "configured": True,
        "host": "hooks.example.com",
        "signed": True,
        "bearer_token": True,
    }
    assert statuses["syslog"]["configured"] is False
