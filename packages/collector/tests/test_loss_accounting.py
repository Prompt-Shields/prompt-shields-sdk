"""Telemetry loss accounting (#87): batch metadata on /ingest/events and the
GET /telemetry/loss read endpoint. DB tests use PS_TEST_DATABASE_URL."""

import pytest
from collector import ingest, loss_accounting
from collector.app import app
from collector.loss_accounting import (
    UNIDENTIFIED_CLIENT,
    ignored_fields,
    parse_metadata,
)
from collector.partner_rate_limit import FixedWindowRateLimiter, get_ingest_rate_limiter
from httpx import ASGITransport, AsyncClient

HEADERS = {"Authorization": "Bearer ps-test-key-12345"}


def _event(**extra):
    return {"vendor": "openai", "model": "gpt-4o", "source": "sdk", **extra}


def _meta(seq, enqueued, dropped=None, client_id="client-a"):
    return {
        "client_id": client_id,
        "seq": seq,
        "enqueued_total": enqueued,
        "dropped": dropped or {},
        "sdk": "python",
    }


def _client(**kwargs):
    return AsyncClient(
        transport=ASGITransport(app=app, **kwargs), base_url="http://test"
    )


async def _loss(client):
    resp = await client.get("/telemetry/loss", headers=HEADERS)
    assert resp.status_code == 200
    return resp.json()


# --- pure helpers -------------------------------------------------------------


def test_parse_metadata_is_lenient():
    assert parse_metadata(None).client_id == UNIDENTIFIED_CLIENT
    assert parse_metadata({"client_id": ""}).client_id == UNIDENTIFIED_CLIENT
    assert parse_metadata({"client_id": "x" * 500}).client_id == UNIDENTIFIED_CLIENT
    meta = parse_metadata(
        {
            "client_id": "c1",
            "seq": "7",
            "enqueued_total": -1,
            "dropped": {"send_failed": 3, "bad": -2, "flag": True, 5: 1},
        }
    )
    assert meta.client_id == "c1"
    assert meta.seq is None
    assert meta.enqueued_total is None
    assert meta.dropped == {"send_failed": 3}


def test_ignored_fields_counts_unknown_keys():
    known = {"vendor", "model"}
    events = [
        {"vendor": "a", "use_case": "x"},
        {"vendor": "b", "use_case": "y", "owner": 1},
        "junk",
    ]
    assert ignored_fields(events, known) == {"use_case": 2, "owner": 1}
    assert ignored_fields(None, known) == {}


# --- end to end ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_loss_endpoint_reconciles_sdk_metadata(seeded_db):
    async with _client() as client:
        r1 = await client.post(
            "/ingest/events",
            headers=HEADERS,
            json={"events": [_event(), _event()], "metadata": _meta(1, 2)},
        )
        # The SDK queued 10 in total, dropped 3 on overflow while sends were
        # failing, delivers 4 now: 1 event is unaccounted for.
        r2 = await client.post(
            "/ingest/events",
            headers=HEADERS,
            json={
                "events": [_event()] * 4,
                "metadata": _meta(2, 10, {"send_failed": 3}),
            },
        )
        loss = await _loss(client)

    assert r1.status_code == r2.status_code == 200
    (c,) = loss["clients"]
    assert c["client_id"] == "client-a"
    assert c["sdk"] == "python"
    assert c["received_events"] == 6
    assert c["received_batches"] == 2
    assert c["last_seq"] == 2
    assert c["sdk_enqueued_total"] == 10
    assert c["sdk_dropped"] == {"send_failed": 3}
    assert c["unaccounted"] == 1
    assert c["coverage"] == 0.6
    assert loss["totals"]["received_events"] == 6
    assert loss["totals"]["sdk_dropped_total"] == 3
    assert loss["totals"]["unaccounted"] == 1
    assert loss["totals"]["coverage"] == 0.6


@pytest.mark.asyncio
async def test_stale_batch_does_not_roll_back_sdk_figures(seeded_db):
    async with _client() as client:
        await client.post(
            "/ingest/events",
            headers=HEADERS,
            json={"events": [_event()], "metadata": _meta(5, 9, {"shutdown": 2})},
        )
        await client.post(
            "/ingest/events",
            headers=HEADERS,
            json={"events": [_event()], "metadata": _meta(3, 4)},
        )
        (c,) = (await _loss(client))["clients"]

    assert c["received_events"] == 2
    assert c["last_seq"] == 5
    assert c["sdk_enqueued_total"] == 9
    assert c["sdk_dropped"] == {"shutdown": 2}


@pytest.mark.asyncio
async def test_batches_without_metadata_and_ignored_fields(seeded_db):
    async with _client() as client:
        resp = await client.post(
            "/ingest/events",
            headers=HEADERS,
            json={
                "events": [_event(use_case="hr", owner="a@b.c"), _event(use_case="hr")]
            },
        )
        loss = await _loss(client)

    assert resp.status_code == 200
    (c,) = loss["clients"]
    assert c["client_id"] == UNIDENTIFIED_CLIENT
    assert c["received_events"] == 2
    assert c["unaccounted"] is None
    assert c["sdk_enqueued_total"] is None
    assert c["ignored_fields"] == {"use_case": 2, "owner": 1}
    assert c["coverage"] == 1.0


@pytest.mark.asyncio
async def test_rate_limited_batch_counted_as_rejected(seeded_db, monkeypatch):
    monkeypatch.setattr(ingest.settings, "rate_limit_per_minute", 1)
    fresh = FixedWindowRateLimiter()
    app.dependency_overrides[get_ingest_rate_limiter] = lambda: fresh
    try:
        async with _client() as client:
            ok = await client.post(
                "/ingest/events",
                headers=HEADERS,
                json={"events": [_event()], "metadata": _meta(1, 1)},
            )
            blocked = await client.post(
                "/ingest/events",
                headers=HEADERS,
                json={"events": [_event()] * 3, "metadata": _meta(2, 4)},
            )
            (c,) = (await _loss(client))["clients"]
    finally:
        app.dependency_overrides.pop(get_ingest_rate_limiter, None)

    assert ok.status_code == 200
    assert blocked.status_code == 429
    assert c["received_events"] == 1
    assert c["collector_rejected"] == {"rate_limited": 3}


@pytest.mark.asyncio
async def test_write_failure_counted_and_rolled_back(seeded_db, monkeypatch):
    async def boom(db, tenant_id, events):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ingest, "_store_events", boom)
    async with _client(raise_app_exceptions=False) as client:
        resp = await client.post(
            "/ingest/events",
            headers=HEADERS,
            json={"events": [_event()] * 2, "metadata": _meta(1, 2)},
        )
        (c,) = (await _loss(client))["clients"]

    assert resp.status_code == 500
    assert c["received_events"] == 0
    assert c["collector_rejected"] == {"write_failed": 2}


@pytest.mark.asyncio
async def test_record_rejected_never_raises(monkeypatch):
    class BrokenSession:
        async def execute(self, *a, **k):
            raise RuntimeError("db down")

        async def rollback(self):
            raise RuntimeError("still down")

    await loss_accounting.record_rejected(
        BrokenSession(), None, parse_metadata(None), 1, "rate_limited"
    )


@pytest.mark.asyncio
async def test_loss_endpoint_requires_auth():
    async with _client() as client:
        resp = await client.get("/telemetry/loss")
    assert resp.status_code == 401
