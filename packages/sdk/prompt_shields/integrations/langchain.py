"""LangChain callback handler. Requires `langchain-core`.

    import prompt_shields
    from prompt_shields.integrations.langchain import PromptShieldsCallbackHandler

    prompt_shields.instrument(ps_api_key="ps_...")
    handler = PromptShieldsCallbackHandler()
    chain.invoke(inputs, config={"callbacks": [handler]})

One event per chat model / LLM run. The use-case hint is, in order: the
handler's `use_case`, `ps_use_case` in the run's metadata (e.g.
`config={"metadata": {"ps_use_case": "support-bot"}}`), then the name of the
outermost named chain or agent the run belongs to (LangChain's default names,
such as `RunnableSequence` or `AgentExecutor`, are skipped). See
`prompt_shields.integrations` for delivery and double-counting rules.
"""

import logging
import time
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

from prompt_shields.instrumentation import (
    _FRAMEWORK_CALL,
    FrameworkCall,
    current_framework_call,
)
from prompt_shields.integrations._common import emit, normalize_vendor, text_of
from prompt_shields.providers import _int, openai_usage_fields

logger = logging.getLogger("prompt_shields.integrations.langchain")


class _Run:
    __slots__ = ("fc", "messages", "model", "start", "vendor")

    def __init__(self, fc, vendor, model, messages):
        self.fc = fc
        self.vendor = vendor
        self.model = model
        self.messages = messages
        self.start = time.monotonic()


class PromptShieldsCallbackHandler(BaseCallbackHandler):
    """Emits a Prompt Shields event per LangChain model run. Never raises."""

    # Run in the caller's context (also under async managers) so the
    # FrameworkCall set on start is visible to the provider SDK call.
    run_inline = True

    def __init__(self, use_case: str | None = None):
        super().__init__()
        self._use_case = use_case
        self._roots: dict[UUID, str | None] = {}  # run -> outermost chain name
        self._runs: dict[UUID, _Run] = {}

    # --- chains, agents and tools: track the outermost name -----------------

    def _enter(self, serialized: Any, run_id: UUID, parent_run_id: UUID | None,
               kwargs: dict) -> None:
        try:
            inherited = (self._roots.get(parent_run_id)
                         if parent_run_id is not None else None)
            self._roots[run_id] = inherited or _meaningful(
                kwargs.get("name") or _name(serialized))
        except Exception as e:  # noqa: BLE001 - fail-open
            logger.debug(f"LangChain handler error: {e}")

    def _exit(self, run_id: UUID) -> None:
        try:
            self._roots.pop(run_id, None)
        except Exception as e:  # noqa: BLE001 - fail-open
            logger.debug(f"LangChain handler error: {e}")

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None,
                       **kwargs):
        self._enter(serialized, run_id, parent_run_id, kwargs)

    def on_chain_end(self, outputs, *, run_id, **kwargs):
        self._exit(run_id)

    def on_chain_error(self, error, *, run_id, **kwargs):
        self._exit(run_id)

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None,
                      **kwargs):
        self._enter(serialized, run_id, parent_run_id, kwargs)

    def on_tool_end(self, output, *, run_id, **kwargs):
        self._exit(run_id)

    def on_tool_error(self, error, *, run_id, **kwargs):
        self._exit(run_id)

    # --- model runs ---------------------------------------------------------

    def on_chat_model_start(self, serialized, messages, *, run_id,
                            parent_run_id=None, metadata=None, **kwargs):
        try:
            flat = [
                {"role": getattr(m, "type", None), "content": text_of(
                    getattr(m, "content", None))}
                for batch in messages or [] for m in batch
            ]
        except Exception:  # noqa: BLE001 - fail-open
            flat = []
        self._start(run_id, parent_run_id, metadata, kwargs, flat)

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None,
                     metadata=None, **kwargs):
        try:
            flat = [{"role": "user", "content": p} for p in prompts or []
                    if isinstance(p, str)]
        except Exception:  # noqa: BLE001 - fail-open
            flat = []
        self._start(run_id, parent_run_id, metadata, kwargs, flat)

    def _start(self, run_id, parent_run_id, metadata, kwargs, messages):
        try:
            metadata = metadata or {}
            invocation = kwargs.get("invocation_params") or {}
            hint = (
                self._use_case
                or metadata.get("ps_use_case")
                or self._roots.get(parent_run_id)
            )
            fc = FrameworkCall("langchain", hint,
                               previous=current_framework_call())
            _FRAMEWORK_CALL.set(fc)
            self._runs[run_id] = _Run(
                fc,
                normalize_vendor(metadata.get("ls_provider")
                                 or invocation.get("_type")),
                metadata.get("ls_model_name")
                or invocation.get("model")
                or invocation.get("model_name"),
                messages,
            )
        except Exception as e:  # noqa: BLE001 - fail-open
            logger.debug(f"LangChain handler error: {e}")

    def _finish(self, run_id: UUID) -> _Run | None:
        run = self._runs.pop(run_id, None)
        if run is None:
            return None
        run.fc.finished = True
        try:
            # Unwind finished calls (batch runs end in any order).
            current = top = _FRAMEWORK_CALL.get()
            while current is not None and current.finished:
                current = current.previous
            if current is not top:
                _FRAMEWORK_CALL.set(current)
        except Exception:  # noqa: BLE001, S110 - fail-open
            pass
        return run

    def on_llm_end(self, response, *, run_id, **kwargs):
        try:
            run = self._finish(run_id)
            if run is None or run.fc.captured:
                return
            latency_ms = int((time.monotonic() - run.start) * 1000)
            emit(run.fc, vendor=run.vendor, model=run.model,
                 messages=run.messages, latency_ms=latency_ms,
                 adapter_fields=_extract(response))
        except Exception as e:  # noqa: BLE001 - fail-open
            logger.debug(f"LangChain handler error: {e}")

    def on_llm_error(self, error, *, run_id, **kwargs):
        try:
            self._finish(run_id)
        except Exception as e:  # noqa: BLE001 - fail-open
            logger.debug(f"LangChain handler error: {e}")


# Names LangChain gives unnamed runnables and agents: not a use case.
_GENERIC_NAMES = frozenset({"AgentExecutor", "LangGraph"})


def _meaningful(name: Any) -> str | None:
    """`name` unless it is a LangChain default (`RunnableSequence`, ...)."""
    if not isinstance(name, str) or not name or name in _GENERIC_NAMES \
            or name.startswith("Runnable"):
        return None
    return name


def _name(serialized: Any) -> str | None:
    if not isinstance(serialized, dict):
        return None
    if isinstance(serialized.get("name"), str):
        return serialized["name"]
    ids = serialized.get("id")
    if isinstance(ids, list) and ids and isinstance(ids[-1], str):
        return ids[-1]
    return None


def _extract(result: Any) -> dict:
    """Usage, tool calls and served model from an `LLMResult`."""
    totals = {"tokens_in": None, "tokens_out": None, "tokens_cached": None,
              "tokens_cache_write": None, "tokens_reasoning": None}
    tool_calls: list[dict] = []
    served_model = None

    def add(key: str, value: Any) -> None:
        value = _int(value)
        if value is not None:
            totals[key] = (totals[key] or 0) + value

    for batch in getattr(result, "generations", None) or []:
        for gen in batch or []:
            message = getattr(gen, "message", None)
            if message is None:
                continue
            usage = getattr(message, "usage_metadata", None)
            if isinstance(usage, dict):
                add("tokens_in", usage.get("input_tokens"))
                add("tokens_out", usage.get("output_tokens"))
                in_details = usage.get("input_token_details") or {}
                out_details = usage.get("output_token_details") or {}
                add("tokens_cached", in_details.get("cache_read"))
                add("tokens_cache_write", in_details.get("cache_creation"))
                add("tokens_reasoning", out_details.get("reasoning"))
            for call in getattr(message, "tool_calls", None) or []:
                name = call.get("name") if isinstance(call, dict) else None
                tool_calls.append({"name": name, "type": "function"})
            meta = getattr(message, "response_metadata", None) or {}
            served_model = served_model or meta.get("model_name") or meta.get("model")

    llm_output = getattr(result, "llm_output", None) or {}
    if totals["tokens_in"] is None and totals["tokens_out"] is None:
        usage = llm_output.get("token_usage") or llm_output.get("usage")
        if usage:
            totals = openai_usage_fields(usage)
    served_model = served_model or llm_output.get("model_name")
    return {
        **totals,
        "tool_calls_used": tool_calls or None,
        "served_model": served_model if isinstance(served_model, str) else None,
    }
