"""Event-loss accounting in TelemetrySender (#87).

HTTP is mocked with httpx.MockTransport, as in test_atlas_sink.py.
"""

import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from prompt_shields import telemetry
from prompt_shields.telemetry import (
    MAX_BUFFER_SIZE,
    MAX_RETRY_AFTER_SECONDS,
    AtlasTelemetrySender,
    TelemetrySender,
    parse_retry_after,
)


def _sender(handler, cls=TelemetrySender, *args):
    sender = cls(*(args or ("http://collector.test", "ps-test")))
    transport = httpx.MockTransport(handler)
    sender._sync_client = httpx.Client(transport=transport)
    sender._async_client = httpx.AsyncClient(transport=transport)
    return sender


def _respond(status, headers=None, body=None, captured=None):
    def handler(request):
        if captured is not None:
            captured.append(request)
        return httpx.Response(status, headers=headers or {}, json=body or {})

    return handler


def test_batch_carries_loss_metadata_with_monotonic_seq():
    captured = []
    sender = _sender(_respond(200, captured=captured))
    sender.enqueue({"vendor": "openai"})
    sender.flush_sync()
    sender.enqueue({"vendor": "openai"})
    sender.enqueue({"vendor": "openai"})
    sender.flush_sync()

    metas = [json.loads(r.content)["metadata"] for r in captured]
    assert [m["seq"] for m in metas] == [1, 2]
    assert metas[0]["client_id"] == metas[1]["client_id"] == sender.stats()["client_id"]
    assert [m["enqueued_total"] for m in metas] == [1, 3]
    assert metas[1]["dropped"] == {}
    assert metas[0]["sdk"] == "python"
    stats = sender.stats()
    assert stats["sent"] == 3 and stats["buffered"] == 0 and stats["dropped_total"] == 0


def test_atlas_sender_never_sends_metadata():
    captured = []
    sender = _sender(
        _respond(200, captured=captured),
        AtlasTelemetrySender,
        "http://atlas.test",
        "aigrc_k",
    )
    sender.enqueue({"source": "sdk"})
    sender.flush_sync()
    assert set(json.loads(captured[0].content)) == {"events"}


def test_overflow_counted_as_buffer_overflow():
    sender = TelemetrySender("http://collector.test", "ps-test")
    for i in range(MAX_BUFFER_SIZE + 7):
        sender.enqueue({"i": i})
    stats = sender.stats()
    assert stats["dropped"]["buffer_overflow"] == 7
    assert stats["dropped_total"] == 7
    assert stats["enqueued"] == MAX_BUFFER_SIZE + 7


def test_overflow_while_sends_fail_counted_as_send_failed_and_reported():
    captured = []
    sender = _sender(_respond(503, captured=captured))
    for i in range(MAX_BUFFER_SIZE):
        sender.enqueue({"i": i})
    sender.flush_sync()  # fails, re-buffers
    sender.enqueue({"i": "new"})  # evicts the oldest
    assert sender.stats()["dropped"]["send_failed"] == 1

    sender.flush_sync()
    meta = json.loads(captured[-1].content)["metadata"]
    assert meta["dropped"] == {"send_failed": 1}
    assert meta["enqueued_total"] == MAX_BUFFER_SIZE + 1


def test_server_skipped_rows_counted():
    sender = _sender(
        _respond(200, body={"ingested": 1, "skipped": 2}),
        AtlasTelemetrySender,
        "http://atlas.test",
        "aigrc_k",
    )
    for _ in range(3):
        sender.enqueue({"source": "sdk"})
    sender.flush_sync()
    stats = sender.stats()
    assert stats["sent"] == 1
    assert stats["dropped"]["server_skipped"] == 2


def test_429_honours_retry_after_and_rebuffers(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(telemetry.time, "monotonic", lambda: now[0])
    calls = []
    status = [429]

    def handler(request):
        calls.append(request)
        return httpx.Response(status[0], headers={"Retry-After": "10"}, json={})

    sender = _sender(handler)
    sender.enqueue({"vendor": "openai"})
    sender.flush_sync()
    assert len(calls) == 1
    stats = sender.stats()
    assert stats["buffered"] == 1
    assert stats["rate_limited_responses"] == 1
    assert stats["retry_after_seconds"] == 10

    now[0] += 5
    sender.flush_sync()  # still backing off: no request
    assert len(calls) == 1

    now[0] += 6
    status[0] = 200
    sender.flush_sync()
    assert len(calls) == 2
    assert sender.stats()["buffered"] == 0
    assert sender.stats()["sent"] == 1


def test_overflow_during_rate_limit_backoff_counted_as_rate_limited(monkeypatch):
    monkeypatch.setattr(telemetry.time, "monotonic", lambda: 0.0)
    sender = _sender(_respond(429, headers={"Retry-After": "30"}))
    sender.enqueue({"i": 0})
    sender.flush_sync()
    for i in range(MAX_BUFFER_SIZE):
        sender.enqueue({"i": i + 1})
    assert sender.stats()["dropped"]["rate_limited"] == 1


@pytest.mark.asyncio
async def test_async_429_stops_remaining_chunks(monkeypatch):
    monkeypatch.setattr(telemetry.time, "monotonic", lambda: 0.0)
    captured = []
    sender = _sender(
        _respond(429, headers={"Retry-After": "5"}, captured=captured),
        AtlasTelemetrySender,
        "http://atlas.test",
        "aigrc_k",
    )
    for i in range(telemetry.ATLAS_MAX_BATCH + 1):
        sender.enqueue({"i": i})
    await sender.flush()
    assert len(captured) == 1  # second chunk not attempted
    assert sender.stats()["buffered"] == telemetry.ATLAS_MAX_BATCH + 1


@pytest.mark.asyncio
async def test_close_counts_unsent_as_shutdown():
    sender = _sender(_respond(500))
    sender.enqueue({"vendor": "openai"})
    sender.enqueue({"vendor": "openai"})
    await sender.close()
    stats = sender.stats()
    assert stats["dropped"]["shutdown"] == 2
    assert stats["buffered"] == 0


def test_close_sync_flushes_despite_backoff(monkeypatch):
    monkeypatch.setattr(telemetry.time, "monotonic", lambda: 0.0)
    status = [429]

    def handler(request):
        return httpx.Response(status[0], headers={"Retry-After": "60"}, json={})

    sender = _sender(handler)
    sender.enqueue({"vendor": "openai"})
    sender.flush_sync()
    status[0] = 200
    sender.close_sync()
    stats = sender.stats()
    assert stats["sent"] == 1
    assert stats["dropped"]["shutdown"] == 0


def test_parse_retry_after():
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None
    assert parse_retry_after("3") == 3.0
    assert parse_retry_after("-4") == 0.0
    assert parse_retry_after("999999") == MAX_RETRY_AFTER_SECONDS
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=20), usegmt=True)
    assert 15 <= parse_retry_after(future) <= 20


def test_client_telemetry_stats(monkeypatch):
    monkeypatch.delenv("PS_ATLAS_URL", raising=False)
    monkeypatch.delenv("PS_ATLAS_API_KEY", raising=False)
    from prompt_shields import ShieldsOpenAI

    client = ShieldsOpenAI(api_key="sk-test", ps_api_key="ps-test")
    stats = client.telemetry_stats()
    assert stats["atlas"] is None
    assert stats["collector"]["enqueued"] == 0
    assert set(stats["collector"]["dropped"]) == set(telemetry.DROP_REASONS)
    client.close()
