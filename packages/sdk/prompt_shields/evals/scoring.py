"""Scoring a candidate's answer (M§7.2, M§7.3).

Each scorer decides whether one candidate answer is *acceptable*. Parity for a
(use case, candidate) cell is the share of acceptable answers.

- :class:`ExactMatch` — equal to the reference after whitespace (and,
  optionally, case) normalisation.
- :class:`SchemaValid` — parses as JSON (a fenced ```json block is accepted)
  and, when a JSON Schema is given, validates against it.
- :class:`SimilarityMatch` — character-level similarity to the reference at or
  above a threshold. A cheap, lexical stand-in for semantic match; any
  embedding-based scorer can be plugged in through the same interface.
- :class:`PairwiseJudge` — LLM-as-judge against the reference answer. Every
  pair is judged in both orders and a disagreement counts as a tie;
  acceptable = win or tie. The judge model goes through the same policy,
  spend cap and rate limit as candidates (the harness enforces it).

A scorer returns ``Score(passed=None)`` when it cannot score (no reference,
judge refused or failed); such trials are reported but left out of parity.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import random
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from prompt_shields.evals.executors import Candidate, Completion
from prompt_shields.evals.records import EvalRecord

JudgeCall = Callable[[list[dict]], Completion | None]


@dataclass(frozen=True)
class Score:
    passed: bool | None
    method: str
    detail: str | None = None
    #: Pairwise judge only: "win", "tie" or "loss" for the candidate.
    outcome: str | None = None
    #: Similarity scorers: the measured similarity in [0, 1].
    value: float | None = None


class Scorer(Protocol):
    method: str
    needs_reference: bool
    #: The judge model, for scorers that call one; None otherwise.
    judge: Candidate | None

    def score(
        self,
        record: EvalRecord,
        reference: str | None,
        candidate_text: str,
        judge_call: JudgeCall,
    ) -> Score: ...


def _normalise(text: str, *, casefold: bool) -> str:
    text = " ".join(text.split())
    return text.casefold() if casefold else text


class ExactMatch:
    method = "exact_match"
    needs_reference = True
    judge = None

    def __init__(self, *, casefold: bool = False):
        self.casefold = casefold

    def score(self, record, reference, candidate_text, judge_call):
        if reference is None:
            return Score(None, self.method, "no reference answer")
        same = _normalise(reference, casefold=self.casefold) == _normalise(
            candidate_text, casefold=self.casefold
        )
        return Score(same, self.method)


_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


def parse_json_answer(text: str) -> Any:
    match = _FENCE.match(text)
    return json.loads(match.group(1) if match else text)


class SchemaValid:
    """Valid JSON, and valid against ``schema`` when one is given.

    Schema validation uses the ``jsonschema`` package (``prompt-shields[dev]``
    installs it); constructing a scorer with a schema without it fails early.
    """

    method = "schema_validity"
    needs_reference = False
    judge = None

    def __init__(self, schema: Mapping[str, Any] | None = None):
        self.schema = dict(schema) if schema is not None else None
        self._validator = None
        if self.schema is not None:
            try:
                import jsonschema
            except ImportError as exc:  # pragma: no cover - dev extra installs it
                raise ImportError(
                    "schema validation needs the 'jsonschema' package"
                ) from exc
            cls = jsonschema.validators.validator_for(self.schema)
            cls.check_schema(self.schema)
            self._validator = cls(self.schema)

    def score(self, record, reference, candidate_text, judge_call):
        try:
            value = parse_json_answer(candidate_text)
        except (json.JSONDecodeError, ValueError):
            return Score(False, self.method, "not valid JSON")
        if self._validator is not None:
            error = next(iter(self._validator.iter_errors(value)), None)
            if error is not None:
                return Score(False, self.method, f"schema: {error.message[:200]}")
        return Score(True, self.method)


class SimilarityMatch:
    method = "similarity"
    needs_reference = True
    judge = None

    def __init__(self, threshold: float = 0.85, *, casefold: bool = True):
        if not 0 <= threshold <= 1:
            raise ValueError("threshold must be in [0, 1]")
        self.threshold = threshold
        self.casefold = casefold

    def score(self, record, reference, candidate_text, judge_call):
        if reference is None:
            return Score(None, self.method, "no reference answer")
        ratio = difflib.SequenceMatcher(
            None,
            _normalise(reference, casefold=self.casefold),
            _normalise(candidate_text, casefold=self.casefold),
            autojunk=False,
        ).ratio()
        return Score(ratio >= self.threshold, self.method, value=round(ratio, 4))


# --- LLM-as-judge ------------------------------------------------------------

JUDGE_PROMPT_VERSION = "pairwise-v1"

JUDGE_SYSTEM = (
    "You compare two answers to the same request. Judge which answer serves the "
    "request better: correctness first, then completeness, then following the "
    "request's instructions and format. Length and style alone are not reasons "
    "to prefer an answer. If they are equally good, say TIE. Reply with one "
    "line: 'VERDICT: A', 'VERDICT: B' or 'VERDICT: TIE'."
)

_VERDICT = re.compile(r"VERDICT:\s*(A|B|TIE)\b", re.IGNORECASE)


def _render_request(messages: list[dict]) -> str:
    lines = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(
                str(part.get("text", "")) for part in content if isinstance(part, Mapping)
            )
        lines.append(f"[{message.get('role', 'user')}]\n{content}")
    return "\n\n".join(lines)


def judge_messages(request: list[dict], answer_a: str, answer_b: str) -> list[dict]:
    return [
        {"role": "system", "content": JUDGE_SYSTEM},
        {
            "role": "user",
            "content": (
                f"<request>\n{_render_request(request)}\n</request>\n\n"
                f"<answer_a>\n{answer_a}\n</answer_a>\n\n"
                f"<answer_b>\n{answer_b}\n</answer_b>"
            ),
        },
    ]


def parse_verdict(text: str | None) -> str | None:
    """'A', 'B' or 'TIE' from the judge's reply (last verdict wins)."""
    found = _VERDICT.findall(text or "")
    return found[-1].upper() if found else None


class PairwiseJudge:
    """Pairwise LLM-as-judge: candidate answer vs. the reference answer."""

    method = "llm_judge"
    needs_reference = True

    def __init__(self, judge: Candidate, *, prompt_version: str = JUDGE_PROMPT_VERSION):
        self.judge = judge
        self.prompt_version = prompt_version

    def score(self, record, reference, candidate_text, judge_call):
        if reference is None:
            return Score(None, self.method, "no reference answer")
        # Both orders are always judged; the record id seeds which goes first.
        seed = int(hashlib.sha256(record.record_id.encode()).hexdigest()[:8], 16)
        orders = [("candidate", "reference"), ("reference", "candidate")]
        random.Random(seed).shuffle(orders)
        outcomes = []
        for first, _second in orders:
            a, b = (
                (candidate_text, reference) if first == "candidate"
                else (reference, candidate_text)
            )
            completion = judge_call(judge_messages(record.messages or [], a, b))
            if completion is None or completion.error is not None:
                why = "judge refused" if completion is None else "judge error"
                return Score(None, self.method, why)
            verdict = parse_verdict(completion.text)
            if verdict is None:
                return Score(None, self.method, "judge reply has no verdict")
            if verdict == "TIE":
                outcomes.append("tie")
            else:
                candidate_slot = "A" if first == "candidate" else "B"
                outcomes.append("win" if verdict == candidate_slot else "loss")
        outcome = outcomes[0] if outcomes[0] == outcomes[1] else "tie"
        return Score(outcome in ("win", "tie"), self.method, outcome=outcome)


def scorer_from_name(
    name: str,
    *,
    schema: Mapping[str, Any] | None = None,
    judge: Candidate | None = None,
    threshold: float | None = None,
) -> Scorer:
    """Build a scorer from a CLI-style name."""
    if name in ("exact", "exact_match"):
        return ExactMatch()
    if name in ("schema", "json", "schema_validity"):
        return SchemaValid(schema)
    if name in ("similarity", "semantic"):
        return SimilarityMatch(threshold if threshold is not None else 0.85)
    if name in ("judge", "llm_judge"):
        if judge is None:
            raise ValueError("the judge scorer needs a judge model")
        return PairwiseJudge(judge)
    raise ValueError(f"unknown scorer {name!r}")
