"""Score a PII detector on the labelled benchmark (#35).

    PYTHONPATH=packages/sdk python benchmarks/pii/score.py            # SDK report
    PYTHONPATH=packages/sdk python benchmarks/pii/score.py --gate     # also apply the gate
    PYTHONPATH=packages/sdk python benchmarks/pii/score.py --misses email

Scoring is per example and per category, the way the detectors report
(category presence, never spans): for each category, an example is a true
positive when it is labelled and detected, a false positive when detected
but not labelled, and a false negative when labelled but missed.

`thresholds.json` holds the recall gate. A key is a category (all locales)
or `category@locale`. The gateway port is scored the same way by
`gateway/src/middlewares/router/piiBenchmark.test.ts`, against the same
thresholds; the SDK side runs in `packages/sdk/tests/test_pii_benchmark.py`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATASET = HERE / "dataset.v1.jsonl"
THRESHOLDS = HERE / "thresholds.json"

# Labelled in the dataset, but no detector reports them yet: they are listed
# in the report and never gated.
UNSUPPORTED = ("person_name", "postal_address")


def load_dataset(path: Path = DATASET) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_thresholds(path: Path = THRESHOLDS) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _ratio(num: int, den: int) -> float | None:
    return None if den == 0 else num / den


def score(records: Iterable[dict], detect: Callable[[str], Iterable[str]]) -> dict:
    """Per-key counts and P/R/F1. Keys: `category` and `category@locale`."""
    counts: dict[str, dict[str, int]] = {}

    def bump(key: str, field: str):
        counts.setdefault(key, {"tp": 0, "fp": 0, "fn": 0})[field] += 1

    for rec in records:
        expected = set(rec["labels"])
        predicted = set(detect(rec["text"]))
        for cat in expected | predicted:
            field = "tp" if cat in expected and cat in predicted else (
                "fp" if cat in predicted else "fn")
            bump(cat, field)
            bump(f"{cat}@{rec['locale']}", field)

    metrics = {}
    for key, c in sorted(counts.items()):
        p = _ratio(c["tp"], c["tp"] + c["fp"])
        r = _ratio(c["tp"], c["tp"] + c["fn"])
        f1 = None if p is None or r is None or p + r == 0 else 2 * p * r / (p + r)
        metrics[key] = {**c, "support": c["tp"] + c["fn"], "precision": p, "recall": r, "f1": f1}
    return metrics


def gate_failures(metrics: dict, thresholds: dict) -> list[str]:
    """Gated keys whose recall is below the threshold (or that have no support)."""
    failures = []
    for key, minimum in sorted(thresholds["min_recall"].items()):
        recall = metrics.get(key, {}).get("recall")
        if recall is None or recall + 1e-9 < minimum:
            failures.append(f"{key}: recall {recall} < {minimum}")
    return failures


def _fmt(x: float | None) -> str:
    return "  -  " if x is None else f"{x:.3f}"


def report(metrics: dict, title: str) -> str:
    cats = [k for k in metrics if "@" not in k]
    lines = [f"### {title}", "",
             "| category | support | TP | FP | FN | precision | recall | F1 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for k in sorted(cats, key=lambda c: (c in UNSUPPORTED, c)):
        m = metrics[k]
        name = f"{k} (not detected)" if k in UNSUPPORTED else k
        lines.append(f"| {name} | {m['support']} | {m['tp']} | {m['fp']} | {m['fn']} | "
                     f"{_fmt(m['precision'])} | {_fmt(m['recall'])} | {_fmt(m['f1'])} |")
    locales = sorted({k.split("@")[1] for k in metrics if "@" in k})
    lines += ["", "Recall by locale (support in brackets):", "",
              "| category | " + " | ".join(locales) + " |",
              "|---|" + "---:|" * len(locales)]
    for k in sorted(c for c in cats if c not in UNSUPPORTED):
        row = []
        for loc in locales:
            m = metrics.get(f"{k}@{loc}")
            row.append("" if not m or not m["support"] else f"{_fmt(m['recall'])} ({m['support']})")
        if any(row):
            lines.append(f"| {k} | " + " | ".join(row) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score the SDK PII detector on the benchmark.")
    ap.add_argument("--gate", action="store_true", help="exit 1 if a gated recall is below threshold")
    ap.add_argument("--misses", metavar="CATEGORY", help="print missed and false-positive examples")
    args = ap.parse_args(argv)

    from prompt_shields.pii import detect_pii_categories

    records = load_dataset()
    metrics = score(records, detect_pii_categories)
    print(report(metrics, f"SDK (prompt_shields.pii), {len(records)} examples"))
    if args.misses:
        for rec in records:
            got = set(detect_pii_categories(rec["text"]))
            want = args.misses in rec["labels"]
            if want != (args.misses in got):
                print(("MISS " if want else "FP   ") + f"{rec['id']}: {rec['text']}")
    if args.gate:
        failures = gate_failures(metrics, load_thresholds())
        for f in failures:
            print("GATE FAIL", f, file=sys.stderr)
        return 1 if failures else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
