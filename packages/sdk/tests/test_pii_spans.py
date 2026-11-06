"""``find_pii_spans`` overlap resolution (#36): the bisecting resolver must
return exactly what the original quadratic one did, on real detector output
and on synthetic span lists in any order.
"""

from __future__ import annotations

import random

import pytest
from prompt_shields import pii
from prompt_shields.pii import SPAN_CATEGORIES, find_pii_spans


def _reference_find_pii_spans(text):
    """The original implementation, verbatim apart from the name: every
    candidate is checked against every span kept so far (quadratic)."""
    if not text:
        return []
    taken = []

    def free(start, end):
        return all(end <= s or start >= e for s, e, _ in taken)

    spans = pii._entity_spans(text)
    for category in SPAN_CATEGORIES:
        for start, end, cat in spans:
            if cat == category and end > start and free(start, end):
                taken.append((start, end, category))
    return sorted(taken)


# Pieces that match one or several patterns, so concatenations overlap across
# categories (a card that is also a phone, an IBAN that passes Luhn, ...).
_PIECES = (
    "4111 1111 1111 1111", "4111111111111111", "5500-0000-0000-0004",
    "SE45 5000 0000 0583 9825 7466", "NO9386011117947", "GB82WEST12345698765432",
    "19900101-0017", "199001010017", "900101-0017", "123-45-6789",
    "+46 70 174 06 12", "070-174 06 12", "(07700) 900 290", "555-123-4567",
    "+1 555 123 4567", "5551234567", "10.20.30.40", "192.0.2.1",
    "kari.nordmann@example.no", "a@b.co", "0", "00", "12", "4111", "-", ".",
    "+", "(", ")", "@", "x", "SE45", "1111 1111",
)
_SEPS = ("", " ", "  ", "-", ".", "/", "\n", ", ", "x", "@")


def _random_text(rng: random.Random, pieces: int) -> str:
    return "".join(rng.choice(_PIECES) + rng.choice(_SEPS) for _ in range(pieces))


@pytest.mark.parametrize("seed", range(300))
def test_matches_reference_on_random_text(seed):
    rng = random.Random(seed)
    text = _random_text(rng, rng.randint(1, 60))
    assert find_pii_spans(text) == _reference_find_pii_spans(text)


@pytest.mark.parametrize(
    "text",
    [
        "0 " * 2_000,
        "4111 1111 1111 1111 " * 500,
        "4111 1111 1111 1111" * 200,
        "1.2.3.4 " * 1_000,
        "1.2.3.4.5.6.7.8" * 300,
        "SE45 5000 0000 0583 9825 7466 " * 300,
        "+46 70 174 06 12 " * 500,
        "070-174 06 12 555-123-4567 " * 300,
        "19900101-0017 123-45-6789 " * 300,
        "a@b.co 10.20.30.40 " * 500,
        "+1 " * 3_000,
        "".join(_PIECES) * 20,
        " ".join(_PIECES) * 20,
    ],
    ids=lambda t: repr(t[:24]),
)
def test_matches_reference_on_adversarial_text(text):
    assert find_pii_spans(text) == _reference_find_pii_spans(text)


def test_random_text_exercises_overlaps():
    # The differential test is only meaningful if candidates do collide.
    rejected = 0
    for seed in range(50):
        text = _random_text(random.Random(seed), 40)
        candidates = {(s, e) for s, e, _ in pii._entity_spans(text) if e > s}
        rejected += len(candidates) - len(find_pii_spans(text))
    assert rejected > 50


@pytest.mark.parametrize("seed", range(300))
def test_matches_reference_on_synthetic_spans(seed, monkeypatch):
    # Raw candidate lists in arbitrary order, with duplicates, nesting, shared
    # edges and empty spans: tie-breaking is by category priority, then by
    # candidate order, whatever order ``_entity_spans`` yields them in.
    rng = random.Random(seed)
    size = rng.choice((5, 30, 200))
    categories = SPAN_CATEGORIES + ("health_data",)
    spans = []
    for _ in range(rng.randint(0, 120)):
        start = rng.randint(0, size)
        spans.append((start, start + rng.randint(0, 12), rng.choice(categories)))
    if seed % 3 == 0:
        spans.sort()
    elif seed % 3 == 1:
        spans.sort(reverse=True)
    spans += rng.sample(spans, len(spans) // 4)
    monkeypatch.setattr(pii, "_entity_spans", lambda text: list(spans))
    assert find_pii_spans("x") == _reference_find_pii_spans("x")
