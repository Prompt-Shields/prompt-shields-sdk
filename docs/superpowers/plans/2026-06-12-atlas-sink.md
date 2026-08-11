# Atlas Telemetry Sink (SDK) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional second telemetry destination to the prompt-shields Python SDK: when `atlas_url` + `atlas_api_key` are configured (kwargs or `PS_ATLAS_URL` / `PS_ATLAS_API_KEY` env vars), every LLM call also emits one hash-and-metadata-only `activity` event to atlas.ai's `POST /api/v1/telemetry/prompt-events`. The existing collector integration is completely unchanged. Prompt text is structurally incapable of reaching atlas, even when the collector-side `send_prompt_text=True` toggle is on. (Linear: **PRO-17**; spec: `atlas.ai/docs/superpowers/specs/2026-06-11-prompt-telemetry-design.md`, "SDK" client section.)

**Architecture:** `TelemetrySender` (in `packages/sdk/prompt_shields/telemetry.py`) gains three keyword-only constructor params (`path`, `headers`, `max_batch`) whose defaults reproduce today's behavior byte-for-byte; a new `AtlasTelemetrySender` subclass passes the atlas path, `X-API-Key` header, and a 500-event chunk limit, inheriting the buffer/flush/fail-open machinery unchanged. A pure function `build_atlas_event(collector_event, messages)` whitelist-maps the already-built collector event onto the atlas wire schema (so PII scanning runs once, both sync and async clients share one mapping implementation, and `prompt_text` can never leak — it is simply not in the whitelist). `ShieldsClient` and `AsyncShieldsClient` construct the sink when configured and enqueue+flush to it right after the existing collector enqueue+flush in their respective `create()` paths.

**Tech Stack:** Python ≥3.11, httpx (only runtime dep), pytest + pytest-asyncio (`asyncio_mode = "auto"` from the root `pyproject.toml`). HTTP is mocked with `httpx.MockTransport` injected into the sender's `_sync_client` / `_async_client` (respx is declared in dev extras but is unused and not installed in the working env — do not introduce it).

---

## Ground truth

### Wire contract (verified against `atlas.ai/.worktrees/feat-prompt-telemetry/backend/app/schemas/telemetry.py` and `backend/app/routers/telemetry.py`)

- `POST {atlas_url}/api/v1/telemetry/prompt-events`, header `X-API-Key: <aigrc_ key>`, body `{"events": [...]}` with **1–500** items (`min_length=1, max_length=500` on the envelope).
- Per-event schema is `extra="forbid"` — **any unknown field rejects that row** (skip-with-reason, not a 422). Fields the SDK sends: `source: "sdk"`, `event_kind: "activity"`, `action: "allowed"`, `vendor` (≤50), `model` (≤120), `tokens_in`/`tokens_out` (int ≥0), `estimated_cost_usd` (float ≥0), `session_id` (≤120), `user_external_id` (≤255, from the per-request `user_id`), `pii_categories` (dict category→positive int; SDK maps each detected category to count `1`), `prompt_hash` (exactly 64 lowercase hex chars — server lowercases then validates `^[0-9a-f]{64}$`; omit entirely when there is no text), `occurrences: 1`, optional `occurred_at` (ISO-8601; naive timestamps treated as UTC; server defaults to now).
- **`prompt_text` must never be sent** — there is no such field, and `extra="forbid"` would skip the row. This is the privacy contract.
- Success is HTTP **200** with `{"ingested": n, "skipped": m, "skipped_reasons": [...]}`. **Server-skipped rows are final** (validation failures — retrying can never succeed). Only network errors / non-200 responses may re-buffer. The existing `TelemetrySender` re-buffer condition (`status_code != 200`) already implements exactly this; keep it.
- 401 for bad/inactive API key (whole request); tenant comes from the key.

### Verified in the SDK code (`/Users/junseki/Documents/GitHub/prompt-shields-sdk`)

- `packages/sdk/prompt_shields/telemetry.py` — `TelemetrySender(collector_url, api_key)` hardcodes `{url}/ingest/events` and a `Authorization: Bearer` header inline in **both** `flush()` (async) and `flush_sync()`; `deque(maxlen=1000)` buffer; each flush drains the **entire** buffer into **one** POST (no chunking — fine for the collector, exceeds the atlas 500 cap after re-buffered failures, hence `max_batch`); on non-200 or exception it logs a warning and re-enqueues the drained events (fail-open).
- `packages/sdk/prompt_shields/client.py` — sync path: `_CompletionsNamespace.create()` builds the collector event via `_build_event(...)`, then `enqueue(event)` + `flush_sync()` **after every call** (no background timer). The collector event in scope at that point contains `vendor`, `model`, `tokens_in`, `tokens_out`, `cost` (from `estimate_cost`, may be `None`), `latency_ms`, `tool_calls_used`, `api_key_fingerprint`, metadata fields (`business_unit`, `use_case_name`, `owner_email`, ...), `session_id`/`user_id` (only if passed in `ps_metadata`), `detected_pii_types` (list[str], only when `scan_pii=True` and non-empty), and `prompt_text` (only when `send_prompt_text=True`). The raw `messages` list is also in scope — needed for hashing.
- `packages/sdk/prompt_shields/async_client.py` — `AsyncShieldsClient` **duplicates `_build_event` verbatim** and its `_AsyncCompletionsNamespace.create()` does `enqueue(event)` + `await flush()`. It has an `aclose()` that closes the telemetry sender (the sync client has no close method — mirror that asymmetry, don't fix it here).
- `packages/sdk/prompt_shields/pii.py` — `detect_pii_categories(text) -> list[str]` (sorted, names only, no counts); `scan_messages(messages)` joins `m.get("content", "")` for dict messages with `" "` and delegates. Category names: `email`, `phone`, `ssn`, `credit_card`, `ip_address`, `iban`, `health_data`, `financial_data`.
- `packages/sdk/prompt_shields/types.py` — `PSConfig` TypedDict with `NotRequired` optional keys; `PSMetadata` has `session_id` and `user_id`.
- Tests (`packages/sdk/tests/`): plain pytest functions, `MagicMock` for provider responses (see `_mock_openai_response` in `test_client.py`), `@pytest.mark.asyncio` on async tests, no HTTP mocking today (existing tests never hit the network). Empty `conftest.py`.
- Collector schema vs atlas schema: the collector event carries many fields atlas's `extra="forbid"` schema rejects (`latency_ms`, `tool_calls_used`, `api_key_fingerprint`, `business_unit`, `cost`, `prompt_text`, ...). A naive "send the same dict to both" would have **100% of rows skipped**. The whitelist mapper is mandatory, not a nicety.

### Test command (verified)

```bash
cd /Users/junseki/Documents/GitHub/prompt-shields-sdk && python3 -m pytest packages/sdk/tests -q
```

Current output: `49 passed` (~1.3s). All commands below run from `/Users/junseki/Documents/GitHub/prompt-shields-sdk`.

---

## Task 1: `AtlasTelemetrySender` — transport, header, chunking, fail-open (PRO-17: reuse buffering/flush/fail-open; batch envelope + X-API-Key; fail-open + skipped-rows-final tests)

- [ ] Create the working branch:

```bash
git checkout main && git pull && git checkout -b feat/atlas-telemetry-sink
```

- [ ] Write failing tests — create `packages/sdk/tests/test_atlas_sink.py`:

```python
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
```

- [ ] Run them and confirm they fail for the right reason (import error):

```bash
python3 -m pytest packages/sdk/tests/test_atlas_sink.py -q
```

Expected: collection error — `ImportError: cannot import name 'ATLAS_EVENTS_PATH' from 'prompt_shields.telemetry'`.

- [ ] Implement — replace `packages/sdk/prompt_shields/telemetry.py` with:

```python
import asyncio
import logging
from collections import deque
import httpx

logger = logging.getLogger("prompt_shields.telemetry")

MAX_BUFFER_SIZE = 1000

# atlas.ai prompt-events ingestion (see atlas.ai
# docs/superpowers/specs/2026-06-11-prompt-telemetry-design.md)
ATLAS_EVENTS_PATH = "/api/v1/telemetry/prompt-events"
ATLAS_MAX_BATCH = 500  # server rejects envelopes with more than 500 events


class TelemetrySender:
    """Buffers telemetry events and sends to collector. Fail-open: never blocks LLM calls.

    The keyword-only params exist for subclasses (see AtlasTelemetrySender);
    their defaults reproduce the original collector behavior exactly.
    """

    def __init__(
        self,
        collector_url: str,
        api_key: str,
        *,
        path: str = "/ingest/events",
        headers: dict[str, str] | None = None,
        max_batch: int | None = None,
    ):
        self._url = f"{collector_url.rstrip('/')}{path}"
        self._api_key = api_key
        self._headers = (
            headers if headers is not None
            else {"Authorization": f"Bearer {api_key}"}
        )
        self._max_batch = max_batch
        self._buffer: deque[dict] = deque(maxlen=MAX_BUFFER_SIZE)
        self._sync_client = httpx.Client(timeout=5.0)
        self._async_client = httpx.AsyncClient(timeout=5.0)

    def enqueue(self, event: dict) -> None:
        """Add event to buffer. If buffer full, oldest event is dropped (fail-open)."""
        if len(self._buffer) >= MAX_BUFFER_SIZE:
            logger.warning("Telemetry buffer full, dropping oldest event")
        self._buffer.append(event)

    def _drain_chunks(self) -> list[list[dict]]:
        """Drain the buffer into request-sized chunks (one chunk when unlimited)."""
        events = list(self._buffer)
        self._buffer.clear()
        if self._max_batch is None:
            return [events]
        return [
            events[i:i + self._max_batch]
            for i in range(0, len(events), self._max_batch)
        ]

    async def flush(self) -> None:
        """Async flush: send all buffered events. Swallows errors (fail-open).

        A 200 response is final even if the server skipped rows — skipped rows
        failed validation and can never succeed on retry. Only network errors
        and non-200 responses re-buffer.
        """
        if not self._buffer:
            return
        for chunk in self._drain_chunks():
            try:
                resp = await self._async_client.post(
                    self._url,
                    json={"events": chunk},
                    headers=self._headers,
                )
                if resp.status_code != 200:
                    logger.warning(f"Telemetry send failed: {resp.status_code}")
                    for e in chunk:
                        self.enqueue(e)
            except Exception as e:
                logger.warning(f"Telemetry send error: {e}")
                for ev in chunk:
                    self.enqueue(ev)

    def flush_sync(self) -> None:
        """Synchronous flush using httpx.Client. Fail-open with 5s timeout."""
        if not self._buffer:
            return
        for chunk in self._drain_chunks():
            try:
                resp = self._sync_client.post(
                    self._url,
                    json={"events": chunk},
                    headers=self._headers,
                )
                if resp.status_code != 200:
                    logger.warning(f"Telemetry send failed: {resp.status_code}")
                    for e in chunk:
                        self.enqueue(e)
            except Exception as e:
                logger.warning(f"Telemetry send error: {e}")
                for ev in chunk:
                    self.enqueue(ev)

    async def close(self) -> None:
        await self.flush()
        await self._async_client.aclose()
        self._sync_client.close()


class AtlasTelemetrySender(TelemetrySender):
    """Second telemetry destination: atlas.ai prompt-events ingestion.

    Same buffer/flush/fail-open machinery as TelemetrySender, but:
      - POSTs to {atlas_url}/api/v1/telemetry/prompt-events
      - authenticates with X-API-Key (aigrc_ keys), not a Bearer token
      - chunks each flush at 500 events (server-side envelope limit)
    """

    def __init__(self, atlas_url: str, atlas_api_key: str):
        super().__init__(
            atlas_url,
            atlas_api_key,
            path=ATLAS_EVENTS_PATH,
            headers={"X-API-Key": atlas_api_key},
            max_batch=ATLAS_MAX_BATCH,
        )
```

- [ ] Run the full suite (new tests pass AND existing collector behavior is untouched — `test_telemetry.py`'s three tests must still pass):

```bash
python3 -m pytest packages/sdk/tests -q
```

Expected: `55 passed` (49 existing + 6 new), 0 failures.

- [ ] Commit:

```bash
git add packages/sdk/prompt_shields/telemetry.py packages/sdk/tests/test_atlas_sink.py
git commit -m "feat(sdk): AtlasTelemetrySender — X-API-Key transport with 500-event chunking (PRO-17)"
```

---

## Task 2: `build_atlas_event` + `hash_messages` — whitelist mapper and prompt hash (PRO-17: event shape; hash determinism/lowercase/omit-when-empty; pii list→counts; never prompt_text)

- [ ] Write failing tests — append to `packages/sdk/tests/test_atlas_sink.py` (add `hash_messages`, `build_atlas_event` to the existing `from prompt_shields.telemetry import (...)` line):

```python
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
```

- [ ] Run and confirm the new tests fail with `ImportError` (cannot import `hash_messages`):

```bash
python3 -m pytest packages/sdk/tests/test_atlas_sink.py -q
```

- [ ] Implement — add to `packages/sdk/prompt_shields/telemetry.py`, after the constants and before `class TelemetrySender` (and add `import hashlib` and `from datetime import datetime, timezone` to the imports at the top):

```python
# Whitelist mapping: collector-event key -> atlas wire key. The atlas schema
# is extra="forbid", so anything NOT in this map (prompt_text, latency_ms,
# api_key_fingerprint, business_unit, ...) must never be copied across —
# the server would skip the row, and prompt_text must never leave the host.
_ATLAS_FIELD_MAP = {
    "vendor": "vendor",
    "model": "model",
    "tokens_in": "tokens_in",
    "tokens_out": "tokens_out",
    "cost": "estimated_cost_usd",
    "session_id": "session_id",
    "user_id": "user_external_id",
}


def hash_messages(messages: list) -> str | None:
    """SHA-256 hex digest (64 lowercase chars) of concatenated message text.

    Returns None when there is no text content; callers omit prompt_hash.
    Non-dict messages and non-string contents are skipped, matching how
    pii.scan_messages and the prompt_text join treat them.
    """
    combined = "".join(
        m["content"]
        for m in messages
        if isinstance(m, dict) and isinstance(m.get("content"), str)
    )
    if not combined:
        return None
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def build_atlas_event(collector_event: dict, messages: list) -> dict:
    """Map an already-built collector event onto the atlas prompt-event schema.

    PRIVACY CONTRACT: whitelist-only copy. prompt_text (present on the
    collector event when send_prompt_text=True) is structurally incapable of
    reaching the atlas payload because it is not in _ATLAS_FIELD_MAP.
    """
    event: dict = {
        "source": "sdk",
        "event_kind": "activity",
        "action": "allowed",
        "occurrences": 1,
        # Stamp at build time: events may sit in the buffer across retries,
        # so send time is not event time.
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    for src_key, wire_key in _ATLAS_FIELD_MAP.items():
        value = collector_event.get(src_key)
        if value is not None:
            event[wire_key] = value

    # detect_pii_categories returns category names only — each detected
    # category maps to count 1; we do not invent per-category match counts.
    categories = collector_event.get("detected_pii_types")
    if categories:
        event["pii_categories"] = {category: 1 for category in categories}

    prompt_hash = hash_messages(messages)
    if prompt_hash is not None:
        event["prompt_hash"] = prompt_hash
    return event
```

- [ ] Run the full suite:

```bash
python3 -m pytest packages/sdk/tests -q
```

Expected: `62 passed` (55 + 7 new), 0 failures.

- [ ] Commit:

```bash
git add packages/sdk/prompt_shields/telemetry.py packages/sdk/tests/test_atlas_sink.py
git commit -m "feat(sdk): build_atlas_event whitelist mapper + prompt hash (PRO-17)"
```

---

## Task 3: Sync client wiring — config, env vars, sink invocation, privacy test (PRO-17: `atlas_url`/`atlas_api_key` config; sink inactive unless both set; collector unchanged; load-bearing privacy test)

- [ ] Write failing tests — append to `packages/sdk/tests/test_atlas_sink.py`:

```python
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
```

- [ ] Run and confirm the right failures (`_atlas` attribute missing / unexpected `atlas_url` kwarg):

```bash
python3 -m pytest packages/sdk/tests/test_atlas_sink.py -q
```

- [ ] Implement — edit `packages/sdk/prompt_shields/client.py`:

1. Add `import os` next to `import hashlib`, and change the telemetry import to:

```python
from prompt_shields.telemetry import (
    AtlasTelemetrySender,
    TelemetrySender,
    build_atlas_event,
)
```

2. In `ShieldsClient.__init__`, add two params right after `ps_collector_url: str = "http://localhost:8000",`:

```python
        # Optional atlas.ai sink (second destination; collector unchanged).
        # Env fallback: PS_ATLAS_URL / PS_ATLAS_API_KEY. Inactive unless BOTH set.
        atlas_url: str | None = None,
        atlas_api_key: str | None = None,
```

3. In the body, right after `self._telemetry = TelemetrySender(ps_collector_url, ps_api_key)`:

```python
        atlas_url = atlas_url if atlas_url is not None else os.environ.get("PS_ATLAS_URL")
        atlas_api_key = (
            atlas_api_key if atlas_api_key is not None
            else os.environ.get("PS_ATLAS_API_KEY")
        )
        self._atlas: AtlasTelemetrySender | None = (
            AtlasTelemetrySender(atlas_url, atlas_api_key)
            if atlas_url and atlas_api_key
            else None
        )
```

4. In `_CompletionsNamespace.create`, after the existing two telemetry lines:

```python
        self._parent._telemetry.enqueue(event)
        self._parent._telemetry.flush_sync()

        if self._parent._atlas is not None:
            self._parent._atlas.enqueue(build_atlas_event(event, messages))
            self._parent._atlas.flush_sync()
```

5. In `packages/sdk/prompt_shields/types.py`, add to `PSConfig` (after `ps_collector_url: str`):

```python
    atlas_url: NotRequired[str]
    atlas_api_key: NotRequired[str]
```

- [ ] Run the full suite (the 13 existing `test_client.py` tests guard "collector behavior unchanged"):

```bash
python3 -m pytest packages/sdk/tests -q
```

Expected: `67 passed` (62 + 5 new), 0 failures.

- [ ] Commit:

```bash
git add packages/sdk/prompt_shields/client.py packages/sdk/prompt_shields/types.py packages/sdk/tests/test_atlas_sink.py
git commit -m "feat(sdk): wire atlas sink into sync client with env-var config (PRO-17)"
```

---

## Task 4: Async client wiring — mirror sync, close sink on `aclose()` (PRO-17: both sync and async paths feed the sink)

- [ ] Write failing tests — append to `packages/sdk/tests/test_atlas_sink.py`:

```python
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
```

- [ ] Run and confirm failures (unexpected `atlas_url` kwarg on `AsyncShieldsClient`):

```bash
python3 -m pytest packages/sdk/tests/test_atlas_sink.py -q
```

- [ ] Implement — edit `packages/sdk/prompt_shields/async_client.py`:

1. Add `import os` next to `import time`, and change the telemetry import to:

```python
from prompt_shields.telemetry import (
    AtlasTelemetrySender,
    TelemetrySender,
    build_atlas_event,
)
```

2. In `AsyncShieldsClient.__init__`, add the same two params after `ps_collector_url: str = "http://localhost:8000",`:

```python
        atlas_url: str | None = None,
        atlas_api_key: str | None = None,
```

and the same resolution block after `self._telemetry = TelemetrySender(ps_collector_url, ps_api_key)`:

```python
        atlas_url = atlas_url if atlas_url is not None else os.environ.get("PS_ATLAS_URL")
        atlas_api_key = (
            atlas_api_key if atlas_api_key is not None
            else os.environ.get("PS_ATLAS_API_KEY")
        )
        self._atlas: AtlasTelemetrySender | None = (
            AtlasTelemetrySender(atlas_url, atlas_api_key)
            if atlas_url and atlas_api_key
            else None
        )
```

3. In `_AsyncCompletionsNamespace.create`, after the existing telemetry lines:

```python
        self._parent._telemetry.enqueue(event)
        await self._parent._telemetry.flush()

        if self._parent._atlas is not None:
            self._parent._atlas.enqueue(build_atlas_event(event, messages))
            await self._parent._atlas.flush()
```

4. Update `aclose`:

```python
    async def aclose(self):
        await self._telemetry.close()
        if self._atlas is not None:
            await self._atlas.close()
```

- [ ] Run the full suite:

```bash
python3 -m pytest packages/sdk/tests -q
```

Expected: `69 passed` (67 + 2 new), 0 failures.

- [ ] Commit:

```bash
git add packages/sdk/prompt_shields/async_client.py packages/sdk/tests/test_atlas_sink.py
git commit -m "feat(sdk): wire atlas sink into async client, close on aclose (PRO-17)"
```

---

## Task 5: Final verification (PRO-17: all requirements demonstrably met)

- [ ] Full suite, clean run:

```bash
cd /Users/junseki/Documents/GitHub/prompt-shields-sdk && python3 -m pytest packages/sdk/tests -q
```

Expected: `69 passed`, 0 failures, 0 errors. (Do NOT run the repo-root `tests/` e2e suite — it requires a local Postgres for the collector and is unrelated to this change.)

- [ ] Structural privacy audit — confirm no code path can put prompt text in an atlas payload:

```bash
grep -n "prompt_text" packages/sdk/prompt_shields/telemetry.py
```

Expected: matches only in comments/docstrings (the `_ATLAS_FIELD_MAP` comment and `build_atlas_event` docstring) — never as a dict key being set.

```bash
grep -rn "send_prompt_text" packages/sdk/prompt_shields/telemetry.py
```

Expected: no matches — the atlas mapper does not even know the toggle exists.

- [ ] Requirements checklist against PRO-17 (verify each is covered by a passing test):
  - [ ] `atlas_url`/`atlas_api_key` kwargs + `PS_ATLAS_URL`/`PS_ATLAS_API_KEY` env, inactive unless both set → `test_atlas_sink_inactive_*`, `test_atlas_sink_active_via_*` (and async mirror)
  - [ ] Second destination; collector unchanged → existing `test_client.py`/`test_telemetry.py` all green + collector-payload contrast assertions in the sync privacy test
  - [ ] Reuses buffering/flush/fail-open → `AtlasTelemetrySender` is a parametrized `TelemetrySender`; `test_atlas_sender_fail_open_*`, `*_rebuffers_*`, `*_does_not_rebuffer_server_skipped_rows`
  - [ ] Sync AND async paths feed the sink → `test_sync_create_sends_atlas_event_with_no_prompt_text`, `test_async_create_sends_atlas_event_with_no_prompt_text`
  - [ ] Event shape / hash determinism + lowercase hex + omit-when-empty / pii list→counts / batch envelope + `X-API-Key` → Task 1 + Task 2 tests
  - [ ] Privacy: `send_prompt_text=True` never leaks to atlas, asserted on serialized bodies → both end-to-end privacy tests + `test_build_atlas_event_never_copies_prompt_text_or_unknown_fields`
- [ ] `git log --oneline main..HEAD` shows the four feature commits; working tree clean (`git status`). Then use superpowers:finishing-a-development-branch to decide merge/PR.
