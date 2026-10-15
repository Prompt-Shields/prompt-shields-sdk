"""LlamaIndex instrumentation handlers. Requires `llama-index-core`.

    import prompt_shields
    from prompt_shields.integrations.llamaindex import register

    prompt_shields.instrument(ps_api_key="ps_...")
    register()              # adds handlers to LlamaIndex's root dispatcher
    index.as_query_engine().query("...")

One event per LLM call (`chat`, `complete` and their async and streaming
forms; a stream is reported once it ends). `register()` attaches an event
handler and a span handler to the root dispatcher of
`llama_index.core.instrumentation`; `unregister()` removes both.

The use-case hint is, in order: the `use_case` given to `register()`,
`ps_use_case` in LlamaIndex's instrument tags (e.g.
`with instrument_tags({"ps_use_case": "support-bot"}): ...`), then the name of
the outermost named agent or workflow the call runs in: an agent's `name`, an
`AgentWorkflow`'s root agent, or the class name of your own `Workflow`
subclass. LlamaIndex's defaults (the agent name `"Agent"`, its own class
names such as `FunctionAgent` or `RetrieverQueryEngine`) are skipped.

An LLM call made inside another one (`CustomLLM.chat` calling `complete`,
a structured LLM wrapping another LLM) is reported once, by the innermost
call. See `prompt_shields.integrations` for delivery and double-counting
rules.
"""

import inspect
import logging
import threading
import time
from collections import OrderedDict
from types import SimpleNamespace
from typing import Any

from llama_index.core.instrumentation import get_dispatcher
from llama_index.core.instrumentation import root_manager as _root_manager
from llama_index.core.instrumentation.event_handlers import BaseEventHandler
from llama_index.core.instrumentation.span import SimpleSpan
from llama_index.core.instrumentation.span_handlers import BaseSpanHandler
from pydantic import PrivateAttr

from prompt_shields import instrumentation
from prompt_shields.instrumentation import (
    _FRAMEWORK_CALL,
    FrameworkCall,
    current_framework_call,
    defer_framework_call,
    forget_framework_call,
)
from prompt_shields.integrations._common import (
    claim_outer_call,
    emit,
    normalize_vendor,
    text_of,
)
from prompt_shields.providers import ADAPTERS, _field, _int

try:  # Agents and workflows are optional parts of llama-index-core.
    from llama_index.core.workflow import Workflow as _Workflow
except Exception:  # noqa: BLE001
    _Workflow = None
try:
    from llama_index.core.agent.workflow.base_agent import (
        DEFAULT_AGENT_NAME as _DEFAULT_AGENT_NAME,
    )
except Exception:  # noqa: BLE001
    _DEFAULT_AGENT_NAME = "Agent"

logger = logging.getLogger("prompt_shields.integrations.llamaindex")

FRAMEWORK = "llamaindex"

# Open spans and LLM calls tracked at once (a stream that is never consumed
# to the end never reports its end).
_MAX_TRACKED = 10_000

_START_EVENTS = frozenset({"LLMChatStartEvent", "LLMCompletionStartEvent"})
_END_EVENTS = frozenset({"LLMChatEndEvent", "LLMCompletionEndEvent"})
_FAIL_EVENTS = frozenset({"SpanDropEvent", "ExceptionEvent"})

# LlamaIndex LLM `class_name()` values -> SDK vendor names, after lowercasing
# and dropping a trailing `_llm` (`openai_llm`, `Anthropic_LLM`, ...).
_LLAMAINDEX_VENDORS = {
    "openai_responses": "openai",
    "googlegenai": "google",
    "vertex": "google",
    "custom": "custom",
}


class _Run:
    __slots__ = ("fc", "messages", "model", "span_id", "start", "vendor")

    def __init__(self, fc, span_id, vendor, model, messages):
        self.fc = fc
        self.span_id = span_id
        self.vendor = vendor
        self.model = model
        self.messages = messages
        self.start = time.monotonic()


class PromptShieldsEventHandler(BaseEventHandler):
    """Emits a Prompt Shields event per LlamaIndex LLM call. Never raises.

    Add it with `register()`, which also adds the `PromptShieldsSpanHandler`
    it reads agent and workflow names (and call nesting) from.
    """

    use_case: str | None = None

    _lock: Any = PrivateAttr(default_factory=threading.Lock)
    # span id -> (parent span id, outermost meaningful name)
    _spans: Any = PrivateAttr(default_factory=OrderedDict)
    # span id -> runs started in it (one, unless spans are not unique)
    _runs: Any = PrivateAttr(default_factory=OrderedDict)

    @classmethod
    def class_name(cls) -> str:
        return "PromptShieldsEventHandler"

    # --- spans (called by PromptShieldsSpanHandler) --------------------------

    def _span_enter(self, id_: Any, instance: Any, parent_id: Any) -> None:
        try:
            with self._lock:
                parent = self._spans.get(parent_id) if parent_id else None
                root = (parent[1] if parent else None) or _workflow_name(instance)
                self._spans[id_] = (parent_id, root)
                while len(self._spans) > _MAX_TRACKED:
                    self._spans.popitem(last=False)
        except Exception as e:  # noqa: BLE001 - fail-open
            logger.debug(f"LlamaIndex handler error: {e}")

    def _span_exit(self, id_: Any, result: Any = None) -> None:
        try:
            with self._lock:
                self._spans.pop(id_, None)
                runs = list(self._runs.get(id_) or ())
            if inspect.isgenerator(result) or inspect.isasyncgen(result):
                # A stream: the provider is called when it is iterated,
                # maybe in another context. Bind the call to the generator.
                for run in runs:
                    defer_framework_call(run.fc, result)
        except Exception as e:  # noqa: BLE001 - fail-open
            logger.debug(f"LlamaIndex handler error: {e}")

    # --- events ---------------------------------------------------------------

    def handle(self, event: Any, **kwargs: Any) -> None:
        try:
            kind = type(event).__name__
            if kind in _START_EVENTS:
                self._start(event, kind)
            elif kind in _END_EVENTS:
                self._end(event)
            elif kind in _FAIL_EVENTS:
                self._fail(event)
        except Exception as e:  # noqa: BLE001 - fail-open
            logger.debug(f"LlamaIndex handler error: {e}")

    async def ahandle(self, event: Any, **kwargs: Any) -> None:
        self.handle(event, **kwargs)

    def _start(self, event: Any, kind: str) -> None:
        span_id = getattr(event, "span_id", None)
        tags = getattr(event, "tags", None) or {}
        with self._lock:
            entry = self._spans.get(span_id)
            hint = (
                self.use_case
                or (str(tags["ps_use_case"]) if tags.get("ps_use_case") else None)
                or (entry[1] if entry else None)
            )
            outer = self._enclosing_run(entry)
        if outer is not None:
            # A call inside another LlamaIndex call: the inner one reports.
            with instrumentation._claim_lock:
                outer.fc.captured = True
            hint = hint or outer.fc.use_case_hint
        else:
            # Called from another framework's run (LangChain), or while a
            # LlamaIndex stream is iterated: this call reports it.
            claimed = claim_outer_call(FRAMEWORK)
            if claimed is not None:
                hint = hint or claimed.use_case_hint

        model_dict = getattr(event, "model_dict", None) or {}
        if kind == "LLMChatStartEvent":
            messages = [
                {"role": _role(m), "content": text_of(getattr(m, "content", None))}
                for m in getattr(event, "messages", None) or []
            ]
        else:
            prompt = getattr(event, "prompt", None)
            messages = [{"role": "user", "content": prompt}] \
                if isinstance(prompt, str) else []
        fc = FrameworkCall(FRAMEWORK, hint, previous=current_framework_call())
        _FRAMEWORK_CALL.set(fc)
        run = _Run(fc, span_id, _vendor(model_dict.get("class_name")),
                   model_dict.get("model") or model_dict.get("model_name"),
                   messages)
        with self._lock:
            self._runs.setdefault(span_id, []).append(run)
            while len(self._runs) > _MAX_TRACKED:
                _, dropped = self._runs.popitem(last=False)
                for old in dropped:
                    old.fc.finished = True

    def _enclosing_run(self, entry: Any) -> "_Run | None":
        """The unfinished run of an ancestor span, if any (lock held)."""
        seen = 0
        parent_id = entry[0] if entry else None
        while parent_id is not None and seen < 1000:
            for run in self._runs.get(parent_id) or ():
                if not run.fc.finished:
                    return run
            parent = self._spans.get(parent_id)
            parent_id = parent[0] if parent else None
            seen += 1
        return None

    def _take(self, span_id: Any) -> "_Run | None":
        with self._lock:
            runs = self._runs.get(span_id)
            if not runs:
                return None
            # Prefer the run announced in this context.
            chain = []
            fc = _FRAMEWORK_CALL.get()
            while fc is not None and len(chain) < 1000:
                chain.append(fc)
                fc = fc.previous
            run = next((r for r in runs if any(r.fc is c for c in chain)), runs[0])
            runs.remove(run)
            if not runs:
                del self._runs[span_id]
        run.fc.finished = True
        forget_framework_call(run.fc)
        # Unwind finished calls in this context.
        current = top = _FRAMEWORK_CALL.get()
        while current is not None and current.finished:
            current = current.previous
        if current is not top:
            _FRAMEWORK_CALL.set(current)
        return run

    def _end(self, event: Any) -> None:
        run = self._take(getattr(event, "span_id", None))
        if run is None or run.fc.captured:
            return
        latency_ms = int((time.monotonic() - run.start) * 1000)
        emit(run.fc, vendor=run.vendor, model=run.model, messages=run.messages,
             latency_ms=latency_ms,
             adapter_fields=_extract(getattr(event, "response", None), run.vendor))

    def _fail(self, event: Any) -> None:
        span_id = getattr(event, "span_id", None)
        while self._take(span_id) is not None:
            pass


class PromptShieldsSpanHandler(BaseSpanHandler[SimpleSpan]):
    """Tracks open spans for a `PromptShieldsEventHandler`. Never raises."""

    _events: Any = PrivateAttr(default=None)

    @classmethod
    def class_name(cls) -> str:
        return "PromptShieldsSpanHandler"

    def span_enter(self, id_, bound_args, instance=None, parent_id=None,
                   tags=None, **kwargs):
        if self._events is not None:
            self._events._span_enter(id_, instance, parent_id)

    def span_exit(self, id_, bound_args, instance=None, result=None, **kwargs):
        if self._events is not None:
            self._events._span_exit(id_, result)

    def span_drop(self, id_, bound_args, instance=None, err=None, **kwargs):
        if self._events is not None:
            self._events._span_exit(id_)

    # Spans are not kept: the event handler holds what it needs.
    def new_span(self, *args, **kwargs):
        return None

    def prepare_to_exit_span(self, *args, **kwargs):
        return None

    def prepare_to_drop_span(self, *args, **kwargs):
        return None


# --- helpers ---------------------------------------------------------------------


def _role(message: Any) -> str | None:
    role = getattr(message, "role", None)
    role = getattr(role, "value", role)
    return role if isinstance(role, str) else None


def _vendor(class_name: Any) -> str:
    if not isinstance(class_name, str) or not class_name:
        return "custom"
    key = class_name.lower().removesuffix("_llm")
    return _LLAMAINDEX_VENDORS.get(key) or normalize_vendor(key)


def _workflow_name(instance: Any) -> str | None:
    """A use-case name for an agent or workflow span, never a default."""
    if _Workflow is None or not isinstance(instance, _Workflow):
        return None
    cls = type(instance)
    name = getattr(instance, "name", None)
    field = (getattr(cls, "model_fields", None) or {}).get("name")
    default = getattr(field, "default", None)
    if isinstance(name, str) and name and name not in (
            default, _DEFAULT_AGENT_NAME, cls.__name__):
        return name
    root = getattr(instance, "root_agent", None)  # AgentWorkflow
    if isinstance(root, str) and root and root != _DEFAULT_AGENT_NAME:
        return root
    module = getattr(cls, "__module__", "") or ""
    if not module.startswith(("llama_index", "workflows")):
        return cls.__name__  # the user's own Workflow subclass
    return None


def _namespace(value: Any, depth: int = 0) -> Any:
    """Dicts (raw JSON responses) as attribute objects, for the adapters."""
    if depth > 8:
        return value
    if isinstance(value, dict):
        return SimpleNamespace(**{str(k): _namespace(v, depth + 1)
                                  for k, v in value.items()})
    if isinstance(value, list):
        return [_namespace(v, depth + 1) for v in value]
    return value


_TOKEN_KEYS = {
    "tokens_in": ("prompt_tokens", "input_tokens"),
    "tokens_out": ("completion_tokens", "output_tokens"),
}


def _extract(response: Any, vendor: str) -> dict:
    """Usage, tool calls and served model from a `ChatResponse` /
    `CompletionResponse`: the provider's raw response when the vendor has an
    adapter, then LlamaIndex's own token counts and tool-call blocks."""
    fields: dict = {}
    raw = getattr(response, "raw", None)
    adapter = ADAPTERS.get(vendor)
    if raw is not None and adapter is not None:
        try:
            fields = adapter.extract(_namespace(raw))
        except Exception:  # noqa: BLE001 - not this vendor's shape
            fields = {}
    extra = getattr(response, "additional_kwargs", None) or {}
    for key, names in _TOKEN_KEYS.items():
        if fields.get(key) is None:
            fields[key] = next((_int(extra.get(n)) for n in names
                                if _int(extra.get(n)) is not None), None)
    if not fields.get("tool_calls_used"):
        fields["tool_calls_used"] = _tool_calls(getattr(response, "message", None))
    if not fields.get("served_model"):
        served = _field(raw, "model")
        fields["served_model"] = served if isinstance(served, str) else None
    return fields


def _tool_calls(message: Any) -> list | None:
    """Names of the tools the model asked for (never their arguments)."""
    names: list[str] = []
    for block in getattr(message, "blocks", None) or []:
        if getattr(block, "block_type", None) == "tool_call":
            name = getattr(block, "tool_name", None)
            if isinstance(name, str):
                names.append(name)
    if not names:
        extra = getattr(message, "additional_kwargs", None) or {}
        for call in extra.get("tool_calls") or []:
            name = _field(_field(call, "function"), "name")
            if isinstance(name, str):
                names.append(name)
    return [{"name": n, "type": "function"} for n in names] or None


# --- registration ------------------------------------------------------------------


_registered: tuple[PromptShieldsEventHandler, PromptShieldsSpanHandler] | None = None
_register_lock = threading.Lock()


def register(use_case: str | None = None) -> PromptShieldsEventHandler:
    """Attach the handlers to LlamaIndex's root dispatcher once; return the
    event handler."""
    global _registered
    with _register_lock:
        dispatcher = get_dispatcher()
        # Identity checks: pydantic handlers compare equal by field values.
        if _registered is not None \
                and any(h is _registered[0] for h in dispatcher.event_handlers) \
                and any(h is _registered[1] for h in dispatcher.span_handlers):
            return _registered[0]
        _remove(_registered)
        events = PromptShieldsEventHandler(use_case=use_case)
        spans = PromptShieldsSpanHandler()
        spans._events = events
        dispatcher.add_span_handler(spans)
        dispatcher.add_event_handler(events)
        _registered = (events, spans)
        return events


def unregister() -> None:
    """Remove the handlers added by `register()` from every dispatcher."""
    global _registered
    with _register_lock:
        _remove(_registered)
        _registered = None


def _remove(handlers: Any) -> None:
    if handlers is None:
        return
    for dispatcher in list(_root_manager.dispatchers.values()):
        dispatcher.event_handlers = [
            h for h in dispatcher.event_handlers if not any(h is x for x in handlers)]
        dispatcher.span_handlers = [
            h for h in dispatcher.span_handlers if not any(h is x for x in handlers)]
