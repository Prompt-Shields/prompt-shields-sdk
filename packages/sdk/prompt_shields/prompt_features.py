"""Local prompt feature extraction (#45, PRD §6.2 and §7.13).

Turns a chat ``messages`` list into the context a router or learner uses:
complexity features (approximate tokens, code, JSON schema, language, use
case) and, optionally, a small local embedding. Everything runs in this
process, with no network calls and no model download.

Privacy contract:

* :meth:`PromptFeatures.to_dict` holds no prompt text and no hash of it, only
  counts, booleans and coarse labels. It is safe to log or export locally,
  as the teardown toolkit's learning tuples do.
* :attr:`PromptFeatures.embedding` is a vector derived from the prompt's
  words. It is **local only**: it is not in :meth:`~PromptFeatures.to_dict`,
  not in any telemetry event, and must not be sent to the collector (PRD
  §7.13: "never prompt text or embeddings").
* The SDK clients compute features only when given ``on_prompt_features``.
  The features go to that callback and nowhere else. The telemetry event is
  identical with or without it.

Determinism: the same messages give the same features on every platform and
Python version (no ``hash()`` salt, no randomness). Any change to what a
field means bumps :data:`FEATURE_SCHEMA_VERSION`, so learner state and
exported tuples can tell feature generations apart.

Cost: one linear pass over the message text (the embedding and the language
guess look at the first :data:`EMBED_MAX_CHARS` / :data:`LANG_MAX_CHARS`
characters only). See ``tests/test_prompt_features.py`` for the benchmark.
"""

from __future__ import annotations

import logging
import math
import re
import zlib
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from prompt_shields.route_policy import (
    _JSON_SCHEMA,  # the router heuristic's schema wording, shared on purpose
    COMPLEX_TOKEN_THRESHOLD,
    estimate_input_tokens,
)

logger = logging.getLogger(__name__)

#: Version of the feature schema. Bump on any change to a field's meaning,
#: the embedding (dimension, tokenisation, hashing) or the language labels.
FEATURE_SCHEMA_VERSION = "ps-prompt-features.v1"

#: Embedding dimension.
EMBED_DIM = 64
#: Characters of prompt text the embedding reads (bounds per-call cost).
EMBED_MAX_CHARS = 16_000
#: Characters of prompt text the language guess reads.
LANG_MAX_CHARS = 2_000

_WORD = re.compile(r"\w+")
_LATIN_WORD = re.compile(r"[a-zà-ÿ]+")

def _words(s: str) -> frozenset[str]:
    return frozenset(s.split())


# Small stopword sets: enough to tell the common Latin-script languages apart
# on a few sentences. Anything else in Latin script is "und-Latn".
_STOPWORDS: dict[str, frozenset[str]] = {
    "en": _words("the and of to is in that it for you with this are be on not"),
    "es": _words("el la de que y en los las por una para con es del se"),
    "fr": _words("le la les de des et est une pour que dans un du pas avec"),
    "de": _words("der die das und ist nicht ein eine zu mit den von sie ich auf"),
    "pt": _words("o a os de que e do da em um uma para com não por"),
    "it": _words("il di che la e per un una non sono gli della con del"),
    "nl": _words("de het een van en is dat niet op te zijn met voor"),
}


@dataclass(frozen=True)
class PromptFeatures:
    """Features of one prompt. See the module docstring for the contract."""

    #: :data:`FEATURE_SCHEMA_VERSION` at extraction time.
    feature_schema: str
    #: ``ceil(chars / 4)`` over all message text (the router's estimate).
    approx_input_tokens: int
    message_count: int
    #: A Markdown code fence appears in the text.
    has_code: bool
    #: The text asks for JSON-schema / structured output.
    has_json_schema: bool
    #: ISO 639-1 code (``en``, ``es``, ``fr``, ``de``, ``pt``, ``it``, ``nl``,
    #: ``ja``, ``ko``, ``zh``), else ``und-<Script>`` (``und-Latn``,
    #: ``und-Cyrl``, ``und-Arab``, ``und-Deva``, ``und-Grek``, ``und-Hebr``)
    #: or ``und`` when there is no text.
    language: str
    #: The caller's declared use case label, passed through (not inferred).
    use_case: str | None
    #: ``complex`` when tokens exceed the router threshold, or code or schema
    #: wording is present; else ``simple``.
    complexity: str
    #: L2-normalised signed feature-hashing vector of the prompt's words,
    #: :data:`EMBED_DIM` floats, or ``None`` when not requested. Local only.
    embedding: tuple[float, ...] | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """The exportable features: everything except the embedding."""
        d = asdict(self)
        del d["embedding"]
        return d


def _texts(messages: Any) -> list[str]:
    out: list[str] = []
    for m in messages if isinstance(messages, list) else []:
        content = m.get("content") if isinstance(m, Mapping) else None
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            out.extend(
                p["text"]
                for p in content
                if isinstance(p, Mapping) and isinstance(p.get("text"), str)
            )
    return out


def _script_of(ch: str) -> str | None:
    o = ord(ch)
    if o < 0x250:
        return "Latn" if ch.isalpha() else None
    if 0x3040 <= o <= 0x30FF:
        return "Kana"
    if 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF:
        return "Hang"
    if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
        return "Hani"
    if 0x0400 <= o <= 0x04FF:
        return "Cyrl"
    if 0x0600 <= o <= 0x06FF:
        return "Arab"
    if 0x0900 <= o <= 0x097F:
        return "Deva"
    if 0x0370 <= o <= 0x03FF:
        return "Grek"
    if 0x0590 <= o <= 0x05FF:
        return "Hebr"
    return None


def detect_language(text: str) -> str:
    """Coarse language label for ``text`` (see :attr:`PromptFeatures.language`)."""
    sample = text[:LANG_MAX_CHARS]
    counts: dict[str, int] = {}
    for ch in sample:
        s = _script_of(ch)
        if s is not None:
            counts[s] = counts.get(s, 0) + 1
    if not counts:
        return "und"
    # Japanese mixes kana with kanji; any real share of kana means Japanese.
    if counts.get("Kana", 0) * 5 >= sum(counts.get(s, 0) for s in ("Kana", "Hani")) > 0:
        return "ja"
    script = max(sorted(counts), key=lambda s: counts[s])
    if script == "Hang":
        return "ko"
    if script == "Hani":
        return "zh"
    if script != "Latn":
        return f"und-{script}"
    words = _LATIN_WORD.findall(sample.lower())
    best, best_hits = "und-Latn", 0
    for lang in sorted(_STOPWORDS):
        hits = sum(1 for w in words if w in _STOPWORDS[lang])
        if hits > best_hits:
            best, best_hits = lang, hits
    return best if best_hits >= 2 else "und-Latn"


def embed_text(text: str, dim: int = EMBED_DIM) -> tuple[float, ...]:
    """Signed feature hashing of lowercased words into ``dim`` buckets.

    CRC-32 picks the bucket and the sign, so the vector is the same on every
    platform. Hashing many words into few buckets loses the words; the
    vector still must stay in this process (module docstring).
    """
    vec = [0.0] * dim
    for word in _WORD.findall(text[:EMBED_MAX_CHARS].lower()):
        h = zlib.crc32(word.encode("utf-8"))
        vec[h % dim] += 1.0 if (h >> 31) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return tuple(vec)
    return tuple(round(v / norm, 6) for v in vec)


def extract_prompt_features(
    messages: Any,
    *,
    use_case: str | None = None,
    embed: bool = False,
) -> PromptFeatures:
    """Compute :class:`PromptFeatures` for a chat ``messages`` list, locally.

    Reads string contents and the ``text`` of content parts; images, audio
    and tool payloads are ignored. ``embed=True`` also computes the local
    embedding. Never raises on odd input: a non-list counts as no messages.
    """
    msgs = messages if isinstance(messages, list) else []
    texts = _texts(msgs)
    approx = estimate_input_tokens(msgs)
    has_code = any("```" in t for t in texts)
    has_schema = any(_JSON_SCHEMA.search(t) for t in texts)
    joined = "\n".join(texts)
    return PromptFeatures(
        feature_schema=FEATURE_SCHEMA_VERSION,
        approx_input_tokens=approx,
        message_count=len(msgs),
        has_code=has_code,
        has_json_schema=has_schema,
        language=detect_language(joined),
        use_case=use_case,
        complexity=(
            "complex"
            if approx > COMPLEX_TOKEN_THRESHOLD or has_code or has_schema
            else "simple"
        ),
        embedding=embed_text(joined) if embed else None,
    )


def notify_prompt_features(
    callback: Callable[[PromptFeatures], Any] | None,
    messages: Any,
    use_case: str | None,
) -> None:
    """Hand one call's features, with the embedding, to ``callback``.

    Used by the SDK clients' ``on_prompt_features`` option. Does nothing
    when ``callback`` is None. Fail-open: an error in extraction or in the
    callback never reaches the provider call.
    """
    if callback is None:
        return
    try:
        callback(extract_prompt_features(messages, use_case=use_case, embed=True))
    except Exception:
        logger.debug("on_prompt_features failed", exc_info=True)


__all__ = [
    "EMBED_DIM",
    "FEATURE_SCHEMA_VERSION",
    "PromptFeatures",
    "detect_language",
    "embed_text",
    "extract_prompt_features",
    "notify_prompt_features",
]
