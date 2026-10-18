"""Loss accounting per source (#87): gateway batches, the OTLP receiver and
the coverage figure in the registry and Partner API. DB tests use
PS_TEST_DATABASE_URL."""

from __future__ import annotations

import json

import pytest
from collector import otel_ingest
from collector.app import app
from collector.loss_accounting import coverage_by_source, otel_client_id
from collector.otel_ingest import classify_spans
from collector.otel_mapping import invalid_attributes
from collector.partner_auth import hash_secret, issue_access_token
from db.models import PartnerCredential, Tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

HEADERS = {"Authorization": "Bearer ps-test-key-12345"}
OTLP = "/ingest/otel/v1/traces"
OTLP_HEADERS = {**HEADERS, "Content-Type": "application/json"}


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _event(source="sdk"):
    return {"vendor": "openai", "model": "gpt-4o", "source": source}


def _meta(client_id, seq, enqueued, dropped=None, sdk="python"):
    return {
        "client_id": client_id,
        "seq": seq,
        "enqueued_total": enqueued,
        "dropped": dropped or {},
        "sdk": sdk,
    }


def _attr(key, value):
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": value}}


def _span(attrs, start="1700000000000000000", end="1700000000450000000"):
    return {
        "name": "chat",
        "startTimeUnixNano": start,
        "endTimeUnixNano": end,
        "attributes": [_attr(k, v) for k, v in attrs.items()],
    }


def _payload(*spans, service="hr-screener"):
    resource = [_attr("service.name", service)] if service else []
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": resource},
                "scopeSpans": [{"scope": {"name": "x"}, "spans": list(spans)}],
            }
        ]
    }


GENAI = {
    "gen_ai.operation.name": "chat",
    "gen_ai.provider.name": "openai",
    "gen_ai.request.model": "gpt-4o",
    "gen_ai.usage.input_tokens": 10,
    "gen_ai.usage.output_tokens": 5,
}
NO_VENDOR = {"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-4o"}
NON_GENAI = {"http.method": "GET"}


# --- pure helpers -------------------------------------------------------------


def test_otel_client_id_per_service():
    assert otel_client_id({"service.name": " billing "}) == "otel:billing"
    assert otel_client_id({}) == "otel"
    assert len(otel_client_id({"service.name": "x" * 500})) == 128


def test_invalid_attributes():
    span = _span(
        {**GENAI, "gen_ai.usage.input_tokens": -1, "gen_ai.usage.output_tokens": "lots"},
        start="5",
        end="1",
    )
    assert invalid_attributes(span) == {
        "gen_ai.usage.input_tokens:invalid": 1,
        "gen_ai.usage.output_tokens:invalid": 1,
        "span.duration:invalid": 1,
    }
    assert invalid_attributes(_span(GENAI)) == {}


def test_classify_spans_counts_per_service():
    payload = _payload(_span(GENAI), _span(NO_VENDOR), _span(NON_GENAI))
    payload["resourceSpans"] += _payload(
        _span({**GENAI, "gen_ai.request.model": "m" * 300}), service=None
    )["resourceSpans"]
    mapped, counts = classify_spans(payload)
    assert len(mapped) == 2
    hr, anon = counts["otel:hr-screener"], counts["otel"]
    assert (hr.received, hr.rejected, hr.ignored) == (
        1,
        {"no_vendor": 1},
        {"span:non_genai": 1},
    )
    assert (anon.received, anon.rejected) == (1, {})
    assert anon.ignored == {"gen_ai.request.model:invalid": 1}


def test_unidentified_source_has_no_coverage():
    clients = [
        {"source": "unidentified", "received_events": 4, "lost_events": 0,
         "unaccounted": None, "last_seen": None},
        {"source": "gateway", "received_events": 8, "lost_events": 2,
         "unaccounted": 0, "last_seen": "2026-10-18T00:00:00+00:00"},
    ]
    gw, un = coverage_by_source(clients)
    assert (gw["source"], gw["coverage"], gw["dropped_events"]) == ("gateway", 0.8, 2)
    assert (un["source"], un["coverage"], un["received_events"]) == ("unidentified", None, 4)


# --- end to end -----------------------------------------------------------------


async def _ingest_all_sources(c):
    """SDK: 4 queued, 3 received, 1 dropped. Gateway: 5 queued, 3 received,
    1 dropped (send_failed), 1 unaccounted. OTLP: 2 stored, 1 rejected."""
    r = await c.post("/ingest/events", headers=HEADERS, json={
        "events": [_event()] * 3,
        "metadata": _meta("sdk-1", 1, 4, {"buffer_overflow": 1}),
    })
    assert r.status_code == 200, r.text
    for seq in (1, 2, 3):
        r = await c.post("/ingest/events", headers=HEADERS, json={
            "events": [_event("gateway")],
            "metadata": _meta(
                "gw-1", seq, seq + (2 if seq == 3 else 0),
                {"send_failed": 1} if seq == 3 else None, sdk="gateway",
            ),
        })
        assert r.status_code == 200, r.text
    r = await c.post(OTLP, headers=OTLP_HEADERS, content=json.dumps(
        _payload(_span(GENAI), _span(GENAI), _span(NO_VENDOR), _span(NON_GENAI))
    ))
    assert r.status_code == 200, r.text
    assert r.json() == {"partialSuccess": {
        "rejectedSpans": 1,
        "errorMessage": "GenAI spans without gen_ai.provider.name or gen_ai.system",
    }}


@pytest.mark.asyncio
async def test_loss_reports_each_source(seeded_db):
    async with _client() as c:
        await _ingest_all_sources(c)
        loss = (await c.get("/telemetry/loss", headers=HEADERS)).json()
        registry = await c.get("/api/v1/registry/telemetry/coverage", headers=HEADERS)

    by_client = {cl["client_id"]: cl for cl in loss["clients"]}
    assert by_client["sdk-1"]["source"] == "sdk"
    gw = by_client["gw-1"]
    assert (gw["source"], gw["sdk"], gw["last_seq"]) == ("gateway", "gateway", 3)
    assert (gw["received_events"], gw["sdk_dropped"], gw["unaccounted"]) == (
        3, {"send_failed": 1}, 1,
    )
    otel = by_client["otel:hr-screener"]
    assert (otel["source"], otel["sdk"], otel["received_events"]) == ("otel", "otel", 2)
    assert otel["collector_rejected"] == {"no_vendor": 1}
    assert otel["ignored_fields"] == {"span:non_genai": 1}
    assert (otel["lost_events"], otel["coverage"]) == (1, 0.6667)

    sources = {s["source"]: s for s in loss["sources"]}
    assert [s["source"] for s in loss["sources"]] == ["sdk", "gateway", "otel"]
    assert sources["sdk"]["coverage"] == 0.75
    assert sources["gateway"] | {"last_seen": None} == {
        "source": "gateway", "clients": 1, "received_events": 3,
        "dropped_events": 1, "unaccounted_events": 1, "coverage": 0.6,
        "last_seen": None,
    }
    assert sources["otel"]["dropped_events"] == 1
    # 8 received of 8 + 3 dropped + 1 unaccounted.
    assert loss["totals"]["coverage"] == round(8 / 12, 4)
    assert loss["totals"]["lost_events"] == 3

    assert registry.status_code == 200
    body = registry.json()
    assert body["coverage"] == round(8 / 12, 4)
    assert (body["received_events"], body["dropped_events"], body["unaccounted_events"]) == (
        8, 3, 1,
    )
    assert body["sources"] == loss["sources"]


@pytest.mark.asyncio
async def test_partner_api_coverage(seeded_db):
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        tenant = (await s.execute(select(Tenant))).scalar_one()
        cred = PartnerCredential(
            tenant_id=tenant.id, partner_name="ea", client_id="ea-client",
            client_secret_hash=hash_secret("x"), scopes=["usage:read", "assets:read"],
        )
        s.add(cred)
        await s.commit()
    token, _ = issue_access_token(tenant.id, cred.id, "ea-client", ["usage:read"])
    assets_only, _ = issue_access_token(tenant.id, cred.id, "ea-client", ["assets:read"])

    async with _client() as c:
        await _ingest_all_sources(c)
        resp = await c.get(
            "/partner/v1/telemetry/coverage", headers={"Authorization": f"Bearer {token}"}
        )
        denied = await c.get(
            "/partner/v1/telemetry/coverage",
            headers={"Authorization": f"Bearer {assets_only}"},
        )
        registry = (
            await c.get("/api/v1/registry/telemetry/coverage", headers=HEADERS)
        ).json()

    assert resp.status_code == 200, resp.text
    assert resp.json() == registry
    assert [s["source"] for s in resp.json()["sources"]] == ["sdk", "gateway", "otel"]
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_coverage_is_null_without_telemetry(seeded_db):
    async with _client() as c:
        body = (await c.get("/api/v1/registry/telemetry/coverage", headers=HEADERS)).json()
    assert body == {
        "coverage": None, "received_events": 0, "dropped_events": 0,
        "unaccounted_events": 0, "sources": [],
    }


@pytest.mark.asyncio
async def test_otlp_rejections_are_counted(seeded_db, monkeypatch):
    async with _client() as c:
        # Invalid values are stored as null and reported.
        bad = _span({**GENAI, "gen_ai.usage.input_tokens": -5})
        r1 = await c.post(OTLP, headers=OTLP_HEADERS, content=json.dumps(_payload(bad)))

        monkeypatch.setattr(otel_ingest, "MAX_SPANS", 2)
        r2 = await c.post(OTLP, headers=OTLP_HEADERS, content=json.dumps(
            _payload(_span(GENAI), _span(GENAI), _span(NON_GENAI))
        ))
        monkeypatch.setattr(otel_ingest, "MAX_SPANS", 2000)

        async def boom(*_a, **_k):
            raise RuntimeError("db down")

        monkeypatch.setattr(otel_ingest, "write_spans", boom)
        with pytest.raises(RuntimeError):
            await c.post(OTLP, headers=OTLP_HEADERS, content=json.dumps(
                _payload(_span(GENAI), _span(NO_VENDOR))
            ))
        monkeypatch.undo()
        loss = (await c.get("/telemetry/loss", headers=HEADERS)).json()

    assert r1.status_code == 200 and r1.json() == {}
    assert r2.status_code == 413
    (otel,) = loss["clients"]
    assert otel["client_id"] == "otel:hr-screener"
    assert otel["received_events"] == 1
    assert otel["received_batches"] == 1
    assert otel["ignored_fields"] == {"gen_ai.usage.input_tokens:invalid": 1}
    assert otel["collector_rejected"] == {
        "too_large": 2, "write_failed": 1, "no_vendor": 1,
    }
    assert otel["lost_events"] == 4
    assert otel["coverage"] == 0.2
