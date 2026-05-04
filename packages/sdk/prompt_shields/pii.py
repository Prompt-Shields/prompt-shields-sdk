"""Lightweight PII detection on prompt input text.

Pattern-based heuristics — fast, no ML, no network. Detects category presence,
not content. Used to populate `detected_pii_types` on events without ever
storing the prompt text.

Phase 1 covers the common categories most enterprises care about for AI
governance. Best-effort — designed as a signal, not a guarantee.
"""

import re
from typing import Iterable


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

    lower = text.lower()
    for category, keywords in _KEYWORD_CATEGORIES.items():
        if any(kw in lower for kw in keywords):
            found.add(category)

    return sorted(found)


def scan_messages(messages: Iterable[dict]) -> list[str]:
    """Apply detection across all message contents in a chat completion."""
    combined = " ".join(
        m.get("content", "") if isinstance(m, dict) else ""
        for m in messages
    )
    return detect_pii_categories(combined)
