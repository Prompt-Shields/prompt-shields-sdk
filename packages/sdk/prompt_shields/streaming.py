"""Streaming telemetry — wrap `stream=True` responses so they emit one event.

The wrappers hand every chunk to the caller unchanged, as soon as it arrives,
and only *observe* it on the way through. One telemetry event is emitted when
the stream ends: exhausted (`stream_completed=True`), raises, or is closed
early (`stream_completed=False`, with `stream_error` set on errors). With
`on_abandon` (auto-instrumentation), a stream garbage-collected unfinished
is reported too (`stream_abandoned=True`). The `latency_ms` of a stream that
ends early without an error (closed or abandoned) runs to the last chunk
read, not to the close or the garbage collection.

Usage comes from the provider when it sends it:
  - OpenAI: the final usage chunk, present when
    `stream_options={"include_usage": True}`. The SDK requests it by default
    (see `request_openai_stream_usage`) and hides the extra usage-only chunk
    from callers who did not ask for it, so their chunk sequence is unchanged.
  - Anthropic: `message_start` (input and cache tokens) and `message_delta`
    (cumulative output tokens, and cumulative input/cache tokens when sent).
  - Gemini: `usage_metadata` on each chunk (cumulative; the last one wins).
  - Mistral: the final chunk's `usage` (Chat Completions shape).
  - Cohere: v2 `message-end` `delta.usage`; v1 `stream-end` `response.meta`.
  - Bedrock: ConverseStream `metadata.usage`; InvokeModelWithResponseStream
    `amazon-bedrock-invocationMetrics` on the last chunk.
Cached-input, cache-write and reasoning token counts are parsed with the same
helpers as non-streamed responses (`prompt_shields.providers`).
When usage is missing (truncated stream, gateway that drops it), tokens are
estimated at ~4 characters per token and the event carries
`tokens_estimated=True`.

Fail-open: any error inside observation or emission is logged and swallowed;
the caller's stream is never affected.
"""

import contextlib
import json
import logging
import sys
import time
from collections.abc import Callable
from typing import Any

from prompt_shields.providers import (
    BedrockAdapter,
    CohereAdapter,
    GeminiAdapter,
    _field,
    _int,
    _items,
    _str,
    anthropic_usage_extras,
    anthropic_usage_fields,
    mcp_tool_call,
    openai_usage_fields,
)

logger = logging.getLogger("prompt_shields.streaming")

_CHARS_PER_TOKEN = 4


def upstream_accepts_stream_options(upstream: Any) -> bool:
    """True when the OpenAI client talks to api.openai.com.

    OpenAI-compatible servers (older Azure API versions, self-hosted
    gateways) may reject `stream_options` with a 400, so the SDK only adds
    it for the official API. Elsewhere, callers can opt in by passing
    `stream_options={"include_usage": True}` themselves.
    """
    from urllib.parse import urlsplit

    base_url = getattr(upstream, "base_url", None)
    if base_url is None:
        return False
    try:
        host = urlsplit(str(base_url)).hostname or ""
    except ValueError:
        return False
    return host == "api.openai.com"


def request_openai_stream_usage(kwargs: dict) -> bool:
    """Ask OpenAI to send usage on the final stream chunk.

    Mutates `kwargs` in place. Returns True when the SDK injected
    `include_usage` (the caller did not set it), meaning the trailing
    usage-only chunk should be hidden from the caller. An explicit
    `include_usage` from the caller, True or False, is left alone.
    """
    opts = kwargs.get("stream_options")
    if opts is None:
        kwargs["stream_options"] = {"include_usage": True}
        return True
    if isinstance(opts, dict) and "include_usage" not in opts:
        kwargs["stream_options"] = {**opts, "include_usage": True}
        return True
    return False


def _estimate_tokens(chars: int) -> int:
    return (chars + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def _message_chars(messages: list) -> int:
    return sum(
        len(m["content"])
        for m in messages
        if isinstance(m, dict) and isinstance(m.get("content"), str)
    )


class StreamAccumulator:
    """Collects usage, served model, and tool calls from stream chunks."""

    def __init__(self, vendor: str, messages: list):
        self._vendor = vendor
        self._messages = messages
        self.tokens_in: int | None = None
        self.tokens_out: int | None = None
        self.tokens_cached: int | None = None
        self.tokens_cache_write: int | None = None
        self.tokens_reasoning: int | None = None
        # Anthropic reports these separately and they may arrive in
        # different events; `tokens_in` is derived from them.
        self._anthropic_input: int | None = None
        self._anthropic_cache_read: int | None = None
        self._anthropic_cache_write: int | None = None
        # Further pricing details (service tier, audio tokens, 1-hour cache
        # writes, tool fees, provider-reported cost); latest non-None wins.
        self.pricing_fields: dict[str, Any] = {}
        self.served_model: str | None = None
        self.output_chars = 0
        self._tool_calls: dict[Any, str | None] = {}
        self._mcp_calls: list[dict] = []

    def observe(self, chunk: Any) -> None:
        if self._vendor == "openai":
            self._observe_openai(chunk)
        elif self._vendor == "anthropic":
            self._observe_anthropic(chunk)
        elif self._vendor == "google":
            self._observe_gemini(chunk)
        elif self._vendor == "mistral":
            # `CompletionEvent.data` is a Chat Completions-shaped chunk.
            self._observe_openai(_field(chunk, "data"))
        elif self._vendor == "cohere":
            self._observe_cohere(chunk)
        elif self._vendor == "bedrock":
            self._observe_bedrock(chunk)

    def _set_counts(self, fields: dict) -> None:
        """Take the token counts an adapter extracted; None leaves a count."""
        for name in ("tokens_in", "tokens_out", "tokens_cached",
                     "tokens_cache_write", "tokens_reasoning"):
            if fields.get(name) is not None:
                setattr(self, name, fields[name])
        if fields.get("served_model"):
            self.served_model = fields["served_model"]
        self._update_pricing(fields)

    def _observe_gemini(self, chunk: Any) -> None:
        # Every chunk is a `GenerateContentResponse`; `usage_metadata` is
        # cumulative, so the last one seen holds the totals.
        fields = GeminiAdapter().extract(chunk)
        self._set_counts(fields)
        for call in fields["tool_calls_used"] or ():
            self._tool_calls[len(self._tool_calls)] = call["name"]
        for candidate in _items(_field(chunk, "candidates")):
            for part in _items(_field(_field(candidate, "content"), "parts")):
                text = _field(part, "text")
                if isinstance(text, str):
                    self.output_chars += len(text)

    def _observe_cohere(self, event: Any) -> None:
        etype = _field(event, "type")  # v2
        delta = _field(event, "delta")
        if etype == "content-delta":
            content = _field(_field(delta, "message"), "content")
            text = _field(content, "text")
            if isinstance(text, str):
                self.output_chars += len(text)
        elif etype == "tool-call-start":
            call = _field(_field(delta, "message"), "tool_calls")
            self._tool_calls[len(self._tool_calls)] = _field(
                _field(call, "function"), "name")
        elif etype == "message-end":
            self._set_counts(CohereAdapter().extract({"usage": _field(delta, "usage")}))
            return
        etype = _field(event, "event_type")  # v1
        if etype == "text-generation":
            text = _field(event, "text")
            if isinstance(text, str):
                self.output_chars += len(text)
        elif etype == "stream-end":
            # The final event carries the whole response, tool calls included.
            fields = CohereAdapter().extract(_field(event, "response"))
            self._set_counts(fields)
            if fields["tool_calls_used"]:
                self._tool_calls = {
                    i: call["name"] for i, call in enumerate(fields["tool_calls_used"])
                }

    def _observe_bedrock(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        if "chunk" in event:  # InvokeModelWithResponseStream
            self._observe_bedrock_body(_field(event["chunk"], "bytes"))
            return
        # ConverseStream; a Nova `invoke_model` chunk body has the same keys.
        delta = _field(event.get("contentBlockDelta"), "delta")
        text = _field(delta, "text")
        if isinstance(text, str):
            self.output_chars += len(text)
        tool_use = _field(_field(event.get("contentBlockStart"), "start"), "toolUse")
        if tool_use is not None:
            self._tool_calls[len(self._tool_calls)] = _field(tool_use, "name")
        usage = _field(event.get("metadata"), "usage")
        if usage is not None:
            self._set_counts(BedrockAdapter().extract({"usage": usage}))

    def _observe_bedrock_body(self, data: Any) -> None:
        """One `invoke_model` stream chunk: the model's own JSON event, and on
        the last one Bedrock's `amazon-bedrock-invocationMetrics`."""
        if not isinstance(data, (bytes, bytearray)):
            return
        body = json.loads(data)
        if not isinstance(body, dict):
            return
        metrics = body.get("amazon-bedrock-invocationMetrics")
        if isinstance(metrics, dict):
            self._set_counts(anthropic_usage_fields(
                _int(metrics.get("inputTokenCount")),
                _int(metrics.get("outputTokenCount")),
                _int(metrics.get("cacheReadInputTokenCount")),
                _int(metrics.get("cacheWriteInputTokenCount")),
            ))
        if isinstance(body.get("type"), str):
            # Anthropic: the same events as the Messages API stream, as dicts.
            # Its usage counts are left to the invocation metrics.
            etype = body["type"]
            if etype == "message_start":
                model = _field(body.get("message"), "model")
                if isinstance(model, str) and model:
                    self.served_model = model
            elif etype == "content_block_start":
                block = body.get("content_block")
                if _field(block, "type") == "tool_use":
                    self._tool_calls[len(self._tool_calls)] = _field(block, "name")
            elif etype == "content_block_delta":
                text = _field(body.get("delta"), "text")
                if isinstance(text, str):
                    self.output_chars += len(text)
            return
        if "contentBlockDelta" in body or "contentBlockStart" in body:  # Nova
            self._observe_bedrock({k: v for k, v in body.items() if k != "metadata"})
            return
        for key in ("outputText", "generation"):  # Titan, Llama
            if isinstance(body.get(key), str):
                self.output_chars += len(body[key])

    def _observe_openai(self, chunk: Any) -> None:
        model = getattr(chunk, "model", None)
        if model:
            self.served_model = model
        self._update_pricing(
            {"service_tier": _str(getattr(chunk, "service_tier", None))}
        )
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            fields = openai_usage_fields(usage)
            self.tokens_in = fields["tokens_in"]
            self.tokens_out = fields["tokens_out"]
            self.tokens_cached = fields["tokens_cached"]
            self.tokens_reasoning = fields["tokens_reasoning"]
            self._update_pricing(fields)
        for choice in getattr(chunk, "choices", None) or []:
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue
            content = getattr(delta, "content", None)
            if isinstance(content, str):
                self.output_chars += len(content)
            for tc in getattr(delta, "tool_calls", None) or []:
                key = (getattr(choice, "index", 0), getattr(tc, "index", 0))
                fn = getattr(tc, "function", None)
                name = getattr(fn, "name", None) if fn is not None else None
                # The name arrives on the first delta for each tool call;
                # later deltas carry only argument fragments.
                if name or key not in self._tool_calls:
                    self._tool_calls[key] = name or self._tool_calls.get(key)

    def _observe_anthropic(self, event: Any) -> None:
        etype = getattr(event, "type", None)
        if etype == "message_start":
            message = getattr(event, "message", None)
            model = getattr(message, "model", None)
            if model:
                self.served_model = model
            self._observe_anthropic_usage(getattr(message, "usage", None))
        elif etype == "message_delta":
            # Counts on message_delta are cumulative, not deltas.
            self._observe_anthropic_usage(getattr(event, "usage", None))
        elif etype == "content_block_start":
            block = getattr(event, "content_block", None)
            if getattr(block, "type", None) == "tool_use":
                self._tool_calls[len(self._tool_calls)] = getattr(block, "name", None)
            elif (mcp := mcp_tool_call(block)) is not None:
                self._mcp_calls.append(mcp)
        elif etype == "content_block_delta":
            text = getattr(getattr(event, "delta", None), "text", None)
            if isinstance(text, str):
                self.output_chars += len(text)

    def _update_pricing(self, fields: dict) -> None:
        for key in ("service_tier", "tokens_audio_in", "tokens_audio_out",
                    "tokens_cache_write_1h", "web_search_requests",
                    "provider_cost"):
            if fields.get(key) is not None:
                self.pricing_fields[key] = fields[key]

    def _observe_anthropic_usage(self, usage: Any) -> None:
        if usage is None:
            return
        self._update_pricing(anthropic_usage_extras(usage))
        out = _int(_field(usage, "output_tokens"))
        if out is not None:
            self.tokens_out = out
        for attr, name in (
            ("_anthropic_input", "input_tokens"),
            ("_anthropic_cache_read", "cache_read_input_tokens"),
            ("_anthropic_cache_write", "cache_creation_input_tokens"),
        ):
            value = _int(_field(usage, name))
            if value is not None:
                setattr(self, attr, value)
        fields = anthropic_usage_fields(
            self._anthropic_input,
            self.tokens_out,
            self._anthropic_cache_read,
            self._anthropic_cache_write,
        )
        self.tokens_in = fields["tokens_in"]
        self.tokens_cached = fields["tokens_cached"]
        self.tokens_cache_write = fields["tokens_cache_write"]

    def adapter_fields(self) -> dict:
        """Event fields in the same shape as `ProviderAdapter.extract`."""
        tool_type = (
            "tool_use" if self._vendor in ("anthropic", "bedrock") else "function"
        )
        tool_calls = [
            {"name": name, "type": tool_type} for name in self._tool_calls.values()
        ] + self._mcp_calls
        fields: dict = {
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_cached": self.tokens_cached,
            "tokens_cache_write": self.tokens_cache_write,
            "tokens_reasoning": self.tokens_reasoning,
            **self.pricing_fields,
            "tool_calls_used": tool_calls or None,
            "served_model": self.served_model,
        }
        if fields["tokens_in"] is None:
            fields["tokens_in"] = _estimate_tokens(_message_chars(self._messages))
            fields["tokens_estimated"] = True
        if fields["tokens_out"] is None:
            fields["tokens_out"] = _estimate_tokens(self.output_chars)
            fields["tokens_estimated"] = True
        return fields


def _is_injected_usage_chunk(chunk: Any) -> bool:
    """The trailing OpenAI chunk that only carries usage (empty `choices`)."""
    return getattr(chunk, "usage", None) is not None and not getattr(
        chunk, "choices", None
    )


class _StreamBase:
    """Shared state for the sync and async wrappers.

    `on_finish(adapter_fields, stream_fields, latency_ms)` builds and sends
    the event; it is called at most once.

    `on_failed_start`, when given, is for streams whose request is only sent
    on the first read (lazy generators): an error before any chunk is then a
    failed request, not a truncated stream, so no event is emitted (as for a
    failed non-streamed call) and `on_failed_start()` is called instead.

    `on_abandon`, when given, is called with the same arguments as
    `on_finish` (`stream_fields` also carrying `stream_abandoned=True`) if
    the wrapper is garbage-collected before it ended: the caller broke out
    of the loop without `close()`. It runs inside the garbage collector, so
    it must not block or do I/O (enqueue the event, do not send it). It is
    skipped at interpreter shutdown, and a lazy stream (`on_failed_start`)
    that was never read sent no request, so it gets `on_failed_start()`.

    `close_flush=False` makes `close()` call `on_finish(..., flush=False)`:
    enqueue, do not send. For a stream closed by code that may run in the
    garbage collector (cohere's `chat_stream` generator closes its event
    stream when it is collected).
    """

    def __init__(
        self,
        stream: Any,
        accumulator: StreamAccumulator,
        on_finish: Callable[..., Any],
        start: float,
        hide_usage_chunk: bool = False,
        on_failed_start: Callable[[], Any] | None = None,
        close_flush: bool = True,
    ):
        self._stream = stream
        self._acc = accumulator
        self._on_finish = on_finish
        self._start = start
        self._hide_usage_chunk = hide_usage_chunk
        self._on_failed_start = on_failed_start
        self._close_flush = close_flush
        self._chunks = 0
        # When the last chunk was read (or the stream was wrapped): the end
        # of an early-ended stream's latency.
        self._last_read = time.monotonic()
        self._finished = False
        # Set by the subclasses once fully constructed.
        self._on_abandon: Callable[..., Any] | None = None

    def __del__(self):
        # Fail-open, and never raise from a finalizer.
        try:
            if (self._finished or self._on_abandon is None
                    or sys.is_finalizing()):
                return
            if self._chunks == 0 and self._on_failed_start is not None:
                self._finished = True
                self._on_failed_start()
                return
            args = self._finish_args(completed=False, error=None)
            if args is not None:
                args[1]["stream_abandoned"] = True
                self._on_abandon(*args)
        except Exception as e:  # noqa: BLE001
            with contextlib.suppress(Exception):
                logger.debug(f"Stream telemetry abandon error: {e}")

    def discard(self) -> None:
        """Drop this wrapper unused: it will emit nothing, not even when
        garbage-collected."""
        self._finished = True

    def __getattr__(self, name: str) -> Any:
        # Delegate anything else (e.g. `.response`) to the upstream stream.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._stream, name)

    def _observe(self, chunk: Any) -> bool:
        """Record a chunk; return True if it should be hidden from the caller."""
        self._chunks += 1
        self._last_read = time.monotonic()
        try:
            self._acc.observe(chunk)
            return self._hide_usage_chunk and _is_injected_usage_chunk(chunk)
        except Exception as e:  # noqa: BLE001 - fail-open
            logger.debug(f"Stream telemetry observe error: {e}")
            return False

    def _finish_args(self, completed: bool, error: BaseException | None):
        """Mark finished; return on_finish args, or None if already finished."""
        if self._finished:
            return None
        self._finished = True
        try:
            if (error is not None and self._chunks == 0
                    and self._on_failed_start is not None):
                self._on_failed_start()
                return None
            stream_fields: dict = {"stream": True, "stream_completed": completed}
            if error is not None:
                stream_fields["stream_error"] = type(error).__name__
            # A stream ended early (closed or abandoned) is timed to its last
            # chunk: the close may come much later, from the garbage collector.
            end = (self._last_read if not completed and error is None
                   else time.monotonic())
            latency_ms = int((end - self._start) * 1000)
            return self._acc.adapter_fields(), stream_fields, latency_ms
        except Exception as e:  # noqa: BLE001 - fail-open
            logger.debug(f"Stream telemetry finish error: {e}")
            return None


class ShieldsStream(_StreamBase):
    """Sync wrapper for an upstream stream (iterator + context manager)."""

    def __init__(self, stream: Any, *args,
                 on_abandon: Callable[..., Any] | None = None, **kwargs):
        super().__init__(stream, *args, **kwargs)
        self._iterator = iter(stream)
        self._on_abandon = on_abandon

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            try:
                chunk = next(self._iterator)
            except StopIteration:
                self._finish(completed=True)
                raise
            except BaseException as e:
                self._finish(completed=False, error=e)
                raise
            if self._finished or not self._observe(chunk):
                return chunk

    def _finish(self, completed: bool, error: BaseException | None = None,
                flush: bool = True) -> None:
        args = self._finish_args(completed, error)
        if args is None:
            return
        try:
            if flush:
                self._on_finish(*args)
            else:
                self._on_finish(*args, flush=False)
        except Exception as e:  # noqa: BLE001 - fail-open
            logger.debug(f"Stream telemetry emit error: {e}")

    def close(self) -> None:
        """Close the upstream stream; emits a truncated event if unfinished."""
        try:
            close = getattr(self._stream, "close", None)
            if close is not None:
                close()
        finally:
            self._finish(completed=False, flush=self._close_flush)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class AsyncShieldsStream(_StreamBase):
    """Async wrapper for an upstream stream (async iterator + context manager)."""

    def __init__(self, stream: Any, *args,
                 on_abandon: Callable[..., Any] | None = None, **kwargs):
        super().__init__(stream, *args, **kwargs)
        self._iterator = stream.__aiter__()
        self._on_abandon = on_abandon

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            try:
                chunk = await self._iterator.__anext__()
            except StopAsyncIteration:
                await self._finish(completed=True)
                raise
            except Exception as e:
                await self._finish(completed=False, error=e)
                raise
            except BaseException as e:
                # Cancellation: record the event but do not await network I/O.
                await self._finish(completed=False, error=e, flush=False)
                raise
            if self._finished or not self._observe(chunk):
                return chunk

    async def _finish(
        self,
        completed: bool,
        error: BaseException | None = None,
        flush: bool = True,
    ) -> None:
        args = self._finish_args(completed, error)
        if args is None:
            return
        try:
            await self._on_finish(*args, flush=flush)
        except Exception as e:  # noqa: BLE001 - fail-open
            logger.debug(f"Stream telemetry emit error: {e}")

    async def close(self) -> None:
        """Close the upstream stream; emits a truncated event if unfinished."""
        try:
            close = getattr(self._stream, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result
        finally:
            await self._finish(completed=False, flush=self._close_flush)

    async def aclose(self) -> None:
        """Alias of `close()` (openai's AsyncStream, `contextlib.aclosing`).

        Defined explicitly so `__getattr__` does not forward it upstream and
        skip the truncated-stream event.
        """
        await self.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()
