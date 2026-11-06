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

import copy
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Self

from prompt_shields.pii import find_pii_spans

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
    )

    def __init__(self, messages: Iterable[Any] | None, *,
                 categories: Iterable[str] | None = None) -> None:
        wanted = None if categories is None else frozenset(categories)
        self._by_token: dict[str, str] = {}
        self._by_value: dict[str, str] = {}
        self._counts: dict[str, int] = {}
        self._next: dict[str, int] = {}
        self._restored = 0
        self._unknown = 0
        messages = list(messages or ())
        # Placeholder-shaped text already in the prompt is never minted, so
        # an echo of it can never be restored to a value.
        self._reserved = {
            (kind.upper(), int(n))
            for text in _all_texts(messages)
            for kind, n in _TOKEN_JSON.findall(text)
        }
        self.messages = [self._message(m, wanted) for m in messages]

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

    def _json_value(self, value: Any, wanted: frozenset[str] | None) -> Any:
        if isinstance(value, str):
            return self._text(value, wanted)
        if isinstance(value, list):
            return [self._json_value(v, wanted) for v in value]
        if isinstance(value, dict):
            return {self._text(k, wanted): self._json_value(v, wanted)
                    for k, v in value.items()}
        return value

    def _arguments(self, arguments: str, wanted: frozenset[str] | None) -> str:
        """Tool-call arguments: JSON string values (and keys) when they parse,
        else the raw text."""
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError):
            return self._text(arguments, wanted)
        return json.dumps(self._json_value(parsed, wanted), ensure_ascii=False)

    def _message(self, message: Any, wanted: frozenset[str] | None) -> Any:
        if not isinstance(message, Mapping):
            dump = getattr(message, "model_dump", None)
            if not callable(dump):
                return message
            message = dump(exclude_none=True)
        out = dict(message)
        content = out.get("content")
        if isinstance(content, str):
            out["content"] = self._text(content, wanted)
        elif isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                    part = {**part, "text": self._text(part["text"], wanted)}
                parts.append(part)
            out["content"] = parts
        if isinstance(out.get("tool_calls"), list):
            calls = []
            for tc in out["tool_calls"]:
                fn = tc.get("function") if isinstance(tc, Mapping) else None
                if isinstance(fn, Mapping) and isinstance(fn.get("arguments"), str):
                    tc = {**tc, "function": {
                        **fn, "arguments": self._arguments(fn["arguments"], wanted),
                    }}
                calls.append(tc)
            out["tool_calls"] = calls
        return out

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

    def restore_stream(self, stream: Any) -> Any:
        """Wrap a chat-completions (OpenAI chunk shape) or Anthropic Messages
        stream, sync or async, so placeholders split across chunks are
        restored. The wrapper closes and forwards attributes to ``stream``."""
        if hasattr(stream, "__aiter__") and not hasattr(stream, "__iter__"):
            return _AsyncRestoringStream(stream, _ChunkRestorer(self))
        return _RestoringStream(stream, _ChunkRestorer(self))


def pseudonymise(messages: Iterable[Any] | None, *,
                 categories: Iterable[str] | None = None) -> Pseudonymisation:
    """Pseudonymise chat ``messages`` for one call (see the module docstring).

    ``categories`` limits which ``prompt_shields.pii.SPAN_CATEGORIES`` are
    replaced (default: all of them).
    """
    return Pseudonymisation(messages, categories=categories)


def _all_texts(messages: list) -> list[str]:
    texts: list[str] = []
    for m in messages:
        get = m.get if isinstance(m, Mapping) else lambda k, m=m: getattr(m, k, None)
        content = get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                text = part.get("text") if isinstance(part, Mapping) else None
                if isinstance(text, str):
                    texts.append(text)
        for tc in get("tool_calls") or ():
            fn = tc.get("function") if isinstance(tc, Mapping) else getattr(tc, "function", None)
            args = fn.get("arguments") if isinstance(fn, Mapping) else getattr(fn, "arguments", None)
            if isinstance(args, str):
                texts.append(args)
    return texts


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
    def __init__(self, stream: Any, restorer: _ChunkRestorer) -> None:
        self._stream = stream
        self._restorer = restorer
        self._gen = self._run()

    def _run(self):
        for chunk in self._stream:
            yield from self._restorer.process(chunk)
        yield from self._restorer.finish()

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._gen)

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if close is not None:
            close()

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class _AsyncRestoringStream:
    def __init__(self, stream: Any, restorer: _ChunkRestorer) -> None:
        self._stream = stream
        self._restorer = restorer
        self._gen = self._run()

    async def _run(self):
        async for chunk in self._stream:
            for out in self._restorer.process(chunk):
                yield out
        for out in self._restorer.finish():
            yield out

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._gen.__anext__()

    async def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result

    async def aclose(self) -> None:
        await self.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)
