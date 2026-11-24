"""The shared pseudonymisation vectors (schemas/pseudonymisation/vectors.v1.json).

The gateway's port (#350, gateway/src/middlewares/psPseudonymise) runs the
same file, so a call pseudonymised by the SDK and one pseudonymised by the
gateway carry the same placeholders. If a change here is intended,
regenerate the file from this SDK and update the gateway in the same PR.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from prompt_shields.pseudonymise import Pseudonymisation

ROOT = Path(__file__).resolve().parents[3]
VECTORS = json.loads(
    (ROOT / "schemas" / "pseudonymisation" / "vectors.v1.json").read_text(encoding="utf-8")
)
CASES = {case["name"]: case for case in VECTORS["pseudonymise"]}


def _run(case: dict) -> Pseudonymisation:
    return Pseudonymisation(
        case["messages"],
        categories=case.get("categories"),
        system=case.get("system"),
        prediction=case.get("prediction"),
    )


@pytest.mark.parametrize("case", VECTORS["pseudonymise"], ids=lambda c: c["name"])
def test_pseudonymise_vectors(case):
    p = _run(case)
    expected = case["expected"]
    assert p.messages == expected["messages"]
    if "system" in expected:
        assert p.system == expected["system"]
    if "prediction" in expected:
        assert p.prediction == expected["prediction"]
    assert dict(p.stats().entities) == expected["entities"]


def test_restore_vectors():
    p = _run(CASES[VECTORS["restore"][0]["mapping_from"]])
    for case in VECTORS["restore"]:
        got = p.restore(case["text"], json_escape=case["json_escape"])
        assert got == case["expected"], case["name"]
    stats = p.stats()
    assert {"restored": stats.restored, "unknown": stats.unknown} == VECTORS["restore_stats"]


@pytest.mark.parametrize("case", VECTORS["stream"], ids=lambda c: c["name"])
def test_stream_vectors(case):
    restorer = _run(CASES[case["mapping_from"]]).stream_restorer(json_escape=case["json_escape"])
    assert [restorer.feed(piece) for piece in case["pieces"]] == case["outputs"]
    assert restorer.flush() == case["flush"]


def test_dataset_vectors():
    lines = (ROOT / "benchmarks" / "pii" / "dataset.v1.jsonl").read_text(encoding="utf-8")
    seen = 0
    for line in lines.splitlines():
        if not line.strip():
            continue
        example = json.loads(line)
        out = Pseudonymisation([{"role": "user", "content": example["text"]}]).messages[0]
        assert out["content"] == VECTORS["dataset"].get(example["id"], example["text"]), example["id"]
        seen += example["id"] in VECTORS["dataset"]
    assert seen == len(VECTORS["dataset"])
