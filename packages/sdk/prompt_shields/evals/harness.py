"""Re-execute a stratified sample on candidate models and score the answers.

Safety rules, enforced before any call is made:

- **Opt-in.** An executor that sends prompts off the machine
  (``sends_prompts``) runs only with ``allow_prompt_reexecution=True``. Fake
  and recorded executors need no opt-in.
- **Spend cap.** Such a run also needs ``max_spend_usd``. Every call (judge
  calls included) is checked against the cap with a worst-case estimate first
  (see :mod:`prompt_shields.evals.budget`). A call whose worst case has no
  list price, or whose prompt has non-text parts (images, audio, files), is
  not made, because its spend cannot be bounded.
- **Rate limit.** ``requests_per_minute`` spaces calls out.
- **Policy.** A prompt is sent only to candidates, and judged only by a judge,
  that :class:`EvalPolicy` allows for the prompt's sensitivity level (M§2.3,
  M§7.3). Levels the policy does not list allow nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from prompt_shields.evals.budget import RateLimiter, SpendCap, worst_case_input_tokens
from prompt_shields.evals.executors import Candidate, Completion, Executor
from prompt_shields.evals.records import SENSITIVITY_LEVELS
from prompt_shields.evals.sampling import Sample, StratumKey
from prompt_shields.evals.scoring import Score, Scorer
from prompt_shields.pricing import estimate_cost
from prompt_shields.route_policy import estimate_input_tokens

JUDGE_MAX_TOKENS = 64


class PromptReexecutionNotAllowed(RuntimeError):
    """Raised when a run would send prompts out without the explicit opt-in."""


@dataclass(frozen=True)
class EvalPolicy:
    """Which models may see a prompt, by sensitivity level.

    ``allowed`` maps a level to model ids or ``vendor:model`` labels (``"*"``
    allows any). ``allowed=None`` allows every model at every level and must
    be asked for with :meth:`allow_all`.
    """

    allowed: Mapping[str, frozenset[str]] | None
    unclassified_default: str = "confidential"

    @classmethod
    def allow_all(cls, unclassified_default: str = "confidential") -> EvalPolicy:
        return cls(allowed=None, unclassified_default=unclassified_default)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvalPolicy:
        """``{"unclassified_default": ..., "allowed_models": {level: [...]}}``
        or ``{"allow_all": true}``."""
        default = data.get("unclassified_default", "confidential")
        if default not in SENSITIVITY_LEVELS:
            raise ValueError(f"unknown unclassified_default {default!r}")
        if data.get("allow_all") is True:
            return cls.allow_all(default)
        allowed = data.get("allowed_models")
        if not isinstance(allowed, Mapping):
            raise TypeError("policy needs 'allowed_models' or 'allow_all': true")
        unknown = set(allowed) - set(SENSITIVITY_LEVELS)
        if unknown:
            raise ValueError(f"unknown sensitivity levels: {sorted(unknown)}")
        return cls(
            allowed={level: frozenset(models) for level, models in allowed.items()},
            unclassified_default=default,
        )

    def allows(self, candidate: Candidate, level: str) -> bool:
        if self.allowed is None:
            return True
        models = self.allowed.get(level, frozenset())
        return "*" in models or candidate.label in models or candidate.model in models

    def describe(self) -> dict:
        if self.allowed is None:
            return {"allow_all": True, "unclassified_default": self.unclassified_default}
        return {
            "unclassified_default": self.unclassified_default,
            "allowed_models": {k: sorted(v) for k, v in sorted(self.allowed.items())},
        }


@dataclass
class EvalConfig:
    candidates: Sequence[Candidate]
    #: Scorer per use case; the ``"*"`` entry is the default.
    scorers: Mapping[str, Scorer]
    policy: EvalPolicy
    allow_prompt_reexecution: bool = False
    max_spend_usd: float | None = None
    requests_per_minute: float | None = None
    #: Used when the logged call's temperature is unknown (M§7.3).
    temperature: float = 0.0
    #: Output cap per call; a logged ``max_tokens`` is used when it is lower.
    max_tokens: int = 1024
    #: Re-run the logged model when a record has no reference answer.
    rerun_baseline: bool = False
    pricing_table: dict | None = None

    def scorer_for(self, use_case: str) -> Scorer | None:
        return self.scorers.get(use_case) or self.scorers.get("*")


# Trial status values.
SCORED = "scored"
UNSCORED = "unscored"  # scorer could not decide (no reference, judge refused...)
ERROR = "error"
SKIPPED_POLICY = "skipped_policy"
SKIPPED_BUDGET = "skipped_budget"
SKIPPED_UNPRICED = "skipped_unpriced"
NO_SCORER = "no_scorer"


@dataclass(frozen=True)
class Trial:
    """One (sampled record, candidate) pair."""

    record_id: str
    use_case: str
    stratum: StratumKey
    candidate: str
    status: str
    score: Score | None = None
    baseline_cost: float | None = None
    candidate_cost: float | None = None
    baseline_latency_ms: int | None = None
    candidate_latency_ms: int | None = None
    baseline_tokens_out: int | None = None
    candidate_tokens_out: int | None = None
    detail: str | None = None

    @property
    def passed(self) -> bool | None:
        return self.score.passed if self.status == SCORED and self.score else None


@dataclass
class EvalRun:
    sample: Sample
    config: EvalConfig
    trials: list[Trial] = field(default_factory=list)
    spend_usd: float = 0.0
    calls: int = 0
    judge_calls: int = 0
    baseline_reruns: int = 0
    notes: list[str] = field(default_factory=list)
    executor_sends_prompts: bool = False


class _Guard:
    """Applies the spend cap and rate limit around every executor call."""

    def __init__(self, executor: Executor, cap: SpendCap, limiter: RateLimiter,
                 pricing_table: dict | None):
        self.executor = executor
        self.cap = cap
        self.limiter = limiter
        self.pricing_table = pricing_table

    def call(
        self, candidate: Candidate, messages: list[dict], *, max_tokens: int,
        temperature: float | None,
    ) -> Completion | str:
        """A completion, or a refusal status string."""
        tokens_in = worst_case_input_tokens(messages)
        if tokens_in is None:
            if self.executor.sends_prompts:
                return SKIPPED_UNPRICED
            tokens_in = estimate_input_tokens(messages)
        worst = estimate_cost(
            candidate.vendor, candidate.model, tokens_in, max_tokens,
            self.pricing_table,
        )
        if worst is None:
            if self.executor.sends_prompts:
                return SKIPPED_UNPRICED
            worst = 0.0
        if not self.cap.allows(worst):
            return SKIPPED_BUDGET
        self.limiter.acquire()
        completion = self.executor.complete(
            candidate, messages, max_tokens=max_tokens, temperature=temperature
        )
        self.cap.book(completion.cost, worst)
        return completion


def run_eval(
    sample: Sample,
    executor: Executor,
    config: EvalConfig,
    *,
    judge_executor: Executor | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> EvalRun:
    """Re-run every sampled record on every candidate and score the answers."""
    judge_executor = judge_executor or executor
    sends = executor.sends_prompts or judge_executor.sends_prompts
    if sends and not config.allow_prompt_reexecution:
        raise PromptReexecutionNotAllowed(
            "re-executing logged prompts on real models needs an explicit opt-in "
            "(allow_prompt_reexecution=True / --allow-prompt-reexecution)"
        )
    if sends and config.max_spend_usd is None:
        raise ValueError("re-executing on real models needs max_spend_usd")
    if not config.candidates:
        raise ValueError("no candidate models")

    cap = SpendCap(config.max_spend_usd)
    limiter_kwargs = {k: v for k, v in {"clock": clock, "sleep": sleep}.items() if v}
    limiter = RateLimiter(config.requests_per_minute, **limiter_kwargs)
    guard = _Guard(executor, cap, limiter, config.pricing_table)
    judge_guard = _Guard(judge_executor, cap, limiter, config.pricing_table)
    run = EvalRun(sample=sample, config=config, executor_sends_prompts=sends)
    policy = config.policy

    for record in sample.records:
        level = record.sensitivity(policy.unclassified_default)
        stratum = sample.stratum(record)
        scorer = config.scorer_for(record.use_case)
        max_tokens = (
            min(record.max_tokens, config.max_tokens)
            if record.max_tokens else config.max_tokens
        )
        temperature = (
            record.temperature if record.temperature is not None else config.temperature
        )
        common: dict[str, Any] = {
            "record_id": record.record_id,
            "use_case": record.use_case,
            "stratum": stratum,
            "baseline_cost": record.baseline_cost(config.pricing_table),
            "baseline_latency_ms": record.latency_ms,
            "baseline_tokens_out": record.tokens_out,
        }
        if scorer is None:
            for candidate in config.candidates:
                run.trials.append(Trial(candidate=candidate.label, status=NO_SCORER,
                                        **common))
            continue

        reference = record.reference
        if (reference is None and scorer.needs_reference and config.rerun_baseline
                and record.vendor and record.model):
            baseline = Candidate(record.vendor, record.model)
            if policy.allows(baseline, level):
                outcome = guard.call(baseline, record.messages or [],
                                     max_tokens=max_tokens, temperature=temperature)
                if isinstance(outcome, Completion):
                    run.calls += 1
                    run.baseline_reruns += 1
                    if outcome.error is None:
                        reference = outcome.text

        def judge_call(messages: list[dict], _scorer: Scorer = scorer,
                       _level: str = level) -> Completion | None:
            judge = _scorer.judge
            if judge is None or not policy.allows(judge, _level):
                return None
            outcome = judge_guard.call(judge, messages, max_tokens=JUDGE_MAX_TOKENS,
                                       temperature=0.0)
            if not isinstance(outcome, Completion):
                return None
            run.judge_calls += 1
            return outcome

        for candidate in config.candidates:
            if not policy.allows(candidate, level):
                run.trials.append(Trial(candidate=candidate.label,
                                        status=SKIPPED_POLICY, **common))
                continue
            if scorer.judge is not None and scorer.judge.vendor == candidate.vendor:
                note = (f"judge {scorer.judge.label} is from the same family as "
                        f"candidate {candidate.label} (M§7.3)")
                if note not in run.notes:
                    run.notes.append(note)
            outcome = guard.call(candidate, record.messages or [],
                                 max_tokens=max_tokens, temperature=temperature)
            if not isinstance(outcome, Completion):
                run.trials.append(Trial(candidate=candidate.label, status=outcome,
                                        **common))
                continue
            run.calls += 1
            measured = {
                "candidate_cost": outcome.cost,
                "candidate_latency_ms": outcome.latency_ms,
                "candidate_tokens_out": outcome.tokens_out,
            }
            if outcome.error is not None or outcome.text is None:
                run.trials.append(Trial(
                    candidate=candidate.label, status=ERROR,
                    detail=(outcome.error or "empty answer")[:200],
                    **common, **measured,
                ))
                continue
            score = scorer.score(record, reference, outcome.text, judge_call)
            run.trials.append(Trial(
                candidate=candidate.label,
                status=SCORED if score.passed is not None else UNSCORED,
                score=score, detail=score.detail, **common, **measured,
            ))

    run.spend_usd = round(cap.spent, 6)
    return run


def candidates_from(labels: Collection[str]) -> list[Candidate]:
    return [Candidate.parse(label) for label in labels]
