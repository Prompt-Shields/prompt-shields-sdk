import asyncio
import hashlib
import logging
from collections import deque
from datetime import datetime, timezone
import httpx

logger = logging.getLogger("prompt_shields.telemetry")

MAX_BUFFER_SIZE = 1000

# atlas.ai prompt-events ingestion (see atlas.ai
# docs/superpowers/specs/2026-06-11-prompt-telemetry-design.md)
ATLAS_EVENTS_PATH = "/api/v1/telemetry/prompt-events"
ATLAS_MAX_BATCH = 500  # server rejects envelopes with more than 500 events


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
