"""Auto-instrumentation — emit telemetry from unmodified provider SDK calls.

    import prompt_shields
    prompt_shields.instrument(ps_api_key="ps_...")   # once, at startup

    client = openai.OpenAI()                          # unchanged app code
    client.chat.completions.create(model=..., messages=...)  # -> one event

`instrument()` patches the official SDK resource classes, sync and async:
  - openai:    `chat.completions.create`, `responses.create` (if present)
  - anthropic: `messages.create`
Missing packages, or methods an installed version lacks, are skipped.

Guarantees:
  - Idempotent: a second `instrument()` is a no-op and returns False.
  - Reversible: `uninstrument()` restores the original methods.
  - Fail-open: telemetry errors are logged and swallowed; the upstream
    response (or exception) is what the caller gets.
  - No double events: calls made through `ShieldsClient` / `AsyncShieldsClient`
    run inside `suppress_instrumentation()` and are left to those clients.
    Framework integrations (`prompt_shields.integrations`) announce each call
    as a `FrameworkCall`; a patched call made inside it claims it, adds the
    framework fields to its event, and the integration then emits nothing.

Events are built by the same code as `ShieldsClient` (`_build_event`) and
streams are wrapped by the same `ShieldsStream` / `AsyncShieldsStream`.
Discovery metadata comes from arguments or `PS_*` environment variables.
"""

import contextlib
import contextvars
import functools
import importlib
import inspect
import logging
import os
import threading
import time
from types import SimpleNamespace
from typing import Any

from prompt_shields.providers import (
    ADAPTERS,
    ProviderAdapter,
    _field,
    _int,
    get_adapter,
)
from prompt_shields.streaming import (
    AsyncShieldsStream,
    ShieldsStream,
    StreamAccumulator,
    request_openai_stream_usage,
    upstream_accepts_stream_options,
)
from prompt_shields.telemetry import (
    AtlasTelemetrySender,
    TelemetrySender,
    build_atlas_event,
)

logger = logging.getLogger("prompt_shields.instrumentation")

# (vendor, module, class, api). Each class's `create` is patched.
_TARGETS = (
    ("openai", "openai.resources.chat.completions", "Completions", "chat"),
    ("openai", "openai.resources.chat.completions", "AsyncCompletions", "chat"),
    ("openai", "openai.resources.responses", "Responses", "responses"),
    ("openai", "openai.resources.responses", "AsyncResponses", "responses"),
    ("anthropic", "anthropic.resources.messages", "Messages", "messages"),
    ("anthropic", "anthropic.resources.messages", "AsyncMessages", "messages"),
)

# Discovery metadata: argument name -> environment variable.
_METADATA_ENV = {
    "business_unit": "PS_BUSINESS_UNIT",
    "use_case": "PS_USE_CASE",
    "owner": "PS_OWNER",
    "data_classification": "PS_DATA_CLASSIFICATION",
    "environment": "PS_ENVIRONMENT",
    "calling_service": "PS_CALLING_SERVICE",
}

# Header set by the SDKs' `.with_raw_response` / `.with_streaming_response`;
# those return HTTP wrappers we cannot read usage from, so they pass through.
_RAW_RESPONSE_HEADER = "x-stainless-raw-response"

_SUPPRESSED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "prompt_shields_instrumentation_suppressed", default=False
)

_lock = threading.Lock()
_state: "_State | None" = None


class FrameworkCall:
    """One LLM call announced by a framework integration.

    Set in `_FRAMEWORK_CALL` before the framework reaches the provider SDK.
    The first patched call made inside it claims it (`captured`), so the
    integration knows the event was already emitted. `model`, when set, must
    match the requested model for the claim (a guard against a stale value).
    `previous` is the value it replaced: a batch (LangChain `generate` with
    several prompts) announces every run before making the calls, so a call
    that finds the newest one already claimed claims the next older one.
    A call that raises gives its claim back (it emits no event).
    """

    __slots__ = ("captured", "finished", "framework", "model", "previous",
                 "use_case_hint")

    def __init__(self, framework: str, use_case_hint: str | None = None,
                 model: str | None = None,
                 previous: "FrameworkCall | None" = None):
        self.framework = framework
        self.use_case_hint = use_case_hint
        self.model = model
        self.previous = previous
        self.captured = False
        self.finished = False


_FRAMEWORK_CALL: contextvars.ContextVar[FrameworkCall | None] = (
    contextvars.ContextVar("prompt_shields_framework_call", default=None)
)


def _same_model(a: str, b: str) -> bool:
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


_claim_lock = threading.Lock()


def current_framework_call() -> FrameworkCall | None:
    """The innermost unfinished `FrameworkCall` (the `previous` for a new one)."""
    fc = _FRAMEWORK_CALL.get()
    while fc is not None and fc.finished:
        fc = fc.previous
    return fc


def _claim_framework_call(model: Any) -> FrameworkCall | None:
    with _claim_lock:
        fc = _FRAMEWORK_CALL.get()
        while fc is not None and fc.captured and not fc.finished:
            fc = fc.previous
        if fc is None or fc.captured or fc.finished:
            return None
        if fc.model and isinstance(model, str) and not _same_model(fc.model, model):
            return None
        fc.captured = True
        return fc


def _release_framework_call(call: dict) -> None:
    fc = call.get("framework")
    if fc is not None:
        fc.captured = False


def apply_framework_fields(event: dict, fc: FrameworkCall) -> None:
    """Tag an event with its framework; the hint fills an unset use case."""
    event["framework"] = fc.framework
    if fc.use_case_hint and not event.get("use_case_name"):
        event["use_case_name"] = fc.use_case_hint


@contextlib.contextmanager
def suppress_instrumentation():
    """Calls made inside this block emit no auto-instrumentation events.

    `ShieldsClient` uses it around its upstream call, since it emits its own
    event. Applies to the current thread / asyncio task only.
    """
    token = _SUPPRESSED.set(True)
    try:
        yield
    finally:
        _SUPPRESSED.reset(token)


class _State:
    def __init__(self, telemetry, atlas, metadata, scan_pii, send_prompt_text,
                 pricing_table):
        self.telemetry = telemetry
        self.atlas = atlas
        self.metadata = metadata
        self.scan_pii = scan_pii
        self.send_prompt_text = send_prompt_text
        self.pricing_table = pricing_table
        self.patches: list[tuple[type, str, Any, Any]] = []


def is_instrumented() -> bool:
    return _state is not None


def instrument(
    ps_api_key: str | None = None,
    ps_collector_url: str | None = None,
    *,
    business_unit: str | None = None,
    use_case: str | None = None,
    owner: str | None = None,
    data_classification: str | None = None,
    environment: str | None = None,
    calling_service: str | None = None,
    scan_pii: bool = True,
    send_prompt_text: bool = False,
    pricing_table: dict | None = None,
    atlas_url: str | None = None,
    atlas_api_key: str | None = None,
    vendors: tuple[str, ...] | list[str] | None = None,
) -> bool:
    """Patch installed provider SDKs so every call emits a telemetry event.

    Unset arguments fall back to environment variables: `PS_API_KEY`,
    `PS_COLLECTOR_URL` (default http://localhost:8000), `PS_BUSINESS_UNIT`,
    `PS_USE_CASE`, `PS_OWNER`, `PS_DATA_CLASSIFICATION`, `PS_ENVIRONMENT`,
    `PS_CALLING_SERVICE`, `PS_ATLAS_URL`, `PS_ATLAS_API_KEY`.

    `vendors` limits patching (e.g. `("openai",)`); default is all.
    Returns True if this call instrumented, False if already instrumented.
    Raises ValueError when no Prompt Shields API key is available.
    """
    global _state
    with _lock:
        if _state is not None:
            return False

        ps_api_key = ps_api_key or os.environ.get("PS_API_KEY")
        if not ps_api_key:
            raise ValueError(
                "prompt_shields.instrument() needs ps_api_key or PS_API_KEY"
            )
        ps_collector_url = (
            ps_collector_url
            or os.environ.get("PS_COLLECTOR_URL")
            or "http://localhost:8000"
        )
        args = {
            "business_unit": business_unit,
            "use_case": use_case,
            "owner": owner,
            "data_classification": data_classification,
            "environment": environment,
            "calling_service": calling_service,
        }
        metadata = {}
        for key, env in _METADATA_ENV.items():
            value = args[key] if args[key] is not None else os.environ.get(env)
            if value is not None:
                metadata[key] = value

        atlas_url = atlas_url if atlas_url is not None else os.environ.get("PS_ATLAS_URL")
        atlas_api_key = (
            atlas_api_key if atlas_api_key is not None
            else os.environ.get("PS_ATLAS_API_KEY")
        )
        state = _State(
            telemetry=TelemetrySender(ps_collector_url, ps_api_key),
            atlas=(
                AtlasTelemetrySender(atlas_url, atlas_api_key)
                if atlas_url and atlas_api_key else None
            ),
            metadata=metadata,
            scan_pii=scan_pii,
            send_prompt_text=send_prompt_text,
            pricing_table=pricing_table,
        )

        for vendor, module_name, class_name, api in _TARGETS:
            if vendors is not None and vendor not in vendors:
                continue
            try:
                module = importlib.import_module(module_name)
            except Exception:  # noqa: BLE001, S112 - not installed or broken
                continue
            cls = getattr(module, class_name, None)
            original = cls.__dict__.get("create") if cls is not None else None
            if original is None or getattr(original, "__ps_original__", None):
                continue
            wrapper = _make_wrapper(original, vendor, api)
            cls.create = wrapper
            state.patches.append((cls, "create", original, wrapper))

        _state = state
        logger.debug(f"Instrumented {len(state.patches)} provider methods")
        return True


def uninstrument() -> bool:
    """Restore the original provider methods. Returns False if not instrumented."""
    global _state
    with _lock:
        if _state is None:
            return False
        for cls, name, original, wrapper in reversed(_state.patches):
            # Only restore what is still ours; leave later patches alone.
            if cls.__dict__.get(name) is wrapper:
                setattr(cls, name, original)
        _state = None
        return True


# --- wrappers ----------------------------------------------------------------


def _make_wrapper(original: Any, vendor: str, api: str) -> Any:
    # One wrapper for sync and async: the SDKs decorate async `create` with
    # plain functions that return a coroutine, so detect it on the result.
    @functools.wraps(original)
    def wrapper(self, *args, **kwargs):
        call = _prepare(self, vendor, api, args, kwargs)
        if call is None:
            return original(self, *args, **kwargs)
        start = time.monotonic()
        try:
            response = original(self, *args, **call["kwargs"])
        except BaseException:
            _release_framework_call(call)
            raise
        if inspect.isawaitable(response):
            return _await_and_finish(call, response, start)
        return _finish_sync(call, response, start)

    wrapper.__ps_original__ = original
    return wrapper


async def _await_and_finish(call: dict, awaitable: Any, start: float) -> Any:
    try:
        response = await awaitable
    except BaseException:
        _release_framework_call(call)
        raise
    return await _finish_async(call, response, start)


def _prepare(resource: Any, vendor: str, api: str, args: tuple,
             kwargs: dict) -> dict | None:
    """Per-call context, or None to pass the call through untouched."""
    state = _state
    if state is None or _SUPPRESSED.get() or args:
        return None
    try:
        headers = kwargs.get("extra_headers") or {}
        if any(str(k).lower() == _RAW_RESPONSE_HEADER for k in headers):
            return None
        call_kwargs = dict(kwargs)
        streaming = call_kwargs.get("stream") is True
        upstream = getattr(resource, "_client", None)
        hide_usage_chunk = False
        if (
            streaming
            and api == "chat"
            and upstream_accepts_stream_options(upstream)
        ):
            hide_usage_chunk = request_openai_stream_usage(call_kwargs)
        api_key = getattr(upstream, "api_key", None)
        framework = _claim_framework_call(call_kwargs.get("model"))
        return {
            "state": state,
            "vendor": vendor,
            "api": api,
            "kwargs": call_kwargs,
            "model": call_kwargs.get("model"),
            "messages": _messages(api, call_kwargs),
            "streaming": streaming,
            "hide_usage_chunk": hide_usage_chunk,
            "api_key": api_key if isinstance(api_key, str) else None,
            "framework": framework,
        }
    except Exception as e:  # noqa: BLE001 - fail-open
        logger.debug(f"Instrumentation prepare error: {e}")
        return None


def _messages(api: str, kwargs: dict) -> list:
    """Message list in the shape `_build_event` scans (dicts with `content`)."""
    if api != "responses":
        messages = kwargs.get("messages")
        return list(messages) if isinstance(messages, (list, tuple)) else []
    messages: list = []
    instructions = kwargs.get("instructions")
    if isinstance(instructions, str):
        messages.append({"role": "system", "content": instructions})
    data = kwargs.get("input")
    if isinstance(data, str):
        messages.append({"role": "user", "content": data})
    elif isinstance(data, (list, tuple)):
        messages.extend(data)
    return messages


def _finish_sync(call: dict, response: Any, start: float) -> Any:
    try:
        if call["streaming"]:
            def on_finish(adapter_fields, stream_fields, latency_ms):
                _send_sync(call, _stream_event(call, adapter_fields,
                                               stream_fields, latency_ms))

            return ShieldsStream(response, _accumulator(call), on_finish, start,
                                 hide_usage_chunk=call["hide_usage_chunk"])
        latency_ms = int((time.monotonic() - start) * 1000)
        _send_sync(call, _event(call, response, latency_ms))
    except Exception as e:  # noqa: BLE001 - fail-open
        logger.debug(f"Instrumentation telemetry error: {e}")
    return response


async def _finish_async(call: dict, response: Any, start: float) -> Any:
    try:
        if call["streaming"]:
            async def on_finish(adapter_fields, stream_fields, latency_ms,
                                flush=True):
                await _send_async(call, _stream_event(
                    call, adapter_fields, stream_fields, latency_ms), flush=flush)

            return AsyncShieldsStream(response, _accumulator(call), on_finish,
                                      start,
                                      hide_usage_chunk=call["hide_usage_chunk"])
        latency_ms = int((time.monotonic() - start) * 1000)
        await _send_async(call, _event(call, response, latency_ms))
    except Exception as e:  # noqa: BLE001 - fail-open
        logger.debug(f"Instrumentation telemetry error: {e}")
    return response


# --- events ------------------------------------------------------------------


def build_event(state: _State, *, vendor: str, model: Any, messages: list,
                latency_ms: int, adapter_fields: dict,
                api_key: str | None = None) -> dict:
    """A `ShieldsClient`-shaped event from already extracted usage fields."""
    # Imported here: client.py imports this module for suppress_instrumentation.
    from prompt_shields.client import ShieldsClient, _fingerprint

    # Duck-typed stand-in for a ShieldsClient: the attributes _build_event reads.
    shim = SimpleNamespace(
        _vendor=vendor,
        # Only pricing_model() is used (fields are already extracted); a
        # vendor without an adapter is priced by the requested model.
        _adapter=ADAPTERS.get(vendor) or ProviderAdapter(),
        _metadata=state.metadata,
        _scan_pii=state.scan_pii,
        _send_prompt_text=state.send_prompt_text,
        _pricing_table=state.pricing_table,
        _api_key_fingerprint=_fingerprint(api_key) if api_key else None,
    )
    return ShieldsClient._build_event(
        shim,
        model=model,
        messages=messages,
        response=None,
        latency_ms=latency_ms,
        adapter_fields=adapter_fields,
    )


def _event(call: dict, response: Any, latency_ms: int,
           adapter_fields: dict | None = None) -> dict:
    if adapter_fields is None:
        adapter_fields = (
            _extract_responses(response) if call["api"] == "responses"
            else get_adapter(call["vendor"]).extract(response)
        )
    event = build_event(
        call["state"],
        vendor=call["vendor"],
        model=call["model"],
        messages=call["messages"],
        latency_ms=latency_ms,
        adapter_fields=adapter_fields,
        api_key=call["api_key"],
    )
    if call.get("framework") is not None:
        apply_framework_fields(event, call["framework"])
    return event


def _stream_event(call: dict, adapter_fields: dict, stream_fields: dict,
                  latency_ms: int) -> dict:
    event = _event(call, None, latency_ms, adapter_fields=adapter_fields)
    event.update(stream_fields)
    if adapter_fields.get("tokens_estimated"):
        event["tokens_estimated"] = True
    return event


def _send_sync(call: dict, event: dict) -> None:
    state = call["state"]
    state.telemetry.enqueue(event)
    state.telemetry.flush_sync()
    if state.atlas is not None:
        state.atlas.enqueue(build_atlas_event(event, call["messages"]))
        state.atlas.flush_sync()


async def _send_async(call: dict, event: dict, flush: bool = True) -> None:
    state = call["state"]
    state.telemetry.enqueue(event)
    if flush:
        await state.telemetry.flush()
    if state.atlas is not None:
        state.atlas.enqueue(build_atlas_event(event, call["messages"]))
        if flush:
            await state.atlas.flush()


# --- OpenAI Responses API ----------------------------------------------------


def _extract_responses(response: Any) -> dict:
    """`ProviderAdapter.extract` equivalent for a Responses API object."""
    usage = getattr(response, "usage", None)
    tool_calls = [
        {"name": getattr(item, "name", None), "type": "function"}
        for item in getattr(response, "output", None) or []
        if getattr(item, "type", None) == "function_call"
    ]
    # Like Chat Completions, `input_tokens` includes cached tokens and
    # `output_tokens` includes reasoning tokens; the details are subsets.
    input_details = _field(usage, "input_tokens_details")
    output_details = _field(usage, "output_tokens_details")
    return {
        "tokens_in": _int(_field(usage, "input_tokens")),
        "tokens_out": _int(_field(usage, "output_tokens")),
        "tokens_cached": _int(_field(input_details, "cached_tokens")),
        "tokens_cache_write": None,
        "tokens_reasoning": _int(_field(output_details, "reasoning_tokens")),
        "tool_calls_used": tool_calls or None,
        "served_model": getattr(response, "model", None),
    }


class _ResponsesAccumulator(StreamAccumulator):
    """Reads Responses API stream events (`response.*`)."""

    def observe(self, event: Any) -> None:
        etype = getattr(event, "type", None)
        if etype in ("response.created", "response.completed",
                     "response.incomplete", "response.failed"):
            fields = _extract_responses(getattr(event, "response", None))
            if fields["served_model"]:
                self.served_model = fields["served_model"]
            if fields["tokens_in"] is not None:
                self.tokens_in = fields["tokens_in"]
            if fields["tokens_out"] is not None:
                self.tokens_out = fields["tokens_out"]
            if fields["tokens_cached"] is not None:
                self.tokens_cached = fields["tokens_cached"]
            if fields["tokens_reasoning"] is not None:
                self.tokens_reasoning = fields["tokens_reasoning"]
        elif etype == "response.output_text.delta":
            delta = getattr(event, "delta", None)
            if isinstance(delta, str):
                self.output_chars += len(delta)
        elif etype == "response.output_item.added":
            item = getattr(event, "item", None)
            if getattr(item, "type", None) == "function_call":
                self._tool_calls[len(self._tool_calls)] = getattr(item, "name", None)


def _accumulator(call: dict) -> StreamAccumulator:
    if call["api"] == "responses":
        return _ResponsesAccumulator(call["vendor"], call["messages"])
    return StreamAccumulator(call["vendor"], call["messages"])
