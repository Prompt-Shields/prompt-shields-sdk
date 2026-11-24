"""OpenTelemetry spans for agent traces (#385). Optional:

    pip install "prompt-shields[opentelemetry]"

With ``opentelemetry-api`` installed, the SDK creates spans that follow the
OTel GenAI semantic conventions and join the active context, so one trace id
follows an agent turn from the app through every model and tool call, the
gateway and the collector (``GET /partner/v1/traces/{trace_id}``):

* ``ShieldsClient`` / ``ShieldsOpenAI`` / ``ShieldsAnthropic``: one CLIENT
  span ``chat {model}`` per call (``gen_ai.operation.name``,
  ``gen_ai.provider.name``, ``gen_ai.request.model``,
  ``gen_ai.response.model``, ``gen_ai.usage.input_tokens`` /
  ``output_tokens``, ``gen_ai.response.finish_reasons``, ``error.type``,
  ``gen_ai.conversation.id`` from ``ps_metadata["session_id"]``). The call's
  ``traceparent`` is sent with the request when it goes to a gateway on an
  internal host (loopback, private address, single-label or ``.internal`` /
  ``.local`` / ``.svc`` name); ``propagate_trace=True`` sends it to any
  ``base_url`` (a gateway on a public name), ``False`` never.
* :func:`tool_span` (context manager) and :func:`traced_tool` (decorator):
  an INTERNAL ``execute_tool {name}`` span per tool run.
* :func:`agent_span`: an ``invoke_agent {name}`` span around a turn.
* ``PromptShieldsCallbackHandler`` (LangChain): an ``invoke_agent`` span per
  outermost chain or agent run, a ``chat`` span per model run and an
  ``execute_tool`` span per tool run, parented by LangChain's run tree.
* :func:`instrument_http`: patches ``httpx`` and ``requests`` so an HTTP call
  made inside a tool span becomes a CLIENT child span, and a call made inside
  a ``chat`` span (a LangChain model's request to the gateway) carries its
  ``traceparent``. :func:`uninstrument_http` undoes it.

Nothing is exported by this module: spans go wherever the application's
``TracerProvider`` sends them (to the Prompt Shields collector with an OTLP
exporter pointed at ``<collector>/ingest/otel/v1/traces`` and the tenant
ingest key). Without a configured provider the spans are OTel's no-op spans,
and without ``opentelemetry-api`` every helper here is a no-op.

No content: no prompt, completion, tool argument, tool result, exception
message or URL query is ever put on a span (the SDK's ``send_prompt_text``
does not change that). The user id is not put on spans either.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import ipaddress
import logging
import weakref
from collections.abc import Callable, Iterator, Mapping
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger("prompt_shields.tracing")

try:  # pragma: no cover - exercised with and without the extra
    from opentelemetry import context as _otel_context
    from opentelemetry import propagate as _propagate
    from opentelemetry import trace as _trace
    from opentelemetry.trace import SpanKind, Status, StatusCode

    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False

TRACER_NAME = "prompt_shields"

# SDK vendor names -> gen_ai.provider.name (semconv 1.40 well-known values).
PROVIDER_NAMES = {
    "openai": "openai",
    "anthropic": "anthropic",
    "azure_openai": "azure.ai.openai",
    "bedrock": "aws.bedrock",
    "google": "gcp.gemini",
    "vertex": "gcp.vertex_ai",
    "cohere": "cohere",
    "mistral": "mistral_ai",
    "groq": "groq",
    "deepseek": "deepseek",
}

# Hosts that get a traceparent by default: a gateway in the same network.
# Public hosts (providers, or a gateway on a public name) get one only with
# propagate_trace=True, so no trace id leaves the network unasked.
INTERNAL_SUFFIXES = (".local", ".internal", ".svc", ".cluster.local", ".localhost")

# What an HTTP call made now belongs to, with the span that set it: "tool"
# (becomes a child span), "chat" (carries the chat span's traceparent) or
# None (left alone). Only honoured while that span is still recording, so a
# context a framework left behind (callbacks ending out of order) cannot
# make later calls spans.
_HTTP_MODE: contextvars.ContextVar[tuple[str, Any] | None] = contextvars.ContextVar(
    "prompt_shields_http_mode", default=None
)


# The chat spans start_chat_span made (#386: an OTLP exporter puts its
# attributes on a live one instead of exporting a second span for the call).
_CHAT_SPANS: weakref.WeakSet = weakref.WeakSet()


def live_chat_span(span: Any) -> Any:
    """The OTel span behind ``span`` (a :class:`ChatSpan`, a raw span, or
    None for the current span) when it is a chat span of this module that
    is still recording, else None."""
    if not AVAILABLE:
        return None
    try:
        if span is None:
            span = _trace.get_current_span()
        span = getattr(span, "span", span) if isinstance(span, ChatSpan) else span
        if span in _CHAT_SPANS and span.is_recording():
            return span
    except Exception:  # noqa: BLE001 - fail-open
        return None
    return None


def _tracer():
    from prompt_shields import __version__

    return _trace.get_tracer(TRACER_NAME, __version__)


def provider_name(vendor: str | None) -> str:
    v = (vendor or "").strip().lower()
    return PROVIDER_NAMES.get(v, v or "unknown")


def is_internal_host(host: str | None) -> bool:
    """Loopback, private or link-local addresses, single-label names (a
    Kubernetes service, ``localhost``) and ``.local`` / ``.internal`` /
    ``.svc`` / ``.cluster.local`` names."""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host or host.endswith(INTERNAL_SUFFIXES)
    return addr.is_private or addr.is_loopback or addr.is_link_local


def propagates_to(url: Any, propagate: bool | None = None) -> bool:
    """Whether a request to ``url`` gets a ``traceparent``: ``propagate``
    when given, else only an internal host (:func:`is_internal_host`)."""
    if propagate is not None:
        return propagate
    if url is None:
        return False
    try:
        host = urlsplit(str(url)).hostname
    except ValueError:
        return False
    return is_internal_host(host)


def inject(headers: dict | None = None, ctx: Any = None) -> dict:
    """``headers`` (a new dict when None) with the W3C trace context of
    ``ctx`` (default: the current context) added. Unchanged without OTel or
    without a valid span."""
    headers = {} if headers is None else headers
    if not AVAILABLE:
        return headers
    try:
        _propagate.inject(headers, context=ctx)
    except Exception:
        logger.debug("traceparent injection failed", exc_info=True)
    return headers


def _error_type(exc: BaseException) -> str:
    return type(exc).__qualname__


def _fail(span: Any, exc: BaseException) -> None:
    """Status ERROR with ``error.type``; never the message (it may hold
    content)."""
    with contextlib.suppress(Exception):
        span.set_attribute("error.type", _error_type(exc))
        span.set_status(Status(StatusCode.ERROR))


def _set(span: Any, attrs: Mapping[str, Any]) -> None:
    for key, value in attrs.items():
        if value is None or value == "" or value == []:
            continue
        with contextlib.suppress(Exception):
            span.set_attribute(key, value)


@contextlib.contextmanager
def _http_mode(mode: str | None, owner: Any = None) -> Iterator[None]:
    token = _HTTP_MODE.set((mode, owner) if mode else None)
    try:
        yield
    finally:
        try:
            _HTTP_MODE.reset(token)
        except ValueError:  # reset from another context
            _HTTP_MODE.set(None)


# ──────────────────────────────────────────────────────────────────────
# Chat spans
# ──────────────────────────────────────────────────────────────────────


class ChatSpan:
    """One model call's span. Create with :func:`start_chat_span`; run the
    call under :meth:`activate`, then :meth:`finish` (also for streams,
    when the stream ends). Every method is fail-open."""

    __slots__ = ("_ended", "span")

    def __init__(self, span: Any):
        self.span = span
        self._ended = False

    @contextlib.contextmanager
    def activate(self, *, http: bool = True) -> Iterator[ChatSpan]:
        """Make the span current (its context is what an HTTP call or a
        gateway sees) without ending it. ``http=False``: the hook of
        :func:`instrument_http` leaves the HTTP calls made meanwhile alone
        (the SDK clients add the traceparent themselves, per
        ``propagate_trace``)."""
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(
                    _trace.use_span(
                        self.span,
                        end_on_exit=False,
                        record_exception=False,
                        set_status_on_exception=False,
                    )
                )
                stack.enter_context(_http_mode("chat" if http else None, self.span))
            except Exception:
                logger.debug("chat span activation failed", exc_info=True)
            yield self

    def inject(self, headers: dict | None = None) -> dict:
        """``headers`` with this span's ``traceparent``."""
        try:
            ctx = _trace.set_span_in_context(self.span)
        except Exception:  # noqa: BLE001 - fail-open
            return {} if headers is None else headers
        return inject(headers, ctx)

    def record(self, fields: Mapping[str, Any] | None) -> None:
        """Response attributes from an SDK telemetry event or adapter fields
        (``tokens_in``, ``tokens_out``, ``served_model``,
        ``finish_reasons``)."""
        if not fields:
            return
        reasons = fields.get("finish_reasons")
        _set(
            self.span,
            {
                "gen_ai.usage.input_tokens": _int(fields.get("tokens_in")),
                "gen_ai.usage.output_tokens": _int(fields.get("tokens_out")),
                "gen_ai.response.model": fields.get("served_model")
                if isinstance(fields.get("served_model"), str)
                else None,
                "gen_ai.response.finish_reasons": [r for r in reasons if isinstance(r, str)]
                if isinstance(reasons, (list, tuple))
                else None,
            },
        )

    def fail(self, exc: BaseException) -> None:
        _fail(self.span, exc)

    def finish(self, fields: Mapping[str, Any] | None = None) -> None:
        """Record ``fields`` and end the span (once)."""
        if self._ended:
            return
        self._ended = True
        try:
            self.record(fields)
            self.span.end()
        except Exception:
            logger.debug("chat span end failed", exc_info=True)


def activate(span: ChatSpan | None, *, http: bool = True):
    """``span.activate(http=http)``, or a no-op context without a span."""
    return span.activate(http=http) if span is not None else contextlib.nullcontext()


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def start_chat_span(
    vendor: str | None,
    model: str | None,
    *,
    operation: str = "chat",
    session_id: str | None = None,
    parent: Any = None,
) -> ChatSpan | None:
    """A started (not yet current) CLIENT span for one model call, child of
    ``parent`` (an OTel context) or of the current context. None without
    OTel."""
    if not AVAILABLE:
        return None
    try:
        span = _tracer().start_span(
            f"{operation} {model}" if model else operation,
            context=parent,
            kind=SpanKind.CLIENT,
        )
        _set(
            span,
            {
                "gen_ai.operation.name": operation,
                "gen_ai.provider.name": provider_name(vendor),
                "gen_ai.request.model": model if isinstance(model, str) else None,
                "gen_ai.conversation.id": session_id if isinstance(session_id, str) else None,
            },
        )
        try:
            _CHAT_SPANS.add(span)
        except TypeError:  # a span type that cannot be weakly referenced
            pass
        return ChatSpan(span)
    except Exception:
        logger.debug("chat span start failed", exc_info=True)
        return None


# ──────────────────────────────────────────────────────────────────────
# Tool and agent spans
# ──────────────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _internal_span(name: str, attrs: Mapping[str, Any], http_mode: str | None):
    if not AVAILABLE:
        yield None
        return
    try:
        cm = _tracer().start_as_current_span(
            name,
            kind=SpanKind.INTERNAL,
            record_exception=False,
            set_status_on_exception=False,
        )
        span = cm.__enter__()
    except Exception:
        logger.debug("span start failed", exc_info=True)
        yield None
        return
    _set(span, attrs)
    try:
        with _http_mode(http_mode, span):
            yield span
    except BaseException as exc:
        if not isinstance(exc, GeneratorExit):
            _fail(span, exc)
        cm.__exit__(type(exc), exc, exc.__traceback__)
        raise
    cm.__exit__(None, None, None)


def tool_span(
    name: str,
    *,
    call_id: str | None = None,
    tool_type: str | None = None,
    description: str | None = None,
):
    """Context manager: an ``execute_tool {name}`` span around a tool run.

    ::

        with tool_span("lookup_order", call_id=call.id):
            result = lookup_order(**args)

    An exception marks the span ERROR with ``error.type`` (its class) and is
    re-raised. HTTP calls inside become child spans once
    :func:`instrument_http` is on. Arguments and results are never
    recorded."""
    return _internal_span(
        f"execute_tool {name}",
        {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": name,
            "gen_ai.tool.call.id": call_id,
            "gen_ai.tool.type": tool_type,
            "gen_ai.tool.description": description,
        },
        "tool",
    )


def agent_span(name: str | None = None, *, agent_id: str | None = None,
               session_id: str | None = None):
    """Context manager: an ``invoke_agent {name}`` span around one agent
    turn, so its model and tool spans share a trace."""
    return _internal_span(
        f"invoke_agent {name}" if name else "invoke_agent",
        {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": name,
            "gen_ai.agent.id": agent_id,
            "gen_ai.conversation.id": session_id,
        },
        None,
    )


def traced_tool(name: str | Callable | None = None, **span_kwargs):
    """Decorator: run the function (sync or async) in a :func:`tool_span`
    named ``name`` (default: the function's name).

    ::

        @traced_tool
        def lookup_order(order_id: str) -> dict: ...

        @traced_tool("refund")
        async def refund(order_id: str) -> dict: ...
    """

    def decorate(fn: Callable) -> Callable:
        tool = name if isinstance(name, str) else getattr(fn, "__name__", "tool")
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def run_async(*args, **kwargs):
                with tool_span(tool, **span_kwargs):
                    return await fn(*args, **kwargs)

            return run_async

        @functools.wraps(fn)
        def run(*args, **kwargs):
            with tool_span(tool, **span_kwargs):
                return fn(*args, **kwargs)

        return run

    if callable(name):
        return decorate(name)
    return decorate


# ──────────────────────────────────────────────────────────────────────
# Spans keyed by framework run ids (LangChain callbacks)
# ──────────────────────────────────────────────────────────────────────


class RunSpans:
    """Spans for a framework's run tree: a run's span is the child of its
    nearest ancestor run that has one, else of the current context.
    ``activate=True`` makes a span current until its run ends (callbacks
    that run inline with the work, like LangChain's ``run_inline``)."""

    def __init__(self) -> None:
        self._parents: dict[Any, Any] = {}
        self._spans: dict[Any, tuple[Any, Any, Any]] = {}

    def track(self, run_id: Any, parent_run_id: Any) -> None:
        """Remember a run without a span (its children skip over it)."""
        if parent_run_id is not None:
            self._parents[run_id] = parent_run_id

    def forget(self, run_id: Any) -> None:
        self._parents.pop(run_id, None)

    def has_span(self, run_id: Any) -> bool:
        return run_id in self._spans

    def span_of(self, run_id: Any) -> Any:
        """The (not yet ended) OTel span of a run, or None."""
        entry = self._spans.get(run_id)
        return entry[0] if entry is not None else None

    def _parent_context(self, parent_run_id: Any) -> Any:
        seen = 0
        run = parent_run_id
        while run is not None and seen < 1000:
            entry = self._spans.get(run)
            if entry is not None:
                return _trace.set_span_in_context(entry[0])
            run = self._parents.get(run)
            seen += 1
        return None

    def start(
        self,
        run_id: Any,
        parent_run_id: Any,
        kind: str,
        name: str | None,
        *,
        vendor: str | None = None,
        model: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Start the span of a run: ``kind`` is ``agent``, ``chat`` or
        ``tool``."""
        if not AVAILABLE:
            return
        try:
            self.track(run_id, parent_run_id)
            parent = self._parent_context(parent_run_id)
            if kind == "chat":
                chat = start_chat_span(vendor, model, session_id=session_id, parent=parent)
                if chat is None:
                    return
                span, mode = chat.span, "chat"
            else:
                tool = kind == "tool"
                label = name or ("tool" if tool else None)
                span = _tracer().start_span(
                    f"execute_tool {label}" if tool
                    else (f"invoke_agent {label}" if label else "invoke_agent"),
                    context=parent,
                    kind=SpanKind.INTERNAL,
                )
                _set(
                    span,
                    {
                        "gen_ai.operation.name": "execute_tool" if tool else "invoke_agent",
                        "gen_ai.tool.name" if tool else "gen_ai.agent.name": label,
                        "gen_ai.conversation.id": session_id,
                    },
                )
                mode = "tool" if tool else None
            token = _otel_context.attach(_trace.set_span_in_context(span))
            mode_token = _HTTP_MODE.set((mode, span) if mode else None)
            self._spans[run_id] = (span, token, mode_token)
        except Exception:
            logger.debug("run span start failed", exc_info=True)

    def end(
        self,
        run_id: Any,
        *,
        error: BaseException | None = None,
        fields: Mapping[str, Any] | None = None,
    ) -> None:
        self.forget(run_id)
        entry = self._spans.pop(run_id, None)
        if entry is None:
            return
        span, token, mode_token = entry
        try:
            if fields:
                ChatSpan(span).record(fields)
            if error is not None:
                _fail(span, error)
            # Leave the context as it was before the run, but only when this
            # run's span is still the current one (runs end in order).
            if _trace.get_current_span() is span:
                _otel_context.detach(token)
                try:
                    _HTTP_MODE.reset(mode_token)
                except ValueError:
                    _HTTP_MODE.set(None)
            span.end()
        except Exception:
            logger.debug("run span end failed", exc_info=True)


# ──────────────────────────────────────────────────────────────────────
# httpx / requests hook
# ──────────────────────────────────────────────────────────────────────

_originals: dict[str, tuple[Any, str, Callable]] = {}
_http_propagate: bool | None = None


def _url_parts(url: Any) -> tuple[str | None, int | None, str | None]:
    try:
        parts = urlsplit(str(url))
        return parts.hostname, parts.port, parts.scheme or None
    except ValueError:
        return None, None, None


@contextlib.contextmanager
def _http_call(method: str, url: Any, headers: Any) -> Iterator[Any]:
    """What a patched ``send`` runs its request in; yields the HTTP span or
    None."""
    state = _HTTP_MODE.get()
    if state is None or not AVAILABLE or not _recording(state[1]):
        yield None
        return
    mode = state[0]
    propagate = propagates_to(url, _http_propagate)
    if mode == "chat":
        if propagate:
            _inject_into(headers, None)
        yield None
        return
    host, port, scheme = _url_parts(url)
    method = (method or "GET").upper()
    try:
        cm = _tracer().start_as_current_span(
            method,
            kind=SpanKind.CLIENT,
            record_exception=False,
            set_status_on_exception=False,
        )
        span = cm.__enter__()
    except Exception:  # noqa: BLE001 - fail-open
        yield None
        return
    _set(
        span,
        {
            "http.request.method": method,
            "server.address": host,
            "server.port": port,
            "url.scheme": scheme,
        },
    )
    if propagate:
        _inject_into(headers, None)
    try:
        with _http_mode(None):  # no spans for calls made by the call itself
            yield span
    except BaseException as exc:
        _fail(span, exc)
        cm.__exit__(type(exc), exc, exc.__traceback__)
        raise
    cm.__exit__(None, None, None)


def _recording(span: Any) -> bool:
    try:
        return bool(span.is_recording())
    except Exception:  # noqa: BLE001 - fail-open
        return False


def _inject_into(headers: Any, ctx: Any) -> None:
    carrier: dict[str, str] = {}
    inject(carrier, ctx)
    for key, value in carrier.items():
        with contextlib.suppress(Exception):
            headers[key] = value


def _status(span: Any, status_code: Any) -> None:
    if span is None or not isinstance(status_code, int):
        return
    _set(span, {"http.response.status_code": status_code})
    if status_code >= 500:
        _set(span, {"error.type": str(status_code)})
        with contextlib.suppress(Exception):
            span.set_status(Status(StatusCode.ERROR))


def _patch(key: str, owner: Any, attr: str, wrapper_factory: Callable) -> None:
    if key in _originals:
        return
    original = getattr(owner, attr)
    _originals[key] = (owner, attr, original)
    setattr(owner, attr, wrapper_factory(original))


def instrument_http(*, httpx: bool = True, requests: bool = True,
                    propagate: bool | None = None) -> None:
    """Patch ``httpx`` (sync and async clients) and ``requests`` (when
    installed): a call made inside a :func:`tool_span` (or a LangChain tool
    run) becomes a CLIENT child span (method, host, port, scheme, status
    code; never the path or query), and a call made inside a ``chat`` span
    carries its ``traceparent``. ``propagate`` as for ``propagate_trace``
    (default: internal hosts only). Calls outside those
    spans are untouched. Idempotent; no-op without OTel."""
    global _http_propagate
    if not AVAILABLE:
        return
    _http_propagate = propagate
    if httpx:
        try:
            import httpx as _httpx
        except ImportError:
            _httpx = None
        if _httpx is not None:

            def sync_factory(original):
                @functools.wraps(original)
                def send(self, request, *args, **kwargs):
                    with _http_call(request.method, request.url, request.headers) as span:
                        response = original(self, request, *args, **kwargs)
                        _status(span, getattr(response, "status_code", None))
                        return response

                return send

            def async_factory(original):
                @functools.wraps(original)
                async def send(self, request, *args, **kwargs):
                    with _http_call(request.method, request.url, request.headers) as span:
                        response = await original(self, request, *args, **kwargs)
                        _status(span, getattr(response, "status_code", None))
                        return response

                return send

            _patch("httpx.Client.send", _httpx.Client, "send", sync_factory)
            _patch("httpx.AsyncClient.send", _httpx.AsyncClient, "send", async_factory)
    if requests:
        try:
            import requests as _requests
        except ImportError:
            _requests = None
        if _requests is not None:

            def requests_factory(original):
                @functools.wraps(original)
                def send(self, request, *args, **kwargs):
                    with _http_call(request.method, request.url, request.headers) as span:
                        response = original(self, request, *args, **kwargs)
                        _status(span, getattr(response, "status_code", None))
                        return response

                return send

            _patch("requests.Session.send", _requests.Session, "send", requests_factory)


def uninstrument_http() -> None:
    """Undo :func:`instrument_http`."""
    while _originals:
        _, (owner, attr, original) = _originals.popitem()
        setattr(owner, attr, original)
