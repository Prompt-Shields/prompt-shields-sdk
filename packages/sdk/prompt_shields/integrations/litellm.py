"""LiteLLM custom logger. Requires `litellm`.

    import prompt_shields
    from prompt_shields.integrations.litellm import register

    prompt_shields.instrument(ps_api_key="ps_...")
    register()                      # adds a PromptShieldsLogger to litellm.callbacks
    litellm.completion(model="gpt-4o-mini", messages=[...])

One event per successful `completion` / `acompletion`, streams included
(LiteLLM reports a stream once it ends). The use-case hint is the logger's
`use_case`, else `ps_use_case` in the call's `metadata`. See
`prompt_shields.integrations` for delivery and double-counting rules.
"""

import logging
import threading
from collections import OrderedDict
from typing import Any

import litellm
from litellm.integrations.custom_logger import CustomLogger

from prompt_shields.instrumentation import (
    _FRAMEWORK_CALL,
    FrameworkCall,
    _claim_framework_call,
    current_framework_call,
)
from prompt_shields.integrations._common import emit, normalize_vendor, text_of
from prompt_shields.providers import _field, _int, get_adapter

logger = logging.getLogger("prompt_shields.integrations.litellm")

# Calls whose pre-call hook ran but whose outcome has not been logged yet,
# and calls already handled (LiteLLM may report one call more than once).
_MAX_TRACKED = 10_000


class PromptShieldsLogger(CustomLogger):
    """Emits a Prompt Shields event per LiteLLM call. Never raises."""

    def __init__(self, use_case: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self._use_case = use_case
        self._lock = threading.Lock()
        self._pending: OrderedDict[str, FrameworkCall] = OrderedDict()
        self._done: OrderedDict[str, None] = OrderedDict()

    def _hint(self, kwargs: dict) -> str | None:
        if self._use_case:
            return self._use_case
        params = kwargs.get("litellm_params") or {}
        for metadata in (params.get("metadata"), kwargs.get("metadata")):
            if isinstance(metadata, dict) and metadata.get("ps_use_case"):
                return str(metadata["ps_use_case"])
        return None

    # Runs in the request's context, just before the provider call: announce
    # it so a provider SDK patched by instrument() claims it.
    def log_pre_api_call(self, model, messages, kwargs):
        try:
            call_id = kwargs.get("litellm_call_id")
            if not call_id:
                return
            with self._lock:
                previous = self._pending.pop(call_id, None)
                hint = self._hint(kwargs)
                outer = current_framework_call()
                if previous is not None:  # a retry: keep the hint and claim
                    hint = previous.use_case_hint
                elif outer is not None and outer.framework != "litellm":
                    # Called from a framework run (LangChain's ChatLiteLLM):
                    # this call reports it, the outer integration must not.
                    outer = _claim_framework_call(None)
                    if outer is not None and not hint:
                        hint = outer.use_case_hint
                fc = FrameworkCall("litellm", hint,
                                   model=model if isinstance(model, str) else None,
                                   previous=current_framework_call())
                if previous is not None:
                    fc.captured = previous.captured
                self._pending[call_id] = fc
                while len(self._pending) > _MAX_TRACKED:
                    self._pending.popitem(last=False)
            _FRAMEWORK_CALL.set(fc)
        except Exception as e:  # noqa: BLE001 - fail-open
            logger.debug(f"LiteLLM logger error: {e}")

    # Runs in the request's context once the provider answered (success
    # logging may run later, on another thread): close the announcement so a
    # later SDK call in this context cannot claim it.
    def log_post_api_call(self, kwargs, response_obj, start_time, end_time):
        try:
            call_id = kwargs.get("litellm_call_id")
            with self._lock:
                fc = self._pending.get(call_id) if call_id else None
            if fc is None:
                return
            fc.finished = True
            if _FRAMEWORK_CALL.get() is fc:
                _FRAMEWORK_CALL.set(current_framework_call())
        except Exception as e:  # noqa: BLE001 - fail-open
            logger.debug(f"LiteLLM logger error: {e}")

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._success(kwargs, response_obj, start_time, end_time)

    async def async_log_success_event(self, kwargs, response_obj, start_time,
                                      end_time):
        self._success(kwargs, response_obj, start_time, end_time)

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self._take(kwargs)

    async def async_log_failure_event(self, kwargs, response_obj, start_time,
                                      end_time):
        self._take(kwargs)

    def _take(self, kwargs: dict) -> tuple[bool, FrameworkCall | None]:
        """(first report of this call?, its FrameworkCall if announced)."""
        try:
            call_id = kwargs.get("litellm_call_id")
            with self._lock:
                if call_id in self._done:
                    return False, None
                if call_id:
                    self._done[call_id] = None
                    while len(self._done) > _MAX_TRACKED:
                        self._done.popitem(last=False)
                fc = self._pending.pop(call_id, None) if call_id else None
            if fc is not None:
                fc.finished = True
            return True, fc
        except Exception as e:  # noqa: BLE001 - fail-open
            logger.debug(f"LiteLLM logger error: {e}")
            return False, None

    def _success(self, kwargs, response_obj, start_time, end_time):
        try:
            first, fc = self._take(kwargs)
            if not first:
                return
            if fc is None:
                fc = FrameworkCall("litellm", self._hint(kwargs))
                fc.finished = True
            if fc.captured:
                return
            params = kwargs.get("litellm_params") or {}
            provider = (params.get("custom_llm_provider")
                        or kwargs.get("custom_llm_provider"))
            messages = [
                {"role": m.get("role"), "content": text_of(m.get("content"))}
                for m in kwargs.get("messages") or [] if isinstance(m, dict)
            ]
            try:
                latency_ms = int((end_time - start_time).total_seconds() * 1000)
            except Exception:  # noqa: BLE001
                latency_ms = None
            emit(fc, vendor=normalize_vendor(provider), model=kwargs.get("model"),
                 messages=messages, latency_ms=latency_ms,
                 adapter_fields=_extract(response_obj),
                 cost_fallback=kwargs.get("response_cost"))
        except Exception as e:  # noqa: BLE001 - fail-open
            logger.debug(f"LiteLLM logger error: {e}")


def _extract(response: Any) -> dict:
    """Usage, tool calls and served model from a LiteLLM `ModelResponse`.

    LiteLLM normalises every provider to the OpenAI shape; `prompt_tokens`
    includes cached tokens. Anthropic cache writes are reported separately.
    """
    fields = get_adapter("openai").extract(response)
    usage = _field(response, "usage")
    fields["tokens_cache_write"] = _int(_field(usage, "cache_creation_input_tokens"))
    return fields


_registered: PromptShieldsLogger | None = None
_register_lock = threading.Lock()


def register(use_case: str | None = None) -> PromptShieldsLogger:
    """Add a `PromptShieldsLogger` to `litellm.callbacks` once; return it."""
    global _registered
    with _register_lock:
        if _registered is None or _registered not in litellm.callbacks:
            _remove(_registered)
            _registered = PromptShieldsLogger(use_case=use_case)
            litellm.callbacks.append(_registered)
        return _registered


def unregister() -> None:
    """Remove the logger added by `register()`."""
    global _registered
    with _register_lock:
        _remove(_registered)
        _registered = None


def _remove(callback: Any) -> None:
    # LiteLLM copies `callbacks` entries into its per-event lists.
    if callback is None:
        return
    for name in ("callbacks", "input_callback", "success_callback",
                 "failure_callback", "_async_success_callback",
                 "_async_failure_callback"):
        callbacks = getattr(litellm, name, None)
        while isinstance(callbacks, list) and callback in callbacks:
            callbacks.remove(callback)
