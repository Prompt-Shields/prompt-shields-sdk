"""Policy replay, sensitivity mix and quality parity in the teardown report.

``ps-teardown run --policy policy.json`` replays every priced record through
the Python reference router (``prompt_shields.teardown_replay``, #13) and
adds the sensitivity mix (template §7) and projected spend per strategy
(§8) to the report. ``--eval-results eval.json`` reads the parity cells of a
prior ``ps-eval`` run (#14) and fills quality parity (§9) and savings at
parity (§10): the *policy at parity* strategy is the same replay with each
use case limited to the models that reached parity for it (methodology
M§6.2), never the *policy, cheapest* number.

The teardown run itself never executes a prompt and opens no socket: eval
results are read from a file. Replay uses the teardown's own baseline cost
per record, so every section shares one baseline. Only aggregates and
category names reach the report.
"""

from __future__ import annotations

import copy
import math
import random
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import prompt_shields.teardown_replay as tr  # attributes used lazily: no import cycle
from prompt_shields import catalog as model_catalog
from prompt_shields.route_policy import data_policy_rule, tier_complies
from prompt_shields.teardown.catalog import PriceSource

if TYPE_CHECKING:  # engine imports this module
    from prompt_shields.teardown.engine import PricedRecord

#: Gate 1 (PRD): at least 30% savings at at least 95% parity.
GATE1_SAVING = 0.30
GATE1_PARITY = 0.95

AT_PARITY = "at_parity"
NO_EVAL = "no_eval"
NONE_AT_PARITY = "none_at_parity"
ROUTED_OFF_PARITY = "routed_outside_parity_set"

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def _label(value: Any) -> str | None:
    """A string from an input file, safe for one Markdown line."""
    if value is None:
        return None
    text = _CONTROL.sub(" ", str(value)).strip()
    return text or None


def _money(x: float) -> float:
    return round(float(x), 6)


def _share(part: float, whole: float) -> float | None:
    return round(part / whole, 6) if whole else None


# --- replay ---------------------------------------------------------------------


def catalog_projector(source: PriceSource) -> Callable[[Any, Mapping[str, Any]], tuple]:
    """Price a record on a policy tier through the teardown's price source.

    Catalog price first, with every priced dimension of the record (the
    price source handles aggregated export rows). A service tier the
    candidate does not price is priced at standard. A candidate not in the
    catalog is priced at the policy's per-1k rates, without cache or tier
    discounts (M§4.6). Missing token counts give a null cost.
    """

    def project(record, tier: Mapping[str, Any]) -> tuple[float | None, str | None]:
        if record.tokens_in is None or record.tokens_out is None:
            return None, None
        provider = tier.get("provider")
        model = source.resolve_model(provider, tier["model"]) if provider else None
        if model is not None:
            cost = source.cost(record, provider, model)
            if cost is None and record.service_tier is not None:
                cost = source.cost(replace(record, service_tier=None), provider, model)
            if cost is not None:
                return cost, "catalog"
        cost = (record.tokens_in / 1000) * tier["input_cost_per_1k"] + (
            record.tokens_out / 1000
        ) * tier["output_cost_per_1k"]
        return round(cost, 6), "policy_rates"

    return project


def replay_priced(
    priced: Sequence[PricedRecord],
    policy: Mapping[str, Any],
    source: PriceSource,
    *,
    scan_prompts: bool = False,
) -> list:
    """One ``teardown_replay.CallReplay`` per priced record, on its baseline."""
    project = catalog_projector(source)
    return [
        tr.replay_call(
            p.record, policy, scan_prompts=scan_prompts,
            baseline=(p.cost, p.null_reason), projector=project,
        )
        for p in priced
    ]


def host_facts(policy: Mapping[str, Any], provider: str, model: str) -> Mapping[str, Any]:
    """Sovereignty facts of one host of a model, for the data-policy check.

    The route policy's own tier for that provider and model first (what
    replay uses), else the SDK catalog's sovereignty fields (``None`` means
    unverified), else nothing. ``tier_complies`` fails closed on a missing
    or ``None`` fact, so an unverified host never passes a restrictive rule.
    """
    for tier in tr._policy_tiers(policy):
        if tier.get("provider") == provider and tr._same_model(tier.get("model"), model):
            return tier
    entry = model_catalog.get_model(provider, model)
    facts = model_catalog.sovereignty(entry) if entry is not None else {}
    return {**facts, "provider": provider, "model": model}


def arbitrage_check(
    policy: Mapping[str, Any], level_of: Callable[[Any], str]
) -> Callable[[Any, str, str], bool]:
    """``complies(priced_record, provider, model)`` for same-model arbitrage:
    the cheaper host must meet the data policy at the call's effective level."""

    def complies(p, provider: str, model: str) -> bool:
        level = level_of(p)
        rule = data_policy_rule(policy, level if level != tr.UNCLASSIFIED else None)
        return tier_complies(host_facts(policy, provider, model), rule)

    return complies


def replay_sections(calls: list, policy: Mapping[str, Any], *, policy_name: str,
                    scan_prompts: bool) -> dict:
    """Template §7 and §8 data from the replayed calls."""
    rep = tr.summarize(calls, policy, mode="full" if scan_prompts else "cost_only")
    sensitivity = policy.get("sensitivity") or {}
    return {
        "policy": {
            "file": _label(policy_name),
            "policy_version": _label(policy.get("policy_version")),
            "sha256": rep["policy"]["sha256"],
            "strategy": rep["policy"]["strategy"],
            "unclassified_default": sensitivity.get("unclassified_default"),
            "pii_escalation": dict(sorted((sensitivity.get("pii_escalation") or {}).items())),
        },
        "calls": rep["calls"],
        "calls_unknown_rows": rep["calls_unknown_rows"],
        "sensitivity_mix": rep["sensitivity_mix"],
        "jurisdictions": rep["jurisdictions"],
        "baseline_noncompliant": rep["baseline_noncompliant"],
        "policy_cheapest": rep["strategies"]["policy_cheapest"],
    }


# --- eval results (ps-eval --out-json) -----------------------------------------------


class EvalResultsError(ValueError):
    """The eval results file is not a ``ps-eval --out-json`` report."""


def _model_id(label: str) -> str:
    """``vendor:model`` → ``model`` (policy tiers name models by id)."""
    return label.split(":", 1)[1] if ":" in label else label


@dataclass
class _Cell:
    use_case: str
    candidate: str
    n: int
    passed: int
    parity: float | None
    ci_low: float | None
    ci_high: float | None
    status: str
    conservative: bool


#: Upper bound on ``bootstrap_resamples`` read from an eval results file.
MAX_BOOTSTRAP_RESAMPLES = 100_000


def _num(value: Any) -> float | None:
    """A finite number from an input file, else None (never a string)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _int(value: Any) -> int | None:
    """A whole number from an input file, else None (never a string)."""
    x = _num(value)
    return int(x) if x is not None and x == int(x) else None


def _unit(value: Any) -> float | None:
    """A 0–1 share from an input file, else None."""
    x = _num(value)
    return x if x is not None and 0.0 <= x <= 1.0 else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _methodology(meth: Mapping[str, Any]) -> dict:
    """The methodology record of an eval report, reduced to typed values.

    The file is untrusted input: every value is either a number, a bool, a
    control-character-free label, or dropped, so no free text reaches the
    teardown report beyond short labels (use cases, model ids, scorers).
    """
    rp = _mapping(_mapping(meth.get("policy")).get("route_policy"))
    sha = rp.get("sha256")
    strata = meth.get("strata") if isinstance(meth.get("strata"), list) else []
    scoring = _mapping(meth.get("scoring"))
    judges = meth.get("judges") if isinstance(meth.get("judges"), list) else []
    return {
        "sample_seed": _int(meth.get("sample_seed")),
        "per_stratum": _int(meth.get("per_stratum")),
        "strata": [
            {"use_case": _label(st.get("use_case")), "sensitivity": _label(st.get("sensitivity")),
             "complexity": _label(st.get("complexity")),
             "population": _int(st.get("population")), "sampled": _int(st.get("sampled"))}
            for st in strata if isinstance(st, Mapping)
        ],
        "scoring": {(_label(k) or "?"): _label(v) for k, v in scoring.items()},
        "judges": [_label(j) for j in judges if _label(j)],
        "policy": ({"route_policy": {"sha256": sha}}
                   if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha) else {}),
        "temperature_default": _num(meth.get("temperature_default")),
        "spend_usd": _num(meth.get("spend_usd")),
        "max_spend_usd": _num(meth.get("max_spend_usd")),
        "calls": _int(meth.get("calls")),
        "judge_calls": _int(meth.get("judge_calls")),
        "prompts_sent_to_providers": meth.get("prompts_sent_to_providers") is True,
    }


def read_eval_results(doc: Any) -> dict:
    """Validate and normalise a ``ps-eval`` JSON report (``EvalReport.to_dict``).

    The file is untrusted: malformed values raise :class:`EvalResultsError`
    (whose message never quotes the file) or are dropped, and only numbers
    and short labels are kept.
    """
    if not isinstance(doc, Mapping) or not isinstance(doc.get("cells"), list):
        raise EvalResultsError("expected a ps-eval JSON report with a 'cells' list")
    params = _mapping(doc.get("params"))
    cells = []
    for c in doc["cells"]:
        if not isinstance(c, Mapping):
            raise EvalResultsError("a cell is not an object")
        n, passed = _int(c.get("n")), _int(c.get("passed"))
        if n is None or passed is None or n < 0 or not 0 <= passed <= n:
            raise EvalResultsError("a cell has a missing or invalid n/passed")
        cells.append(_Cell(
            use_case=_label(c.get("use_case")) or "(unattributed)",
            candidate=_label(c.get("candidate")) or "?",
            n=n, passed=passed,
            parity=_unit(c.get("parity")), ci_low=_unit(c.get("ci_low")),
            ci_high=_unit(c.get("ci_high")),
            status=c.get("status") if c.get("status") in (
                "at_parity", "below_parity", "insufficient_sample") else "insufficient_sample",
            conservative=c.get("conservative_at_parity") is True,
        ))

    def param(key: str, default: Any, conv: Callable[[Any], Any], ok: Callable[[Any], bool]):
        if params.get(key) is None:
            return default
        value = conv(params[key])
        if value is None or not ok(value):
            raise EvalResultsError(f"params.{key} is not valid")
        return value

    ci = doc.get("saving_ci")
    ci = [_num(x) for x in ci] if isinstance(ci, list) and len(ci) == 2 else None
    return {
        "cells": cells,
        "min_parity": param("min_parity", GATE1_PARITY, _num, lambda x: 0 < x <= 1),
        "min_cell_samples": param("min_cell_samples", 30, _int, lambda x: x >= 0),
        "bootstrap_resamples": param("bootstrap_resamples", 1000, _int,
                                     lambda x: 0 <= x <= MAX_BOOTSTRAP_RESAMPLES),
        "bootstrap_seed": param("bootstrap_seed", 0, _int, lambda x: True),
        "methodology": _methodology(_mapping(doc.get("methodology"))),
        "notes": [_label(x) for x in (doc.get("notes") if isinstance(doc.get("notes"), list)
                                      else []) if _label(x)],
        "eval_saving_pct": _num(doc.get("saving_pct")),
        "eval_saving_ci": ci if ci and None not in ci else None,
        "eval_population_calls": _int(doc.get("population_calls")),
    }


def _eligible(cells: Sequence[_Cell], pick: Callable[[_Cell], bool]) -> dict[str, frozenset]:
    out: dict[str, set] = defaultdict(set)
    for c in cells:
        out[c.use_case]  # every evaluated use case gets an entry
        if pick(c):
            out[c.use_case].add(_model_id(c.candidate))
    return {uc: frozenset(m) for uc, m in out.items()}


class _ParityReplay:
    """Replays calls with each use case limited to its at-parity models.

    The logged model stays allowed (it is at parity with itself). A use case
    with no eval, or no model at parity, keeps its baseline cost; so does a
    call the router sends outside the allowed set (an explicit route group
    bypasses the use-case bar). Results are memoised per (use case, allowed
    set) so the bootstrap can reuse them.
    """

    def __init__(self, priced, calls, policy, source, scan_prompts):
        self.policy = policy
        self.project = catalog_projector(source)
        self.scan_prompts = scan_prompts
        self.by_uc: dict[str | None, list[tuple[PricedRecord, Any]]] = defaultdict(list)
        for p, c in zip(priced, calls, strict=True):
            self.by_uc[p.record.use_case].append((p, c))
        self._policies: dict[tuple, Mapping] = {}
        self._memo: dict[tuple, list[dict]] = {}

    def _policy_for(self, uc: str, allowed: frozenset) -> Mapping:
        key = (uc, allowed)
        if key not in self._policies:
            pol = copy.deepcopy(dict(self.policy))
            bars = pol.setdefault("use_cases", {})
            bars[uc] = {**(bars.get(uc) or {}), "eval_set": "ps-eval",
                        "eligible_models": sorted(allowed)}
            self._policies[key] = pol
        return self._policies[key]

    def calls(self, uc: str | None, eligible: frozenset | None) -> list[dict]:
        key = (uc, eligible)
        if key in self._memo:
            return self._memo[key]
        out = []
        for p, base in self.by_uc.get(uc, []):
            row = {"calls": base.calls, "baseline_cost": p.cost, "baseline_model": base.baseline_model,
                   "projected_cost": p.cost, "routed_model": base.baseline_model}
            if eligible is None:
                row["status"] = NO_EVAL
            elif not eligible:
                row["status"] = NONE_AT_PARITY
            else:
                allowed = eligible | {base.baseline_model} if base.baseline_model else eligible
                call = tr.replay_call(
                    p.record, self._policy_for(uc, frozenset(allowed)),
                    scan_prompts=self.scan_prompts, baseline=(p.cost, p.null_reason),
                    projector=self.project,
                )
                if call.routed_model is not None and call.routed_model not in allowed:
                    row["status"] = ROUTED_OFF_PARITY
                else:
                    row["status"] = AT_PARITY if call.routed_model else call.outcome
                    row["projected_cost"] = call.projected_cost
                    row["routed_model"] = call.routed_model or base.baseline_model
            out.append(row)
        self._memo[key] = out
        return out

    def totals(self, uc: str | None, eligible: frozenset | None) -> tuple[float, float]:
        base = proj = 0.0
        for row in self.calls(uc, eligible):
            if row["baseline_cost"] is not None and row["projected_cost"] is not None:
                base += row["baseline_cost"]
                proj += row["projected_cost"]
        return base, proj


def _strategy(replayer: _ParityReplay, eligible: Mapping[str, frozenset]) -> dict:
    status = Counter()
    base = proj = 0.0
    null = 0
    for uc in replayer.by_uc:
        for row in replayer.calls(uc, eligible.get(uc) if uc is not None else None):
            n = row["calls"] or 0
            status[row["status"]] += n
            if row["baseline_cost"] is None or row["projected_cost"] is None:
                null += n
                continue
            base += row["baseline_cost"]
            proj += row["projected_cost"]
    return {
        "baseline_spend_compared": _money(base),
        "projected_spend": _money(proj),
        "savings": _money(base - proj),
        "savings_pct": _share(base - proj, base),
        "calls_by_status": dict(sorted(status.items())),
        "calls_projected_null": null,
    }


def _use_case_rows(replayer: _ParityReplay, eligible: Mapping[str, frozenset]) -> list[dict]:
    rows = []
    for uc in sorted(replayer.by_uc, key=lambda u: (u is None, u or "")):
        data = replayer.calls(uc, eligible.get(uc) if uc is not None else None)
        base_by, proj_by = Counter(), Counter()
        base = proj = 0.0
        calls = unknown = 0
        for row in data:
            if row["calls"] is None:
                unknown += 1
            else:
                calls += row["calls"]
            if row["baseline_cost"] is None or row["projected_cost"] is None:
                continue
            base += row["baseline_cost"]
            proj += row["projected_cost"]
            base_by[row["baseline_model"] or "(unknown)"] += row["baseline_cost"]
            proj_by[row["routed_model"] or "(unknown)"] += row["projected_cost"]
        ev = eligible.get(uc) if uc is not None else None
        rows.append({
            "use_case": uc or "(unattributed)",
            "calls": calls,
            "calls_unknown_rows": unknown,
            "baseline": _money(base),
            "at_parity": _money(proj),
            "saving_pct": _share(base - proj, base),
            "eval": "no_eval" if ev is None else ("none_at_parity" if not ev else "evaluated"),
            "models_at_parity": sorted(ev) if ev else [],
            "from_model": base_by.most_common(1)[0][0] if base_by else None,
            "to_model": proj_by.most_common(1)[0][0] if proj_by else None,
        })
    return rows


def _bootstrap(replayer: _ParityReplay, cells: Sequence[_Cell], ev: dict) -> tuple | None:
    """Percentile interval of the at-parity saving over the parity sample.

    Each cell's trial outcomes are resampled (``passed`` of ``n``) and the
    at-parity set is chosen again; the logged traffic is the whole
    population, so it is not resampled.
    """
    resamples, seed = ev["bootstrap_resamples"], ev["bootstrap_seed"]
    if resamples <= 0 or not cells:
        return None
    rng = random.Random(seed)
    stats = []
    for _ in range(resamples):
        eligible: dict[str, set] = defaultdict(set)
        for c in cells:
            eligible[c.use_case]
            if c.n == 0 or c.n < ev["min_cell_samples"]:
                continue
            outcomes = [True] * c.passed + [False] * (c.n - c.passed)
            if sum(rng.choices(outcomes, k=c.n)) / c.n >= ev["min_parity"]:
                eligible[c.use_case].add(_model_id(c.candidate))
        base = proj = 0.0
        for uc in replayer.by_uc:
            sel = frozenset(eligible[uc]) if uc in eligible else None
            b, p = replayer.totals(uc, sel)
            base += b
            proj += p
        if base > 0:
            stats.append((base - proj) / base)
    if not stats:
        return None
    stats.sort()
    low = stats[int(0.025 * (len(stats) - 1))]
    high = stats[math.ceil(0.975 * (len(stats) - 1))]
    return round(low, 4), round(high, 4)


def _judge_rows(meth: Mapping[str, Any], policy: Mapping[str, Any] | None) -> list[str]:
    tiers = {}
    for group in ((policy or {}).get("groups") or {}).values():
        for t in group if isinstance(group, list) else []:
            if t.get("provider"):
                tiers[f"{t['provider']}:{t['model']}"] = t
    out = []
    for j in meth.get("judges") or []:
        text = _label(j) or "?"
        label = text.split(" (", 1)[0]
        tier = tiers.get(label)
        where = (f"jurisdiction {tier.get('jurisdiction', 'unknown')} per the route policy"
                 if tier else "not a tier of the route policy")
        out.append(f"{text}, {where}")
    return out


def parity_sections(
    priced: Sequence[PricedRecord],
    calls: list,
    policy: Mapping[str, Any],
    source: PriceSource,
    eval_results: Mapping[str, Any],
    *,
    scan_prompts: bool = False,
) -> dict:
    """Template §9 and §10 data, and the *policy at parity* strategy (M§6.2)."""
    ev = read_eval_results(eval_results)
    cells = ev["cells"]
    replayer = _ParityReplay(priced, calls, policy, source, scan_prompts)
    point = _eligible(cells, lambda c: c.status == AT_PARITY)
    cons = _eligible(cells, lambda c: c.conservative)
    strategy = _strategy(replayer, point)
    conservative = _strategy(replayer, cons)
    meth = ev["methodology"]
    pol = meth.get("policy") if isinstance(meth.get("policy"), Mapping) else {}
    rp = pol.get("route_policy") if isinstance(pol.get("route_policy"), Mapping) else None
    same_policy = bool(rp and rp.get("sha256") == tr.policy_sha256(policy))
    caveats = list(ev["notes"])
    if not same_policy:
        caveats.append(
            "The eval run was not constrained by this route policy (ps-eval "
            "--route-policy): its candidates and judge were checked against its own "
            "allowlist, without PII escalation (M§7.3)."
        )
    saving = strategy["savings_pct"]
    if ev["min_parity"] < GATE1_PARITY:
        gate1 = "not comparable"
    elif saving is None:
        gate1 = "not measured"
    else:
        gate1 = "met" if saving >= GATE1_SAVING else "not met"
    strata = [
        s for s in meth.get("strata") or [] if isinstance(s, Mapping)
    ]
    return {
        "quality_parity": {
            "cells": [
                {"use_case": c.use_case, "candidate": c.candidate, "n": c.n,
                 "passed": c.passed, "parity": c.parity, "ci_low": c.ci_low,
                 "ci_high": c.ci_high, "status": c.status,
                 "conservative_at_parity": c.conservative}
                for c in cells
            ],
            "scoring": {(_label(k) or "?"): _label(v) for k, v in
                        sorted((meth.get("scoring") or {}).items())},
            "judges": _judge_rows(meth, policy),
            "eval_policy_is_route_policy": same_policy,
            "min_parity": ev["min_parity"],
            "min_cell_samples": ev["min_cell_samples"],
            "sample": {
                "seed": meth.get("sample_seed"),
                "per_stratum": meth.get("per_stratum"),
                "strata": [
                    {k: (_label(s.get(k)) if k in ("use_case", "sensitivity", "complexity")
                         else s.get(k))
                     for k in ("use_case", "sensitivity", "complexity", "population",
                               "sampled")}
                    for s in strata
                ],
            },
            "reexecution": {
                "temperature_default": meth.get("temperature_default"),
                "spend_usd": meth.get("spend_usd"),
                "max_spend_usd": meth.get("max_spend_usd"),
                "calls": meth.get("calls"),
                "judge_calls": meth.get("judge_calls"),
                "prompts_sent_to_providers": meth.get("prompts_sent_to_providers"),
            },
            "caveats": caveats,
        },
        "savings_at_parity": {
            "policy_at_parity": strategy,
            "conservative": conservative,
            "saving_ci": _bootstrap(replayer, cells, ev),
            "bootstrap_resamples": ev["bootstrap_resamples"],
            "bootstrap_seed": ev["bootstrap_seed"],
            "use_cases": _use_case_rows(replayer, point),
            "gate1": gate1,
            "ps_eval_cross_check": {
                "saving_pct": ev["eval_saving_pct"],
                "saving_ci": ev["eval_saving_ci"],
                "population_calls": ev["eval_population_calls"],
            },
        },
    }
