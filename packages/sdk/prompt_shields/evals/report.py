"""Parity per (use case, candidate) and savings at parity (M§7.2, M§7.4).

- **Parity** of a cell is the share of scored trials whose answer was
  acceptable, with a two-sided 95% Wilson interval. A cell with fewer than
  ``min_cell_samples`` scored trials is "insufficient sample" and its model is
  never counted as at parity.
- **Savings at parity** per use case: every logged call of the use case (the
  population, not just the sample) is re-priced at the candidate's list price
  with its observed tokens (M§4.6, "same tokens"; cached tokens are priced at
  the full input rate, which understates savings). A call keeps its baseline
  cost when the candidate has no price for it or the policy does not allow the
  candidate at the call's sensitivity level. The use case moves to the
  at-parity candidate with the largest saving, if any saves. Use cases with no
  at-parity candidate (or no eval) save nothing.
- The **total** is spend-weighted over all priced calls. Its 95% interval is a
  percentile bootstrap: population calls are resampled within strata and each
  cell's trial outcomes are resampled, and the at-parity choice is made again
  in every resample.
- The **conservative** figure requires the Wilson lower bound, not the point
  estimate, to reach ``min_parity``.

The Markdown output fills sections 9 and 10 of
``docs/teardown/teardown-report-template.md`` and the eval rows of its
methodology record. It contains aggregates only: no prompt or response text,
no record ids, no prompt hashes.
"""

from __future__ import annotations

import math
import random
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

from prompt_shields.evals.executors import Candidate
from prompt_shields.evals.harness import SCORED, EvalRun
from prompt_shields.evals.records import EvalRecord
from prompt_shields.evals.sampling import stratum_of
from prompt_shields.pricing import estimate_cost

Z_95 = 1.96


def wilson_interval(passed: int, n: int, z: float = Z_95) -> tuple[float, float] | None:
    """Two-sided Wilson score interval for ``passed`` successes out of ``n``."""
    if n <= 0:
        return None
    p = passed / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


AT_PARITY = "at_parity"
BELOW_PARITY = "below_parity"
INSUFFICIENT = "insufficient_sample"


def _cell_status(passed: int, n: int, min_parity: float, min_cell_samples: int) -> str:
    if n < min_cell_samples or n == 0:
        return INSUFFICIENT
    return AT_PARITY if passed / n >= min_parity else BELOW_PARITY


@dataclass
class Cell:
    use_case: str
    candidate: str
    method: str | None
    n: int
    passed: int
    parity: float | None
    ci_low: float | None
    ci_high: float | None
    status: str
    conservative_at_parity: bool
    trials_by_status: dict[str, int]
    judge_outcomes: dict[str, int]
    #: Sum of candidate cost / sum of logged cost, over trials with both.
    cost_ratio: float | None
    #: Median of (candidate latency - logged latency), ms.
    median_latency_delta_ms: float | None
    #: Sum of candidate output tokens / sum of logged output tokens.
    output_token_ratio: float | None
    outcomes: list[bool] = field(default_factory=list, repr=False)


def _ratio(pairs: list[tuple[float, float]]) -> float | None:
    base = sum(b for _, b in pairs)
    return round(sum(c for c, _ in pairs) / base, 4) if pairs and base > 0 else None


def parity_cells(
    run: EvalRun, *, min_parity: float = 0.95, min_cell_samples: int = 30
) -> list[Cell]:
    grouped: dict[tuple[str, str], list] = defaultdict(list)
    for trial in run.trials:
        grouped[(trial.use_case, trial.candidate)].append(trial)
    cells = []
    for (use_case, candidate), trials in sorted(grouped.items()):
        scored = [t for t in trials if t.status == SCORED]
        outcomes = [bool(t.passed) for t in scored]
        n, passed = len(outcomes), sum(outcomes)
        ci = wilson_interval(passed, n)
        methods = {t.score.method for t in trials if t.score is not None}
        scorer = run.config.scorer_for(use_case)
        cost_pairs = [(t.candidate_cost, t.baseline_cost) for t in trials
                      if t.candidate_cost is not None and t.baseline_cost is not None]
        token_pairs = [(t.candidate_tokens_out, t.baseline_tokens_out) for t in trials
                       if t.candidate_tokens_out is not None
                       and t.baseline_tokens_out is not None]
        latency = [t.candidate_latency_ms - t.baseline_latency_ms for t in trials
                   if t.candidate_latency_ms is not None
                   and t.baseline_latency_ms is not None]
        cells.append(Cell(
            use_case=use_case,
            candidate=candidate,
            method=(methods.pop() if len(methods) == 1
                    else getattr(scorer, "method", None)),
            n=n,
            passed=passed,
            parity=round(passed / n, 4) if n else None,
            ci_low=round(ci[0], 4) if ci else None,
            ci_high=round(ci[1], 4) if ci else None,
            status=_cell_status(passed, n, min_parity, min_cell_samples),
            conservative_at_parity=bool(
                ci and n >= min_cell_samples and ci[0] >= min_parity
            ),
            trials_by_status=dict(Counter(t.status for t in trials)),
            judge_outcomes=dict(Counter(
                t.score.outcome for t in scored if t.score and t.score.outcome
            )),
            cost_ratio=_ratio(cost_pairs),
            median_latency_delta_ms=(statistics.median(latency) if latency else None),
            output_token_ratio=_ratio(token_pairs),
            outcomes=outcomes,
        ))
    return cells


# --- savings at parity -------------------------------------------------------


@dataclass
class _Call:
    """One priced population call: baseline cost and per-candidate saving."""

    stratum: tuple[str, str, str]
    baseline: float
    saving: dict[str, float]  # candidate label -> baseline - projected (0 if n/a)


def _population_calls(
    population: Sequence[EvalRecord], candidates: Sequence[Candidate], run: EvalRun
) -> tuple[list[_Call], int]:
    policy = run.config.policy
    pricing_table = run.config.pricing_table
    calls, unpriced = [], 0
    for record in population:
        base = record.baseline_cost(pricing_table)
        if base is None:
            unpriced += 1
            continue
        level = record.sensitivity(policy.unclassified_default)
        saving = {}
        for candidate in candidates:
            projected = None
            if policy.allows(candidate, level):
                projected = estimate_cost(
                    candidate.vendor, candidate.model, record.tokens_in,
                    record.tokens_out, pricing_table,
                )
            saving[candidate.label] = base - projected if projected is not None else 0.0
        calls.append(_Call(stratum_of(record, policy.unclassified_default), base, saving))
    return calls, unpriced


@dataclass
class UseCaseSavings:
    use_case: str
    calls: int
    baseline_usd: float
    at_parity_usd: float
    saving_pct: float | None
    from_model: str | None
    to_model: str | None
    conservative_at_parity_usd: float
    conservative_to_model: str | None
    eval_cells: int


def _choose(savings: dict[str, float], eligible: set[str]) -> tuple[str | None, float]:
    best, best_saving = None, 0.0
    for label in sorted(eligible):
        if savings.get(label, 0.0) > best_saving:
            best, best_saving = label, savings[label]
    return best, best_saving


def _total_saving(
    calls_by_use_case: dict[str, list[_Call]], eligible: dict[str, set[str]]
) -> tuple[float, float]:
    baseline = saving = 0.0
    for use_case, calls in calls_by_use_case.items():
        sums: dict[str, float] = defaultdict(float)
        for call in calls:
            baseline += call.baseline
            for label, value in call.saving.items():
                sums[label] += value
        saving += _choose(sums, eligible.get(use_case, set()))[1]
    return baseline, saving


@dataclass
class EvalReport:
    cells: list[Cell]
    use_cases: list[UseCaseSavings]
    baseline_usd: float
    at_parity_usd: float
    saving_pct: float | None
    saving_ci: tuple[float, float] | None
    conservative_at_parity_usd: float
    conservative_saving_pct: float | None
    unpriced_calls: int
    population_calls: int
    params: dict
    methodology: dict
    notes: list[str]

    def to_dict(self) -> dict:
        data = asdict(self)
        for cell in data["cells"]:
            cell.pop("outcomes", None)
        return data

    def to_markdown(self) -> str:
        return render_markdown(self)


def build_report(
    population: Sequence[EvalRecord],
    run: EvalRun,
    *,
    min_parity: float = 0.95,
    min_cell_samples: int = 30,
    min_cell_calls: int = 10,
    bootstrap_resamples: int = 1000,
    bootstrap_seed: int = 0,
) -> EvalReport:
    """Parity cells and savings at parity for ``population`` (all logged calls)."""
    cells = parity_cells(run, min_parity=min_parity, min_cell_samples=min_cell_samples)
    candidates = list(run.config.candidates)
    calls, unpriced = _population_calls(population, candidates, run)
    by_use_case: dict[str, list[_Call]] = defaultdict(list)
    for call in calls:
        by_use_case[call.stratum[0]].append(call)

    eligible = defaultdict(set)
    conservative = defaultdict(set)
    cells_per_use_case = Counter()
    for cell in cells:
        cells_per_use_case[cell.use_case] += 1
        if cell.status == AT_PARITY:
            eligible[cell.use_case].add(cell.candidate)
        if cell.conservative_at_parity:
            conservative[cell.use_case].add(cell.candidate)

    main_model = _main_models(population, run)
    rows, total_base, total_saving, total_cons = [], 0.0, 0.0, 0.0
    for use_case in sorted(by_use_case):
        uc_calls = by_use_case[use_case]
        base = sum(c.baseline for c in uc_calls)
        sums: dict[str, float] = defaultdict(float)
        for call in uc_calls:
            for label, value in call.saving.items():
                sums[label] += value
        to_model, saving = _choose(sums, eligible[use_case])
        cons_model, cons_saving = _choose(sums, conservative[use_case])
        total_base += base
        total_saving += saving
        total_cons += cons_saving
        rows.append(UseCaseSavings(
            use_case=use_case,
            calls=len(uc_calls),
            baseline_usd=round(base, 6),
            at_parity_usd=round(base - saving, 6),
            saving_pct=round(saving / base, 4) if base > 0 else None,
            from_model=main_model.get(use_case),
            to_model=to_model,
            conservative_at_parity_usd=round(base - cons_saving, 6),
            conservative_to_model=cons_model,
            eval_cells=cells_per_use_case[use_case],
        ))

    saving_ci = _bootstrap(
        by_use_case, cells, min_parity, min_cell_samples,
        bootstrap_resamples, bootstrap_seed,
    )
    params = {
        "min_parity": min_parity,
        "min_cell_samples": min_cell_samples,
        "min_cell_calls": min_cell_calls,
        "bootstrap_resamples": bootstrap_resamples,
        "bootstrap_seed": bootstrap_seed,
    }
    return EvalReport(
        cells=cells,
        use_cases=rows,
        baseline_usd=round(total_base, 6),
        at_parity_usd=round(total_base - total_saving, 6),
        saving_pct=round(total_saving / total_base, 4) if total_base > 0 else None,
        saving_ci=saving_ci,
        conservative_at_parity_usd=round(total_base - total_cons, 6),
        conservative_saving_pct=(
            round(total_cons / total_base, 4) if total_base > 0 else None
        ),
        unpriced_calls=unpriced,
        population_calls=len(population),
        params=params,
        methodology=_methodology(run),
        notes=list(run.notes),
    )


def _main_models(population: Sequence[EvalRecord], run: EvalRun) -> dict[str, str]:
    spend: dict[str, Counter] = defaultdict(Counter)
    for record in population:
        if record.model:
            label = f"{record.vendor}:{record.model}" if record.vendor else record.model
            spend[record.use_case][label] += record.baseline_cost(
                run.config.pricing_table) or 0.0
    return {uc: c.most_common(1)[0][0] for uc, c in spend.items() if c}


def _bootstrap(
    by_use_case: dict[str, list[_Call]],
    cells: list[Cell],
    min_parity: float,
    min_cell_samples: int,
    resamples: int,
    seed: int,
) -> tuple[float, float] | None:
    if resamples <= 0 or not by_use_case:
        return None
    rng = random.Random(seed)
    strata: dict[tuple, list[_Call]] = defaultdict(list)
    for calls in by_use_case.values():
        for call in calls:
            strata[call.stratum].append(call)
    stats = []
    for _ in range(resamples):
        resampled: dict[str, list[_Call]] = defaultdict(list)
        for key, members in strata.items():
            resampled[key[0]].extend(rng.choices(members, k=len(members)))
        eligible: dict[str, set[str]] = defaultdict(set)
        for cell in cells:
            n = len(cell.outcomes)
            if n == 0:
                continue
            passed = sum(rng.choices(cell.outcomes, k=n))
            if _cell_status(passed, n, min_parity, min_cell_samples) == AT_PARITY:
                eligible[cell.use_case].add(cell.candidate)
        base, saving = _total_saving(resampled, eligible)
        if base > 0:
            stats.append(saving / base)
    if not stats:
        return None
    stats.sort()
    low = stats[int(0.025 * (len(stats) - 1))]
    high = stats[math.ceil(0.975 * (len(stats) - 1))]
    return round(low, 4), round(high, 4)


def _methodology(run: EvalRun) -> dict:
    sample = run.sample
    config = run.config
    scorers = {}
    judges = set()
    for use_case, scorer in sorted(config.scorers.items()):
        scorers[use_case] = scorer.method
        judge = getattr(scorer, "judge", None)
        if judge is not None:
            judges.add(f"{judge.label} (prompt {getattr(scorer, 'prompt_version', '?')})")
    return {
        "sample_seed": sample.seed,
        "per_stratum": sample.per_stratum,
        "strata": [
            {"use_case": k[0], "sensitivity": k[1], "complexity": k[2],
             "population": v.population, "eligible": v.eligible, "sampled": v.sampled}
            for k, v in sorted(sample.strata.items())
        ],
        "candidates": [c.label for c in config.candidates],
        "scoring": scorers,
        "judges": sorted(judges),
        "policy": config.policy.describe(),
        "temperature_default": config.temperature,
        "max_tokens": config.max_tokens,
        "prompts_sent_to_providers": run.executor_sends_prompts,
        "max_spend_usd": config.max_spend_usd,
        "spend_usd": run.spend_usd,
        "calls": run.calls,
        "judge_calls": run.judge_calls,
        "baseline_reruns": run.baseline_reruns,
        "requests_per_minute": config.requests_per_minute,
    }


# --- Markdown ----------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _usd(value: float | None) -> str:
    return "—" if value is None else f"USD {value:,.2f}"


def _status_text(cell: Cell) -> str:
    return {AT_PARITY: "yes", BELOW_PARITY: "no", INSUFFICIENT: "insufficient sample"}[
        cell.status
    ]


def render_markdown(report: EvalReport) -> str:
    m = report.methodology
    p = report.params
    lines = []
    lines.append("## 9. Quality parity")
    lines.append("")
    scoring = ", ".join(f"{uc}: {method}" for uc, method in m["scoring"].items())
    judges = "; ".join(m["judges"]) or "none"
    lines.append(f"Scoring: {scoring}. Judge: {judges}.")
    lines.append("")
    lines.append("| Use case | Candidate model | Sample n | Parity | 95% Wilson CI | "
                 "At parity? |")
    lines.append("|---|---|---|---|---|---|")
    for cell in report.cells:
        ci = ("—" if cell.ci_low is None
              else f"{_pct(cell.ci_low)} – {_pct(cell.ci_high)}")
        lines.append(f"| {cell.use_case} | {cell.candidate} | {cell.n} | "
                     f"{_pct(cell.parity)} | {ci} | {_status_text(cell)} |")
    lines.append("")
    lines.append("Every tested cell is listed, including the ones that failed (M§7.4). "
                 f"At parity: point estimate ≥ {_pct(p['min_parity'])} and "
                 f"n ≥ {p['min_cell_samples']}.")
    lines.append("")
    lines.append("### 9.1 Re-execution: cost, latency and output length")
    lines.append("")
    lines.append("| Use case | Candidate model | Trials (by status) | Cost vs. logged | "
                 "Median latency delta | Output tokens vs. logged |")
    lines.append("|---|---|---|---|---|---|")
    for cell in report.cells:
        statuses = ", ".join(f"{k} {v}" for k, v in sorted(cell.trials_by_status.items()))
        latency = ("—" if cell.median_latency_delta_ms is None
                   else f"{cell.median_latency_delta_ms:+.0f} ms")
        cost = "—" if cell.cost_ratio is None else f"{cell.cost_ratio:.2f}×"
        tokens = ("—" if cell.output_token_ratio is None
                  else f"{cell.output_token_ratio:.2f}×")
        lines.append(f"| {cell.use_case} | {cell.candidate} | {statuses} | {cost} | "
                     f"{latency} | {tokens} |")
    lines.append("")
    lines.append("Ratios compare the re-executed calls with the logged calls they "
                 "replay. Projected spend below uses observed tokens and is not "
                 "corrected by the output-token ratio (M§4.6).")
    lines.append("")

    lines.append("## 10. Savings at parity")
    lines.append("")
    lines.append("| Use case | Baseline spend | At-parity spend | Saving | Model change |")
    lines.append("|---|---|---|---|---|")
    other = None
    for row in report.use_cases:
        if row.calls < p["min_cell_calls"]:
            other = other or {"base": 0.0, "parity": 0.0, "calls": 0}
            other["base"] += row.baseline_usd
            other["parity"] += row.at_parity_usd
            other["calls"] += row.calls
            continue
        change = (f"{row.from_model or '?'} → {row.to_model}" if row.to_model
                  else ("no eval set" if not row.eval_cells else "none at parity"))
        lines.append(f"| {row.use_case} | {_usd(row.baseline_usd)} | "
                     f"{_usd(row.at_parity_usd)} | {_pct(row.saving_pct)} | {change} |")
    if other:
        saving = ((other["base"] - other["parity"]) / other["base"]
                  if other["base"] > 0 else None)
        lines.append(f"| Other (use cases under {p['min_cell_calls']} calls) | "
                     f"{_usd(other['base'])} | {_usd(other['parity'])} | "
                     f"{_pct(saving)} | various |")
    ci = ("—" if report.saving_ci is None
          else f"{_pct(report.saving_ci[0])} – {_pct(report.saving_ci[1])}")
    lines.append(f"| **Total (spend-weighted)** | **{_usd(report.baseline_usd)}** | "
                 f"**{_usd(report.at_parity_usd)}** | "
                 f"**{_pct(report.saving_pct)} (95% CI {ci})** | |")
    lines.append(f"| Conservative (Wilson lower bound) | | "
                 f"{_usd(report.conservative_at_parity_usd)} | "
                 f"{_pct(report.conservative_saving_pct)} | |")
    lines.append("")
    lines.append("Use cases with no eval set count as zero savings here (M§7.4). "
                 f"{report.unpriced_calls} of {report.population_calls} calls have no "
                 "baseline price and are left out (M§4.5). Percentages are of priced "
                 "baseline spend over the window.")
    if report.notes:
        lines.append("")
        lines.append("Notes:")
        lines.extend(f"- {note}" for note in report.notes)
    lines.append("")

    lines.append("### Methodology record (eval rows)")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    strata = "; ".join(
        f"{s['use_case']}/{s['sensitivity']}/{s['complexity']}: "
        f"{s['sampled']} of {s['population']}"
        for s in m["strata"]
    )
    lines.append(f"| Sample size per stratum, seed | up to {m['per_stratum']} per "
                 f"stratum, seed {m['sample_seed']} ({strata}) |")
    lines.append(f"| Scoring method per use case | {scoring} |")
    lines.append(f"| Judge model, provider, jurisdiction, prompt version | {judges} "
                 "(jurisdiction: record by hand) |")
    spend = (f"USD {m['spend_usd']:.4f} of a USD {m['max_spend_usd']:.2f} cap"
             if m["max_spend_usd"] is not None else f"USD {m['spend_usd']:.4f}, no cap")
    lines.append(f"| Re-execution temperature, spend | {m['temperature_default']} unless "
                 f"the logged call's is known; {spend}; {m['calls']} calls, "
                 f"{m['judge_calls']} judge calls |")
    lines.append(f"| `min_parity`, `min_cell_samples`, `min_cell_calls` | "
                 f"{p['min_parity']}, {p['min_cell_samples']}, {p['min_cell_calls']} |")
    lines.append(f"| CI methods | Wilson 95% two-sided per cell; bootstrap "
                 f"{p['bootstrap_resamples']} resamples, seed {p['bootstrap_seed']} |")
    lines.append("")
    return "\n".join(lines)
