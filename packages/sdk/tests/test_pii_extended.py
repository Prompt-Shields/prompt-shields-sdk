"""Opt-in PII categories (#351): person names, postal addresses and the
multilingual health/financial vocabulary in ``prompt_shields.pii_extended``,
and their recall gate on the benchmark (``benchmarks/pii``)."""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest
from prompt_shields.pii import detect_pii_categories, find_pii_spans
from prompt_shields.pii_extended import (
    OPT_IN_CATEGORIES,
    detect_pii_categories_extended,
)

BENCH = Path(__file__).resolve().parents[3] / "benchmarks" / "pii"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"pii_bench_ext_{name}", BENCH / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


score_mod = _load("score")
THRESHOLDS = score_mod.load_thresholds()
V1 = score_mod.load_dataset()
HOLDOUT = score_mod.load_dataset(score_mod.OPTIN_DATASET)
V1_METRICS = score_mod.score(V1, detect_pii_categories_extended)
HOLDOUT_METRICS = score_mod.score(HOLDOUT, detect_pii_categories_extended,
                                  score_mod.OPTIN_CATEGORIES)


# --- opt-in only: the default detector is unchanged -----------------------------

def test_default_detector_never_reports_opt_in_categories():
    for rec in V1 + HOLDOUT:
        assert not set(detect_pii_categories(rec["text"])) & set(OPT_IN_CATEGORIES), rec["id"]
        assert not {c for _, _, c in find_pii_spans(rec["text"])} & set(OPT_IN_CATEGORIES)


def test_extended_is_a_superset_of_the_default():
    for rec in V1 + HOLDOUT:
        assert set(detect_pii_categories(rec["text"])) <= set(
            detect_pii_categories_extended(rec["text"])), rec["id"]


def test_extended_passes_the_default_gate_too():
    assert not score_mod.gate_failures(V1_METRICS, THRESHOLDS)


def test_opt_in_gates_never_below_the_default_gate():
    for cat, minimum in THRESHOLDS["opt_in"]["min_recall"].items():
        assert minimum >= THRESHOLDS["min_recall"].get(cat, 0), cat


@pytest.mark.parametrize("key", sorted(THRESHOLDS["opt_in"]["min_recall"]))
def test_opt_in_recall_gate(key):
    m = V1_METRICS.get(key)
    assert m and m["recall"] is not None
    assert m["recall"] + 1e-9 >= THRESHOLDS["opt_in"]["min_recall"][key], \
        score_mod.report(V1_METRICS, "extended", ())


@pytest.mark.parametrize("key", sorted(THRESHOLDS["opt_in"]["min_recall_holdout"]))
def test_opt_in_holdout_recall_gate(key):
    m = HOLDOUT_METRICS.get(key)
    assert m and m["recall"] is not None
    assert m["recall"] + 1e-9 >= THRESHOLDS["opt_in"]["min_recall_holdout"][key], \
        score_mod.report(HOLDOUT_METRICS, "extended, held-out", ())


def test_holdout_shape():
    assert len(HOLDOUT) > 100
    assert len({r["id"] for r in HOLDOUT}) == len(HOLDOUT)
    for r in HOLDOUT:
        assert set(r["labels"]) <= set(score_mod.OPTIN_CATEGORIES)
        assert bool(r["labels"]) == (r["kind"] == "positive")
    # Every opt-in category has labelled examples in every locale.
    for cat in score_mod.OPTIN_CATEGORIES:
        assert {r["locale"] for r in HOLDOUT if cat in r["labels"]} == {
            r["locale"] for r in HOLDOUT}, cat


def test_holdout_precision_floor():
    # Not a gate value from security; guards against a rule change that buys
    # recall with false positives on the hard negatives.
    for cat in score_mod.OPTIN_CATEGORIES:
        assert HOLDOUT_METRICS[cat]["precision"] >= 0.9, cat


# --- behaviour -------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Ny kontakt: Lars Andersson",
    "Stuur het naar Daan de Jong",
    "Anna-Karin Berg ringer i morgon",
    "Bitte an Frau Weber weiterleiten",
    "Merci, M. Dubois",
    "My name is Oliver",
    "Jag heter Gunnel",
    "Namn: Torbjörn Ek",
])
def test_person_name(text):
    assert "person_name" in detect_pii_categories_extended(text)


@pytest.mark.parametrize("text", [
    "Maria",                      # a given name alone
    "Anna och jag",               # not followed by a capitalised word
    "Anna AB levererar",          # company form
    "Erik Monday standup",        # weekday
    "Summarise the meeting",
])
def test_not_person_name(text):
    assert "person_name" not in detect_pii_categories_extended(text)


@pytest.mark.parametrize("text", [
    "Storgatan 12, 114 55 Stockholm",
    "Kongens gate 5, 7011 Trondheim",
    "Vesterbrogade 4, 1620 København V",
    "Hämeenkatu 9, 33100 Tampere",
    "Bahnhofstraße 3, 80335 München",
    "12 rue de la République, 69002 Lyon",
    "Kerkstraat 7, 1017 GB Amsterdam",
    "Calle Mayor 12, 28013 Madrid",
    "Via Roma 3, 00184 Roma",
    "ul. Długa 5, 00-238 Warszawa",
    "10 High Street, Bristol",
    "Postcode SW1A 1AA",
])
def test_postal_address(text):
    assert "postal_address" in detect_pii_categories_extended(text)


@pytest.mark.parametrize("text", [
    "Storgatan är avstängd",           # street, no number
    "Version 2024 Release notes",
    "Room 12 in the main building",
    "Via the portal 3 times",          # "Via" not followed by a capitalised name
    "Firmware 4.2.1",
])
def test_not_postal_address(text):
    assert "postal_address" not in detect_pii_categories_extended(text)


@pytest.mark.parametrize(("text", "category"), [
    ("Hon fick diagnosen typ 2-diabetes", "health_data"),
    ("Bei ihr wurde Diabetes diagnostiziert", "health_data"),
    ("Le médecin a changé le traitement", "health_data"),
    ("Lääkäri määräsi uuden lääkkeen", "health_data"),
    ("U pacjenta zdiagnozowano astmę", "health_data"),
    ("Uppdatera kontonumret", "financial_data"),
    ("Wie lautet die Steuernummer?", "financial_data"),
    ("Qual è la partita IVA?", "financial_data"),
    ("¿Cuál es el NIF de la empresa?", "financial_data"),
    ("Send mig dit reg.nr.", "financial_data"),
    ("Pengarna ska sättas in på SE45 5000 0000 0583 9825 7466", "financial_data"),
])
def test_vocabulary(text, category):
    assert category in detect_pii_categories_extended(text)
    assert category not in detect_pii_categories(text)  # default unchanged


@pytest.mark.parametrize("text", [
    "Run the diagnostics again",       # not "diagnosis"
    "Kör diagnostiken igen",
    "Das Rezept für Apfelstrudel",     # recipe
    "Il reato è caduto in prescrizione",
    "nip to the shops",                # acronyms are case-sensitive
    "P.IVA 01234567890",               # company identifiers stay out
    "Org.nr 556677-8899",
])
def test_vocabulary_lookalikes(text):
    cats = detect_pii_categories_extended(text)
    assert "health_data" not in cats
    assert "financial_data" not in cats


def test_switches():
    text = "Lars Andersson, Storgatan 12, diagnos, kontonummer"
    assert set(detect_pii_categories_extended(text)) >= {
        "person_name", "postal_address", "health_data", "financial_data"}
    off = detect_pii_categories_extended(text, names=False, addresses=False, vocabulary=False)
    assert off == detect_pii_categories(text)
    assert detect_pii_categories_extended(None) == []
    assert detect_pii_categories_extended("") == []


def test_unknown_ner_backend():
    with pytest.raises(ValueError):
        detect_pii_categories_extended("x", ner="flair")


def test_spacy_backend_adds_model_names():
    spacy = pytest.importorskip("spacy")
    try:
        spacy.load("xx_ent_wiki_sm")
    except OSError:
        pytest.skip("spaCy model xx_ent_wiki_sm not installed")
    assert "person_name" in detect_pii_categories_extended(
        "The minutes were taken by Harriet Wilson.", ner="spacy")


# --- linear time ------------------------------------------------------------------

def _cpu_seconds(text: str, times: int) -> float:
    start = time.thread_time()
    for _ in range(times):
        detect_pii_categories_extended(text)
    return time.thread_time() - start


def _growth(small: str, large: str) -> float:
    """CPU cost of `large` over `small` (a quarter of its size), best of a
    few interleaved samples, as in test_pii_benchmark."""
    best_small = best_large = float("inf")
    deadline = time.perf_counter() + 1.0
    for i in range(5):
        if i and time.perf_counter() > deadline:
            break
        best_small = min(best_small, _cpu_seconds(small, 4))
        best_large = min(best_large, _cpu_seconds(large, 1))
    return 4 * best_large / max(best_small, 1e-5)


_ADVERSARIAL = {
    "capitalised words": (lambda n: "Anna " * n, 20_000),
    "given name chains": (lambda n: "Anna-" * n, 20_000),
    "titles": (lambda n: "Mr. " * n, 25_000),
    "street words": (lambda n: "Storgatan " * n, 10_000),
    "street hyphen": (lambda n: "A-" * n, 50_000),
    "numbers and words": (lambda n: "12 Main " * n, 12_500),
    "letters": (lambda n: "A" * n, 100_000),
    "via chains": (lambda n: "Via Roma " * n, 11_000),
    "particle chains": (lambda n: "1 rue " + "de " * n, 33_000),
    "cue phrases": (lambda n: "my name is " * n, 9_000),
    "vocabulary words": (lambda n: "numero di " * n, 10_000),
}


@pytest.mark.parametrize("name", sorted(_ADVERSARIAL))
def test_extended_detection_stays_linear(name):
    # 4x the input must cost under 8x as much (linear scanning gives ~4x).
    make, n = _ADVERSARIAL[name]
    large = make(n)
    assert len(large) >= 99_000
    assert _growth(make(n // 4), large) < 8
