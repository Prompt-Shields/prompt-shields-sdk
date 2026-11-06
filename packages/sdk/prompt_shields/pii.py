"""Lightweight PII detection on prompt input text.

Pattern-based heuristics — fast, no ML, no network. Detects category presence,
not content. Used to populate `detected_pii_types` on events without ever
storing the prompt text.

Phase 1 covers the common categories most enterprises care about for AI
governance. Best-effort — designed as a signal, not a guarantee.
"""

import re
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from itertools import accumulate

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

# Card numbers: 13-19 digits with optional separators, Luhn checked. The
# shape is r"\b(?:\d[ -]*?){13,19}\b", retried from every digit group
# ("Ref 12 4111 1111 1111 1111"); ``_card_spans`` evaluates it in one scan
# over runs of 13+ digits joined by spaces and dashes.
# The lookahead checks for 13 digits; the rest takes whole groups at a time.
_CARD_CHAIN = re.compile(r"\d(?=(?:[ -]*\d){12})\d*(?:[ -]+\d+)*")
_CARD_SEPS = re.compile(r"[ -]+")
_WORD = re.compile(r"\w")

# IBAN, compact or in groups of four ("SE45 5000 0000 ..."), mod-97 checked.
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")

# Grouped phone numbers the US-shaped pattern above misses: international
# ("+46 70 174 06 12", "+45 26 73 64 47") and national with a trunk 0
# ("070-174 06 12", "06 39 98 22 48", "(07700) 900 290").
_INTL_PHONE_RE = re.compile(r"(?<![\w+])\+\d[\d ()./-]{6,20}\d(?!\w)")
_TRUNK_PHONE_RE = re.compile(r"(?<![\w+.,/-])\(?0\d[\d ()./-]{6,16}\d(?!\w)")
_PHONE_SEPS = re.compile(r"[ ./-]+")


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
    until the checksum holds (15-34 characters, and the digits of
    ``compact[4:] + compact[:4]``, A=10 ... Z=35, are 1 mod 97). Each
    character is converted once and shared by every prefix.
    """
    groups = match.split(" ")
    compact = "".join(groups)
    conv = [str(int(c, 36)) for c in compact]
    head = "".join(conv[:4])
    n_compact = len(compact)
    n_spaced = len(match)
    for group in reversed(groups):
        if 15 <= n_compact <= 34 and int("".join(conv[4:n_compact]) + head) % 97 == 1:
            return n_spaced
        n_compact -= len(group)
        n_spaced -= len(group) + 1
    return 0


_LUHN_DOUBLE = str.maketrans("0123456789", "0246813579")


def _card_spans(text: str) -> list[tuple[int, int]]:
    """Luhn-valid card number spans, as the retry loop over
    ``\\b(?:\\d[ -]*?){13,19}\\b`` finds them, in one scan.

    That loop tries the pattern from every digit group start (a group: a run
    of digits; a start: one after a non-word character) and, after a valid
    match, resumes past it. From a start the pattern takes whole groups of
    its run (digits joined by spaces and dashes) until it has 13 digits,
    then stops at the end of that group (the lazy separator ends the match
    there); it fails when that is more than 19 digits, or the run ends
    there before a word character. Here each start is checked once, with a
    moving window and Luhn prefix sums, instead of by a fresh regex match,
    so runs like "1-1-1-..." are not rescanned per group.
    """
    spans: list[tuple[int, int]] = []
    for chain in _CARD_CHAIN.finditer(text):
        start, end = chain.span()
        run = chain.group()
        groups = run.replace("-", " ").split()
        cum = [0, *accumulate(map(len, groups))]
        n = len(groups)
        sums: tuple[list[int], list[int]] | None = None
        # The run's first group is a start only after a non-word character;
        # a window may end at its last group only before one.
        i = 0 if start == 0 or _WORD.match(text[start - 1]) is None else 1
        last_end = n - 1 if end < len(text) and _WORD.match(text[end]) else n
        # From start group i the window takes groups i..e-1, the fewest with
        # 13+ digits.
        windows = []
        e = i
        while i < n:
            e = bisect_left(cum, cum[i] + 13, max(e, i + 1))
            if e > n:
                break
            if e <= last_end and cum[e] - cum[i] <= 19:
                if sums is None:
                    sums = _luhn_sums("".join(groups))
                # A window ending at digit cum[e] - 1 doubles the indices of
                # the other parity.
                total = sums[cum[e] % 2]
                if (total[cum[e]] - total[cum[i]]) % 10 == 0:
                    windows.append((i, e))
                    i = e
                    continue
            i += 1
        if windows:
            # Groups and separator runs alternate, starting with a group;
            # sep[k]: separator characters before group k.
            sep: Sequence[int]
            if len(run) == cum[n] + n - 1:
                sep = range(n)  # single-character separators
            else:
                sep = [0, *accumulate(map(len, _CARD_SEPS.findall(run)))]
            spans.extend(
                (start + cum[k] + sep[k], start + cum[e] + sep[e - 1])
                for k, e in windows
            )
    return spans


def _luhn_sums(digits: str) -> tuple[list[int], list[int]]:
    """Luhn prefix sums of ``digits``, doubling the even (first list) or odd
    (second list) indices."""
    if not digits.isascii():
        digits = "".join(str(int(c)) for c in digits)
    doubled = digits.translate(_LUHN_DOUBLE)
    even = list(digits)
    even[0::2] = doubled[0::2]
    odd = list(digits)
    odd[1::2] = doubled[1::2]
    return [0, *accumulate(map(int, even))], [0, *accumulate(map(int, odd))]


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
    spans.extend((s, e, "credit_card") for s, e in _card_spans(masked))
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
