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
  - anthropic:    `messages.create`, `beta.messages.create` (MCP connector),
                  and the `messages.stream()` / `beta.messages.stream()` helpers
  - google:       google-genai `models.generate_content`,
                  `models.generate_content_stream`
  - mistral:      mistralai `chat.complete` / `chat.complete_async`,
                  `chat.stream` / `chat.stream_async`
  - cohere:       `chat` and `chat_stream` on v1 (`Client`) and v2
                  (`ClientV2`) clients, through the raw clients they call, so
                  clients created before `instrument()` are covered too
  - bedrock:      boto3 `bedrock-runtime` `converse`, `converse_stream`,
                  `invoke_model` and `invoke_model_with_response_stream`, via
                  botocore's `BaseClient._make_api_call`. `invoke_model` usage
                  comes from response headers; its body stream is never read.
  - mcp:          the official `mcp` package's `ClientSession.call_tool`
                  (client-side MCP tool calls; see `mcp_instrumentation`).
                  Names, outcome and latency only, sent to `/ingest/mcp`;
                  tool arguments and results are never read.
Missing packages, or methods an installed version lacks, are skipped.
openai and anthropic are imported and patched by `instrument()`; the other
SDKs are patched when the app imports them (at once if already imported),
through an import hook that `uninstrument()` removes.
Streams (`stream=True` or a streaming method) are wrapped so they emit one
event when they end or are closed; chunks reach the caller unchanged. A
stream left unfinished and unclosed (a `break` out of the loop) emits its
event, marked `stream_abandoned`, when it is garbage-collected. cohere's
`chat_stream` is a generator that closes its event stream itself when it is
closed or collected, so a `break` out of it is an early close, as a
`close()` of an OpenAI or Anthropic stream is (`stream_completed=False`,
not `stream_abandoned`). Events emitted from the garbage collector or from
that close are only enqueued (no network I/O there): they go out with the
next flush, or with the bounded flush at interpreter exit
(`exit_flush_timeout`, `PS_EXIT_FLUSH_TIMEOUT`, off with
`PS_DISABLE_EXIT_FLUSH=1`).

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
import itertools
import json
import logging
import os
import sys
import threading
import time
import weakref
from types import SimpleNamespace
from typing import Any

from prompt_shields import mcp_instrumentation
from prompt_shields.providers import (
    ADAPTERS,
    AzureOpenAIAdapter,
    ProviderAdapter,
    _field,
    _int,
    _str,
    get_adapter,
    mcp_tool_call,
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
    register_exit_flush,
    set_exit_flush_timeout,
    unregister_exit_flush,
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
    ("anthropic", "anthropic.resources.beta.messages.messages", "Messages",
     "create", "messages"),
    ("anthropic", "anthropic.resources.beta.messages.messages", "AsyncMessages",
     "create", "messages"),
    # `messages.stream()` helpers: they post directly, not through `create`.
    ("anthropic", "anthropic.resources.messages", "Messages", "stream",
     "messages_stream"),
    ("anthropic", "anthropic.resources.messages", "AsyncMessages", "stream",
     "messages_stream"),
    ("anthropic", "anthropic.resources.beta.messages.messages", "Messages",
     "stream", "messages_stream"),
    ("anthropic", "anthropic.resources.beta.messages.messages", "AsyncMessages",
     "stream", "messages_stream"),
    ("google", "google.genai.models", "Models", "generate_content", "gemini"),
    ("google", "google.genai.models", "AsyncModels", "generate_content", "gemini"),
    ("google", "google.genai.models", "Models", "generate_content_stream",
     "gemini_stream"),
    ("google", "google.genai.models", "AsyncModels", "generate_content_stream",
     "gemini_stream"),
    # mistralai >= 2 lives in `mistralai.client`; 1.x in `mistralai.chat`.
    ("mistral", "mistralai.client.chat", "Chat", "complete", "mistral"),
    ("mistral", "mistralai.client.chat", "Chat", "complete_async", "mistral"),
    ("mistral", "mistralai.chat", "Chat", "complete", "mistral"),
    ("mistral", "mistralai.chat", "Chat", "complete_async", "mistral"),
    ("mistral", "mistralai.client.chat", "Chat", "stream", "mistral_stream"),
    ("mistral", "mistralai.client.chat", "Chat", "stream_async", "mistral_stream"),
    ("mistral", "mistralai.chat", "Chat", "stream", "mistral_stream"),
    ("mistral", "mistralai.chat", "Chat", "stream_async", "mistral_stream"),
    # cohere: the raw clients, which the public methods call through
    # `self._raw_client`. `Client.__init__` stores bound public methods on
    # the instance, so patching those would miss clients created before
    # `instrument()`. The public methods below are a fallback for versions
    # without raw clients (see `_SUPERSEDED_BY`).
    ("cohere", "cohere.raw_base_client", "RawBaseCohere", "chat",
     "cohere_v1_raw"),
    ("cohere", "cohere.raw_base_client", "AsyncRawBaseCohere", "chat",
     "cohere_v1_raw"),
    ("cohere", "cohere.v2.raw_client", "RawV2Client", "chat", "cohere_v2_raw"),
    ("cohere", "cohere.v2.raw_client", "AsyncRawV2Client", "chat",
     "cohere_v2_raw"),
    ("cohere", "cohere.raw_base_client", "RawBaseCohere", "chat_stream",
     "cohere_v1_raw_stream"),
    ("cohere", "cohere.raw_base_client", "AsyncRawBaseCohere", "chat_stream",
     "cohere_v1_raw_stream"),
    ("cohere", "cohere.v2.raw_client", "RawV2Client", "chat_stream",
     "cohere_v2_raw_stream"),
    ("cohere", "cohere.v2.raw_client", "AsyncRawV2Client", "chat_stream",
     "cohere_v2_raw_stream"),
    ("cohere", "cohere.base_client", "BaseCohere", "chat", "cohere_v1"),
    ("cohere", "cohere.base_client", "AsyncBaseCohere", "chat", "cohere_v1"),
    ("cohere", "cohere.v2.client", "V2Client", "chat", "cohere_v2"),
    ("cohere", "cohere.v2.client", "AsyncV2Client", "chat", "cohere_v2"),
    ("cohere", "cohere.base_client", "BaseCohere", "chat_stream",
     "cohere_v1_stream"),
    ("cohere", "cohere.base_client", "AsyncBaseCohere", "chat_stream",
     "cohere_v1_stream"),
    ("cohere", "cohere.v2.client", "V2Client", "chat_stream", "cohere_v2_stream"),
    ("cohere", "cohere.v2.client", "AsyncV2Client", "chat_stream",
     "cohere_v2_stream"),
    # boto3 builds client classes per service at runtime; every operation
    # goes through this one method, which passes non-Bedrock calls through.
    ("bedrock", "botocore.client", "BaseClient", "_make_api_call", "bedrock"),
    # Client-side MCP tool calls (mcp_instrumentation). `initialize` only
    # remembers the server's name for mcp 1.x, which has no `server_info`.
    ("mcp", "mcp.client.session", "ClientSession", "initialize", "mcp_initialize"),
    ("mcp", "mcp.client.session", "ClientSession", "call_tool", "mcp_call_tool"),
)

# Patched for either vendor: Azure clients reuse the OpenAI resource classes.
_TARGET_VENDORS = {"openai": ("openai", "azure_openai")}

# Vendors patched when their SDK is first imported (at once if it already
# is), so `instrument()` does not import them: together they add ~1s to
# startup. openai and anthropic are imported and patched up front.
_LAZY_VENDORS = ("google", "mistral", "cohere", "bedrock", "mcp")

# Largest batch the MCP sender posts (the collector accepts up to 1000).
_MCP_MAX_BATCH = 500

# (module, class) -> the (module, class) that replaces it when present: the
# public cohere clients are only patched when there is no raw client (the
# public modules import the raw ones, so these are loaded first).
_SUPERSEDED_BY = {
    ("cohere.base_client", "BaseCohere"): ("cohere.raw_base_client",
                                           "RawBaseCohere"),
    ("cohere.base_client", "AsyncBaseCohere"): ("cohere.raw_base_client",
                                                "AsyncRawBaseCohere"),
    ("cohere.v2.client", "V2Client"): ("cohere.v2.raw_client", "RawV2Client"),
    ("cohere.v2.client", "AsyncV2Client"): ("cohere.v2.raw_client",
                                            "AsyncRawV2Client"),
}

# Raw (HTTP-level) APIs -> the API whose call details they share. Their
# results hold the SDK's response in `.data`; a streaming one is a context
# manager that sends the request on enter and yields such a result, whose
# `.data` is the event stream.
_HTTP_RESPONSE_APIS = {"cohere_v1_raw": "cohere_v1", "cohere_v2_raw": "cohere_v2"}
_STREAM_CONTEXT_APIS = {"cohere_v1_raw_stream": "cohere_v1",
                        "cohere_v2_raw_stream": "cohere_v2"}

# Streaming operations -> the response key holding the botocore EventStream.
_BEDROCK_STREAMS = {"ConverseStream": "stream",
                    "InvokeModelWithResponseStream": "body"}
_BEDROCK_OPERATIONS = ("Converse", "InvokeModel", *_BEDROCK_STREAMS)

# Vendors whose stream methods return a generator that sends the request on
# the first read, so a failed request raises from the stream, not the call.
_LAZY_STREAM_VENDORS = ("google", "cohere")

# Methods that always stream -> the API whose call details they share.
_STREAM_ONLY_APIS = {
    "messages_stream": "messages",
    "gemini_stream": "gemini",
    "mistral_stream": "mistral",
    "cohere_v1_stream": "cohere_v1",
    "cohere_v2_stream": "cohere_v2",
}

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

# Reentrant: a module patched on import may be imported while it is held.
_lock = threading.RLock()
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

    A framework stream that calls the provider only once it is iterated
    (LlamaIndex) binds its call to the stream's generator with
    `defer_framework_call`: from then on only a patched call made while that
    generator runs can claim it, whichever context iterates it.
    """

    __slots__ = ("captured", "finished", "frame", "framework", "model",
                 "previous", "seq", "use_case_hint")

    def __init__(self, framework: str, use_case_hint: str | None = None,
                 model: str | None = None,
                 previous: "FrameworkCall | None" = None):
        self.framework = framework
        self.use_case_hint = use_case_hint
        self.model = model
        self.previous = previous
        self.captured = False
        self.finished = False
        self.frame = None  # the generator frame of a deferred call
        self.seq = next(_framework_call_seq)


_framework_call_seq = itertools.count()


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


# Deferred calls (see `FrameworkCall`) not yet forgotten, by id of their
# generator frame (kept alive by `fc.frame`, so the id stays unique). Kept
# as an index so a claim costs one walk up the stack, however many streams
# are pending.
_deferred_calls: dict[int, FrameworkCall] = {}
_MAX_STACK_DEPTH = 300


def defer_framework_call(fc: FrameworkCall, generator: Any) -> bool:
    """Bind `fc` to a (sync or async) generator that makes the call lazily."""
    frame = getattr(generator, "gi_frame", None) or getattr(
        generator, "ag_frame", None)
    if frame is None:
        return False
    with _claim_lock:
        if fc.captured or fc.finished or fc.frame is not None:
            return False
        fc.frame = frame
        _deferred_calls[id(frame)] = fc
    return True


def forget_framework_call(fc: FrameworkCall) -> None:
    """Drop a finished deferred call (and its frame reference)."""
    with _claim_lock:
        _forget(fc)


def _forget(fc: FrameworkCall) -> None:
    # Caller holds `_claim_lock`.
    if fc.frame is not None and _deferred_calls.get(id(fc.frame)) is fc:
        del _deferred_calls[id(fc.frame)]
    fc.frame = None


def _deferred_on_stack() -> FrameworkCall | None:
    """The innermost unclaimed deferred call whose generator is running.

    Caller holds `_claim_lock`.
    """
    frame = sys._getframe(1)
    for _ in range(_MAX_STACK_DEPTH):
        if frame is None:
            break
        fc = _deferred_calls.get(id(frame))
        if fc is not None:
            if fc.finished:
                _forget(fc)
            elif not fc.captured:  # a claim given back is claimable again
                return fc
        frame = frame.f_back
    return None


def _claimable_framework_call() -> FrameworkCall | None:
    """The call a provider (or nested framework) call made here belongs to.

    Caller holds `_claim_lock`.
    """
    fc = _FRAMEWORK_CALL.get()
    while fc is not None and (fc.captured or fc.frame is not None) \
            and not fc.finished:
        fc = fc.previous
    if fc is None or fc.captured or fc.finished or fc.frame is not None:
        fc = None
    if _deferred_calls:
        deferred = _deferred_on_stack()
        # The newer of the two is the innermost framework call.
        if deferred is not None and (fc is None or deferred.seq > fc.seq):
            fc = deferred
    return fc


def _claim_framework_call(model: Any) -> FrameworkCall | None:
    with _claim_lock:
        fc = _claimable_framework_call()
        if fc is None:
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
                 pricing_table, vendors=None, azure_deployments=None,
                 collector_url=None, api_key=None):
        self.telemetry = telemetry
        self.collector_url = collector_url
        self.api_key = api_key
        self.mcp_telemetry: TelemetrySender | None = None
        self.vendors = vendors
        self.azure_adapter = AzureOpenAIAdapter(azure_deployments)
        self.atlas = atlas
        self.metadata = metadata
        self.scan_pii = scan_pii
        self.send_prompt_text = send_prompt_text
        self.pricing_table = pricing_table
        self.patches: list[tuple[type, str, Any, Any]] = []
        self.finder: _PatchOnImport | None = None

    def mcp_sender(self) -> TelemetrySender:
        """The `/ingest/mcp` sender, created on the first MCP tool call so
        apps that never use MCP get no extra client."""
        with _lock:
            if self.mcp_telemetry is None:
                self.mcp_telemetry = TelemetrySender(
                    self.collector_url,
                    self.api_key,
                    path="/ingest/mcp",
                    max_batch=_MCP_MAX_BATCH,
                    report_loss=False,
                )
                if _state is self:
                    register_exit_flush(self.mcp_telemetry)
            return self.mcp_telemetry


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
    exit_flush_timeout: float | None = None,
) -> bool:
    """Patch installed provider SDKs so every call emits a telemetry event.

    Unset arguments fall back to environment variables: `PS_API_KEY`,
    `PS_COLLECTOR_URL` (default http://localhost:8000), `PS_BUSINESS_UNIT`,
    `PS_USE_CASE`, `PS_OWNER`, `PS_DATA_CLASSIFICATION`, `PS_ENVIRONMENT`,
    `PS_CALLING_SERVICE`, `PS_ATLAS_URL`, `PS_ATLAS_API_KEY`.

    `vendors` limits instrumentation (e.g. `("openai",)`); default is all:
    openai, azure_openai, anthropic, google, mistral, cohere, bedrock, mcp
    (client-side MCP tool calls, when the `mcp` package is installed).
    "openai" also covers Azure clients (tagged azure_openai); select only
    "azure_openai" to instrument Azure without plain OpenAI.
    `azure_deployments` maps Azure deployment names to model ids for
    pricing, e.g. `{"prod-chat": "gpt-4o"}` (see `AzureOpenAIAdapter`).
    `exit_flush_timeout` bounds the best-effort flush of still-buffered
    events at interpreter exit, in seconds (default `PS_EXIT_FLUSH_TIMEOUT`,
    else 2; 0 turns it off, as does `PS_DISABLE_EXIT_FLUSH=1`).
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
            collector_url=ps_collector_url,
            api_key=ps_api_key,
        )

        lazy: dict[str, list[tuple]] = {}
        for target in _TARGETS:
            vendor, module_name = target[:2]
            covers = _TARGET_VENDORS.get(vendor, (vendor,))
            if vendors is not None and not any(v in vendors for v in covers):
                continue
            if vendor in _LAZY_VENDORS:
                lazy.setdefault(module_name, []).append(target)
                continue
            try:
                module = importlib.import_module(module_name)
            except Exception:  # noqa: BLE001, S112 - not installed or broken
                continue
            _patch(state, module, target)

        _state = state
        if exit_flush_timeout is not None:
            set_exit_flush_timeout(exit_flush_timeout)
        register_exit_flush(state.telemetry, state.atlas)
        in_progress = []
        if lazy:
            # Installed before looking at `sys.modules`, so a module imported
            # meanwhile (by another thread) is patched by one or the other.
            state.finder = _PatchOnImport(state, lazy)
            sys.meta_path.insert(0, state.finder)
            for module_name in list(lazy):
                module = sys.modules.get(module_name)
                if module is not None:
                    state.finder.patch(module_name, module)
                    if module_name in lazy and getattr(
                            getattr(module, "__spec__", None),
                            "_initializing", False):
                        in_progress.append(module_name)
        logger.debug(f"Instrumented {len(state.patches)} provider methods")

    # A module another thread was still running when the hook went in (found
    # before it, so not hooked): wait for that import to finish, then patch
    # it. Outside `_lock`, which that import may need for its own targets.
    for module_name in in_progress:
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001, S112 - its import failed
            continue
        state.finder.patch(module_name, module)
    return True


def _patch(state: _State, module: Any, target: tuple) -> bool:
    """Patch one target of an imported module. Returns False when its class
    is missing (a module still being imported, or another SDK version)."""
    vendor, module_name, class_name, method, api = target
    superseded = _SUPERSEDED_BY.get((module_name, class_name))
    if superseded is not None and getattr(
            sys.modules.get(superseded[0]), superseded[1], None) is not None:
        return True
    cls = getattr(module, class_name, None)
    if cls is None:
        return False
    original = cls.__dict__.get(method)
    if original is None or getattr(original, "__ps_original__", None):
        return True
    if vendor == "mcp":
        wrapper = mcp_instrumentation.make_wrapper(original, api, _mcp_state)
    else:
        wrapper = _make_wrapper(original, vendor, api)
    setattr(cls, method, wrapper)
    state.patches.append((cls, method, original, wrapper))
    return True


class _PatchOnImport:
    """Meta path finder that patches a module's targets when it is first
    imported: it takes the spec the finders after it return and puts a
    `_PatchingLoader` in front of its loader. Removed by `uninstrument()`.
    Fail-open: on any error the import goes ahead unpatched."""

    def __init__(self, state: _State, targets: dict[str, list[tuple]]):
        self._state = state
        self._targets = targets

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in self._targets:
            return None
        try:
            for finder in list(sys.meta_path):
                find_spec = getattr(finder, "find_spec", None)
                if finder is self or find_spec is None:
                    continue
                spec = find_spec(fullname, path, target)
                if spec is not None:
                    break
            else:
                return None
            if spec.loader is not None and hasattr(spec.loader, "exec_module"):
                spec.loader = _PatchingLoader(spec.loader, self, fullname)
            return spec
        except Exception as e:  # noqa: BLE001 - fail-open
            logger.debug(f"Instrumentation import hook error: {e}")
            return None

    def patch(self, fullname: str, module: Any) -> None:
        with _lock:
            if _state is not self._state:
                return  # uninstrumented meanwhile
            pending = []
            for target in self._targets.get(fullname, ()):
                try:
                    if not _patch(self._state, module, target):
                        pending.append(target)
                except Exception as e:  # noqa: BLE001 - fail-open
                    logger.debug(f"Instrumentation patch error: {e}")
            if pending:
                self._targets[fullname] = pending
            else:
                self._targets.pop(fullname, None)


class _PatchingLoader:
    """Runs a module with its own loader, then patches it. The module's
    `__loader__` and `__spec__.loader` are set back to that loader."""

    def __init__(self, loader: Any, finder: _PatchOnImport, fullname: str):
        self._loader = loader
        self._finder = finder
        self._fullname = fullname

    def __getattr__(self, name: str) -> Any:
        return getattr(self._loader, name)

    def create_module(self, spec):
        create = getattr(self._loader, "create_module", None)
        return create(spec) if create is not None else None

    def exec_module(self, module):
        try:
            self._loader.exec_module(module)
        finally:
            with contextlib.suppress(Exception):
                if getattr(module, "__loader__", None) is self:
                    module.__loader__ = self._loader
                spec = getattr(module, "__spec__", None)
                if getattr(spec, "loader", None) is self:
                    spec.loader = self._loader
        self._finder.patch(self._fullname, module)


def uninstrument() -> bool:
    """Restore the original provider methods. Returns False if not instrumented."""
    global _state
    with _lock:
        if _state is None:
            return False
        if _state.finder is not None:
            with contextlib.suppress(ValueError):
                sys.meta_path.remove(_state.finder)
        for cls, name, original, wrapper in reversed(_state.patches):
            # Only restore what is still ours; leave later patches alone.
            if cls.__dict__.get(name) is wrapper:
                setattr(cls, name, original)
        unregister_exit_flush(_state.telemetry, _state.atlas,
                              _state.mcp_telemetry)
        set_exit_flush_timeout(None)
        _state = None
        return True


# --- wrappers ----------------------------------------------------------------


def _mcp_state() -> "_State | None":
    """The state MCP tool calls report to, or None to pass them through."""
    if _SUPPRESSED.get():
        return None
    return _state


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
        helper = api == "messages_stream"  # returns a stream *manager*
        stream_key = _BEDROCK_STREAMS.get(args[0]) if api == "bedrock" else None
        http_response = api in _HTTP_RESPONSE_APIS
        stream_context = api in _STREAM_CONTEXT_APIS
        if http_response:
            api = _HTTP_RESPONSE_APIS[api]
            streaming = False
        elif stream_context:
            api = _STREAM_CONTEXT_APIS[api]
            streaming = True
        elif api in _STREAM_ONLY_APIS:
            api = _STREAM_ONLY_APIS[api]
            streaming = True
        else:
            streaming = stream_key is not None or call_kwargs.get("stream") is True
            if streaming and api not in _STREAMING_APIS and stream_key is None:
                return None  # e.g. mistral `complete(stream=True)`: no stream
        details = _DETAILS[api](resource, args, call_kwargs)
        upstream = details["upstream"]
        if vendor == "openai" and _is_azure(upstream):
            vendor = "azure_openai"
            # A client built with `azure_deployment=` calls that deployment,
            # whatever `model` says.
            details["model"] = (
                getattr(upstream, "_azure_deployment", None) or details["model"]
            )
        if state.vendors is not None and not (
            vendor in state.vendors
            # Azure clients were instrumented as "openai" before they had
            # their own tag; selecting "openai" still covers them.
            or (vendor == "azure_openai" and "openai" in state.vendors)
        ):
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
        framework = _claim_framework_call(model if isinstance(model, str) else None)
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
            "stream_helper": helper,
            "stream_context": stream_context,
            "http_response": http_response,
            "stream_key": stream_key,
            "hide_usage_chunk": hide_usage_chunk,
            "api_key": api_key if isinstance(api_key, str) and api_key else None,
            "framework": framework,
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
    if operation in ("Converse", "ConverseStream"):
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
        if call["stream_helper"]:
            return _observe_stream_manager(call, response)
        if call["stream_context"]:
            return _StreamContext(response, call)
        if call["stream_key"] is not None:
            # Bedrock: the EventStream sits in the response dict.
            key = call["stream_key"]
            response[key] = _observe_event_stream(call, response[key], start)
            return response
        if call["streaming"]:
            return _wrap_stream(call, response, start)
        latency_ms = int((time.monotonic() - start) * 1000)
        _send_sync(call, _event(call, response, latency_ms))
    except Exception as e:  # noqa: BLE001 - fail-open
        logger.debug(f"Instrumentation telemetry error: {e}")
    return response


async def _finish_async(call: dict, response: Any, start: float) -> Any:
    try:
        if call["streaming"]:
            return _wrap_stream(call, response, start, is_async=True)
        latency_ms = int((time.monotonic() - start) * 1000)
        await _send_async(call, _event(call, response, latency_ms))
    except Exception as e:  # noqa: BLE001 - fail-open
        logger.debug(f"Instrumentation telemetry error: {e}")
    return response


def _wrap_stream(call: dict, stream: Any, start: float,
                 is_async: bool = False, close_flush: bool = True) -> Any:
    """`ShieldsStream`, or `AsyncShieldsStream` for an async iterator (an
    async generator such as cohere's `chat_stream` is returned unawaited).
    `close_flush=False`: a `close()` enqueues the event, does not send it."""
    def on_abandon(adapter_fields, stream_fields, latency_ms):
        # Runs in the garbage collector: enqueued, sent by the next flush.
        _send_later(call, _stream_event(call, adapter_fields, stream_fields,
                                        latency_ms))

    options = {"hide_usage_chunk": call["hide_usage_chunk"],
               "on_abandon": on_abandon, "close_flush": close_flush}
    if call["vendor"] in _LAZY_STREAM_VENDORS:
        options["on_failed_start"] = lambda: _release_framework_call(call)
    if is_async or (hasattr(stream, "__aiter__") and not hasattr(stream, "__iter__")):
        async def on_finish_async(adapter_fields, stream_fields, latency_ms,
                                  flush=True):
            await _send_async(call, _stream_event(
                call, adapter_fields, stream_fields, latency_ms), flush=flush)

        return AsyncShieldsStream(stream, _accumulator(call), on_finish_async,
                                  start, **options)

    def on_finish(adapter_fields, stream_fields, latency_ms, flush=True):
        event = _stream_event(call, adapter_fields, stream_fields, latency_ms)
        if flush:
            _send_sync(call, event)
        else:
            _send_later(call, event)

    return ShieldsStream(stream, _accumulator(call), on_finish, start, **options)


# Observing in place: the caller keeps the SDK's own object, switched to a
# subclass of its class (same name) with the observing methods of one of the
# `_Observed*` classes below, so `isinstance`, `type(...).__name__` and every
# attribute, private ones included, stay as they were. An object that cannot
# be switched is wrapped instead.
_OBSERVING_CLASSES: dict[tuple[type, type], type] = {}
_observing_lock = threading.Lock()


def _observe_in_place(obj: Any, mixin: type, state: Any) -> bool:
    """Switch `obj` to a subclass of its class with `mixin`'s methods (copied,
    not inherited, so the object layout is unchanged), `state` kept on it."""
    cls = type(obj)
    try:
        with _observing_lock:
            sub = _OBSERVING_CLASSES.get((cls, mixin))
            if sub is None:
                methods = {k: v for k, v in vars(mixin).items()
                           if inspect.isfunction(v)}
                sub = type(cls.__name__, (cls,), {
                    **methods,
                    "__module__": cls.__module__,
                    "__qualname__": cls.__qualname__,
                    "__doc__": cls.__doc__,
                })
                _OBSERVING_CLASSES[(cls, mixin)] = sub
        obj.__dict__["_ps_observed"] = state
        obj.__class__ = sub
        return True
    except Exception as e:  # noqa: BLE001 - fail-open: wrap instead
        logger.debug(f"Instrumentation in-place observe error: {e}")
        with contextlib.suppress(Exception):
            obj.__dict__.pop("_ps_observed", None)
        return False


class _Upstream:
    """Iteration and `close()` of an object observed in place, through its
    original class (its own methods now go through the observer)."""

    def __init__(self, obj: Any, cls: type):
        self._obj = obj
        self._cls = cls

    def __iter__(self):
        return self._cls.__iter__(self._obj)

    def close(self) -> None:
        close = getattr(self._cls, "close", None)
        if close is not None:
            close(self._obj)


class _ObservedEventStream:
    """In front of botocore's `EventStream`: iteration and `close()` go
    through the `ShieldsStream`."""

    def __iter__(self):
        return self.__dict__["_ps_observed"]

    def close(self) -> None:
        self.__dict__["_ps_observed"].close()


def _observe_event_stream(call: dict, stream: Any, start: float) -> Any:
    """Bedrock's EventStream, observed in place (or wrapped)."""
    observed = _wrap_stream(call, _Upstream(stream, type(stream)), start)
    if _observe_in_place(stream, _ObservedEventStream, observed):
        return stream
    observed.discard()
    return _wrap_stream(call, stream, start)


def _observe_helper_stream(call: dict, stream: Any, start: float,
                           is_async: bool) -> Any:
    """Wrap the raw event stream where Anthropic's `MessageStream` reads it
    (`_raw_stream`, before its first read), so `text_stream`,
    `get_final_message()` and plain iteration are all observed, and closing
    it emits the event. If that attribute is missing (a different SDK
    version) the stream is returned unobserved."""
    wrapped = None
    try:
        raw = stream.__dict__["_raw_stream"]
        wrapped = _wrap_stream(call, raw, start, is_async)
        stream._raw_stream = wrapped
    except Exception as e:  # noqa: BLE001 - fail-open
        logger.debug(f"Instrumentation stream helper error: {e}")
        if wrapped is not None:
            wrapped.discard()
        _release_framework_call(call)  # no event: a framework may report it
    return stream


class _ObservedStreamManager:
    """In front of Anthropic's `MessageStreamManager`: the request is made on
    enter, and the `MessageStream` it returns is observed."""

    def __enter__(self):
        call = self.__dict__["_ps_observed"]
        call["entered"] = True
        start = time.monotonic()
        try:
            stream = super(type(self), self).__enter__()
        except BaseException:
            _release_framework_call(call)
            raise
        return _observe_helper_stream(call, stream, start, is_async=False)


class _ObservedAsyncStreamManager:
    """In front of Anthropic's `AsyncMessageStreamManager`."""

    async def __aenter__(self):
        call = self.__dict__["_ps_observed"]
        call["entered"] = True
        start = time.monotonic()
        try:
            stream = await super(type(self), self).__aenter__()
        except BaseException:
            _release_framework_call(call)
            raise
        return _observe_helper_stream(call, stream, start, is_async=True)


def _observe_stream_manager(call: dict, manager: Any) -> Any:
    """Anthropic's stream manager, observed in place (or wrapped)."""
    is_async = not hasattr(manager, "__enter__")
    mixin = _ObservedAsyncStreamManager if is_async else _ObservedStreamManager
    if not _observe_in_place(manager, mixin, call):
        manager = _MessageStreamManager(manager, call)
    if call["framework"] is not None:
        # A manager never entered makes no request: give its claim back.
        try:
            weakref.finalize(manager, _release_unentered, call).atexit = False
        except Exception as e:  # noqa: BLE001 - fail-open
            logger.debug(f"Instrumentation stream manager error: {e}")
    return manager


def _release_unentered(call: dict) -> None:
    if not call.get("entered"):
        _release_framework_call(call)


class _MessageStreamManager:
    """Stands in for an Anthropic (Async)MessageStreamManager that cannot be
    observed in place."""

    def __init__(self, manager: Any, call: dict):
        self._manager = manager
        self._call = call

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._manager, name)

    def __enter__(self):
        self._call["entered"] = True
        start = time.monotonic()
        try:
            stream = self._manager.__enter__()
        except BaseException:
            _release_framework_call(self._call)
            raise
        return _observe_helper_stream(self._call, stream, start, is_async=False)

    def __exit__(self, *exc):
        return self._manager.__exit__(*exc)

    async def __aenter__(self):
        self._call["entered"] = True
        start = time.monotonic()
        try:
            stream = await self._manager.__aenter__()
        except BaseException:
            _release_framework_call(self._call)
            raise
        return _observe_helper_stream(self._call, stream, start, is_async=True)

    async def __aexit__(self, *exc):
        return await self._manager.__aexit__(*exc)


class _StreamContext:
    """Stands in for a raw streaming context manager (cohere's raw
    `chat_stream`): the request is sent on enter, and the event stream of
    the response it yields (`.data`) is observed.

    Leaving the context ends that stream: one not read to the end is an
    early close (`stream_completed=False`). cohere's public `chat_stream`
    generator iterates inside this context, so it is left (and, sync, the
    stream closed by `yield from`) when that generator is closed or
    garbage-collected: the event is then only enqueued, never sent there.
    A context never entered made no request: its framework claim is given
    back when it is collected."""

    def __init__(self, manager: Any, call: dict):
        self._manager = manager
        self._call = call
        self._wrapped = None
        if call["framework"] is not None:
            try:
                weakref.finalize(self, _release_unentered, call).atexit = False
            except Exception as e:  # noqa: BLE001 - fail-open
                logger.debug(f"Instrumentation stream context error: {e}")

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._manager, name)

    def __enter__(self):
        self._call["entered"] = True
        start = time.monotonic()
        try:
            response = self._manager.__enter__()
        except BaseException:
            _release_framework_call(self._call)
            raise
        return self._observe(response, start, is_async=False)

    def __exit__(self, *exc):
        try:
            return self._manager.__exit__(*exc)
        finally:
            wrapped, self._wrapped = self._wrapped, None
            if wrapped is not None:
                result = wrapped._finish(completed=False, flush=False)
                if inspect.iscoroutine(result):  # an async stream: drop it
                    result.close()

    async def __aenter__(self):
        self._call["entered"] = True
        start = time.monotonic()
        try:
            response = await self._manager.__aenter__()
        except BaseException:
            _release_framework_call(self._call)
            raise
        return self._observe(response, start, is_async=True)

    async def __aexit__(self, *exc):
        try:
            return await self._manager.__aexit__(*exc)
        finally:
            wrapped, self._wrapped = self._wrapped, None
            if wrapped is not None:
                await wrapped._finish(completed=False, flush=False)

    def _observe(self, response: Any, start: float, is_async: bool) -> Any:
        wrapped = None
        try:
            wrapped = _wrap_stream(self._call, response._data, start, is_async,
                                   close_flush=False)
            response._data = wrapped
            self._wrapped = wrapped
        except Exception as e:  # noqa: BLE001 - fail-open
            logger.debug(f"Instrumentation stream context error: {e}")
            if wrapped is not None:
                wrapped.discard()
            _release_framework_call(self._call)
        return response


# --- events ------------------------------------------------------------------


def build_event(state: _State, *, vendor: str, model: Any, messages: list,
                latency_ms: int, adapter_fields: dict,
                api_key: str | None = None,
                adapter: ProviderAdapter | None = None) -> dict:
    """A `ShieldsClient`-shaped event from already extracted usage fields."""
    # Imported here: client.py imports this module for suppress_instrumentation.
    from prompt_shields.client import ShieldsClient, _fingerprint

    # Duck-typed stand-in for a ShieldsClient: the attributes _build_event reads.
    shim = SimpleNamespace(
        _vendor=vendor,
        # Only pricing_model() is used (fields are already extracted); a
        # vendor without an adapter is priced by the requested model.
        _adapter=adapter or ADAPTERS.get(vendor) or ProviderAdapter(),
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
    adapter = call.get("adapter") or get_adapter(call["vendor"])
    if adapter_fields is None:
        if call.get("http_response"):
            response = getattr(response, "data", None)
        adapter_fields = (
            _extract_responses(response) if call["api"] == "responses"
            else adapter.extract(response)
        )
    event = build_event(
        call["state"],
        vendor=call["vendor"],
        adapter=adapter,
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


def _send_later(call: dict, event: dict) -> None:
    """Enqueue without sending (no I/O): it goes out with the next flush."""
    state = call["state"]
    state.telemetry.enqueue(event)
    if state.atlas is not None:
        state.atlas.enqueue(build_atlas_event(event, call["messages"]))


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
    tool_calls: list[dict] = []
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) == "function_call":
            tool_calls.append({"name": getattr(item, "name", None), "type": "function"})
        elif (mcp := mcp_tool_call(item)) is not None:
            tool_calls.append(mcp)
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
        "service_tier": _str(getattr(response, "service_tier", None)),
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
            self._update_pricing(fields)
        elif etype == "response.output_text.delta":
            delta = getattr(event, "delta", None)
            if isinstance(delta, str):
                self.output_chars += len(delta)
        elif etype == "response.output_item.added":
            item = getattr(event, "item", None)
            if getattr(item, "type", None) == "function_call":
                self._tool_calls[len(self._tool_calls)] = getattr(item, "name", None)
        elif etype == "response.output_item.done":
            # MCP calls are recorded once complete: the item then carries the
            # server label and tool name, and `added` would double count.
            mcp = mcp_tool_call(getattr(event, "item", None))
            if mcp is not None:
                self._mcp_calls.append(mcp)


def _accumulator(call: dict) -> StreamAccumulator:
    # Azure OpenAI streams are OpenAI chunks.
    vendor = "openai" if call["vendor"] == "azure_openai" else call["vendor"]
    if call["api"] == "responses":
        return _ResponsesAccumulator(vendor, call["messages"])
    return StreamAccumulator(vendor, call["messages"])
