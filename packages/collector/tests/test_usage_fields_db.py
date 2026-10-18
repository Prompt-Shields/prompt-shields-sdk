"""Extended usage fields (#9, #90, #75): the fields the SDK and gateway send
are stored on ai_usage_events instead of landing in ignored_fields, and
assets carry the reported modality.

The SDK and gateway events below mirror what the clients build:
packages/sdk/prompt_shields/client.py `_build_event` (with
`add_usage_detail_fields`, framework integrations and stream fields) and
gateway/src/middlewares/ps-telemetry.ts `psTelemetry`.
"""

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest
from collector.app import app
from db.models import AIAsset, AIUsageEvent
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

HEADERS = {"Authorization": "Bearer ps-test-key-12345"}

EXTENDED_FIELDS = (
    "requested_model",
    "served_model",
    "tokens_cached",
    "tokens_cache_write",
    "tokens_cache_write_1h",
    "tokens_reasoning",
    "cost_source",
    "service_tier",
    "framework",
    "modality",
    "usage_units",
    "usage_unit_type",
)

# ShieldsClient / instrument() event for an Anthropic call made through the
# LangChain integration, with prompt caching and a stream.
SDK_EVENT = {
    "vendor": "anthropic",
    "model": "claude-sonnet-4-5",
    "requested_model": "claude-sonnet-4-5",
    "served_model": "claude-sonnet-4-5-20250929",
    "source": "sdk",
    "tokens_in": 1200,
    "tokens_out": 400,
    "latency_ms": 900,
    "tool_calls_used": None,
    "cost": 0.0123,
    "api_key_fingerprint": "sk-ant-...abcd",
    "tokens_cached": 800,
    "tokens_cache_write": 300,
    "tokens_cache_write_1h": 100,
    "tokens_reasoning": 150,
    "service_tier": "standard",
    "cost_source": "pricing_table",
    "framework": "langchain",
    "use_case_name": "support-bot",
    "business_unit": "Support",
    "stream": True,
    "stream_completed": True,
}

# psTelemetry event for an audio transcription routed by ps-router.
GATEWAY_EVENT = {
    "vendor": "openai",
    "model": "auto",
    "source": "gateway",
    "api_surface": "audio",
    "modality": "audio",
    "usage_units": 12.5,
    "usage_unit_type": "seconds",
    "latency_ms": 1500,
    "requested_model": "auto",
    "served_model": "gpt-4o-mini-transcribe",
    "route_group": "stt",
    "route_reason": "cheapest",
    "route_est_cost": 0.001,
}


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _ingest(events):
    async with _client() as c:
        resp = await c.post("/ingest/events", headers=HEADERS, json={"events": events})
        loss = (await c.get("/telemetry/loss", headers=HEADERS)).json()
    return resp, loss


async def _rows(engine):
    async with async_sessionmaker(engine, class_=AsyncSession)() as s:
        events = (await s.execute(select(AIUsageEvent))).scalars().all()
        assets = (await s.execute(select(AIAsset))).scalars().all()
    return events, {a.id: a for a in assets}


@pytest.mark.asyncio
async def test_sdk_event_round_trip(seeded_db):
    resp, loss = await _ingest([SDK_EVENT])
    assert resp.status_code == 200, resp.text

    (ev,), assets = await _rows(seeded_db)
    assert ev.requested_model == "claude-sonnet-4-5"
    assert ev.served_model == "claude-sonnet-4-5-20250929"
    assert (ev.tokens_cached, ev.tokens_cache_write, ev.tokens_cache_write_1h) == (
        800,
        300,
        100,
    )
    assert ev.tokens_reasoning == 150
    assert ev.service_tier == "standard"
    assert ev.cost_source == "pricing_table"
    assert ev.framework == "langchain"
    assert ev.modality is None and ev.usage_units is None
    assert assets[ev.asset_id].modality is None

    # Only fields still out of scope are reported as ignored.
    (stats,) = loss["clients"]
    assert set(stats["ignored_fields"]) == {
        "api_key_fingerprint",
        "stream",
        "stream_completed",
    }


@pytest.mark.asyncio
async def test_gateway_event_round_trip(seeded_db):
    resp, loss = await _ingest([GATEWAY_EVENT])
    assert resp.status_code == 200, resp.text

    (ev,), assets = await _rows(seeded_db)
    assert ev.requested_model == "auto"
    assert ev.served_model == "gpt-4o-mini-transcribe"
    assert ev.modality == "audio"
    assert ev.usage_units == Decimal("12.5")
    assert ev.usage_unit_type == "seconds"
    assert ev.tokens_in is None and ev.tokens_cached is None
    assert assets[ev.asset_id].modality == "audio"

    (stats,) = loss["clients"]
    assert set(stats["ignored_fields"]) == {
        "api_surface",
        "route_group",
        "route_reason",
        "route_est_cost",
    }

    async with _client() as c:
        body = (await c.get("/api/v1/registry/assets", headers=HEADERS)).json()
    (asset,) = body["data"]
    assert asset["modality"] == "audio"


@pytest.mark.asyncio
async def test_old_event_without_extended_fields_still_ingests(seeded_db):
    old = {"vendor": "openai", "model": "gpt-4o", "source": "sdk", "tokens_in": 5}
    resp, _ = await _ingest([old])
    assert resp.status_code == 200, resp.text

    (ev,), assets = await _rows(seeded_db)
    assert ev.tokens_in == 5
    assert all(getattr(ev, f) is None for f in EXTENDED_FIELDS)
    assert assets[ev.asset_id].modality is None


@pytest.mark.asyncio
async def test_malformed_extended_fields_are_dropped_not_rejected(seeded_db):
    bad = {
        "vendor": "openai",
        "model": "gpt-4o",
        "requested_model": "x" * 257,
        "served_model": 42,
        "tokens_cached": -1,
        "tokens_cache_write": "12",
        "tokens_cache_write_1h": 2**31,
        "tokens_reasoning": True,
        "cost_source": {"a": 1},
        "service_tier": "  ",
        "framework": "f" * 65,
        "modality": ["audio"],
        "usage_units": -5,  # non-finite values: see the unit test below
        "usage_unit_type": None,
    }
    good = {
        "vendor": "openai",
        "model": "gpt-4o",
        "tokens_reasoning": 7.0,
        "modality": " Image ",
        "usage_units": 3,
    }
    resp, loss = await _ingest([bad, good])
    assert resp.status_code == 200, resp.text

    # Dropped values are reported, not lost silently.
    (stats,) = loss["clients"]
    invalid = {
        k: v for k, v in stats["ignored_fields"].items() if k.endswith(":invalid")
    }
    assert invalid == {
        f"{f}:invalid": 1 for f in EXTENDED_FIELDS if f != "usage_unit_type"
    }

    events, assets = await _rows(seeded_db)
    by_reasoning = {e.tokens_reasoning: e for e in events}
    dropped = by_reasoning[None]
    assert all(getattr(dropped, f) is None for f in EXTENDED_FIELDS)
    kept = by_reasoning[7]
    assert kept.modality == "image"
    assert kept.usage_units == Decimal(3)
    # Same asset (same vendor/model): the first reported modality is kept.
    assert {a.modality for a in assets.values()} == {"image"}


def test_lenient_usage_units_rejects_non_finite():
    from collector.ingest import IngestEvent

    for value in (float("inf"), float("nan"), -1, 1e13, 10**400, "3", True):
        assert IngestEvent(vendor="v", usage_units=value).usage_units is None
    assert IngestEvent(vendor="v", usage_units=0).usage_units == 0


def test_modality_allowed_set():
    from collector.ingest import MODALITIES, IngestEvent

    # What the gateway's modalityOf() emits for each endpoint.
    assert MODALITIES == {"text", "image", "audio", "document"}
    for value in ("text", "IMAGE", " Audio ", "document"):
        assert IngestEvent(vendor="v", modality=value).modality == value.strip().lower()
    for value in ("video", "ocr", "embedding", "speech", "", None, 3):
        assert IngestEvent(vendor="v", modality=value).modality is None


@pytest.mark.asyncio
async def test_unknown_modality_is_stored_null_and_counted(seeded_db):
    event = {"vendor": "openai", "model": "gpt-4o", "source": "gateway"}
    resp, loss = await _ingest(
        [{**event, "modality": "hologram"}, {**event, "modality": "text"}]
    )
    assert resp.status_code == 200, resp.text

    (stats,) = loss["clients"]
    assert stats["ignored_fields"].get("modality:invalid") == 1
    assert "modality" not in stats["ignored_fields"]

    events, assets = await _rows(seeded_db)
    assert sorted(str(e.modality) for e in events) == ["None", "text"]
    # The unknown value never reached the asset; the valid one did.
    assert [a.modality for a in assets.values()] == ["text"]


@pytest.mark.asyncio
async def test_existing_asset_gains_modality_once(seeded_db):
    first = {"vendor": "deepgram", "model": "nova-3", "source": "gateway"}
    resp, _ = await _ingest([first])
    assert resp.status_code == 200
    _, assets = await _rows(seeded_db)
    assert [a.modality for a in assets.values()] == [None]

    resp, _ = await _ingest(
        [{**first, "modality": "audio"}, {**first, "modality": "text"}]
    )
    assert resp.status_code == 200
    _, assets = await _rows(seeded_db)
    assert [a.modality for a in assets.values()] == ["audio"]


# ──────────────────────────────────────────────────────────────────────
# Migration 017
# ──────────────────────────────────────────────────────────────────────


def _load_migration():
    pytest.importorskip("alembic")
    path = (
        Path(__file__).resolve().parents[2]
        / "db"
        / "alembic"
        / "versions"
        / "017_extended_usage_fields.py"
    )
    spec = importlib.util.spec_from_file_location("migration_017", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_revision_chain():
    m = _load_migration()
    assert (m.revision, m.down_revision) == ("017", "016")
    # The migration adds exactly the columns the model declares.
    assert {name for name, _ in m.USAGE_EVENT_COLUMNS} == set(EXTENDED_FIELDS)


def _columns(sync_conn, table):
    return {c["name"]: c for c in inspect(sync_conn).get_columns(table)}


@pytest.mark.asyncio
async def test_migration_downgrade_and_upgrade(seeded_db):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    m = _load_migration()

    def run(sync_conn, step):
        ctx = MigrationContext.configure(sync_conn)
        with Operations.context(ctx):
            step()

    # seeded_db is at head (create_all), so 017's columns exist.
    async with seeded_db.begin() as conn:
        await conn.run_sync(run, m.downgrade)
        events = await conn.run_sync(_columns, "ai_usage_events")
        assets = await conn.run_sync(_columns, "ai_assets")
    assert not set(EXTENDED_FIELDS) & set(events)
    assert "modality" not in assets
    assert {"tokens_in", "flow_context"} <= set(events)

    async with seeded_db.begin() as conn:
        await conn.run_sync(run, m.upgrade)
        events = await conn.run_sync(_columns, "ai_usage_events")
        assets = await conn.run_sync(_columns, "ai_assets")
    assert set(EXTENDED_FIELDS) <= set(events)
    assert all(events[f]["nullable"] for f in EXTENDED_FIELDS)
    assert assets["modality"]["nullable"]

    # The upgraded schema takes both event shapes.
    resp, _ = await _ingest([SDK_EVENT, GATEWAY_EVENT])
    assert resp.status_code == 200, resp.text


def test_otel_model_names_are_bounded():
    from collector.otel_ingest import _bounded_model

    assert _bounded_model("gpt-4o") == "gpt-4o"
    assert _bounded_model("m" * 256) == "m" * 256
    assert _bounded_model("m" * 257) is None
    assert _bounded_model(None) is None
