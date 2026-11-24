"""PII redaction for logged payloads (#352).

``redact_value`` walks a JSON value (request or response body) and replaces
every PII span in its strings, object keys included, with a category
placeholder such as ``[EMAIL]`` or ``[CARD]``. It returns the redacted copy
and a count per category. Values never leave this module: callers store
the copy and the counts only.

Categories: ``email``, ``card`` (13-19 digits, alone or in groups split by
spaces or dashes, Luhn checked), ``iban`` (mod-97 checked), ``ssn``
(``123-45-6789``), ``phone`` (10-15 digits in common national and
international layouts) and ``ip_address`` (IPv4).

Pattern based and deliberately eager: a number that looks like a card or a
phone number is replaced even when it is something else, because a missed
value would be stored. It is the collector's own copy of the detection the
SDK and gateway do (the collector does not depend on either), and runs
before anything is encrypted or written, whatever the sender did.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

PLACEHOLDERS: dict[str, str] = {
    "email": "[EMAIL]",
    "card": "[CARD]",
    "iban": "[IBAN]",
    "ssn": "[SSN]",
    "phone": "[PHONE]",
    "ip_address": "[IP_ADDRESS]",
}

# Bounded quantifiers keep every pattern linear-ish on hostile input.
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}"
    r"(?:\.[A-Za-z0-9-]{1,63}){0,8}\.[A-Za-z]{2,24}(?![A-Za-z0-9-])"
)
# A run of digit groups joined by single spaces or dashes.
_DIGIT_CHAIN = re.compile(r"\d+(?:[ -]\d+)*")
_DIGITS = re.compile(r"\d+")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_PHONE = re.compile(
    r"(?<!\d)(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)"
)
_INTL_PHONE = re.compile(r"(?<![\w+])\+\d[\d ()./-]{6,20}\d(?!\w)")
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")

_LUHN_DOUBLE = str.maketrans("0123456789", "0246813579")


def luhn_ok(digits: str) -> bool:
    if not digits.isdigit():
        return False
    odd = digits[-1::-2]
    even = digits[-2::-2].translate(_LUHN_DOUBLE)
    return (sum(map(int, odd)) + sum(map(int, even))) % 10 == 0


def _card_spans(text: str) -> list[tuple[int, int]]:
    """Every run of whole digit groups with 13-19 digits that passes Luhn
    ("4111111111111111", "4111 1111 1111 1111", "Ref 12 4111-1111-...")."""
    spans = []
    for chain in _DIGIT_CHAIN.finditer(text):
        groups = [(m.start(), m.end()) for m in _DIGITS.finditer(text, chain.start(), chain.end())]
        for i in range(len(groups)):
            digits = ""
            for j in range(i, len(groups)):
                digits += text[groups[j][0] : groups[j][1]]
                if len(digits) > 19:
                    break
                if len(digits) >= 13 and luhn_ok(digits):
                    spans.append((groups[i][0], groups[j][1]))
    return spans


def _iban_ok(match: str) -> bool:
    compact = match.replace(" ", "")
    if not 15 <= len(compact) <= 34:
        return False
    rearranged = compact[4:] + compact[:4]
    try:
        number = int("".join(str(int(ch, 36)) for ch in rearranged))
    except ValueError:
        return False
    return number % 97 == 1


def _phone_ok(text: str) -> bool:
    return 10 <= sum(ch.isdigit() for ch in text) <= 15


def _ipv4_ok(text: str) -> bool:
    return all(int(part) <= 255 for part in text.split("."))


def find_spans(text: str) -> list[tuple[int, int, str]]:
    """Non-overlapping (start, end, category) spans, in order. Where spans
    overlap the earlier, then longer, one wins (its category is kept)."""
    found: list[tuple[int, int, str]] = []
    found += [(m.start(), m.end(), "email") for m in _EMAIL.finditer(text)]
    found += [(s, e, "card") for s, e in _card_spans(text)]
    found += [(m.start(), m.end(), "iban") for m in _IBAN.finditer(text) if _iban_ok(m.group())]
    found += [(m.start(), m.end(), "ssn") for m in _SSN.finditer(text)]
    for pattern in (_PHONE, _INTL_PHONE):
        found += [
            (m.start(), m.end(), "phone") for m in pattern.finditer(text) if _phone_ok(m.group())
        ]
    found += [(m.start(), m.end(), "ip_address") for m in _IPV4.finditer(text) if _ipv4_ok(m.group())]
    found.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    merged: list[tuple[int, int, str]] = []
    for start, end, category in found:
        if merged and start < merged[-1][1]:
            last = merged[-1]
            if end > last[1]:
                merged[-1] = (last[0], end, last[2])
            continue
        merged.append((start, end, category))
    return merged


def redact_text(text: str, counts: Counter | None = None) -> str:
    spans = find_spans(text)
    if not spans:
        return text
    out = []
    pos = 0
    for start, end, category in spans:
        out.append(text[pos:start])
        out.append(PLACEHOLDERS[category])
        pos = end
        if counts is not None:
            counts[category] += 1
    out.append(text[pos:])
    return "".join(out)


def redact_value(value: Any) -> tuple[Any, dict[str, int]]:
    """A redacted copy of a JSON value and the count of spans replaced per
    category. Numbers are redacted too when, written out, they look like a
    card or phone number (a card number sent as a JSON number)."""
    counts: Counter = Counter()

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            return redact_text(v, counts)
        if isinstance(v, bool) or v is None:
            return v
        if isinstance(v, int):
            text = str(v)
            red = redact_text(text, counts)
            return v if red == text else red
        if isinstance(v, float):
            return v
        if isinstance(v, dict):
            return {redact_text(str(k), counts): walk(item) for k, item in v.items()}
        if isinstance(v, list):
            return [walk(item) for item in v]
        return redact_text(str(v), counts)

    return walk(value), dict(sorted(counts.items()))
