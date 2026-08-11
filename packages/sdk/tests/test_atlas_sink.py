"""Tests for the optional atlas.ai telemetry sink.

Wire contract: POST {atlas}/api/v1/telemetry/prompt-events, X-API-Key header,
{"events": [...]} envelope (1-500), 200 + {ingested, skipped, skipped_reasons}.
Server-skipped rows are final; only network/HTTP failures re-buffer.
Spec: atlas.ai docs/superpowers/specs/2026-06-11-prompt-telemetry-design.md.

HTTP is mocked by swapping the sender's private httpx clients for ones built
on httpx.MockTransport (respx is not installed; existing tests never mock
httpx because they never hit the network).
"""
import hashlib
import json
import re

import httpx
import pytest

from prompt_shields.telemetry import (
    ATLAS_EVENTS_PATH,
    ATLAS_MAX_BATCH,
    AtlasTelemetrySender,
    hash_messages,
    build_atlas_event,
)


# --- helpers ----------------------------------------------------------------


def _ok_body(ingested=1, skipped=0, reasons=None):
    return {"ingested": ingested, "skipped": skipped,
            "skipped_reasons": reasons or []}


def _capture_transport(captured: list, status_code: int = 200, body: dict | None = None):
    """MockTransport that records every request and returns a canned response."""
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status_code, json=body if body is not None else _ok_body())
    return httpx.MockTransport(handler)


def _error_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)
    return httpx.MockTransport(handler)


def _atlas_sender(transport: httpx.MockTransport) -> AtlasTelemetrySender:
    sender = AtlasTelemetrySender("http://atlas.test/", "aigrc_test_key")
    sender._sync_client = httpx.Client(transport=transport)
    sender._async_client = httpx.AsyncClient(transport=transport)
    return sender


# --- transport: envelope, header, endpoint ----------------------------------


def test_atlas_sender_posts_envelope_with_x_api_key_header():
    captured: list[httpx.Request] = []
    sender = _atlas_sender(_capture_transport(captured))
    sender.enqueue({"source": "sdk", "event_kind": "activity"})
    sender.flush_sync()

    assert len(captured) == 1
    req = captured[0]
    assert req.url.path == ATLAS_EVENTS_PATH == "/api/v1/telemetry/prompt-events"
    assert req.headers["x-api-key"] == "aigrc_test_key"
    assert "authorization" not in req.headers  # X-API-Key, not Bearer
    body = json.loads(req.content)
    assert body == {"events": [{"source": "sdk", "event_kind": "activity"}]}
    assert len(sender._buffer) == 0  # 200 => delivered, nothing re-buffered


@pytest.mark.asyncio
async def test_atlas_sender_async_flush_same_envelope():
    captured: list[httpx.Request] = []
    sender = _atlas_sender(_capture_transport(captured))
    sender.enqueue({"source": "sdk", "event_kind": "activity"})
    await sender.flush()

    assert len(captured) == 1
    assert captured[0].headers["x-api-key"] == "aigrc_test_key"
    assert json.loads(captured[0].content)["events"][0]["source"] == "sdk"
    assert len(sender._buffer) == 0


# --- fail-open / retry semantics ---------------------------------------------


def test_atlas_sender_fail_open_on_network_error_rebuffers():
    sender = _atlas_sender(_error_transport())
    sender.enqueue({"source": "sdk", "event_kind": "activity"})
    sender.flush_sync()  # must not raise
    assert len(sender._buffer) == 1  # network failure => kept for retry


def test_atlas_sender_rebuffers_on_http_error():
    captured: list[httpx.Request] = []
    sender = _atlas_sender(_capture_transport(captured, status_code=503, body={}))
    sender.enqueue({"source": "sdk", "event_kind": "activity"})
    sender.flush_sync()
    assert len(sender._buffer) == 1


def test_atlas_sender_does_not_rebuffer_server_skipped_rows():
    # 200 with skipped rows is FINAL: those rows failed validation and can
    # never succeed on retry. Re-buffering them would loop forever.
    captured: list[httpx.Request] = []
    sender = _atlas_sender(_capture_transport(
        captured, body=_ok_body(ingested=0, skipped=1,
                                reasons=["events[0]: prompt_hash: bad"])))
    sender.enqueue({"source": "sdk", "event_kind": "activity", "prompt_hash": "bad"})
    sender.flush_sync()
    assert len(sender._buffer) == 0


# --- chunking ----------------------------------------------------------------


def test_atlas_sender_chunks_batches_at_500():
    # The server envelope rejects >500 events; the collector sender sends the
    # whole buffer in one POST, so the atlas sender must chunk.
    captured: list[httpx.Request] = []
    sender = _atlas_sender(_capture_transport(captured))
    for i in range(ATLAS_MAX_BATCH + 1):
        sender.enqueue({"source": "sdk", "event_kind": "activity", "tokens_in": i})
    sender.flush_sync()

    assert len(captured) == 2
    sizes = [len(json.loads(r.content)["events"]) for r in captured]
    assert sizes == [500, 1]
    assert len(sender._buffer) == 0


# --- build_atlas_event: shape and field mapping --------------------------------


def test_build_atlas_event_shape_constants():
    event = build_atlas_event({"vendor": "openai", "model": "gpt-4o"},
                              [{"role": "user", "content": "hi"}])
    assert event["source"] == "sdk"
    assert event["event_kind"] == "activity"
    assert event["action"] == "allowed"
    assert event["occurrences"] == 1
    # occurred_at is ISO-8601 (client-side timestamp survives buffering delay)
    from datetime import datetime
    datetime.fromisoformat(event["occurred_at"])


def test_build_atlas_event_field_mapping():
    collector_event = {
        "vendor": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "tokens_in": 50,
        "tokens_out": 100,
        "cost": 0.00165,
        "session_id": "session-xyz",
        "user_id": "auth0|abc123",
    }
    event = build_atlas_event(collector_event, [{"role": "user", "content": "hi"}])
    assert event["vendor"] == "anthropic"
    assert event["model"] == "claude-sonnet-4-20250514"
    assert event["tokens_in"] == 50
    assert event["tokens_out"] == 100
    assert event["estimated_cost_usd"] == 0.00165   # cost -> estimated_cost_usd
    assert event["session_id"] == "session-xyz"
    assert event["user_external_id"] == "auth0|abc123"  # user_id -> user_external_id
    assert "cost" not in event and "user_id" not in event


def test_build_atlas_event_omits_none_and_absent_fields():
    # The atlas schema is extra="forbid" but nullable — we omit rather than
    # send nulls so payloads stay minimal and unambiguous.
    event = build_atlas_event(
        {"vendor": "openai", "model": "gpt-99-future", "cost": None,
         "tokens_in": None, "tokens_out": None},
        [],
    )
    for absent in ("estimated_cost_usd", "tokens_in", "tokens_out",
                   "session_id", "user_external_id", "pii_categories",
                   "prompt_hash"):
        assert absent not in event


def test_build_atlas_event_pii_list_maps_to_count_one():
    # detect_pii_categories returns names only; each maps to count 1 —
    # we do not invent per-category match counts.
    event = build_atlas_event(
        {"vendor": "openai", "model": "gpt-4o",
         "detected_pii_types": ["email", "ssn"]},
        [{"role": "user", "content": "hi"}],
    )
    assert event["pii_categories"] == {"email": 1, "ssn": 1}


def test_build_atlas_event_never_copies_prompt_text_or_unknown_fields():
    # Collector events legitimately carry fields the atlas schema forbids
    # (extra="forbid" would skip the row) — and prompt_text, which must never
    # leave the host toward atlas. Whitelist-only copy guarantees both.
    secret = "the launch codes are 0000"
    collector_event = {
        "vendor": "openai", "model": "gpt-4o",
        "prompt_text": secret,
        "latency_ms": 100, "tool_calls_used": 2,
        "api_key_fingerprint": "abcd1234abcd1234",
        "business_unit": "HR", "use_case_name": "screening",
        "owner_email": "jane@test.com",
    }
    event = build_atlas_event(collector_event, [{"role": "user", "content": secret}])
    allowed = {"source", "event_kind", "action", "occurrences", "occurred_at",
               "vendor", "model", "tokens_in", "tokens_out",
               "estimated_cost_usd", "session_id", "user_external_id",
               "pii_categories", "prompt_hash"}
    assert set(event) <= allowed
    assert secret not in json.dumps(event)


# --- hash_messages -------------------------------------------------------------


def test_hash_messages_deterministic_lowercase_hex():
    messages = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello world"},
    ]
    h1 = hash_messages(messages)
    h2 = hash_messages(messages)
    assert h1 == h2
    assert re.fullmatch(r"[0-9a-f]{64}", h1)  # exactly 64 LOWERCASE hex chars
    assert h1 == hashlib.sha256("be briefhello world".encode("utf-8")).hexdigest()
    # different text -> different hash
    assert hash_messages([{"role": "user", "content": "hello worlds"}]) != h1


def test_hash_messages_none_when_no_text():
    # No text content => return None => caller omits prompt_hash entirely.
    assert hash_messages([]) is None
    assert hash_messages(["not-a-dict"]) is None
    assert hash_messages([{"role": "user", "content": ""}]) is None
    # Non-string content (e.g. OpenAI content-parts lists) is skipped
    assert hash_messages([{"role": "user", "content": [{"type": "text"}]}]) is None


# --- sync client wiring ----------------------------------------------------


from unittest.mock import MagicMock

from prompt_shields import ShieldsClient


def _mock_openai_response(prompt_tokens=10, completion_tokens=20):
    resp = MagicMock()
    resp.usage.prompt_tokens = prompt_tokens
    resp.usage.completion_tokens = completion_tokens
    resp.choices = []
    return resp


def _clear_atlas_env(monkeypatch):
    monkeypatch.delenv("PS_ATLAS_URL", raising=False)
    monkeypatch.delenv("PS_ATLAS_API_KEY", raising=False)


def test_atlas_sink_inactive_by_default(monkeypatch):
    _clear_atlas_env(monkeypatch)
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test")
    assert client._atlas is None


def test_atlas_sink_inactive_when_partially_configured(monkeypatch):
    _clear_atlas_env(monkeypatch)
    url_only = ShieldsClient(api_key="sk-test", ps_api_key="ps-test",
                             atlas_url="http://atlas.test")
    key_only = ShieldsClient(api_key="sk-test", ps_api_key="ps-test",
                             atlas_api_key="aigrc_k")
    assert url_only._atlas is None
    assert key_only._atlas is None


def test_atlas_sink_active_via_kwargs(monkeypatch):
    _clear_atlas_env(monkeypatch)
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test",
                           atlas_url="http://atlas.test",
                           atlas_api_key="aigrc_k")
    assert isinstance(client._atlas, AtlasTelemetrySender)
    assert client._atlas._url == "http://atlas.test/api/v1/telemetry/prompt-events"


def test_atlas_sink_active_via_env_vars(monkeypatch):
    monkeypatch.setenv("PS_ATLAS_URL", "http://atlas.env")
    monkeypatch.setenv("PS_ATLAS_API_KEY", "aigrc_env")
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test")
    assert isinstance(client._atlas, AtlasTelemetrySender)
    assert client._atlas._headers == {"X-API-Key": "aigrc_env"}


def test_sync_create_sends_atlas_event_with_no_prompt_text(monkeypatch):
    """THE privacy test: send_prompt_text=True must not leak text to atlas.

    Asserts on the SERIALIZED request body — not the event dict — so any
    future field that smuggles text in would fail here too.
    """
    _clear_atlas_env(monkeypatch)
    secret = "contact jane@acme.com about patient id 9"
    client = ShieldsClient(
        api_key="sk-test", ps_api_key="ps-test",
        send_prompt_text=True,  # collector opt-in must NOT affect atlas
        atlas_url="http://atlas.test", atlas_api_key="aigrc_test_key",
    )
    collector_captured: list[httpx.Request] = []
    atlas_captured: list[httpx.Request] = []
    client._telemetry._sync_client = httpx.Client(
        transport=_capture_transport(collector_captured))
    client._atlas._sync_client = httpx.Client(
        transport=_capture_transport(atlas_captured))
    monkeypatch.setattr(client.chat.completions, "_call_upstream",
                        lambda **kwargs: _mock_openai_response())

    client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": secret}],
        ps_metadata={"session_id": "s-1", "user_id": "u-1"},
    )

    # Atlas got exactly one event with the full mapped shape...
    assert len(atlas_captured) == 1
    raw = atlas_captured[0].content.decode()
    event = json.loads(raw)["events"][0]
    assert atlas_captured[0].headers["x-api-key"] == "aigrc_test_key"
    assert event["source"] == "sdk"
    assert event["event_kind"] == "activity"
    assert event["action"] == "allowed"
    assert event["vendor"] == "openai"
    assert event["model"] == "gpt-4o"
    assert event["tokens_in"] == 10
    assert event["tokens_out"] == 20
    assert event["occurrences"] == 1
    assert event["session_id"] == "s-1"
    assert event["user_external_id"] == "u-1"
    assert "estimated_cost_usd" in event
    assert event["prompt_hash"] == hashlib.sha256(secret.encode()).hexdigest()
    assert event["pii_categories"] == {"email": 1, "health_data": 1}

    # ...and NO prompt text anywhere in the serialized atlas payload.
    assert secret not in raw
    assert "jane@acme.com" not in raw
    assert "prompt_text" not in raw

    # Collector behavior unchanged: opt-in text still flows to the collector.
    assert len(collector_captured) == 1
    collector_raw = collector_captured[0].content.decode()
    assert "prompt_text" in collector_raw
    assert secret in collector_raw


# --- async client wiring -----------------------------------------------------


from prompt_shields import AsyncShieldsClient


def test_async_client_atlas_config_mirrors_sync(monkeypatch):
    _clear_atlas_env(monkeypatch)
    assert AsyncShieldsClient(api_key="sk-test", ps_api_key="ps-test")._atlas is None
    configured = AsyncShieldsClient(
        api_key="sk-test", ps_api_key="ps-test",
        atlas_url="http://atlas.test", atlas_api_key="aigrc_k",
    )
    assert isinstance(configured._atlas, AtlasTelemetrySender)


@pytest.mark.asyncio
async def test_async_create_sends_atlas_event_with_no_prompt_text(monkeypatch):
    _clear_atlas_env(monkeypatch)
    secret = "contact jane@acme.com about patient id 9"
    client = AsyncShieldsClient(
        api_key="sk-test", ps_api_key="ps-test",
        send_prompt_text=True,
        atlas_url="http://atlas.test", atlas_api_key="aigrc_test_key",
    )
    atlas_captured: list[httpx.Request] = []
    client._telemetry._async_client = httpx.AsyncClient(
        transport=_capture_transport([]))
    client._atlas._async_client = httpx.AsyncClient(
        transport=_capture_transport(atlas_captured))

    async def fake_upstream(**kwargs):
        return _mock_openai_response()

    monkeypatch.setattr(client.chat.completions, "_call_upstream", fake_upstream)

    await client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": secret}],
    )

    assert len(atlas_captured) == 1
    raw = atlas_captured[0].content.decode()
    event = json.loads(raw)["events"][0]
    assert event["source"] == "sdk"
    assert event["event_kind"] == "activity"
    assert event["prompt_hash"] == hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in raw
    assert "prompt_text" not in raw

    # aclose() must close the atlas sender too (its close() flushes first).
    await client.aclose()
    assert client._atlas._async_client.is_closed
