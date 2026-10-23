"""Implicit outcome signals (PRD 0001 §7.13, #44).

Each telemetry event gets a short list of outcome *classes*, worked out
locally from the request and the response, for the self-learning router to
use later as weak or strong negatives:

==================  ==========================================================
class               when
==================  ==========================================================
``fallback``        the event records a fallback (a non-empty
                    ``fallback_chain``, set by the router or gateway)
``schema_invalid``  JSON output was requested (``response_format`` /
                    Responses ``text.format``) and the answer is not valid
                    JSON, or misses the schema's top-level type or required keys
``tool_call_invalid``  a tool call's ``arguments`` are not a JSON object
``refusal``         the model refused or a content filter stopped it
                    (``refusal`` field or stop reason, ``content_filter``,
                    Gemini ``SAFETY`` …)
``max_tokens``      the answer was cut off at the output token limit
``stream_error``    a stream ended with an error
``abandoned``       the caller stopped reading a stream before its end
``regenerate``      the same session re-asked the same prompt within the
                    window (default 120 s) after a call that returned
``retry``           ... after a call that raised (the caller retries)
==================  ==========================================================

Only these class names leave the process, in ``event["outcome_signals"]``
(sorted, de-duplicated, at most ``MAX_SIGNALS``), with
``event["outcome_signals_version"]``. Prompt and answer text are read in
process (to parse JSON and to fingerprint a prompt) and never sent or kept:
the regenerate detector keeps only truncated SHA-256 digests, in a bounded
in-memory table. ``PS_OUTCOME_SIGNALS=0`` turns the whole feature off.

Without a ``session_id`` the detector only matches a byte-identical request
(same model and message list); with one, the same last user message
(case and whitespace folded) in that session.
"""

import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any

from prompt_shields.providers import _field

logger = logging.getLogger("prompt_shields.outcome_signals")

#: Bumped whenever a class is added, removed or redefined.
OUTCOME_SIGNALS_VERSION = 1

FALLBACK = "fallback"
SCHEMA_INVALID = "schema_invalid"
TOOL_CALL_INVALID = "tool_call_invalid"
REFUSAL = "refusal"
MAX_TOKENS = "max_tokens"
STREAM_ERROR = "stream_error"
ABANDONED = "abandoned"
REGENERATE = "regenerate"
RETRY = "retry"

SIGNAL_CLASSES = frozenset({
    FALLBACK, SCHEMA_INVALID, TOOL_CALL_INVALID, REFUSAL, MAX_TOKENS,
    STREAM_ERROR, ABANDONED, REGENERATE, RETRY,
})
MAX_SIGNALS = len(SIGNAL_CLASSES)

# Provider stop/finish reasons, lower-cased, without an enum prefix.
_TRUNCATED_REASONS = frozenset({
    "length",  # OpenAI, Azure, Mistral
    "max_tokens",  # Anthropic, Gemini, Cohere, Bedrock
    "max_output_tokens",  # Responses API incomplete_details.reason
    "model_context_window_exceeded",  # Anthropic
})
_REFUSAL_REASONS = frozenset({
    "content_filter",  # OpenAI, Azure, Responses API
    "refusal",  # Anthropic
    "safety", "recitation", "prohibited_content", "blocklist", "spii",
    "image_safety",  # Gemini
    "guardrail_intervened", "content_filtered",  # Bedrock
    "error_toxic",  # Cohere
})

_ENV_SWITCH = "PS_OUTCOME_SIGNALS"


def enabled() -> bool:
    return os.environ.get(_ENV_SWITCH, "1").strip().lower() not in (
        "0", "false", "off", "no")


# --- response reading ----------------------------------------------------


def _items(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _reason(value: Any) -> str | None:
    """A stop reason as a lower-case token; enums (Gemini) by name."""
    if value is None:
        return None
    name = getattr(value, "name", None)
    if isinstance(name, str) and not isinstance(value, str):
        value = name
    if not isinstance(value, str) or not value:
        return None
    return value.rsplit(".", 1)[-1].lower()


def finish_reasons(response: Any) -> list[str]:
    """Stop/finish reasons of a non-streamed response, any supported vendor."""
    if response is None:
        return []
    raw: list[Any] = []
    for choice in _items(_field(response, "choices")):  # OpenAI-style
        raw.append(_field(choice, "finish_reason"))
    for candidate in _items(_field(response, "candidates")):  # Gemini
        raw.append(_field(candidate, "finish_reason"))
    raw.append(_field(response, "stop_reason"))  # Anthropic
    raw.append(_field(response, "stopReason"))  # Bedrock Converse
    raw.append(_field(response, "finish_reason"))  # Cohere
    # Responses API: status "incomplete" carries the reason.
    raw.append(_field(_field(response, "incomplete_details"), "reason"))
    # Gemini blocks the prompt itself with prompt_feedback.block_reason.
    block = _reason(_field(_field(response, "prompt_feedback"), "block_reason"))
    if block and block != "block_reason_unspecified":
        raw.append("safety")
    return [r for r in (_reason(v) for v in raw) if r]


def _response_texts(response: Any) -> list[str]:
    """Answer texts of a non-streamed OpenAI-style or Responses response."""
    texts = []
    for choice in _items(_field(response, "choices")):
        content = _field(_field(choice, "message"), "content")
        if isinstance(content, str):
            texts.append(content)
    output_text = []
    for item in _items(_field(response, "output")):
        if _field(item, "type") != "message":
            continue
        for part in _items(_field(item, "content")):
            if _field(part, "type") == "output_text" and isinstance(
                    _field(part, "text"), str):
                output_text.append(_field(part, "text"))
    if output_text:
        texts.append("".join(output_text))
    return texts


def _has_refusal(response: Any) -> bool:
    for choice in _items(_field(response, "choices")):
        refusal = _field(_field(choice, "message"), "refusal")
        if isinstance(refusal, str) and refusal:
            return True
    for item in _items(_field(response, "output")):
        for part in _items(_field(item, "content")):
            if _field(part, "type") == "refusal":
                return True
    return False


def _tool_arguments(response: Any) -> list[Any]:
    args = []
    for choice in _items(_field(response, "choices")):
        for call in _items(_field(_field(choice, "message"), "tool_calls")):
            fn = _field(call, "function")
            if fn is not None:
                args.append(_field(fn, "arguments"))
    for item in _items(_field(response, "output")):
        if _field(item, "type") == "function_call":
            args.append(_field(item, "arguments"))
    return args


def _invalid_arguments(arguments: Any) -> bool:
    if isinstance(arguments, dict):
        return False
    if not isinstance(arguments, str):
        return False  # not something we can judge
    try:
        return not isinstance(json.loads(arguments), dict)
    except ValueError:
        return True


def _json_format(request: dict) -> dict | None:
    """The requested JSON output format, as {"schema": dict | None}."""
    fmt = request.get("response_format")  # Chat Completions, Mistral
    if fmt is None:
        fmt = _field(request.get("text"), "format")  # Responses API
    kind = _field(fmt, "type")
    if kind == "json_object":
        return {"schema": None}
    if kind == "json_schema":
        schema = _field(fmt, "schema")  # Responses API: flat
        if schema is None:
            schema = _field(_field(fmt, "json_schema"), "schema")
        return {"schema": schema if isinstance(schema, dict) else None}
    return None


def _invalid_json(text: str, schema: dict | None) -> bool:
    try:
        value = json.loads(text)
    except ValueError:
        return True
    if not schema:
        return False
    if schema.get("type") == "object":
        if not isinstance(value, dict):
            return True
        required = schema.get("required")
        if isinstance(required, list):
            return any(isinstance(k, str) and k not in value for k in required)
    elif schema.get("type") == "array":
        return not isinstance(value, list)
    return False


def response_signals(response: Any, request: dict | None = None,
                     reasons: list[str] | None = None) -> set[str]:
    """Classes read from one response (`reasons` for a stream's reasons)."""
    signals: set[str] = set()
    request = request or {}
    if reasons is None:
        reasons = finish_reasons(response)
    else:
        reasons = [r for r in (_reason(v) for v in reasons) if r]
    if any(r in _TRUNCATED_REASONS for r in reasons):
        signals.add(MAX_TOKENS)
    refused = any(r in _REFUSAL_REASONS for r in reasons) or _has_refusal(response)
    if refused:
        signals.add(REFUSAL)
    if response is None:
        return signals
    if any(_invalid_arguments(a) for a in _tool_arguments(response)):
        signals.add(TOOL_CALL_INVALID)
    fmt = _json_format(request)
    if fmt is not None and not refused:
        texts = _response_texts(response)
        if texts and any(_invalid_json(t, fmt["schema"]) for t in texts):
            signals.add(SCHEMA_INVALID)
    return signals


# --- regenerate / retry --------------------------------------------------


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for part in _items(content):
        text = _field(part, "text")
        parts.append(text if isinstance(text, str) else f"<{_field(part, 'type')}>")
    return "\n".join(parts)


def _digest(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


def request_key(model: Any, messages: Any, session_id: Any = None) -> str | None:
    """Fingerprint of a request for the regenerate detector, or None."""
    messages = _items(messages)
    if not messages:
        return None
    if isinstance(session_id, str) and session_id:
        last_user = next(
            (m for m in reversed(messages)
             if _field(m, "role") in ("user", None)), messages[-1])
        folded = " ".join(_text(_field(last_user, "content")).casefold().split())
        return _digest("s", session_id, folded)
    flat = [f"{_field(m, 'role')}:{_text(_field(m, 'content'))}" for m in messages]
    return _digest("r", str(model), *flat)


class RegenerateTracker:
    """Bounded, in-memory table of recent request fingerprints.

    `observe(key)` returns REGENERATE or RETRY when `key` was seen within
    `window_s`, depending on whether that earlier call failed, and records
    this one. Only digests and timestamps are kept.
    """

    def __init__(self, window_s: float = 120.0, max_entries: int = 4096,
                 clock=time.monotonic):
        self._window = window_s
        self._max = max_entries
        self._clock = clock
        self._seen: OrderedDict[str, tuple[float, bool]] = OrderedDict()
        # Re-entrant: an abandoned stream's event is built in the garbage
        # collector, which can run while this thread holds the lock.
        self._lock = threading.RLock()

    def observe(self, key: str | None, failed: bool = False) -> str | None:
        if key is None:
            return None
        now = self._clock()
        with self._lock:
            previous = self._seen.pop(key, None)
            self._seen[key] = (now, failed)
            while len(self._seen) > self._max:
                self._seen.popitem(last=False)
        if previous is None or now - previous[0] > self._window:
            return None
        return RETRY if previous[1] else REGENERATE

    def record_failure(self, key: str | None) -> str | None:
        """A call that raised: no event is sent, but a re-send is a retry."""
        return self.observe(key, failed=True)

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


_default_tracker = RegenerateTracker()


def default_tracker() -> RegenerateTracker:
    return _default_tracker


def record_failure(model: Any, messages: Any, session_id: Any = None) -> None:
    """Note a call that raised. Fail-open."""
    try:
        if enabled():
            _default_tracker.record_failure(request_key(model, messages, session_id))
    except Exception as e:  # noqa: BLE001 - fail-open
        logger.debug(f"Outcome signals error: {e}")


# --- event ---------------------------------------------------------------


def attach_outcome_signals(
    event: dict,
    *,
    model: Any,
    messages: Any,
    response: Any = None,
    request: dict | None = None,
    session_id: Any = None,
    reasons: list[str] | None = None,
    tracker: RegenerateTracker | None = None,
) -> dict:
    """Add `outcome_signals` and `outcome_signals_version` to `event`.

    Streams pass `response=None` with the stream's `reasons` and the
    `stream_completed` / `stream_error` fields already on `event`.
    Fail-open: on any error the event is left as it was.
    """
    if not enabled():
        return event
    try:
        signals = response_signals(response, request, reasons)
        chain = event.get("fallback_chain")
        if isinstance(chain, (list, tuple)) and chain:
            signals.add(FALLBACK)
        failed = False
        if event.get("stream_error"):
            signals.add(STREAM_ERROR)
            failed = True
        elif event.get("stream") and event.get("stream_completed") is False:
            signals.add(ABANDONED)
        if session_id is None:
            session_id = event.get("session_id")
        again = (tracker or _default_tracker).observe(
            request_key(model, messages, session_id), failed=failed)
        if again is not None:
            signals.add(again)
        event["outcome_signals"] = sorted(signals & SIGNAL_CLASSES)[:MAX_SIGNALS]
        event["outcome_signals_version"] = OUTCOME_SIGNALS_VERSION
    except Exception as e:  # noqa: BLE001 - fail-open
        logger.debug(f"Outcome signals error: {e}")
    return event
