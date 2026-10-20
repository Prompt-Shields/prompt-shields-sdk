import atexit
import contextlib
import hashlib
import json
import logging
import math
import os
import threading
import time
import uuid
import weakref
from collections import deque
from datetime import UTC, datetime
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
        "occurred_at": datetime.now(UTC).isoformat(),
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
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    if math.isnan(seconds):
        return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


def _encoded_size(event: dict) -> int:
    """Upper bound on an event's bytes in a request body: ASCII-escaped
    JSON with default separators is never shorter than the compact UTF-8
    that httpx sends, whichever httpx version (0.27 escapes, 0.28 does not)."""
    try:
        return len(json.dumps(event, default=str)) + 1  # + the comma
    except (TypeError, ValueError):
        return 0  # unsendable anyway: the request itself fails


def _warn_unsent_at_exit(buffer: deque, url: str) -> None:
    # weakref.finalize callback: runs at interpreter exit (or when the sender
    # is collected) and must not hold a reference to the sender itself.
    if buffer:
        logger.warning(
            "Telemetry: %d buffered event(s) for %s were never sent",
            len(buffer), url,
        )


# Every sender, held weakly, so a forked child can drop the parent's buffered
# events (otherwise both processes send them: on the child's next flush, or
# by its exit flush, e.g. a gunicorn worker forked after events were queued).
_live_senders: "weakref.WeakSet[TelemetrySender]" = weakref.WeakSet()


def _reset_senders_after_fork() -> None:
    for sender in list(_live_senders):
        with contextlib.suppress(Exception):
            sender._reset_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_senders_after_fork)


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
        max_batch_bytes: int | None = None,
        report_loss: bool = True,
    ):
        self._url = f"{collector_url.rstrip('/')}{path}"
        self._api_key = api_key
        self._headers = (
            headers if headers is not None
            else {"Authorization": f"Bearer {api_key}"}
        )
        self._max_batch = max_batch
        self._max_batch_bytes = max_batch_bytes
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
        _live_senders.add(self)

    def _reset_after_fork(self) -> None:
        """In a forked child: the buffered events are the parent's (it sends
        them), so start empty, with a client id and counters of its own."""
        self._buffer.clear()
        self._client_id = uuid.uuid4().hex
        self._seq = 0
        self._enqueued = 0
        self._sent = 0
        self._dropped = dict.fromkeys(DROP_REASONS, 0)
        self._rate_limited_responses = 0
        self._backlog_reason = None

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

    def _take_buffered(self) -> list[dict]:
        events = list(self._buffer)
        self._buffer.clear()
        return events

    def _drain_chunks(self) -> list[list[dict]]:
        """Drain the buffer into request-sized chunks: at most `max_batch`
        events and about `max_batch_bytes` of JSON each (one chunk when
        unlimited)."""
        events = self._take_buffered()
        if self._max_batch is None and self._max_batch_bytes is None:
            return [events]
        chunks: list[list[dict]] = []
        chunk: list[dict] = []
        size = 0
        for ev in events:
            n = _encoded_size(ev) if self._max_batch_bytes is not None else 0
            if chunk and (
                (self._max_batch is not None and len(chunk) >= self._max_batch)
                or (self._max_batch_bytes is not None
                    and size + n > self._max_batch_bytes)
            ):
                chunks.append(chunk)
                chunk, size = [], 0
            chunk.append(ev)
            size += n
        if chunk:
            chunks.append(chunk)
        return chunks

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
            except Exception as e:  # noqa: BLE001 - fail-open: telemetry never raises
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
            except Exception as e:  # noqa: BLE001 - fail-open: telemetry never raises
                self._handle_error(e, chunk)
                continue
            if not self._handle_response(resp, chunk):
                for rest in chunks[i + 1:]:
                    self._rebuffer(rest)
                return

    def flush_at_exit(self, timeout: float) -> None:
        """Best-effort final flush within `timeout` seconds (see
        `register_exit_flush`). Each request's timeouts are the time left;
        what is not sent in time stays buffered. Respects a 429 back-off.
        Never raises."""
        try:
            if self._closed or not self._buffer or self._backing_off():
                return
            deadline = time.monotonic() + timeout
            chunks = self._drain_chunks()
            for i, chunk in enumerate(chunks):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    for rest in chunks[i:]:
                        self._rebuffer(rest)
                    return
                try:
                    resp = self._sync_client.post(
                        self._url,
                        json=self._payload(chunk),
                        headers=self._headers,
                        timeout=remaining,
                    )
                except Exception as e:  # noqa: BLE001 - re-buffered
                    self._handle_error(e, chunk)
                    continue
                if not self._handle_response(resp, chunk):
                    for rest in chunks[i + 1:]:
                        self._rebuffer(rest)
                    return
        except Exception:  # noqa: BLE001, S110 - never raise at exit
            pass

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


class BackgroundTelemetrySender(TelemetrySender):
    """A TelemetrySender that sends from a daemon thread, never from the caller.

    `enqueue()` only appends to the bounded buffer (thread-safe, no I/O), so
    the caller's latency does not depend on the collector. A background
    thread, started on the first event, flushes when `flush_size` events are
    buffered or every `flush_interval` seconds, whichever comes first, one
    request per batch (up to `max_batch` events). It needs no event loop, so
    sync and asyncio callers share it.

    Everything else is TelemetrySender's: a full buffer drops the oldest event
    and counts it (stats()), a 429 pauses sends for Retry-After, failed sends
    re-buffer, and the bounded exit flush (`register_exit_flush`) sends what
    is left, after stopping the thread. Never raises into the caller.
    """

    def __init__(
        self,
        collector_url: str,
        api_key: str,
        *,
        flush_size: int = 100,
        flush_interval: float = 2.0,
        **kwargs,
    ):
        super().__init__(collector_url, api_key, **kwargs)
        self._flush_size = max(1, flush_size)
        self._flush_interval = flush_interval
        self._init_thread_state()

    def _init_thread_state(self) -> None:
        # Guards the buffer and counters (reentrant: _rebuffer -> _append).
        self._lock = threading.RLock()
        # One send at a time: the thread's flush, the exit flush or close.
        self._send_lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = False
        self._final_flush = False
        self._thread: threading.Thread | None = None

    def _reset_after_fork(self) -> None:
        # The parent's thread does not exist in the child, and its locks
        # may have been held at fork time: start over with fresh ones. The
        # parent's pooled connections are its own (its thread keeps using
        # them): give the child a client of its own, without closing them.
        self._init_thread_state()
        super()._reset_after_fork()
        with contextlib.suppress(Exception):
            self._sync_client = httpx.Client(timeout=5.0)

    # --- caller side: no I/O ---------------------------------------------

    def enqueue(self, event: dict) -> None:
        """Buffer the event and return; the background thread sends it."""
        try:
            with self._lock:
                super().enqueue(event)
                full = len(self._buffer) >= self._flush_size
                if self._thread is None and not self._stopping:
                    self._start_thread()
            # While sends fail or are rate limited, retry on the interval
            # only: a new event must not trigger a resend of the backlog.
            if full and self._backlog_reason is None and not self._backing_off():
                self._wake.set()
        except Exception as e:  # noqa: BLE001 - fail-open: never raise
            logger.debug(f"Telemetry enqueue error: {e}")

    def _start_thread(self) -> None:
        thread = threading.Thread(
            target=self._run, name="prompt-shields-telemetry", daemon=True
        )
        try:
            thread.start()
        except RuntimeError as e:  # e.g. at interpreter shutdown
            logger.debug(f"Telemetry thread not started: {e}")
            return
        self._thread = thread

    async def flush(self) -> None:
        """Ask the background thread to flush now; does not wait for it."""
        self._wake.set()

    # --- background thread ---------------------------------------------------

    def _run(self) -> None:
        while True:
            self._wake.wait(self._flush_interval)
            self._wake.clear()
            if self._stopping and not self._final_flush:
                return
            try:
                self.flush_sync()
            except Exception as e:  # noqa: BLE001 - keep the thread alive
                logger.debug(f"Telemetry flush error: {e}")
            if self._stopping:
                return

    def flush_sync(self) -> None:
        with self._send_lock:
            super().flush_sync()

    def stop(self, final_flush: bool = True) -> None:
        """Stop the background thread without waiting for it. With
        `final_flush`, it sends what is buffered first (in the background)."""
        self._final_flush = final_flush
        self._stopping = True
        self._wake.set()

    def flush_at_exit(self, timeout: float) -> None:
        """Stop the thread, wait up to `timeout` for a send it has in flight,
        then send what is left within the rest of `timeout`."""
        try:
            deadline = time.monotonic() + timeout
            self.stop(final_flush=False)
            if not self._send_lock.acquire(timeout=max(0.0, timeout)):
                return
            try:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    super().flush_at_exit(remaining)
            finally:
                self._send_lock.release()
        except Exception:  # noqa: BLE001, S110 - never raise at exit
            pass

    def close_sync(self) -> None:
        self.stop(final_flush=False)
        with self._send_lock:
            super().close_sync()

    async def close(self) -> None:
        # Sends synchronously: this sender's sends never use the event loop.
        self.close_sync()

    # --- buffer and counters, shared by both sides ----------------------------

    def _append(self, event: dict) -> None:
        with self._lock:
            super()._append(event)

    def _take_buffered(self) -> list[dict]:
        # Only the take holds the lock; sizing the chunks does not.
        with self._lock:
            return super()._take_buffered()

    def _payload(self, chunk: list[dict]) -> dict:
        with self._lock:
            return super()._payload(chunk)

    def _handle_response(self, resp: httpx.Response, chunk: list[dict]) -> bool:
        with self._lock:
            return super()._handle_response(resp, chunk)

    def _handle_error(self, exc: Exception, chunk: list[dict]) -> None:
        with self._lock:
            super()._handle_error(exc, chunk)

    def stats(self) -> dict:
        with self._lock:
            return super().stats()


# --- flush at interpreter exit -------------------------------------------------
#
# Events still buffered when a short script exits (a stream abandoned just
# before the end, a send that failed) would be lost: one `atexit` hook makes a
# best-effort flush of every registered sender, bounded by a total time budget.

EXIT_FLUSH_TIMEOUT = 2.0  # seconds, all senders together
EXIT_FLUSH_TIMEOUT_ENV = "PS_EXIT_FLUSH_TIMEOUT"
EXIT_FLUSH_DISABLE_ENV = "PS_DISABLE_EXIT_FLUSH"

_exit_senders: "weakref.WeakSet[TelemetrySender]" = weakref.WeakSet()
_exit_lock = threading.Lock()
_exit_hook_registered = False
_exit_timeout_override: float | None = None


def _exit_flush_disabled() -> bool:
    value = os.environ.get(EXIT_FLUSH_DISABLE_ENV, "")
    return value.strip().lower() in ("1", "true", "yes", "on")


def exit_flush_timeout() -> float:
    """The exit flush budget in seconds: the value given to `instrument()`,
    else `PS_EXIT_FLUSH_TIMEOUT`, else EXIT_FLUSH_TIMEOUT (0 turns it off)."""
    timeout = _exit_timeout_override
    if timeout is None:
        try:
            timeout = float(os.environ.get(EXIT_FLUSH_TIMEOUT_ENV, EXIT_FLUSH_TIMEOUT))
        except ValueError:
            timeout = EXIT_FLUSH_TIMEOUT
    if math.isnan(timeout):
        return EXIT_FLUSH_TIMEOUT
    return max(0.0, timeout)


def set_exit_flush_timeout(timeout: float | None) -> None:
    """Override the exit flush budget (None: back to the environment/default)."""
    global _exit_timeout_override
    _exit_timeout_override = timeout


def register_exit_flush(*senders) -> None:
    """Flush these senders (best effort, bounded) when the interpreter exits.

    Held weakly: a sender that is collected is simply skipped. The `atexit`
    hook is registered once, on the first sender, and removed when the last
    one is unregistered. Off when `PS_DISABLE_EXIT_FLUSH` is set.
    """
    with _exit_lock:
        global _exit_hook_registered
        for sender in senders:
            if sender is not None:
                with contextlib.suppress(TypeError):  # not weak-referenceable
                    _exit_senders.add(sender)
        if not _exit_hook_registered and len(_exit_senders):
            atexit.register(_flush_at_exit)
            _exit_hook_registered = True


def unregister_exit_flush(*senders) -> None:
    with _exit_lock:
        global _exit_hook_registered
        for sender in senders:
            if sender is not None:
                with contextlib.suppress(TypeError):
                    _exit_senders.discard(sender)
        if _exit_hook_registered and not len(_exit_senders):
            atexit.unregister(_flush_at_exit)
            _exit_hook_registered = False


def _flush_at_exit() -> None:
    """The `atexit` hook. Never raises; gives up when the budget is spent."""
    try:
        if _exit_flush_disabled():
            return
        deadline = time.monotonic() + exit_flush_timeout()
        for sender in list(_exit_senders):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                sender.flush_at_exit(remaining)
            except Exception:  # noqa: BLE001, S112 - one sender's failure
                continue
    except BaseException:  # noqa: BLE001, S110 - never raise or block at exit
        pass
