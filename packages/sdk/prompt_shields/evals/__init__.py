"""Eval harness: quality parity by sample re-execution (PRD 0001 §7.4, issue #14).

Pipeline: :func:`load_records` → :func:`stratified_sample` → :func:`run_eval`
(re-run each sampled prompt on candidate models, score each answer) →
:func:`build_report` (parity per use case and model with Wilson intervals,
savings at parity with a bootstrap interval, Markdown for the teardown
report). Method: ``docs/teardown/methodology.md`` §7.2–7.4. CLI: ``ps-eval``.

Nothing here calls a model unless given a :class:`ProviderExecutor`, and that
needs ``allow_prompt_reexecution=True`` and a ``max_spend_usd`` cap.

:class:`ShadowSampler` applies the same safety rules to live traffic: it
samples served calls in the background and judges one compliant alternative
against the primary answer (PRD 0001 §7.13, issue #46).
"""

from prompt_shields.evals.budget import RateLimiter, SpendCap
from prompt_shields.evals.executors import (
    Candidate,
    Completion,
    Executor,
    FakeExecutor,
    ProviderExecutor,
    RecordedExecutor,
    prompt_key,
    recording,
)
from prompt_shields.evals.harness import (
    EvalConfig,
    EvalPolicy,
    EvalRun,
    PromptReexecutionNotAllowed,
    Trial,
    run_eval,
)
from prompt_shields.evals.records import EvalRecord, load_records
from prompt_shields.evals.report import EvalReport, build_report, wilson_interval
from prompt_shields.evals.sampling import Sample, stratified_sample
from prompt_shields.evals.scoring import (
    ExactMatch,
    PairwiseJudge,
    SchemaValid,
    Score,
    SimilarityMatch,
)
from prompt_shields.evals.shadow import (
    ShadowConfig,
    ShadowResult,
    ShadowSampler,
    policy_from_route_policy,
)

__all__ = [
    "Candidate",
    "Completion",
    "EvalConfig",
    "EvalPolicy",
    "EvalRecord",
    "EvalReport",
    "EvalRun",
    "ExactMatch",
    "Executor",
    "FakeExecutor",
    "PairwiseJudge",
    "PromptReexecutionNotAllowed",
    "ProviderExecutor",
    "RateLimiter",
    "RecordedExecutor",
    "Sample",
    "SchemaValid",
    "Score",
    "ShadowConfig",
    "ShadowResult",
    "ShadowSampler",
    "SimilarityMatch",
    "SpendCap",
    "Trial",
    "build_report",
    "load_records",
    "policy_from_route_policy",
    "prompt_key",
    "recording",
    "run_eval",
    "stratified_sample",
    "wilson_interval",
]
