"""Labelled Nordic/EU PII benchmark and recall gate (#35) for the SDK detector.

The synthetic dataset, its generator, the scorer and the thresholds live in
``benchmarks/pii/`` and are shared with the gateway port
(``gateway/src/middlewares/router/piiBenchmark.test.ts``).
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest
from prompt_shields.pii import detect_pii_categories, find_pii_spans

BENCH = Path(__file__).resolve().parents[3] / "benchmarks" / "pii"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"pii_bench_{name}", BENCH / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


score_mod = _load("score")
generate_mod = _load("generate")
RECORDS = score_mod.load_dataset()
METRICS = score_mod.score(RECORDS, detect_pii_categories)
THRESHOLDS = score_mod.load_thresholds()


def test_dataset_matches_generator():
    assert generate_mod.main(["--check"]) == 0, "run benchmarks/pii/generate.py"


def test_dataset_shape():
    assert len(RECORDS) > 500
    assert len({r["id"] for r in RECORDS}) == len(RECORDS)
    kinds = {r["kind"] for r in RECORDS}
    assert kinds == {"positive", "negative", "hard_negative"}
    for r in RECORDS:
        assert r["labels"] == sorted(set(r["labels"]))
        assert bool(r["labels"]) == (r["kind"] == "positive")


def test_dataset_is_synthetic():
    # Emails only on reserved domains, IPs only in documentation ranges.
    import re

    for r in RECORDS:
        for domain in re.findall(r"\w@([\w.-]+)", r["text"]):
            d = domain.lower().rstrip(".")
            assert d.endswith((".example", "example.com", "example.org", "example.net",
                               ".example.co.uk")), (r["id"], domain)
        for ip in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", r["text"]):
            if r["labels"] and "ip_address" in r["labels"]:
                assert ip.startswith(("192.0.2.", "198.51.100.", "203.0.113.")), r["id"]


@pytest.mark.parametrize("key", sorted(THRESHOLDS["min_recall"]))
def test_recall_gate(key):
    m = METRICS.get(key)
    assert m and m["recall"] is not None, f"{key}: no labelled examples"
    assert m["recall"] + 1e-9 >= THRESHOLDS["min_recall"][key], score_mod.report(METRICS, "SDK")


def test_gate_covers_every_detected_category():
    detected = {k for k in METRICS if "@" not in k} - set(score_mod.UNSUPPORTED)
    assert detected - {"ssn"} <= set(THRESHOLDS["min_recall"])


def test_gate_failures_reports_drops():
    fake = {"email": {"recall": 0.5}}
    assert score_mod.gate_failures(fake, {"min_recall": {"email": 0.9}})
    assert score_mod.gate_failures({}, {"min_recall": {"email": 0.9}})
    assert not score_mod.gate_failures({"email": {"recall": 0.9}}, {"min_recall": {"email": 0.9}})


# --- detector fixes found by the benchmark -------------------------------------

@pytest.mark.parametrize("iban", [
    "SE45 5000 0000 0583 9825 7466",
    "NO93 8601 1117 947",
    "DK50 0040 0440 1162 43",
    "FI21 1234 5600 0007 85",
    "DE89 3704 0044 0532 0130 00",
    "FR14 2004 1010 0505 0001 3M02 606",
    "NL91 ABNA 0417 1643 00",
    "GB29 NWBK 6016 1331 9268 19",
])
def test_spaced_iban_is_iban_not_card(iban):
    cats = detect_pii_categories(f"IBAN {iban} BIC ESSESESS")
    assert "iban" in cats
    assert "credit_card" not in cats
    assert "phone" not in cats


def test_generated_spaced_ibans_never_count_as_cards():
    ibans = [r for r in RECORDS if "iban" in r["labels"] and "credit_card" not in r["labels"]]
    assert ibans
    for r in ibans:
        assert "credit_card" not in detect_pii_categories(r["text"]), r["id"]


def test_iban_checksum_required():
    assert "iban" not in detect_pii_categories("SE45 5000 0000 0583 9825 7467")
    assert "iban" not in detect_pii_categories("VAT SE5304576785")


def test_card_luhn_required():
    assert "credit_card" in detect_pii_categories("card 4111-1111-1111-1111")
    assert "credit_card" not in detect_pii_categories("card 4111-1111-1111-1112")



@pytest.mark.parametrize("text", [
    "Ref 12 4111 1111 1111 1111",
    "id 2024-4111-1111-1111-1111",
    # Only the checksum-valid IBAN is masked, not a card joined to its groups.
    "SE45 5000 0000 0583 9825 7466 4111 1111 1111 1111",
])
def test_card_found_next_to_other_digits(text):
    assert "credit_card" in detect_pii_categories(text)


@pytest.mark.parametrize(("text", "spans"), [
    # A run's first group starts a card only after a non-word character, and
    # a card ends at the run's end only before one.
    ("x4111 1111 1111 1111", []),
    ("4111 1111 1111 1111x", []),
    ("4111 1111 1111 1111.", [(0, 19)]),
    # More than 19 digits in a row is no card.
    ("41111111111111111111", []),
    # A card stops at the end of the group that reaches 13 digits.
    ("4111 1111 1111 1111 9", [(0, 19)]),
    ("4 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1", []),
    # After a valid card the scan resumes past it.
    ("0 " * 30, [(0, 25), (26, 51)]),
    # Found from any group start of a long run.
    ("1-" * 300 + "4111-1111-1111-1111", [(576, 604)]),
])
def test_card_windows(text, spans):
    found = [(s, e) for s, e, cat in find_pii_spans(text) if cat == "credit_card"]
    assert found == spans
    assert ("credit_card" in detect_pii_categories(text)) == bool(spans)


def test_phone_with_letters_attached():
    assert "phone" in detect_pii_categories("call +1-555-123-4567x89")
    assert "phone" in detect_pii_categories("Tel:5551234567")


@pytest.mark.parametrize("phone", [
    "+46 70 174 06 12", "070-174 06 12", "+45 26 73 64 47", "06 39 98 22 48",
    "+33 6 39 98 43 15", "(07700) 900 290", "+44 7700 900123", "0151/31524677",
])
def test_grouped_phone_formats(phone):
    assert "phone" in detect_pii_categories(f"Ring {phone} i dag")


@pytest.mark.parametrize("text", ["Datum 05.03.2024 14.30", "00-238 Warszawa", "kl. 14:30"])
def test_phone_lookalikes_rejected(text):
    assert "phone" not in detect_pii_categories(text)


# name -> (build an input from a repeat count, repeat count for ~100 KB)
_ADVERSARIAL = {
    "letters": (lambda n: "a" * n, 100_000),
    "local part then @": (lambda n: "a" * n + "@", 100_000),
    "dotted": (lambda n: "a." * n, 50_000),
    "many @": (lambda n: "a@" * n, 50_000),
    "digits": (lambda n: "1" * n, 100_000),
    "digit-dash": (lambda n: "1-" * n, 50_000),
    "plus groups": (lambda n: "+1 " * n, 33_000),
    "zero groups": (lambda n: "0 " * n, 50_000),
    "iban heads": (lambda n: "SE45 " * n, 20_000),
}


def _cpu_seconds(text: str, times: int) -> float:
    start = time.thread_time()
    for _ in range(times):
        detect_pii_categories(text)
    return time.thread_time() - start


def _growth(small: str, large: str) -> float:
    """Cost of `large` over cost of `small` (a quarter of its size).

    Measured in this thread's CPU time, which other processes (parallel test
    workers) don't inflate. Each sample does the same work (the small input
    four times vs the large one once), and the best of several interleaved
    samples is kept.
    """
    best_small = best_large = float("inf")
    deadline = time.perf_counter() + 1.0
    for i in range(5):
        if i and time.perf_counter() > deadline:
            break
        best_small = min(best_small, _cpu_seconds(small, 4))
        best_large = min(best_large, _cpu_seconds(large, 1))
    return 4 * best_large / max(best_small, 1e-5)


@pytest.mark.parametrize("name", sorted(_ADVERSARIAL))
def test_detection_stays_linear_on_100kb(name):
    # Checked by how the cost grows, not by a wall-clock limit, so a busy
    # machine can't fail it: 4x the input must cost under 8x as much. Linear
    # scanning gives ~4x; the unanchored email pattern this guards against was
    # quadratic (~16x; ~0.5 s on 20 KB of letters, ~10 s on 100 KB).
    make, n = _ADVERSARIAL[name]
    large = make(n)
    assert len(large) >= 99_000
    assert _growth(make(n // 4), large) < 8


def test_anchored_email_still_matches_mid_run():
    assert "email" in detect_pii_categories("xx..jane.doe+1@acme.co.uk")
    assert "email" in detect_pii_categories("jürgen.müller@example.com")
