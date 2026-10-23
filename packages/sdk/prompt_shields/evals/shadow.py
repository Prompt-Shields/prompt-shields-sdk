"""Shadow-eval sampler: judge live traffic against one alternative model
(PRD 0001 §7.13, issue #46).

A :class:`ShadowSampler` is handed each served call with :meth:`offer` (or
:meth:`offer_routed`, for a ``ShieldsRouter`` response). It samples a share
of them, re-runs each sampled prompt on one alternative model the policy
allows at the call's sensitivity, and has a pairwise judge compare the
alternative's answer with the primary one. The caller only ever sees the
primary answer: ``offer`` returns nothing from the shadow call.

Safety rules (the eval harness's, applied to live traffic):

- **Opt-in.** An executor that sends prompts off the machine needs
  ``allow_prompt_reexecution=True`` and ``max_spend_usd``.
- **Never on the primary path.** ``offer`` does a sampling draw and a
  non-blocking put on a bounded queue; a background thread does the work.
  When the queue is full the job is dropped and counted
  (``dropped_queue_full``). ``offer`` never raises.
- **Spend.** Every shadow and judge call is checked first against a
  worst-case estimate (:func:`~prompt_shields.evals.budget.worst_case_input_tokens`
  plus ``max_tokens`` at list price) for two caps: the absolute
  ``max_spend_usd`` and ``max_spend_share`` (at most 2 %) of the primary
  spend offered so far. Calls whose spend cannot be bounded are not made.
- **Rate limit.** ``requests_per_minute`` spaces shadow and judge calls out
  (the waiting happens on the background thread).
- **Policy.** The alternative and the judge must both be allowed by an
  explicit :class:`~prompt_shields.evals.harness.EvalPolicy` allowlist at the
  call's sensitivity; ``EvalPolicy.allow_all()`` is refused. An unknown label
  counts as ``restricted``, a missing one as the policy's
  ``unclassified_default``. A call no alternative may see is not copied or
  queued.
- **No text out.** Prompts and answers stay in memory only for the duration
  of the job. :meth:`stats` and the ``on_result`` callback carry counts,
  costs, verdicts and model labels, never text; errors are reported by
  exception class name only.
"""

from __future__ import annotations

import copy
import itertools
import math
import queue
import random
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from prompt_shields.evals.budget import RateLimiter, SpendCap
from prompt_shields.evals.executors import Candidate, Completion, Executor
from prompt_shields.evals.harness import (
    JUDGE_MAX_TOKENS,
    SKIPPED_BUDGET,
    EvalPolicy,
    PromptReexecutionNotAllowed,
    _Guard,
)
from prompt_shields.evals.records import EvalRecord
from prompt_shields.evals.scoring import PairwiseJudge
from prompt_shields.route_policy import DEFAULT_LEVELS, data_policy_rule, tier_complies

#: Shadow spend may never exceed this share of the primary (routed) spend.
MAX_SPEND_SHARE = 0.02
DEFAULT_SAMPLE_RATE = 0.01

# Result statuses (in addition to the harness's skipped_budget / skipped_unpriced).
JUDGED = "judged"
UNSCORED = "unscored"
ERROR = "error"
SKIPPED_SHARE = "skipped_spend_share"
SKIPPED_JUDGE_POLICY = "skipped_judge_policy"


def policy_from_route_policy(route_policy: Mapping[str, Any]) -> EvalPolicy:
    """The shadow allowlist implied by a ``route-policy.v1`` policy.

    At each sensitivity level, the allowed models are the policy's tiers that
    meet that level's ``data_policy`` rule (the router's own check), as
    ``provider:model``. Tiers without a ``provider`` are left out, so a bare
    model id never admits another vendor. ``unclassified_default`` comes from
    the policy's ``sensitivity`` section (``confidential`` when it has none).
    """
    sensitivity = route_policy.get("sensitivity") or {}
    levels = list(sensitivity.get("levels") or DEFAULT_LEVELS)
    tiers = [
        tier
        for group in (route_policy.get("groups") or {}).values()
        if isinstance(group, list)
        for tier in group
        if isinstance(tier, Mapping) and tier.get("provider") and tier.get("model")
    ]
    allowed = {
        level: frozenset(
            f"{tier['provider']}:{tier['model']}"
            for tier in tiers
            if tier_complies(tier, data_policy_rule(route_policy, level))
        )
        for level in levels
    }
    return EvalPolicy(
        allowed=allowed,
        unclassified_default=sensitivity.get("unclassified_default", "confidential"),
    )


class _ShareCap(SpendCap):
    """The absolute cap, plus shadow spend <= ``share`` x primary spend."""

    def __init__(self, max_usd: float | None, share: float):
        super().__init__(max_usd)
        self.share = share
        self.primary_spent = 0.0
        self.refused_share = 0
        self._lock = threading.Lock()

    def add_primary(self, usd: float) -> None:
        with self._lock:
            self.primary_spent += usd

    def allows(self, worst_case_usd: float) -> bool:
        if not super().allows(worst_case_usd):
            return False
        with self._lock:
            limit = self.share * self.primary_spent
        if self.spent + worst_case_usd > limit + 1e-12:
            self.refused_share += 1
            return False
        return True


@dataclass
class ShadowConfig:
    #: Alternative models; each sampled call goes to one the policy allows.
    candidates: Sequence[Candidate]
    #: Judge model; it also has to be allowed at the call's sensitivity.
    judge: Candidate
    #: An explicit allowlist (``EvalPolicy.allow_all()`` is refused).
    policy: EvalPolicy
    sample_rate: float = DEFAULT_SAMPLE_RATE
    #: Shadow spend (alternative + judge) as a share of primary spend.
    max_spend_share: float = MAX_SPEND_SHARE
    allow_prompt_reexecution: bool = False
    max_spend_usd: float | None = None
    requests_per_minute: float | None = 60.0
    queue_size: int = 64
    max_tokens: int = 1024
    temperature: float = 0.0
    seed: int | None = None
    pricing_table: dict | None = None


@dataclass(frozen=True)
class ShadowResult:
    """The outcome of one shadow job. No prompt or answer text."""

    use_case: str
    sensitivity: str
    primary: str
    candidate: str
    status: str
    #: Judge outcome for the alternative vs. the primary: win, tie or loss.
    outcome: str | None = None
    candidate_cost: float | None = None
    judge_cost: float = 0.0
    candidate_latency_ms: int | None = None
    detail: str | None = None


@dataclass
class _Cell:
    judged: int = 0
    wins: int = 0
    ties: int = 0
    losses: int = 0
    unscored: int = 0
    errors: int = 0
    skipped: int = 0
    shadow_cost_usd: float = 0.0


@dataclass
class _Job:
    record: EvalRecord
    level: str
    primary: str
    primary_text: str
    candidate: Candidate


@dataclass
class _Counters:
    offered: int = 0
    sampled: int = 0
    enqueued: int = 0
    dropped_queue_full: int = 0
    skipped_no_answer: int = 0
    skipped_policy: int = 0
    offer_errors: int = 0
    worker_errors: int = 0
    completed: int = 0
    cells: dict = field(default_factory=dict)


def _primary_label(model: str, vendor: str | None) -> str:
    return f"{vendor}:{model}" if vendor else model


class ShadowSampler:
    """Samples served calls and shadow-evaluates them in the background."""

    def __init__(
        self,
        executor: Executor,
        config: ShadowConfig,
        *,
        judge_executor: Executor | None = None,
        on_result: Callable[[ShadowResult], None] | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        start: bool = True,
    ):
        judge_executor = judge_executor or executor
        sends = executor.sends_prompts or judge_executor.sends_prompts
        if sends and not config.allow_prompt_reexecution:
            raise PromptReexecutionNotAllowed(
                "shadow re-execution of live prompts needs an explicit opt-in "
                "(allow_prompt_reexecution=True)"
            )
        if sends and config.max_spend_usd is None:
            raise ValueError("shadow re-execution on real models needs max_spend_usd")
        if config.policy.allowed is None:
            raise ValueError(
                "shadow eval needs an explicit per-sensitivity model allowlist; "
                "EvalPolicy.allow_all() is not accepted for live traffic"
            )
        if not config.candidates:
            raise ValueError("no candidate models")
        if not (math.isfinite(config.sample_rate) and 0 <= config.sample_rate <= 1):
            raise ValueError("sample_rate must be in [0, 1]")
        if not (
            math.isfinite(config.max_spend_share)
            and 0 <= config.max_spend_share <= MAX_SPEND_SHARE
        ):
            raise ValueError(f"max_spend_share must be in [0, {MAX_SPEND_SHARE}]")
        if config.queue_size < 1:
            raise ValueError("queue_size must be at least 1")

        self.config = config
        self._executor = executor
        self._judge_executor = judge_executor
        self._on_result = on_result
        self._rng = random.Random(config.seed)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._counters = _Counters()
        self._cap = _ShareCap(config.max_spend_usd, config.max_spend_share)
        limiter_kwargs = {k: v for k, v in {"clock": clock, "sleep": sleep}.items() if v}
        limiter = RateLimiter(config.requests_per_minute, **limiter_kwargs)
        self._guard = _Guard(executor, self._cap, limiter, config.pricing_table)
        self._judge_guard = _Guard(judge_executor, self._cap, limiter,
                                   config.pricing_table)
        self._scorer = PairwiseJudge(config.judge)
        self._queue: queue.Queue[_Job | None] = queue.Queue(maxsize=config.queue_size)
        self._thread: threading.Thread | None = None
        self._closed = False
        if start:
            self.start()

    # --- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None and not self._closed:
            self._thread = threading.Thread(
                target=self._run, name="ps-shadow-eval", daemon=True
            )
            self._thread.start()

    def close(self, timeout: float | None = 5.0) -> None:
        """Stop taking jobs; let the worker finish the queue, up to ``timeout``."""
        self._closed = True
        if self._thread is not None:
            try:
                self._queue.put(None, timeout=timeout)
            except queue.Full:
                pass
            self._thread.join(timeout)

    def drain(self) -> None:
        """Process every queued job on the calling thread. For a sampler built
        with ``start=False`` (tests, batch use); do not mix with the worker."""
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                return
            if job is not None:
                self._process(job)
            self._queue.task_done()

    # --- primary-path hooks -----------------------------------------------------

    def offer(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str,
        response_text: str | None,
        vendor: str | None = None,
        sensitivity_level: str | None = None,
        use_case: str | None = None,
        cost_usd: float | None = None,
    ) -> bool:
        """Offer one served call. Returns True when it was queued for a shadow
        run. Never blocks on a model call and never raises."""
        try:
            return self._offer(messages, model, response_text, vendor,
                               sensitivity_level, use_case, cost_usd)
        except Exception:  # noqa: BLE001 - must never break the primary call
            with self._lock:
                self._counters.offer_errors += 1
            return False

    def offer_routed(
        self,
        messages: Sequence[Mapping[str, Any]],
        completion: Any,
        *,
        use_case: str | None = None,
    ) -> bool:
        """``offer`` for a ``RoutedChatCompletion`` from ``ShieldsRouter``:
        reads the served model, provider, sensitivity and cost from ``ps``."""
        try:
            ps = getattr(completion, "ps", None)
            route = getattr(ps, "route", None)
            cost = getattr(ps, "cost", None)
            choices = getattr(completion, "choices", None) or []
            message = getattr(choices[0], "message", None) if choices else None
            text = getattr(message, "content", None)
            model = getattr(route, "model", None) or getattr(completion, "model", None)
            if not model:
                raise ValueError("no served model")
            return self.offer(
                messages,
                model=model,
                vendor=getattr(route, "provider", None),
                response_text=text if isinstance(text, str) else None,
                sensitivity_level=getattr(route, "sensitivity_level", None),
                use_case=use_case,
                cost_usd=getattr(cost, "usd", None),
            )
        except Exception:  # noqa: BLE001 - must never break the primary call
            with self._lock:
                self._counters.offer_errors += 1
            return False

    def _offer(self, messages, model, response_text, vendor, sensitivity_level,
               use_case, cost_usd) -> bool:
        if isinstance(cost_usd, (int, float)) and not isinstance(cost_usd, bool) \
                and math.isfinite(cost_usd) and cost_usd > 0:
            self._cap.add_primary(float(cost_usd))
        with self._lock:
            c = self._counters
            c.offered += 1
            if self._closed or self._rng.random() >= self.config.sample_rate:
                return False
            c.sampled += 1
            if not response_text or not messages:
                c.skipped_no_answer += 1
                return False
            record = EvalRecord(
                record_id=f"shadow-{next(self._ids)}",
                vendor=vendor,
                model=model,
                messages=None,
                use_case=use_case or "(unattributed)",
                data_classification=sensitivity_level,
            )
            level = record.sensitivity(self.config.policy.unclassified_default)
            primary = _primary_label(model, vendor)
            allowed = [
                cand for cand in self.config.candidates
                if cand.label != primary and cand.model != model
                and self.config.policy.allows(cand, level)
            ]
            if not allowed:
                c.skipped_policy += 1
                return False
            candidate = self._rng.choice(allowed)
        # Copy only once the call is known to be shadowed.
        record = replace(record, messages=copy.deepcopy(list(messages)))
        job = _Job(record, level, primary, response_text, candidate)
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            with self._lock:
                self._counters.dropped_queue_full += 1
            return False
        with self._lock:
            self._counters.enqueued += 1
        return True

    # --- background work -----------------------------------------------------

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is None:
                    return
                self._process(job)
            finally:
                self._queue.task_done()

    def _process(self, job: _Job) -> None:
        try:
            result = self._evaluate(job)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            with self._lock:
                self._counters.worker_errors += 1
            result = self._result(job, ERROR, detail=type(exc).__name__)
        self._book(result)
        if self._on_result is not None:
            try:
                self._on_result(result)
            except Exception:  # noqa: BLE001 - a callback must not stop the worker
                with self._lock:
                    self._counters.worker_errors += 1

    def _result(self, job: _Job, status: str, **kwargs: Any) -> ShadowResult:
        return ShadowResult(
            use_case=job.record.use_case, sensitivity=job.level,
            primary=job.primary, candidate=job.candidate.label, status=status,
            **kwargs,
        )

    def _evaluate(self, job: _Job) -> ShadowResult:
        cfg = self.config
        # Check the judge first, so no shadow call is made that cannot be judged.
        if not cfg.policy.allows(cfg.judge, job.level):
            return self._result(job, SKIPPED_JUDGE_POLICY)
        cap = self._cap
        before = cap.refused_share
        outcome = self._guard.call(
            job.candidate, job.record.messages or [],
            max_tokens=cfg.max_tokens, temperature=cfg.temperature,
        )
        if not isinstance(outcome, Completion):
            status = SKIPPED_SHARE if (
                outcome == SKIPPED_BUDGET and cap.refused_share > before
            ) else outcome
            return self._result(job, status)
        measured = {"candidate_cost": outcome.cost,
                    "candidate_latency_ms": outcome.latency_ms}
        if outcome.error is not None or outcome.text is None:
            detail = (outcome.error or "empty answer").split(":", 1)[0][:80]
            return self._result(job, ERROR, detail=detail, **measured)

        judge_spend = [0.0]

        def judge_call(messages: list[dict]) -> Completion | None:
            spent = cap.spent
            done = self._judge_guard.call(cfg.judge, messages,
                                          max_tokens=JUDGE_MAX_TOKENS,
                                          temperature=0.0)
            judge_spend[0] += cap.spent - spent
            return done if isinstance(done, Completion) else None

        # The primary answer is the reference: the judge rates the alternative
        # against what the user was actually served.
        score = self._scorer.score(job.record, job.primary_text, outcome.text,
                                   judge_call)
        status = JUDGED if score.passed is not None else UNSCORED
        return self._result(
            job, status, outcome=score.outcome, judge_cost=judge_spend[0],
            detail=None if status == JUDGED else score.detail, **measured,
        )

    def _book(self, result: ShadowResult) -> None:
        key = (result.use_case, result.sensitivity, result.candidate)
        with self._lock:
            c = self._counters
            c.completed += 1
            cell = c.cells.setdefault(key, _Cell())
            cell.shadow_cost_usd += (result.candidate_cost or 0.0) + result.judge_cost
            if result.status == JUDGED:
                cell.judged += 1
                if result.outcome == "win":
                    cell.wins += 1
                elif result.outcome == "loss":
                    cell.losses += 1
                else:
                    cell.ties += 1
            elif result.status == UNSCORED:
                cell.unscored += 1
            elif result.status == ERROR:
                cell.errors += 1
            else:
                cell.skipped += 1

    # --- reporting -------------------------------------------------------------

    def stats(self) -> dict:
        """Aggregates only: counts, spend, verdicts per (use case, sensitivity,
        alternative). Shadow spend is reported apart from primary spend."""
        cap = self._cap
        with self._lock:
            c = self._counters
            cells = [
                {"use_case": k[0], "sensitivity": k[1], "candidate": k[2],
                 "judged": v.judged, "wins": v.wins, "ties": v.ties,
                 "losses": v.losses, "unscored": v.unscored, "errors": v.errors,
                 "skipped": v.skipped,
                 "acceptable_rate": (
                     round((v.wins + v.ties) / v.judged, 4) if v.judged else None
                 ),
                 "shadow_cost_usd": round(v.shadow_cost_usd, 6)}
                for k, v in sorted(c.cells.items())
            ]
            counters = {
                "offered": c.offered, "sampled": c.sampled, "enqueued": c.enqueued,
                "dropped_queue_full": c.dropped_queue_full,
                "skipped_no_answer": c.skipped_no_answer,
                "skipped_policy": c.skipped_policy,
                "offer_errors": c.offer_errors, "worker_errors": c.worker_errors,
                "completed": c.completed,
            }
        primary = cap.primary_spent
        return {
            **counters,
            "queue_depth": self._queue.qsize(),
            "refused_spend_cap": cap.refused,
            "refused_spend_share": cap.refused_share,
            "primary_spend_usd": round(primary, 6),
            "shadow_spend_usd": round(cap.spent, 6),
            "shadow_spend_share": round(cap.spent / primary, 6) if primary else None,
            "sample_rate": self.config.sample_rate,
            "max_spend_share": self.config.max_spend_share,
            "cells": cells,
        }
