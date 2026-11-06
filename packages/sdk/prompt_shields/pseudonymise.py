"""Reversible pseudonymisation: replace PII with placeholders before a call,
put the values back into the answer after it (opt-in, #36).

``pseudonymise(messages)`` finds the PII entities ``prompt_shields.pii`` can
locate (email addresses, phone numbers, US SSNs, card numbers, IP addresses,
IBANs and checksummed Nordic national IDs) and replaces each distinct value
with a typed placeholder such as ``⟦EMAIL_1⟧``. The same value gets the same
placeholder everywhere in the call: in string content, in text content parts
and in assistant tool-call arguments. ``restore`` and ``restore_stream`` put
the values back into the model's answer.

The mapping lives only in the returned ``Pseudonymisation`` object, in this
process, for one call:

* it is never logged, never put on a telemetry event and never sent;
* it cannot be pickled or copied, and its ``repr`` shows counts only;
* ``clear()`` (or leaving the ``with`` block) drops it.

Restoration only replaces placeholders this object minted. A placeholder the
model invents (``⟦EMAIL_9⟧``), one that was already in the prompt, and text
that only looks like one (``[EMAIL_1]``) are left as they are. Small changes
the model makes inside the brackets (case, spaces) are tolerated.

What it does not do: detect names, street addresses or other free-text PII;
look inside images, audio or files; or remove keyword topics such as health
data. See ``docs/guides/pseudonymisation.mdx``.
"""

from __future__ import annotations

import contextlib
import copy
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Self

from prompt_shields.pii import find_pii_spans, scan_messages

OPEN = "\u27e6"  # ⟦
CLOSE = "\u27e7"  # ⟧

#: A placeholder, tolerating case and spaces inside the brackets.
_BODY = r"\s*([A-Za-z][A-Za-z_]*?)_(\d{1,6})\s*"
_TOKEN = re.compile(OPEN + _BODY + CLOSE)
#: Inside JSON (tool-call arguments) the brackets may also be ``\u`` escapes.
_JSON_OPEN = r"(?:" + OPEN + r"|\\u27[eE]6)"
_JSON_CLOSE = r"(?:" + CLOSE + r"|\\u27[eE]7)"
_TOKEN_JSON = re.compile(_JSON_OPEN + _BODY + _JSON_CLOSE)
#: What a placeholder looks like before its closing bracket has arrived.
_PARTIAL = re.compile(OPEN + r"\s*(?:[A-Za-z][A-Za-z_]*\d*\s*)?")
_ESC_PREFIX = r"\\(?:u(?:2(?:7(?:[eE])?)?)?)?"
_PARTIAL_JSON = re.compile(
    _ESC_PREFIX + "|" + _JSON_OPEN
    + r"\s*(?:[A-Za-z][A-Za-z_]*\d*\s*(?:" + _ESC_PREFIX + r")?)?"
)
#: Longest tail a stream holds back while waiting for a closing bracket.
_MAX_PENDING = 40


@dataclass(frozen=True)
class PseudonymisationStats:
    """Counts only, never values. ``entities`` is distinct values replaced
    per category; ``restored`` placeholders put back; ``unknown`` placeholder-
    shaped tokens in the answer that were left as they are."""

    entities: Mapping[str, int]
    restored: int
    unknown: int


class Pseudonymisation:
    """One call's pseudonymised messages and the mapping to undo it.

    ``messages`` is a new list: the input is not changed. Use it as a
    context manager to drop the mapping when the call is done.
    """

    __slots__ = (
        "_by_token",
        "_by_value",
        "_counts",
        "_next",
        "_reserved",
        "_restored",
        "_unknown",
        "messages",
        "system",
    )

    def __init__(self, messages: Iterable[Any] | None, *,
                 categories: Iterable[str] | None = None,
                 system: Any = None) -> None:
        wanted = None if categories is None else frozenset(categories)
        self._by_token: dict[str, str] = {}
        self._by_value: dict[str, str] = {}
        self._counts: dict[str, int] = {}
        self._next: dict[str, int] = {}
        self._restored = 0
        self._unknown = 0
        messages = list(messages or ())
        # Placeholder-shaped text already in the prompt is never minted, so
        # an echo of it can never be restored to a value. This is the same
        # walk as the replacement below, so every text replaced is scanned.
        self._reserved: set[tuple[str, int]] = set()

        def reserve(text: str) -> str:
            self._reserved.update(
                (kind.upper(), int(n)) for kind, n in _TOKEN_JSON.findall(text)
            )
            return text

        for m in messages:
            _walk_message(m, reserve)
        _walk_content(system, reserve)

        def replace(text: str) -> str:
            return self._text(text, wanted)

        #: The pseudonymised ``system`` prompt (Anthropic: a string or text
        #: blocks), or None. It comes first in the prompt, so it is numbered
        #: first.
        self.system = _walk_content(system, replace)
        self.messages = [_walk_message(m, replace) for m in messages]

    # --- privacy guards ---------------------------------------------------------

    def __repr__(self) -> str:
        return f"<Pseudonymisation entities={len(self._by_token)}>"

    def __reduce_ex__(self, protocol: Any) -> Any:
        raise TypeError("a Pseudonymisation holds PII and cannot be pickled or copied")

    def __copy__(self) -> Any:
        raise TypeError("a Pseudonymisation holds PII and cannot be copied")

    def __deepcopy__(self, memo: Any) -> Any:
        raise TypeError("a Pseudonymisation holds PII and cannot be copied")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.clear()

    def clear(self) -> None:
        """Drop the mapping. Later restores leave every placeholder as is."""
        self._by_token.clear()
        self._by_value.clear()

    # --- pseudonymising ---------------------------------------------------------

    @property
    def active(self) -> bool:
        """True when at least one value was replaced."""
        return bool(self._by_token)

    def stats(self) -> PseudonymisationStats:
        return PseudonymisationStats(dict(self._counts), self._restored, self._unknown)

    def _token(self, value: str, category: str) -> str:
        token = self._by_value.get(value)
        if token is not None:
            return token
        kind = category.upper()
        n = self._next.get(kind, 1)
        while (kind, n) in self._reserved:
            n += 1
        self._next[kind] = n + 1
        token = f"{OPEN}{kind}_{n}{CLOSE}"
        self._by_value[value] = token
        self._by_token[f"{kind}_{n}"] = value
        self._counts[category] = self._counts.get(category, 0) + 1
        return token

    def _text(self, text: str, wanted: frozenset[str] | None) -> str:
        out: list[str] = []
        last = 0
        for start, end, category in find_pii_spans(text):
            if wanted is not None and category not in wanted:
                continue
            out.append(text[last:start])
            out.append(self._token(text[start:end], category))
            last = end
        out.append(text[last:])
        return "".join(out)

    # --- restoring ----------------------------------------------------------------

    def restore(self, text: str | None, *, json_escape: bool = False) -> str | None:
        """``text`` with this call's placeholders replaced by their values.

        ``json_escape`` escapes each value for use inside a JSON string (tool
        call arguments). Unknown placeholders are left as they are.
        """
        if not text or (OPEN not in text and not json_escape):
            return text

        def put_back(m: re.Match) -> str:
            value = self._by_token.get(f"{m.group(1).upper()}_{int(m.group(2))}")
            if value is None:
                self._unknown += 1
                return m.group(0)
            self._restored += 1
            return json.dumps(value, ensure_ascii=False)[1:-1] if json_escape else value

        return (_TOKEN_JSON if json_escape else _TOKEN).sub(put_back, text)

    def stream_restorer(self, *, json_escape: bool = False) -> StreamRestorer:
        return StreamRestorer(self, json_escape=json_escape)

    def restore_response(self, response: Any) -> Any:
        """Restore a whole (not streamed) response in place and return it.

        OpenAI chat completions: each choice's message ``content``,
        ``refusal`` and tool-call ``arguments`` (JSON-escaped). Anthropic
        Messages: ``text`` blocks and string values in ``tool_use`` input.
        Thinking blocks are left as they are (their signature covers the
        text). Objects and dicts are both accepted.
        """
        choices = _get(response, "choices")
        if isinstance(choices, list):
            for choice in choices:
                message = _get(choice, "message")
                if message is None:
                    continue
                for name in ("content", "refusal"):
                    if isinstance(_get(message, name), str):
                        _set(message, name, self.restore(_get(message, name)))
                for tc in _get(message, "tool_calls") or ():
                    fn = _get(tc, "function")
                    if fn is not None and isinstance(_get(fn, "arguments"), str):
                        _set(fn, "arguments",
                             self.restore(_get(fn, "arguments"), json_escape=True))
            return response
        content = _get(response, "content")
        if isinstance(content, list):
            for block in content:
                kind = _get(block, "type")
                if kind == "text" and isinstance(_get(block, "text"), str):
                    _set(block, "text", self.restore(_get(block, "text")))
                elif kind == "tool_use" and isinstance(_get(block, "input"), Mapping):
                    _set(block, "input", _json_value(
                        _get(block, "input"), lambda s: self.restore(s) or s,
                    ))
        return response

    def restore_stream(self, stream: Any, *, clear_when_done: bool = False) -> Any:
        """Wrap a chat-completions (OpenAI chunk shape) or Anthropic Messages
        stream, sync or async, so placeholders split across chunks are
        restored. The wrapper closes and forwards attributes to ``stream``.

        ``clear_when_done`` drops the mapping once the stream ends, fails or
        is closed (the instrumented clients' ``pseudonymise=True`` uses it).
        """
        on_done = self.clear if clear_when_done else None
        if hasattr(stream, "__aiter__") and not hasattr(stream, "__iter__"):
            return _AsyncRestoringStream(stream, _ChunkRestorer(self), on_done)
        return _RestoringStream(stream, _ChunkRestorer(self), on_done)


def pseudonymise(messages: Iterable[Any] | None, *,
                 categories: Iterable[str] | None = None,
                 system: Any = None) -> Pseudonymisation:
    """Pseudonymise chat ``messages`` for one call (see the module docstring).

    ``categories`` limits which ``prompt_shields.pii.SPAN_CATEGORIES`` are
    replaced (default: all of them). ``system`` is an Anthropic system
    prompt (a string or text blocks); its pseudonymised copy is ``.system``,
    with the same placeholders as the messages.
    """
    return Pseudonymisation(messages, categories=categories, system=system)


def _json_value(value: Any, text: Callable[[str], str]) -> Any:
    """``text`` applied to every string, keys included, in a JSON value."""
    if isinstance(value, str):
        return text(value)
    if isinstance(value, list):
        return [_json_value(v, text) for v in value]
    if isinstance(value, Mapping):
        return {text(k) if isinstance(k, str) else k: _json_value(v, text)
                for k, v in value.items()}
    return value


def _arguments(arguments: str, text: Callable[[str], str]) -> str:
    """Tool-call arguments: JSON string values (and keys) when they parse,
    else the raw text."""
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError):
        return text(arguments)
    return json.dumps(_json_value(parsed, text), ensure_ascii=False)


def _walk_content(content: Any, text: Callable[[str], str]) -> Any:
    """``text`` applied to message content: a string or a list of parts.

    Covered: text parts, Anthropic ``tool_result`` parts (their string or
    text-part ``content``) and Anthropic ``tool_use`` parts (string values in
    ``input``). Images, audio, files and other parts are kept as they are.
    """
    if isinstance(content, str):
        return text(content)
    if not isinstance(content, list):
        return content
    parts = []
    for part in content:
        if not isinstance(part, Mapping):
            dump = getattr(part, "model_dump", None)
            if callable(dump):
                part = dump(exclude_none=True)
        if isinstance(part, Mapping):
            if isinstance(part.get("text"), str):
                part = {**part, "text": text(part["text"])}
            if part.get("type") == "tool_result" and "content" in part:
                part = {**part, "content": _walk_content(part["content"], text)}
            if part.get("type") == "tool_use" and isinstance(part.get("input"), Mapping):
                part = {**part, "input": _json_value(part["input"], text)}
        parts.append(part)
    return parts


def _walk_message(message: Any, text: Callable[[str], str]) -> Any:
    """A copy of one chat message with ``text`` applied to its content and
    its assistant tool-call arguments. SDK message objects become dicts;
    anything else is returned as it is."""
    if not isinstance(message, Mapping):
        dump = getattr(message, "model_dump", None)
        if not callable(dump):
            return message
        message = dump(exclude_none=True)
    out = dict(message)
    if "content" in out:
        out["content"] = _walk_content(out["content"], text)
    if isinstance(out.get("tool_calls"), list):
        calls = []
        for tc in out["tool_calls"]:
            fn = tc.get("function") if isinstance(tc, Mapping) else None
            if isinstance(fn, Mapping) and isinstance(fn.get("arguments"), str):
                tc = {**tc, "function": {
                    **fn, "arguments": _arguments(fn["arguments"], text),
                }}
            calls.append(tc)
        out["tool_calls"] = calls
    return out


class ClientCall:
    """``pseudonymise=True`` on ``ShieldsClient`` / ``AsyncShieldsClient``:
    one call's pseudonymisation, from the request to the restored answer.

    Construction pseudonymises ``messages`` and, in place in ``kwargs``,
    Anthropic's ``system``. A call that carries ``X-PS-*`` headers (route
    hints or gateway context) is refused with ``ValueError``: it goes to a
    Prompt Shields gateway, which would classify the placeholders and could
    route the call below the sensitivity of the original prompt.
    """

    __slots__ = ("_original_pii", "_pseudo", "messages")

    def __init__(self, messages: Any, kwargs: dict, *, scan_pii: bool) -> None:
        headers = kwargs.get("extra_headers") or {}
        if any(str(k).lower().startswith("x-ps-") for k in headers):
            raise ValueError(
                "pseudonymise=True cannot be used on a call to the Prompt Shields "
                "gateway (route hints or X-PS-* headers): the gateway would "
                "classify the placeholders, not the original prompt"
            )
        self._pseudo = Pseudonymisation(messages, system=kwargs.get("system"))
        if kwargs.get("system") is not None:
            kwargs["system"] = self._pseudo.system
        self.messages = self._pseudo.messages
        # Labels only: the categories of what the caller wrote, which the
        # client's own scan of the placeholders would no longer see.
        self._original_pii = scan_messages(messages or []) if scan_pii else None

    def __repr__(self) -> str:
        return f"<ClientCall {self._pseudo!r}>"

    def event_hook(self, hook: Callable[[dict], None] | None) -> Callable[[dict], None] | None:
        """``hook`` preceded by putting the original prompt's PII categories
        on the telemetry event (``detected_pii_types``)."""
        if self._original_pii is None:
            return hook
        categories = list(self._original_pii)

        def report_original(event: dict) -> None:
            if categories:
                event["detected_pii_types"] = list(categories)
            else:
                event.pop("detected_pii_types", None)
            if hook is not None:
                hook(event)

        return report_original

    def response(self, response: Any) -> Any:
        """The response restored in place, with counts as
        ``ps_pseudonymisation``; the mapping is dropped."""
        try:
            self._pseudo.restore_response(response)
            with contextlib.suppress(Exception):
                object.__setattr__(response, "ps_pseudonymisation", self._pseudo.stats())
            return response
        finally:
            self._pseudo.clear()

    def stream(self, stream: Any) -> Any:
        """The stream restoring placeholders; the mapping is dropped when it
        ends, fails or is closed."""
        return self._pseudo.restore_stream(stream, clear_when_done=True)

    def clear(self) -> None:
        self._pseudo.clear()


class StreamRestorer:
    """Restores one text stream fed in pieces (one per content field).

    ``feed`` returns what can be emitted now; a tail that could still become
    a placeholder (up to 40 characters after ``⟦``) is held back until it is
    complete or clearly is not one. ``flush`` returns the rest at the end.
    """

    def __init__(self, owner: Pseudonymisation, *, json_escape: bool = False) -> None:
        self._owner = owner
        self._json = json_escape
        self._pending = ""

    @property
    def pending(self) -> bool:
        return bool(self._pending)

    def feed(self, text: str | None) -> str:
        if not text:
            return ""
        data = self._pending + text
        self._pending = ""
        partial = _PARTIAL_JSON if self._json else _PARTIAL
        starts = [data.rfind(OPEN)]
        if self._json:
            starts += [data.rfind("\\u27e6"), data.rfind("\\u27E6"), data.rfind("\\")]
        # The earliest start whose tail could still become a placeholder.
        for cut in sorted(c for c in starts if c != -1 and len(data) - c <= _MAX_PENDING):
            if partial.fullmatch(data, cut):
                data, self._pending = data[:cut], data[cut:]
                break
        return self._owner.restore(data, json_escape=self._json) or ""

    def flush(self) -> str:
        data, self._pending = self._pending, ""
        return self._owner.restore(data, json_escape=self._json) or ""


# --- stream wrappers -------------------------------------------------------------


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _set(obj: Any, name: str, value: Any) -> None:
    if isinstance(obj, dict):
        obj[name] = value
    else:
        setattr(obj, name, value)


class _ChunkRestorer:
    """Per-field restorers over a stream of chunks.

    OpenAI chunks: ``choices[i].delta.content`` / ``.refusal`` and
    ``choices[i].delta.tool_calls[j].function.arguments`` (JSON-escaped),
    flushed on the choice's ``finish_reason``. Anthropic events:
    ``content_block_delta`` ``text`` / ``partial_json``, flushed on
    ``content_block_stop``. Text still held back when a field ends comes out
    in an extra chunk, a copy of the field's last chunk.
    """

    def __init__(self, owner: Pseudonymisation) -> None:
        self._owner = owner
        self._restorers: dict[tuple, StreamRestorer] = {}
        self._last: dict[tuple, Any] = {}

    def _feed(self, key: tuple, text: Any, chunk: Any) -> Any:
        if not isinstance(text, str):
            return text
        restorer = self._restorers.get(key)
        if restorer is None:
            restorer = self._owner.stream_restorer(json_escape=key[0] in ("t", "j"))
            self._restorers[key] = restorer
        self._last[key] = chunk
        return restorer.feed(text)

    def _flush(self, key: tuple) -> Any:
        restorer = self._restorers.pop(key, None)
        last = self._last.pop(key, None)
        if restorer is None or not restorer.pending or last is None:
            return None
        return self._synthetic(key, last, restorer.flush())

    @staticmethod
    def _synthetic(key: tuple, last: Any, text: str) -> Any:
        chunk = copy.deepcopy(last)
        if key[0] in ("a", "j"):  # Anthropic text / partial_json
            _set(_get(chunk, "delta"), "partial_json" if key[0] == "j" else "text", text)
            return chunk
        choice = _choice(chunk, key[1])
        _set(chunk, "choices", [choice])
        _set(choice, "finish_reason", None)
        delta = _get(choice, "delta")
        for name in ("content", "refusal", "tool_calls"):
            if _get(delta, name) is not None:
                _set(delta, name, None)
        if key[0] == "t":
            last_delta = _get(_choice(last, key[1]), "delta")
            tc = copy.deepcopy(next(
                t for t in _get(last_delta, "tool_calls") if _get(t, "index") == key[2]
            ))
            for name in ("id", "type"):
                if _get(tc, name) is not None:
                    _set(tc, name, None)
            fn = _get(tc, "function")
            if _get(fn, "name") is not None:
                _set(fn, "name", None)
            _set(fn, "arguments", text)
            _set(delta, "tool_calls", [tc])
        else:
            _set(delta, "content" if key[0] == "c" else "refusal", text)
        return chunk

    def process(self, chunk: Any) -> list:
        """The chunks to emit for ``chunk``, restored."""
        before: list = []
        kind = _get(chunk, "type")
        if kind == "content_block_delta":
            index = _get(chunk, "index")
            delta = _get(chunk, "delta")
            if isinstance(_get(delta, "text"), str):
                _set(delta, "text", self._feed(("a", index), _get(delta, "text"), chunk))
            elif isinstance(_get(delta, "partial_json"), str):
                _set(delta, "partial_json",
                     self._feed(("j", index), _get(delta, "partial_json"), chunk))
            return [chunk]
        if kind == "content_block_stop":
            index = _get(chunk, "index")
            for key in (("a", index), ("j", index)):
                extra = self._flush(key)
                if extra is not None:
                    before.append(extra)
            return [*before, chunk]

        for choice in _get(chunk, "choices") or ():
            index = _get(choice, "index")
            delta = _get(choice, "delta")
            # Where each field of this choice was written in this chunk.
            fed: dict[tuple, tuple[Any, str]] = {}
            if delta is not None:
                for code, name in (("c", "content"), ("r", "refusal")):
                    if isinstance(_get(delta, name), str):
                        key = (code, index)
                        _set(delta, name, self._feed(key, _get(delta, name), chunk))
                        fed[key] = (delta, name)
                for tc in _get(delta, "tool_calls") or ():
                    fn = _get(tc, "function")
                    if fn is not None and isinstance(_get(fn, "arguments"), str):
                        key = ("t", index, _get(tc, "index"))
                        _set(fn, "arguments", self._feed(key, _get(fn, "arguments"), chunk))
                        fed[key] = (fn, "arguments")
            if _get(choice, "finish_reason") is None:
                continue
            for key in [k for k in self._restorers if k[0] in ("c", "r", "t") and k[1] == index]:
                if key in fed:
                    # Written in this chunk: append the rest in place.
                    obj, name = fed[key]
                    restorer = self._restorers.pop(key)
                    self._last.pop(key, None)
                    _set(obj, name, (_get(obj, name) or "") + restorer.flush())
                    continue
                extra = self._flush(key)
                if extra is not None:
                    before.append(extra)
        return [*before, chunk]

    def finish(self) -> list:
        """Extra chunks for text still held back when the stream ends."""
        out = []
        for key in list(self._restorers):
            extra = self._flush(key)
            if extra is not None:
                out.append(extra)
        return out


def _choice(chunk: Any, index: Any) -> Any:
    return next(c for c in _get(chunk, "choices") if _get(c, "index") == index)


class _RestoringStream:
    def __init__(self, stream: Any, restorer: _ChunkRestorer,
                 on_done: Callable[[], None] | None = None) -> None:
        self._stream = stream
        self._restorer = restorer
        self._on_done = on_done
        self._gen = self._run()

    def _done(self) -> None:
        if self._on_done is not None:
            self._on_done()

    def _run(self):
        try:
            for chunk in self._stream:
                yield from self._restorer.process(chunk)
            yield from self._restorer.finish()
        finally:
            self._done()

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._gen)

    def close(self) -> None:
        try:
            close = getattr(self._stream, "close", None)
            if close is not None:
                close()
        finally:
            self._done()

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class _AsyncRestoringStream:
    def __init__(self, stream: Any, restorer: _ChunkRestorer,
                 on_done: Callable[[], None] | None = None) -> None:
        self._stream = stream
        self._restorer = restorer
        self._on_done = on_done
        self._gen = self._run()

    def _done(self) -> None:
        if self._on_done is not None:
            self._on_done()

    async def _run(self):
        try:
            async for chunk in self._stream:
                for out in self._restorer.process(chunk):
                    yield out
            for out in self._restorer.finish():
                yield out
        finally:
            self._done()

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._gen.__anext__()

    async def close(self) -> None:
        try:
            close = getattr(self._stream, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result
        finally:
            self._done()

    async def aclose(self) -> None:
        await self.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)
