"""Lightweight PII detection on prompt input text.

Pattern-based heuristics — fast, no ML, no network. Detects category presence,
not content. Used to populate `detected_pii_types` on events without ever
storing the prompt text.

Phase 1 covers the common categories most enterprises care about for AI
governance. Best-effort — designed as a signal, not a guarantee.
"""

import re
from collections.abc import Iterable

from prompt_shields.national_ids import (
    contains_nordic_national_id,
    find_nordic_national_ids,
)

# Compiled patterns — order independent, all run in one pass
_PATTERNS = {
    "email": re.compile(
        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
    ),
    "phone": re.compile(
        # international + US formats, 10-15 digits with optional separators
        r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3,4}[-.\s]?\d{4}",
    ),
    "ssn": re.compile(
        r"\b\d{3}-\d{2}-\d{4}\b",
    ),
    "credit_card": re.compile(
        # Loose match — 13-19 digits with optional separators
        r"\b(?:\d[ -]*?){13,19}\b",
    ),
    "ip_address": re.compile(
        r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    ),
    "iban": re.compile(
        r"\b[A-Z]{2}\d{2}[A-Z0-9]{4,30}\b",
    ),
}


# Keyword-based categories — case-insensitive substring match
_KEYWORD_CATEGORIES = {
    "health_data": (
        "diagnosis", "prescription", "medication", "patient id", "medical record",
        "icd-10", "blood pressure", "lab result",
    ),
    "financial_data": (
        "account number", "routing number", "iban", "swift code",
        "tax id", "vat number",
    ),
}


def detect_pii_categories(text: str | None) -> list[str]:
    """Return list of PII category names found in `text`.

    Best-effort — pattern based. Empty list when nothing detected or text is None.
    Categories returned in stable alphabetical order so equal inputs produce
    equal outputs (good for deduplication and tests).
    """
    if not text:
        return []

    found: set[str] = set()
    for name, pattern in _PATTERNS.items():
        if pattern.search(text):
            found.add(name)

    # Checksum-validated categories — regex alone is too noisy for these
    if contains_nordic_national_id(text):
        found.add("national_id")

    lower = text.lower()
    for category, keywords in _KEYWORD_CATEGORIES.items():
        if any(kw in lower for kw in keywords):
            found.add(category)

    return sorted(found)


#: Categories whose matches are entity spans (``find_pii_spans``), most
#: specific first: when two matches overlap, the earlier category wins.
#: Keyword categories (``health_data``, ``financial_data``) mark a topic, not
#: an entity, and have no spans.
SPAN_CATEGORIES: tuple[str, ...] = (
    "national_id", "email", "iban", "ssn", "credit_card", "ip_address", "phone",
)


def find_pii_spans(text: str | None) -> list[tuple[int, int, str]]:
    """Non-overlapping ``(start, end, category)`` entity spans in ``text``.

    The same patterns and validators as ``detect_pii_categories``, for
    in-process pseudonymisation (``prompt_shields.pseudonymise``) only:
    callers must never log, store or send the matched text. Overlaps are
    resolved by ``SPAN_CATEGORIES`` order; spans come back sorted by start.
    """
    if not text:
        return []
    taken: list[tuple[int, int, str]] = []

    def free(start: int, end: int) -> bool:
        return all(end <= s or start >= e for s, e, _ in taken)

    for category in SPAN_CATEGORIES:
        if category == "national_id":
            found = find_nordic_national_ids(text)
        else:
            found = [m.span() for m in _PATTERNS[category].finditer(text)]
        for start, end in found:
            if end > start and free(start, end):
                taken.append((start, end, category))
    return sorted(taken)


def _content_text(content: object) -> str:
    """String content, or the text parts of a list of content parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part["text"] for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def messages_text(messages: Iterable[dict]) -> str:
    """All message text: string content and text content parts alike."""
    return " ".join(
        _content_text(m.get("content")) if isinstance(m, dict) else ""
        for m in messages or ()
    )


def scan_messages(messages: Iterable[dict]) -> list[str]:
    """Apply detection across all message contents in a chat completion."""
    return detect_pii_categories(messages_text(messages))
