"""Lightweight PII detection on prompt input text.

Pattern-based heuristics — fast, no ML, no network. Detects category presence,
not content. Used to populate `detected_pii_types` on events without ever
storing the prompt text.

Phase 1 covers the common categories most enterprises care about for AI
governance. Best-effort — designed as a signal, not a guarantee.
"""

import re
from collections.abc import Iterable

from prompt_shields.national_ids import contains_nordic_national_id

# Compiled patterns — order independent, all run in one pass
_PATTERNS = {
    "email": re.compile(
        # Anchored at the start of a local-part run, so a long run without a
        # match is scanned once, not once per start position (unanchored,
        # 100 KB of letters took seconds). Same yes/no answer: any match
        # inside a run implies one from the run's start.
        r"(?<![a-zA-Z0-9._%+-])[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
    ),
    "phone": re.compile(
        # international + US formats, 10-15 digits with optional separators.
        # Not inside a longer digit run (card numbers, IBANs); letters next
        # to it are fine ("Tel:5551234567", "555-123-4567x89").
        r"(?<!\d)(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)",
    ),
    "ssn": re.compile(
        r"\b\d{3}-\d{2}-\d{4}\b",
    ),
    "ip_address": re.compile(
        r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    ),
}

# Shape regex + validator: the category is found when any match validates.

# Card numbers: 13-19 digits with optional separators, Luhn checked.
_CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
_CARD_SEP = re.compile(r"[ -]+")

# IBAN, compact or in groups of four ("SE45 5000 0000 ..."), mod-97 checked.
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")

# Grouped phone numbers the US-shaped pattern above misses: international
# ("+46 70 174 06 12", "+45 26 73 64 47") and national with a trunk 0
# ("070-174 06 12", "06 39 98 22 48", "(07700) 900 290").
_INTL_PHONE_RE = re.compile(r"(?<![\w+])\+\d[\d ()./-]{6,20}\d(?!\w)")
_TRUNK_PHONE_RE = re.compile(r"(?<![\w+.,/-])\(?0\d[\d ()./-]{6,16}\d(?!\w)")
_PHONE_SEPS = re.compile(r"[ ./-]+")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _has_card(text: str) -> bool:
    pos = 0
    while m := _CARD_RE.search(text, pos):
        if _luhn_ok(re.sub(r"\D", "", m.group())):
            return True
        # The match can start with an unrelated digit group ("Ref 12 4111
        # 1111 1111 1111"): retry from the next group, not after the match.
        sep = _CARD_SEP.search(m.group())
        pos = m.start() + sep.end() if sep else m.end()
    return False


def _iban_ok(compact: str) -> bool:
    if not 15 <= len(compact) <= 34:
        return False
    rearranged = compact[4:] + compact[:4]
    return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1


def _mask_iban(m: re.Match) -> str:
    # A following word or digit group ("... 7466 BIC", "... 7466 4111 1111")
    # can join the last group: drop trailing space-separated groups until the
    # checksum holds, and blank only that prefix so what follows is still
    # scanned (a card number right after an IBAN must not be hidden).
    match = m.group()
    groups = match.split(" ")
    while groups:
        if _iban_ok("".join(groups)):
            n = len(" ".join(groups))
            return " " * n + match[n:]
        groups.pop()
    return match


def _phone_shape_ok(s: str, min_digits: int, max_digits: int) -> bool:
    digits = sum(c.isdigit() for c in s)
    if not min_digits <= digits <= max_digits:
        return False
    seps = _PHONE_SEPS.findall(s.replace("(", "").replace(")", ""))
    # One separator between groups, and the same one throughout after the
    # first group ("070-174 06 12"), so "05.03.2024 14.30" is not a phone.
    if any(len(sep) > 1 for sep in seps):
        return False
    return len(set(seps[1:])) <= 1


def _valid_intl_phone(m: re.Match) -> bool:
    return _phone_shape_ok(m.group(), 8, 15)


def _valid_trunk_phone(m: re.Match) -> bool:
    s = m.group()
    if not any(c in s for c in " ./-"):
        return False  # a bare digit run: left to the pattern above
    return _phone_shape_ok(s, 9, 12)


_VALIDATED = (
    ("phone", _INTL_PHONE_RE, _valid_intl_phone),
    ("phone", _TRUNK_PHONE_RE, _valid_trunk_phone),
)


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
    # IBANs first: a checksum-valid IBAN is blanked out before the other
    # patterns run, so its digits never also count as a card or phone number
    # (a spaced IBAN can pass Luhn).
    original = text
    text = _IBAN_RE.sub(_mask_iban, text)
    if text != original:
        found.add("iban")
    if _has_card(text):
        found.add("credit_card")

    for name, pattern in _PATTERNS.items():
        if pattern.search(text):
            found.add(name)
    for name, pattern, validate in _VALIDATED:
        if name not in found and any(validate(m) for m in pattern.finditer(text)):
            found.add(name)

    # Checksum-validated categories — regex alone is too noisy for these
    if contains_nordic_national_id(text):
        found.add("national_id")

    lower = text.lower()
    for category, keywords in _KEYWORD_CATEGORIES.items():
        if any(kw in lower for kw in keywords):
            found.add(category)

    return sorted(found)


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
