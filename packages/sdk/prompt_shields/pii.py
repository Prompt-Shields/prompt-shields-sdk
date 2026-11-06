"""Lightweight PII detection on prompt input text.

Pattern-based heuristics — fast, no ML, no network. Detects category presence,
not content. Used to populate `detected_pii_types` on events without ever
storing the prompt text.

Phase 1 covers the common categories most enterprises care about for AI
governance. Best-effort — designed as a signal, not a guarantee.
"""

import re
from bisect import bisect_left
from collections.abc import Iterable

from prompt_shields.national_ids import find_nordic_national_ids

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


def _iban_ok(compact: str) -> bool:
    if not 15 <= len(compact) <= 34:
        return False
    rearranged = compact[4:] + compact[:4]
    return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1


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


def _iban_prefix(match: str) -> int:
    """Length of the checksum-valid prefix of an IBAN-shaped match (0: none).

    A following word or digit group ("... 7466 BIC", "... 7466 4111 1111")
    can join the last group: trailing space-separated groups are dropped
    until the checksum holds.
    """
    groups = match.split(" ")
    while groups:
        if _iban_ok("".join(groups)):
            return len(" ".join(groups))
        groups.pop()
    return 0


def _entity_spans(text: str) -> list[tuple[int, int, str]]:
    """Every validated entity match in ``text`` as ``(start, end, category)``,
    possibly overlapping. Shared by ``detect_pii_categories`` and
    ``find_pii_spans``.

    IBANs first: a checksum-valid IBAN is blanked out before the other
    patterns run, so its digits never also count as a card or phone number
    (a spaced IBAN can pass Luhn). Blanking keeps offsets, so every span
    indexes ``text``.
    """
    spans: list[tuple[int, int, str]] = []
    masked = text
    for m in _IBAN_RE.finditer(text):
        n = _iban_prefix(m.group())
        if n:
            spans.append((m.start(), m.start() + n, "iban"))
            masked = masked[: m.start()] + " " * n + masked[m.start() + n:]
    pos = 0
    while m := _CARD_RE.search(masked, pos):
        if _luhn_ok(re.sub(r"\D", "", m.group())):
            spans.append((m.start(), m.end(), "credit_card"))
            pos = m.end()
            continue
        # The match can start with an unrelated digit group ("Ref 12 4111
        # 1111 1111 1111"): retry from the next group, not after the match.
        sep = _CARD_SEP.search(m.group())
        pos = m.start() + sep.end() if sep else m.end()
    for name, pattern in _PATTERNS.items():
        spans.extend((m.start(), m.end(), name) for m in pattern.finditer(masked))
    for name, pattern, validate in _VALIDATED:
        spans.extend(
            (m.start(), m.end(), name) for m in pattern.finditer(masked) if validate(m)
        )
    # Checksum-validated categories — regex alone is too noisy for these
    spans.extend((s, e, "national_id") for s, e in find_nordic_national_ids(masked))
    return spans


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

    # Entities: the same spans the pseudonymiser replaces (one code path, so
    # the scanner and ``find_pii_spans`` cannot drift apart).
    found = {category for _, _, category in _entity_spans(text)}

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
    # Greedy, in priority order: a span is kept when it overlaps no span kept
    # before it (earlier category, or earlier in the same category). Scanning
    # every kept span per candidate was quadratic (seconds on 200 KB of
    # matches), so kept spans live in ``taken``, sorted and disjoint, and are
    # checked by bisection. Candidates are taken in runs ascending by start
    # (each pattern yields matches in order); within a run only the last span
    # kept in that run can overlap, and the run is merged into ``taken`` when
    # it ends. A merge is linear (two sorted runs), and there is a handful of
    # runs per category, so the whole pass is O(n log n).
    by_category: dict[str, list[tuple[int, int, str]]] = {}
    for span in _entity_spans(text):
        by_category.setdefault(span[2], []).append(span)
    taken: list[tuple[int, int, str]] = []
    run: list[tuple[int, int, str]] = []
    run_start = -1
    for category in SPAN_CATEGORIES:
        for start, end, _ in by_category.get(category, ()):
            if end <= start:
                continue
            if start < run_start:
                taken = sorted(taken + run)
                run = []
            run_start = start
            if run and run[-1][1] > start:
                continue
            i = bisect_left(taken, (end,))  # first kept span starting at or after `end`
            if i and taken[i - 1][1] > start:
                continue
            run.append((start, end, category))
        taken = sorted(taken + run)
        run = []
        run_start = -1
    return taken


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
