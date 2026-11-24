"""Token estimates for calls whose provider reported no usage (#389).

When a stream ends without the provider's usage (it was cancelled or cut,
or the provider never sends it), the SDK and the gateway estimate the
tokens instead and mark the event `usage_source: "estimated"`.

The estimate approximates a byte-pair tokenizer without shipping its
vocabulary: the text is split into the pieces a GPT-style pre-tokenizer
would make (words with their leading space, digit groups, punctuation
runs, whitespace runs, CJK characters), and each piece is costed with
per-family parameters fitted to OpenAI's `o200k_base` and `cl100k_base`
encodings. The algorithm and its parameters are specified in
`schemas/stream-usage/README.md`; the gateway implements the same
algorithm (`gateway/src/middlewares/psUsageEstimate.ts`) and both pass the
shared vectors in `schemas/stream-usage/vectors/`.

All arithmetic is in integer hundredths of a token, so both
implementations return the same number for the same text.
"""

from __future__ import annotations

import json
from typing import Any

# Families: the parameter set an estimate uses. `o200k` for OpenAI's
# current models (and Gemini, whose large vocabulary behaves alike on
# non-English text); `cl100k` for everything else.
FAMILY_O200K = "o200k"
FAMILY_CL100K = "cl100k"

# Per-family costs, in hundredths of a token (see the module docstring).
#   word_free:  ASCII word length (per camelCase part) that is one token
#   word_step:  further letters per extra token
#   latin:      per letter of a word with accented Latin letters
#   alpha:      per letter of a Greek/Cyrillic/other alphabetic word
#   cjk:        per CJK ideograph or kana
#   hangul:     per Hangul syllable
#   cjk_punct:  per CJK / full-width punctuation mark
#   symbol:     per emoji or other symbol
#   punct_run:  ASCII punctuation characters per token
#   digit_run:  digits per token
#   glued:      a single punctuation mark glued to the word after it
FAMILY_PARAMS: dict[str, dict[str, int]] = {
    FAMILY_O200K: {
        "word_free": 8, "word_step": 3, "latin": 15, "alpha": 21,
        "cjk": 65, "hangul": 65, "cjk_punct": 50, "symbol": 150,
        "punct_run": 3, "digit_run": 3, "glued": 50,
    },
    FAMILY_CL100K: {
        "word_free": 8, "word_step": 3, "latin": 49, "alpha": 42,
        "cjk": 105, "hangul": 130, "cjk_punct": 50, "symbol": 175,
        "punct_run": 3, "digit_run": 3, "glued": 50,
    },
}

# Framing tokens per message and per request (role markers, reply primer),
# as OpenAI documents for chat models; images and other media count flat.
MESSAGE_FRAMING_TOKENS = 3
REPLY_FRAMING_TOKENS = 3
MEDIA_TOKENS = 1600

# The streaming counter estimates its buffer once it is this long.
_FLUSH_CHARS = 4096
# A buffer this long with no safe cut point is estimated as is.
_MAX_BUFFER_CHARS = 65536

_WORD = ("asc", "lat", "alp")


def token_family(model: str | None, vendor: str | None = None) -> str:
    """The estimate family for a model (`o200k` or `cl100k`)."""
    m = (model or "").lower()
    if "/" in m:
        m = m.rsplit("/", 1)[1]
    if m.startswith(("gpt-4o", "gpt-4.1", "gpt-4.5", "gpt-5", "chatgpt-", "o1", "o3", "o4", "gpt-oss")):
        return FAMILY_O200K
    if m.startswith("gemini") or (vendor or "").lower() in ("google", "vertex-ai", "gemini"):
        return FAMILY_O200K
    return FAMILY_CL100K


_SPACES = frozenset(
    [9, 10, 11, 12, 13, 32, 0x85, 0xA0, 0x1680, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000]
    + list(range(0x2000, 0x200B))
)


def _is_space(ch: str) -> bool:
    return ord(ch) in _SPACES


def _cls(ch: str) -> str:
    o = ord(ch)
    if o in _SPACES:
        return "sp"
    if 48 <= o <= 57:
        return "dig"
    if 65 <= o <= 90 or 97 <= o <= 122:
        return "asc"
    if o < 128:
        return "pun"
    if 0xC0 <= o <= 0x24F and o not in (0xD7, 0xF7):
        return "lat"
    if 0x370 <= o <= 0x1FFF:
        return "alp"
    if 0x2000 <= o <= 0x2BFF:
        return "sym"
    if 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F or 0xAC00 <= o <= 0xD7AF:
        return "han"
    if 0x3000 <= o <= 0x303F or 0xFF01 <= o <= 0xFF0F or 0xFF1A <= o <= 0xFF20:
        return "cjp"
    if 0x2E80 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF:
        return "cjk"
    if o >= 0x1F000 or 0xFE00 <= o <= 0xFE0F:
        return "sym"
    if o < 0x370:
        return "pun"
    return "alp"


def _runs(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    n = len(text)
    i = 0
    while i < n:
        c = _cls(text[i])
        j = i + 1
        if c in _WORD:
            while j < n and _cls(text[j]) in _WORD:
                j += 1
            out.append(("word", text[i:j]))
        else:
            while j < n and _cls(text[j]) == c:
                j += 1
            out.append((c, text[i:j]))
        i = j
    return out


def _camel_parts(word: str) -> list[int]:
    lengths = []
    start = 0
    for k in range(1, len(word)):
        if "a" <= word[k - 1] <= "z" and "A" <= word[k] <= "Z":
            lengths.append(k - start)
            start = k
    lengths.append(len(word) - start)
    return lengths


def _ceil_div(a: int, b: int) -> int:
    return (a + b - 1) // b


def text_hundredths(text: str, family: str = FAMILY_CL100K) -> int:
    """Estimated tokens of `text`, in hundredths of a token."""
    p = FAMILY_PARAMS.get(family) or FAMILY_PARAMS[FAMILY_CL100K]
    total = 0
    runs = _runs(text)
    last = len(runs) - 1
    for idx, (c, run) in enumerate(runs):
        n = len(run)
        prev = runs[idx - 1][0] if idx > 0 else None
        nxt = runs[idx + 1][0] if idx < last else None
        if c == "word":
            kinds = {_cls(ch) for ch in run}
            if kinds == {"asc"}:
                for k in _camel_parts(run):
                    total += 100 if k <= p["word_free"] else 100 + 100 * _ceil_div(k - p["word_free"], p["word_step"])
            elif "alp" in kinds:
                total += max(100, n * p["alpha"])
            else:
                total += max(100, n * p["latin"])
        elif c == "dig":
            total += 100 * _ceil_div(n, p["digit_run"])
        elif c == "pun":
            if n == 1 and nxt == "word":
                total += 100 if prev == "sp" else p["glued"]
            else:
                total += 100 * _ceil_div(n, p["punct_run"])
        elif c == "cjk":
            total += n * p["cjk"]
        elif c == "han":
            total += n * p["hangul"]
        elif c == "cjp":
            total += n * p["cjk_punct"]
        elif c == "sym":
            total += n * p["symbol"]
        else:  # whitespace
            r = run
            if (nxt is not None and nxt != "dig" and r[-1] == " ") or (nxt == "word" and r[-1] == "\t"):
                r = r[:-1]  # the last space joins the next piece
            if not r:
                continue
            if prev == "pun":
                cut = max(r.rfind("\n"), r.rfind("\r"))
                if cut >= 0:
                    r = r[cut + 1:]  # newlines join the punctuation before
                    if not r:
                        continue
            total += 100
    return total


def _round(hundredths: int) -> int:
    return (hundredths + 50) // 100


def estimate_text_tokens(text: str, family: str = FAMILY_CL100K) -> int:
    """Estimated tokens of `text` for a model family."""
    if not text:
        return 0
    return _round(text_hundredths(text, family))


def _safe_cut(buf: str) -> int:
    """Index of a lone space between two non-space characters, from the end.

    Estimating the text before and after such a space separately gives the
    same total as estimating it whole, so the counter can flush there.
    """
    i = len(buf) - 2
    while i > 0:
        if buf[i] == " " and not _is_space(buf[i - 1]) and not _is_space(buf[i + 1]):
            return i
        i -= 1
    return -1


class TokenCounter:
    """Estimates the tokens of text that arrives in pieces (stream deltas).

    Keeps at most a few KB: complete parts are estimated as the buffer
    grows. The total equals `estimate_text_tokens` of the joined text,
    except for a buffer that grows past 64K characters with no space in it.
    """

    def __init__(self, families: tuple[str, ...] = (FAMILY_O200K, FAMILY_CL100K)):
        self._families = families
        self._done = dict.fromkeys(families, 0)
        self._buf = ""

    def feed(self, text: str) -> None:
        if not isinstance(text, str) or not text:
            return
        self._buf += text
        if len(self._buf) < _FLUSH_CHARS:
            return
        cut = _safe_cut(self._buf)
        if cut <= 0:
            if len(self._buf) < _MAX_BUFFER_CHARS:
                return
            cut = len(self._buf)
        head, self._buf = self._buf[:cut], self._buf[cut:]
        for f in self._families:
            self._done[f] += text_hundredths(head, f)

    def tokens(self, family: str) -> int:
        if family not in self._done:
            family = self._families[-1]
        return _round(self._done[family] + text_hundredths(self._buf, family))

    @property
    def empty(self) -> bool:
        return not self._buf and not any(self._done.values())


def _json(value: Any) -> str:
    try:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def _content_hundredths(content: Any, family: str) -> int:
    if isinstance(content, str):
        return text_hundredths(content, family)
    if not isinstance(content, list):
        return 0
    total = 0
    for part in content:
        if isinstance(part, str):
            total += text_hundredths(part, family)
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        text = part.get("text")
        if isinstance(text, str):
            total += text_hundredths(text, family)
        elif kind in ("image", "image_url", "input_image", "input_audio", "input_file", "file", "document"):
            total += MEDIA_TOKENS * 100
        elif kind in ("tool_use", "tool_result"):
            inner = part.get("content")
            if inner is not None:
                total += _content_hundredths(inner, family)
            else:
                total += text_hundredths(_json(part.get("input", {})), family)
        elif isinstance(part.get("thinking"), str):
            total += text_hundredths(part["thinking"], family)
    return total


def estimate_messages_tokens(messages: Any, family: str = FAMILY_CL100K, system: Any = None) -> int:
    """Estimated input tokens of a chat request's messages (and system)."""
    total = 0
    if system is not None:
        total += _content_hundredths(system, family)
    count = 0
    for m in messages if isinstance(messages, list) else []:
        if not isinstance(m, dict):
            continue
        count += 1
        total += _content_hundredths(m.get("content"), family)
        for call in m.get("tool_calls") or []:
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict):
                total += text_hundredths(str(fn.get("name") or ""), family)
                total += text_hundredths(str(fn.get("arguments") or ""), family)
    if not count and not total:
        return 0
    return _round(total) + MESSAGE_FRAMING_TOKENS * count + REPLY_FRAMING_TOKENS
