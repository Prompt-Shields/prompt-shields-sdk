"""Auto-instrumentation — emit telemetry from unmodified provider SDK calls.

    import prompt_shields
    prompt_shields.instrument(ps_api_key="ps_...")   # once, at startup

    client = openai.OpenAI()                          # unchanged app code
    client.chat.completions.create(model=..., messages=...)  # -> one event

`instrument()` patches the official SDK resource classes, sync and async:
  - openai:       `chat.completions.create`, `responses.create` (if present)
  - azure_openai: the same methods, called on an `AzureOpenAI` /
                  `AsyncAzureOpenAI` client (deployment names are priced
                  through `azure_deployments`)
  - anthropic:    `messages.create`
  - google:       google-genai `models.generate_content`
  - mistral:      mistralai `chat.complete` / `chat.complete_async`
  - cohere:       `chat` on v1 (`Client`) and v2 (`ClientV2`) clients
  - bedrock:      boto3 `bedrock-runtime` `converse` and `invoke_model`, via
                  botocore's `BaseClient._make_api_call`. `invoke_model` usage
                  comes from response headers; its body stream is never read.
Missing packages, or methods an installed version lacks, are skipped.
Streams are wrapped for openai, azure_openai and anthropic only; the other
vendors' streaming methods (`generate_content_stream`, `chat.stream`,
`chat_stream`, `converse_stream`, ...) are not patched yet.

Guarantees:
  - Idempotent: a second `instrument()` is a no-op and returns False.
  - Reversible: `uninstrument()` restores the original methods.
  - Fail-open: telemetry errors are logged and swallowed; the upstream
    response (or exception) is what the caller gets.
  - No double events: calls made through `ShieldsClient` / `AsyncShieldsClient`
    run inside `suppress_instrumentation()` and are left to those clients.

Events are built by the same code as `ShieldsClient` (`_build_event`) and
streams are wrapped by the same `ShieldsStream` / `AsyncShieldsStream`.
Discovery metadata comes from arguments or `PS_*` environment variables.
"""

import contextlib
import contextvars
import functools
import importlib
import inspect
import json
import logging
import os
import threading
import time
from types import SimpleNamespace
from typing import Any

from prompt_shields.providers import (
    AzureOpenAIAdapter,
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

# (vendor, module, class, method, api). `api` selects how the call's model,
# messages and response are read.
_TARGETS = (
    ("openai", "openai.resources.chat.completions", "Completions", "create", "chat"),
    ("openai", "openai.resources.chat.completions", "AsyncCompletions", "create",
     "chat"),
    ("openai", "openai.resources.responses", "Responses", "create", "responses"),
    ("openai", "openai.resources.responses", "AsyncResponses", "create",
     "responses"),
    ("anthropic", "anthropic.resources.messages", "Messages", "create", "messages"),
    ("anthropic", "anthropic.resources.messages", "AsyncMessages", "create",
     "messages"),
    ("google", "google.genai.models", "Models", "generate_content", "gemini"),
    ("google", "google.genai.models", "AsyncModels", "generate_content", "gemini"),
    # mistralai >= 2 lives in `mistralai.client`; 1.x in `mistralai.chat`.
    ("mistral", "mistralai.client.chat", "Chat", "complete", "mistral"),
    ("mistral", "mistralai.client.chat", "Chat", "complete_async", "mistral"),
    ("mistral", "mistralai.chat", "Chat", "complete", "mistral"),
    ("mistral", "mistralai.chat", "Chat", "complete_async", "mistral"),
    ("cohere", "cohere.base_client", "BaseCohere", "chat", "cohere_v1"),
    ("cohere", "cohere.base_client", "AsyncBaseCohere", "chat", "cohere_v1"),
    ("cohere", "cohere.v2.client", "V2Client", "chat", "cohere_v2"),
    ("cohere", "cohere.v2.client", "AsyncV2Client", "chat", "cohere_v2"),
    # boto3 builds client classes per service at runtime; every operation
    # goes through this one method, which passes non-Bedrock calls through.
    ("bedrock", "botocore.client", "BaseClient", "_make_api_call", "bedrock"),
)

# Patched for either vendor: Azure clients reuse the OpenAI resource classes.
_TARGET_VENDORS = {"openai": ("openai", "azure_openai")}

_BEDROCK_OPERATIONS = ("Converse", "InvokeModel")

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
                 pricing_table, vendors=None, azure_deployments=None):
        self.telemetry = telemetry
        self.vendors = vendors
        self.azure_adapter = AzureOpenAIAdapter(azure_deployments)
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
    azure_deployments: dict[str, str] | None = None,
) -> bool:
    """Patch installed provider SDKs so every call emits a telemetry event.

    Unset arguments fall back to environment variables: `PS_API_KEY`,
    `PS_COLLECTOR_URL` (default http://localhost:8000), `PS_BUSINESS_UNIT`,
    `PS_USE_CASE`, `PS_OWNER`, `PS_DATA_CLASSIFICATION`, `PS_ENVIRONMENT`,
    `PS_CALLING_SERVICE`, `PS_ATLAS_URL`, `PS_ATLAS_API_KEY`.

    `vendors` limits instrumentation (e.g. `("openai",)`); default is all:
    openai, azure_openai, anthropic, google, mistral, cohere, bedrock.
    `azure_deployments` maps Azure deployment names to model ids for
    pricing, e.g. `{"prod-chat": "gpt-4o"}` (see `AzureOpenAIAdapter`).
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
            vendors=frozenset(vendors) if vendors is not None else None,
            azure_deployments=azure_deployments,
        )

        for vendor, module_name, class_name, method, api in _TARGETS:
            covers = _TARGET_VENDORS.get(vendor, (vendor,))
            if vendors is not None and not any(v in vendors for v in covers):
                continue
            try:
                module = importlib.import_module(module_name)
            except Exception:  # noqa: BLE001, S112 - not installed or broken
                continue
            cls = getattr(module, class_name, None)
            original = cls.__dict__.get(method) if cls is not None else None
            if original is None or getattr(original, "__ps_original__", None):
                continue
            wrapper = _make_wrapper(original, vendor, api)
            setattr(cls, method, wrapper)
            state.patches.append((cls, method, original, wrapper))

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
        response = original(self, *args, **call["kwargs"])
        if inspect.isawaitable(response):
            return _await_and_finish(call, response, start)
        return _finish_sync(call, response, start)

    wrapper.__ps_original__ = original
    return wrapper


async def _await_and_finish(call: dict, awaitable: Any, start: float) -> Any:
    return await _finish_async(call, await awaitable, start)


def _prepare(resource: Any, vendor: str, api: str, args: tuple,
             kwargs: dict) -> dict | None:
    """Per-call context, or None to pass the call through untouched."""
    state = _state
    if state is None or _SUPPRESSED.get():
        return None
    # Only Bedrock's `_make_api_call(operation_name, api_params)` takes
    # positional arguments. Filter it before any other work: every boto3
    # call, of every service, goes through it.
    if api == "bedrock":
        if (
            len(args) != 2
            or kwargs
            or args[0] not in _BEDROCK_OPERATIONS
            or not isinstance(args[1], dict)
            or _bedrock_service(resource) != "bedrock-runtime"
        ):
            return None
    elif args:
        return None
    try:
        headers = kwargs.get("extra_headers") or {}
        if any(str(k).lower() == _RAW_RESPONSE_HEADER for k in headers):
            return None
        call_kwargs = dict(kwargs)
        streaming = call_kwargs.get("stream") is True
        if streaming and api not in _STREAMING_APIS:
            return None  # e.g. mistral `complete(stream=True)`: not wrapped yet
        details = _DETAILS[api](resource, args, call_kwargs)
        upstream = details["upstream"]
        if vendor == "openai" and _is_azure(upstream):
            vendor = "azure_openai"
            # A client built with `azure_deployment=` calls that deployment,
            # whatever `model` says.
            details["model"] = (
                getattr(upstream, "_azure_deployment", None) or details["model"]
            )
        if state.vendors is not None and vendor not in state.vendors:
            return None
        hide_usage_chunk = False
        if (
            streaming
            and api == "chat"
            and upstream_accepts_stream_options(upstream)
        ):
            hide_usage_chunk = request_openai_stream_usage(call_kwargs)
        api_key = details["api_key"]
        model = details["model"]
        return {
            "state": state,
            "vendor": vendor,
            "adapter": (
                state.azure_adapter if vendor == "azure_openai"
                else get_adapter(vendor)
            ),
            "api": api,
            "kwargs": call_kwargs,
            "model": model if isinstance(model, str) else None,
            "messages": details["messages"],
            "streaming": streaming,
            "hide_usage_chunk": hide_usage_chunk,
            "api_key": api_key if isinstance(api_key, str) and api_key else None,
        }
    except Exception as e:  # noqa: BLE001 - fail-open
        logger.debug(f"Instrumentation prepare error: {e}")
        return None


def _is_azure(upstream: Any) -> bool:
    return any(
        c.__name__ == "BaseAzureClient" and c.__module__.startswith("openai")
        for c in type(upstream).__mro__
    )


def _bedrock_service(client: Any) -> str | None:
    try:
        return client.meta.service_model.service_name
    except Exception:  # noqa: BLE001 - not a botocore client we understand
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


# --- per-API call details -----------------------------------------------------
#
# Each returns {"upstream", "model", "messages", "api_key"}. For the vendors
# added after OpenAI and Anthropic, `messages` are flattened to dicts with a
# string `content`, the shape `_build_event` scans for PII and joins for
# `send_prompt_text`; non-text parts are skipped.


def _text(content: Any) -> str | None:
    """Text of a message content: a string, or the text parts of a list of
    parts (strings, `{"text": ...}` dicts or objects with `.text`)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        return None
    texts = []
    for part in content:
        text = part if isinstance(part, str) else _field(part, "text")
        if isinstance(text, str):
            texts.append(text)
    return " ".join(texts) if texts else None


def _flat(role: Any, content: Any) -> dict | None:
    text = _text(content)
    if text is None:
        return None
    return {"role": role if isinstance(role, str) else None, "content": text}


def _flat_messages(items: Any, content_key: str = "content") -> list:
    """Flatten chat messages (dicts or SDK objects with `role` and content)."""
    if not isinstance(items, (list, tuple)):
        return []
    out = []
    for item in items:
        msg = _flat(_field(item, "role"), _field(item, content_key))
        if msg is not None:
            out.append(msg)
    return out


def _openai_details(resource: Any, args: tuple, kwargs: dict, api: str) -> dict:
    upstream = getattr(resource, "_client", None)
    return {
        "upstream": upstream,
        "model": kwargs.get("model"),
        "messages": _messages(api, kwargs),
        "api_key": getattr(upstream, "api_key", None),
    }


def _gemini_turn(item: Any, role: str) -> dict | None:
    """A google-genai content: a string, a Part, or a Content (`parts`)."""
    if isinstance(item, str):
        return _flat(role, item)
    parts = _field(item, "parts")
    if parts is None:
        return _flat(role, [item])  # a Part
    return _flat(_field(item, "role") or role, parts)


def _gemini_details(resource: Any, args: tuple, kwargs: dict) -> dict:
    messages = []
    system = _field(kwargs.get("config"), "system_instruction")
    contents = kwargs.get("contents")
    for items, role in ((system, "system"), (contents, "user")):
        if items is None:
            continue
        for item in items if isinstance(items, (list, tuple)) else [items]:
            msg = _gemini_turn(item, role)
            if msg is not None:
                if role == "system":
                    msg["role"] = "system"
                messages.append(msg)
    api_client = getattr(resource, "_api_client", None)
    return {
        "upstream": api_client,
        "model": kwargs.get("model"),
        "messages": messages,
        "api_key": getattr(api_client, "api_key", None),
    }


def _mistral_details(resource: Any, args: tuple, kwargs: dict) -> dict:
    config = getattr(resource, "sdk_configuration", None)
    return {
        "upstream": None,
        "model": kwargs.get("model"),
        "messages": _flat_messages(kwargs.get("messages")),
        "api_key": _field(getattr(config, "security", None), "api_key"),
    }


def _cohere_api_key(resource: Any) -> Any:
    return getattr(getattr(resource, "_client_wrapper", None), "_token", None)


def _cohere_v1_details(resource: Any, args: tuple, kwargs: dict) -> dict:
    messages = []
    preamble = kwargs.get("preamble")
    if isinstance(preamble, str):
        messages.append({"role": "system", "content": preamble})
    messages.extend(_flat_messages(kwargs.get("chat_history"), "message"))
    message = kwargs.get("message")
    if isinstance(message, str):
        messages.append({"role": "user", "content": message})
    return {
        "upstream": None,
        "model": kwargs.get("model"),  # optional in v1
        "messages": messages,
        "api_key": _cohere_api_key(resource),
    }


def _cohere_v2_details(resource: Any, args: tuple, kwargs: dict) -> dict:
    return {
        "upstream": None,
        "model": kwargs.get("model"),
        "messages": _flat_messages(kwargs.get("messages")),
        "api_key": _cohere_api_key(resource),
    }


def _bedrock_details(resource: Any, args: tuple, kwargs: dict) -> dict:
    operation, params = args
    if operation == "Converse":
        messages = []
        system = _flat("system", params.get("system"))  # [{"text": ...}]
        if system is not None:
            messages.append(system)
        messages.extend(_flat_messages(params.get("messages")))
    else:
        messages = _bedrock_body_messages(params.get("body"))
    return {
        "upstream": None,
        "model": params.get("modelId"),
        "messages": messages,
        "api_key": None,  # AWS credentials; there is no key to fingerprint
    }


def _bedrock_body_messages(body: Any) -> list:
    """Messages from an `invoke_model` request body given as bytes or str
    (Anthropic, Nova, Llama or Titan shape). A file-like body is the
    caller's stream and is left unread."""
    if not isinstance(body, (bytes, bytearray, str)):
        return []
    try:
        data = json.loads(body)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    messages = []
    system = _flat("system", data.get("system"))
    if system is not None:
        messages.append(system)
    messages.extend(_flat_messages(data.get("messages")))
    for key in ("prompt", "inputText"):  # Llama, Titan
        if isinstance(data.get(key), str):
            messages.append({"role": "user", "content": data[key]})
    return messages


_DETAILS = {
    "chat": functools.partial(_openai_details, api="chat"),
    "responses": functools.partial(_openai_details, api="responses"),
    "messages": functools.partial(_openai_details, api="messages"),
    "gemini": _gemini_details,
    "mistral": _mistral_details,
    "cohere_v1": _cohere_v1_details,
    "cohere_v2": _cohere_v2_details,
    "bedrock": _bedrock_details,
}

# APIs whose `stream=True` responses are wrapped; elsewhere they pass through.
_STREAMING_APIS = ("chat", "responses", "messages")


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


def _event(call: dict, response: Any, latency_ms: int,
           adapter_fields: dict | None = None) -> dict:
    # Imported here: client.py imports this module for suppress_instrumentation.
    from prompt_shields.client import ShieldsClient, _fingerprint

    adapter = call.get("adapter") or get_adapter(call["vendor"])
    if adapter_fields is None:
        adapter_fields = (
            _extract_responses(response) if call["api"] == "responses"
            else adapter.extract(response)
        )
    state = call["state"]
    # Duck-typed stand-in for a ShieldsClient: the attributes _build_event reads.
    shim = SimpleNamespace(
        _vendor=call["vendor"],
        _adapter=adapter,
        _metadata=state.metadata,
        _scan_pii=state.scan_pii,
        _send_prompt_text=state.send_prompt_text,
        _pricing_table=state.pricing_table,
        _api_key_fingerprint=(
            _fingerprint(call["api_key"]) if call["api_key"] else None
        ),
    )
    return ShieldsClient._build_event(
        shim,
        model=call["model"],
        messages=call["messages"],
        response=response,
        latency_ms=latency_ms,
        adapter_fields=adapter_fields,
    )


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
    # Azure OpenAI streams are OpenAI chunks.
    vendor = "openai" if call["vendor"] == "azure_openai" else call["vendor"]
    if call["api"] == "responses":
        return _ResponsesAccumulator(vendor, call["messages"])
    return StreamAccumulator(vendor, call["messages"])
