"""Streaming telemetry — wrap `stream=True` responses so they emit one event.

The wrappers hand every chunk to the caller unchanged, as soon as it arrives,
and only *observe* it on the way through. One telemetry event is emitted when
the stream ends: exhausted (`stream_completed=True`), raises, or is closed
early (`stream_completed=False`, with `stream_error` set on errors).

Usage comes from the provider when it sends it:
  - OpenAI: the final usage chunk, present when
    `stream_options={"include_usage": True}`. The SDK requests it by default
    (see `request_openai_stream_usage`) and hides the extra usage-only chunk
    from callers who did not ask for it, so their chunk sequence is unchanged.
  - Anthropic: `message_start` (input and cache tokens) and `message_delta`
    (cumulative output tokens, and cumulative input/cache tokens when sent).
Cached-input, cache-write and reasoning token counts are parsed with the same
helpers as non-streamed responses (`prompt_shields.providers`).
When usage is missing (truncated stream, gateway that drops it), tokens are
estimated at ~4 characters per token and the event carries
`tokens_estimated=True`.

Fail-open: any error inside observation or emission is logged and swallowed;
the caller's stream is never affected.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

from prompt_shields.providers import (
    _field,
    _int,
    anthropic_usage_fields,
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
        self.served_model: str | None = None
        self.output_chars = 0
        self._tool_calls: dict[Any, str | None] = {}

    def observe(self, chunk: Any) -> None:
        if self._vendor == "openai":
            self._observe_openai(chunk)
        elif self._vendor == "anthropic":
            self._observe_anthropic(chunk)

    def _observe_openai(self, chunk: Any) -> None:
        model = getattr(chunk, "model", None)
        if model:
            self.served_model = model
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            fields = openai_usage_fields(usage)
            self.tokens_in = fields["tokens_in"]
            self.tokens_out = fields["tokens_out"]
            self.tokens_cached = fields["tokens_cached"]
            self.tokens_reasoning = fields["tokens_reasoning"]
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
        elif etype == "content_block_delta":
            text = getattr(getattr(event, "delta", None), "text", None)
            if isinstance(text, str):
                self.output_chars += len(text)

    def _observe_anthropic_usage(self, usage: Any) -> None:
        if usage is None:
            return
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
        tool_type = "tool_use" if self._vendor == "anthropic" else "function"
        tool_calls = [
            {"name": name, "type": tool_type} for name in self._tool_calls.values()
        ]
        fields: dict = {
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_cached": self.tokens_cached,
            "tokens_cache_write": self.tokens_cache_write,
            "tokens_reasoning": self.tokens_reasoning,
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
    """

    def __init__(
        self,
        stream: Any,
        accumulator: StreamAccumulator,
        on_finish: Callable[..., Any],
        start: float,
        hide_usage_chunk: bool = False,
    ):
        self._stream = stream
        self._acc = accumulator
        self._on_finish = on_finish
        self._start = start
        self._hide_usage_chunk = hide_usage_chunk
        self._finished = False

    def __getattr__(self, name: str) -> Any:
        # Delegate anything else (e.g. `.response`) to the upstream stream.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._stream, name)

    def _observe(self, chunk: Any) -> bool:
        """Record a chunk; return True if it should be hidden from the caller."""
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
            stream_fields: dict = {"stream": True, "stream_completed": completed}
            if error is not None:
                stream_fields["stream_error"] = type(error).__name__
            latency_ms = int((time.monotonic() - self._start) * 1000)
            return self._acc.adapter_fields(), stream_fields, latency_ms
        except Exception as e:  # noqa: BLE001 - fail-open
            logger.debug(f"Stream telemetry finish error: {e}")
            return None


class ShieldsStream(_StreamBase):
    """Sync wrapper for an upstream stream (iterator + context manager)."""

    def __init__(self, stream: Any, *args, **kwargs):
        super().__init__(stream, *args, **kwargs)
        self._iterator = iter(stream)

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

    def _finish(self, completed: bool, error: BaseException | None = None) -> None:
        args = self._finish_args(completed, error)
        if args is None:
            return
        try:
            self._on_finish(*args)
        except Exception as e:  # noqa: BLE001 - fail-open
            logger.debug(f"Stream telemetry emit error: {e}")

    def close(self) -> None:
        """Close the upstream stream; emits a truncated event if unfinished."""
        try:
            close = getattr(self._stream, "close", None)
            if close is not None:
                close()
        finally:
            self._finish(completed=False)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class AsyncShieldsStream(_StreamBase):
    """Async wrapper for an upstream stream (async iterator + context manager)."""

    def __init__(self, stream: Any, *args, **kwargs):
        super().__init__(stream, *args, **kwargs)
        self._iterator = stream.__aiter__()

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
            await self._finish(completed=False)

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
