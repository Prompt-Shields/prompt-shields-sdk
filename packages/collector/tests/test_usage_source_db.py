"""usage_source on usage events (#389): "provider" or "estimated", from the
gateway and SDK events, from older SDKs' tokens_estimated, and from the
ps.usage_source attribute on OTLP spans. Anything else is stored as null and
counted as usage_source:invalid."""

import importlib.util
from pathlib import Path

import pytest
from collector.app import app
from collector.ingest import IngestEvent
from collector.otel_mapping import map_span
from db.models import AIUsageEvent
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

KEY = "ps-test-key-12345"
HEADERS = {"Authorization": f"Bearer {KEY}"}


def _event(**extra):
    return {"vendor": "openai", "model": "gpt-4o", "source": "gateway",
            "tokens_in": 10, "tokens_out": 4, "stream": True, **extra}


async def _ingest(events):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/ingest/events", headers=HEADERS, json={"events": events})
        loss = (await c.get("/telemetry/loss", headers=HEADERS)).json()
    return resp, loss


async def _sources(engine):
    async with async_sessionmaker(engine, class_=AsyncSession)() as s:
        rows = (await s.execute(select(AIUsageEvent).order_by(AIUsageEvent.tokens_in))).scalars()
        return [r.usage_source for r in rows]


@pytest.mark.asyncio
async def test_usage_source_is_stored(seeded_db):
    resp, loss = await _ingest([
        _event(tokens_in=1, usage_source="provider"),
        _event(tokens_in=2, usage_source="estimated"),
        _event(tokens_in=3, tokens_estimated=True),  # older SDKs
        _event(tokens_in=4),
        _event(tokens_in=5, usage_source="guessed"),
    ])
    assert resp.status_code == 200, resp.text
    assert await _sources(seeded_db) == ["provider", "estimated", "estimated", None, None]
    (stats,) = loss["clients"]
    assert stats["ignored_fields"].get("usage_source:invalid") == 1


@pytest.mark.parametrize("value,expected", [
    ("provider", "provider"),
    ("ESTIMATED", "estimated"),
    ("estimate", None),
    (True, None),
    (None, None),
])
def test_ingest_event_usage_source(value, expected):
    assert IngestEvent(vendor="openai", usage_source=value).usage_source == expected


def test_tokens_estimated_does_not_override_an_explicit_source():
    ev = IngestEvent(vendor="openai", usage_source="provider", tokens_estimated=True)
    assert ev.usage_source == "provider"


def _span(attrs):
    return {
        "traceId": "0af7651916cd43dd8448eb211c80319c",
        "spanId": "b7ad6b7169203331",
        "name": "chat gpt-4o",
        "startTimeUnixNano": "1000000000",
        "endTimeUnixNano": "2000000000",
        "attributes": [
            {"key": k, "value": {"stringValue": v} if isinstance(v, str) else {"intValue": v}}
            for k, v in attrs.items()
        ],
    }


@pytest.mark.parametrize("value,expected", [
    ("estimated", "estimated"),
    ("provider", "provider"),
    ("other", None),
])
def test_otel_span_usage_source(value, expected):
    span = map_span(_span({
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "openai",
        "gen_ai.usage.output_tokens": 3,
        "ps.usage_source": value,
    }), {})
    assert span.usage_source == expected


def test_migration_adds_the_column():
    pytest.importorskip("alembic")
    versions = Path(__file__).resolve().parents[2] / "db" / "alembic" / "versions"
    (path,) = versions.glob("*_usage_event_usage_source.py")
    spec = importlib.util.spec_from_file_location("migration_usage_source", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "056"
    assert "usage_source" in AIUsageEvent.__table__.columns
