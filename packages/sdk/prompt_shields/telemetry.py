import asyncio
import hashlib
import logging
import math
import time
import uuid
import weakref
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

logger = logging.getLogger("prompt_shields.telemetry")

MAX_BUFFER_SIZE = 1000

# Drop reasons counted by TelemetrySender (see stats()):
#   buffer_overflow  buffer full with no failing send behind it
#   send_failed      evicted while sends were failing (network error / non-200)
#   rate_limited     evicted while backing off after a 429
#   server_skipped   a 200 response reported rows it skipped (final, not retried)
#   shutdown         still buffered when the sender was closed
DROP_REASONS = (
    "buffer_overflow", "send_failed", "rate_limited", "server_skipped", "shutdown",
)
# Ceiling on a server-supplied Retry-After so a bad header cannot mute
# telemetry for hours.
MAX_RETRY_AFTER_SECONDS = 300.0

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


def parse_retry_after(value: str | None) -> float | None:
    """Seconds to wait from a Retry-After header (delta-seconds or HTTP-date)."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    if math.isnan(seconds):
        return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


def _warn_unsent_at_exit(buffer: deque, url: str) -> None:
    # weakref.finalize callback: runs at interpreter exit (or when the sender
    # is collected) and must not hold a reference to the sender itself.
    if buffer:
        logger.warning(
            "Telemetry: %d buffered event(s) for %s were never sent",
            len(buffer), url,
        )


class TelemetrySender:
    """Buffers telemetry events and sends to collector. Fail-open: never blocks LLM calls.

    Loss is counted, not silent: every event that leaves the buffer without
    reaching the server is counted under a reason in DROP_REASONS, readable
    via stats(). With report_loss on, each batch carries a "metadata" object
    (client id, per-client monotonic batch seq, cumulative enqueued and
    dropped counts) so the collector can reconcile sent against received.

    A 429 re-buffers the batch and pauses sending for the Retry-After
    interval (capped at MAX_RETRY_AFTER_SECONDS).

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
        report_loss: bool = True,
    ):
        self._url = f"{collector_url.rstrip('/')}{path}"
        self._api_key = api_key
        self._headers = (
            headers if headers is not None
            else {"Authorization": f"Bearer {api_key}"}
        )
        self._max_batch = max_batch
        self._report_loss = report_loss
        self._buffer: deque[dict] = deque(maxlen=MAX_BUFFER_SIZE)
        self._sync_client = httpx.Client(timeout=5.0)
        self._async_client = httpx.AsyncClient(timeout=5.0)

        # Loss accounting
        self._client_id = uuid.uuid4().hex
        self._seq = 0  # per-client monotonic batch sequence
        self._enqueued = 0
        self._sent = 0
        self._dropped: dict[str, int] = dict.fromkeys(DROP_REASONS, 0)
        self._rate_limited_responses = 0
        # Why the buffer is backing up (None while sends succeed); evictions
        # are attributed to it.
        self._backlog_reason: str | None = None
        self._retry_at = 0.0  # time.monotonic() before which sends pause
        self._closed = False
        weakref.finalize(self, _warn_unsent_at_exit, self._buffer, self._url)

    def enqueue(self, event: dict) -> None:
        """Add event to buffer. If buffer full, oldest event is dropped (fail-open)."""
        self._enqueued += 1
        self._append(event)

    def _append(self, event: dict) -> None:
        if len(self._buffer) >= MAX_BUFFER_SIZE:
            reason = self._backlog_reason or "buffer_overflow"
            self._dropped[reason] += 1
            logger.warning("Telemetry buffer full, dropping oldest event (%s)", reason)
        self._buffer.append(event)

    def stats(self) -> dict:
        """Snapshot of this sender's delivery counters."""
        dropped = dict(self._dropped)
        return {
            "client_id": self._client_id,
            "enqueued": self._enqueued,
            "sent": self._sent,
            "buffered": len(self._buffer),
            "dropped": dropped,
            "dropped_total": sum(dropped.values()),
            "rate_limited_responses": self._rate_limited_responses,
            "retry_after_seconds": max(0.0, self._retry_at - time.monotonic()),
        }

    def _backing_off(self) -> bool:
        return time.monotonic() < self._retry_at

    def _payload(self, chunk: list[dict]) -> dict:
        payload: dict = {"events": chunk}
        if self._report_loss:
            self._seq += 1
            payload["metadata"] = {
                "client_id": self._client_id,
                "seq": self._seq,
                "enqueued_total": self._enqueued,
                "dropped": {k: v for k, v in self._dropped.items() if v},
                "sdk": "python",
            }
        return payload

    def _handle_response(self, resp: httpx.Response, chunk: list[dict]) -> bool:
        """Account for one response; re-buffer the chunk unless delivered.

        Returns False when the rest of the flush must wait (rate limited).
        """
        if resp.status_code == 200:
            self._backlog_reason = None
            skipped = 0
            try:
                body = resp.json()
                if isinstance(body, dict) and isinstance(body.get("skipped"), int):
                    skipped = max(0, min(body["skipped"], len(chunk)))
            except ValueError:  # not JSON: nothing reported as skipped
                logger.debug("Telemetry response body is not JSON")
            self._sent += len(chunk) - skipped
            self._dropped["server_skipped"] += skipped
            return True
        if resp.status_code == 429:
            self._rate_limited_responses += 1
            self._backlog_reason = "rate_limited"
            delay = parse_retry_after(resp.headers.get("Retry-After"))
            if delay is None:
                delay = 1.0
            self._retry_at = time.monotonic() + delay
            logger.warning("Telemetry rate limited; pausing sends for %.1fs", delay)
            self._rebuffer(chunk)
            return False
        logger.warning(f"Telemetry send failed: {resp.status_code}")
        self._backlog_reason = "send_failed"
        self._rebuffer(chunk)
        return True

    def _handle_error(self, exc: Exception, chunk: list[dict]) -> None:
        logger.warning(f"Telemetry send error: {exc}")
        self._backlog_reason = "send_failed"
        self._rebuffer(chunk)

    def _rebuffer(self, events: list[dict]) -> None:
        for ev in events:
            self._append(ev)

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
        failed validation and can never succeed on retry (they are counted as
        server_skipped). Only network errors and non-200 responses re-buffer.
        While backing off after a 429 the flush is a no-op.
        """
        if not self._buffer or self._backing_off():
            return
        chunks = self._drain_chunks()
        for i, chunk in enumerate(chunks):
            try:
                resp = await self._async_client.post(
                    self._url,
                    json=self._payload(chunk),
                    headers=self._headers,
                )
            except Exception as e:
                self._handle_error(e, chunk)
                continue
            if not self._handle_response(resp, chunk):
                for rest in chunks[i + 1:]:
                    self._rebuffer(rest)
                return

    def flush_sync(self) -> None:
        """Synchronous flush using httpx.Client. Fail-open with 5s timeout."""
        if not self._buffer or self._backing_off():
            return
        chunks = self._drain_chunks()
        for i, chunk in enumerate(chunks):
            try:
                resp = self._sync_client.post(
                    self._url,
                    json=self._payload(chunk),
                    headers=self._headers,
                )
            except Exception as e:
                self._handle_error(e, chunk)
                continue
            if not self._handle_response(resp, chunk):
                for rest in chunks[i + 1:]:
                    self._rebuffer(rest)
                return

    def _account_shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._buffer:
            n = len(self._buffer)
            self._dropped["shutdown"] += n
            self._buffer.clear()
            logger.warning("Telemetry: %d event(s) unsent at close, dropped", n)

    async def close(self) -> None:
        # The final flush ignores any 429 back-off: there is no later retry.
        self._retry_at = 0.0
        await self.flush()
        self._account_shutdown()
        await self._async_client.aclose()
        self._sync_client.close()

    def close_sync(self) -> None:
        """Sync counterpart of close(): final flush, then count what is left."""
        self._retry_at = 0.0
        self.flush_sync()
        self._account_shutdown()
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
            # atlas envelopes are extra="forbid": never add a metadata key.
            report_loss=False,
        )
