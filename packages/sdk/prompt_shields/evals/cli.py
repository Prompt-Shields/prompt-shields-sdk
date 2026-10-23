"""``ps-eval``: sample logged calls, re-run them on candidate models, report parity.

Modes:

- ``dry-run`` (default): build the sample and print the plan (strata, calls
  per candidate, calls the policy blocks, worst-case spend). No model is
  called.
- ``mock``: run the whole pipeline with a fake executor that answers with
  each record's logged answer. No network. Parity numbers are meaningless;
  use it to check inputs and report wiring.
- ``replay``: answers come from a recordings file (see
  ``prompt_shields.evals.executors.RecordedExecutor``). No network.
- ``live``: calls the providers with the customer's keys (``OPENAI_API_KEY``,
  ``ANTHROPIC_API_KEY``; ``--base-url vendor=URL`` for the gateway). Needs
  ``--allow-prompt-reexecution`` and ``--max-spend-usd``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

from prompt_shields.evals.budget import worst_case_input_tokens
from prompt_shields.evals.executors import (
    Candidate,
    FakeExecutor,
    ProviderExecutor,
    RecordedExecutor,
)
from prompt_shields.evals.harness import EvalConfig, EvalPolicy, run_eval
from prompt_shields.evals.records import EvalRecord, load_records
from prompt_shields.evals.report import build_report
from prompt_shields.evals.sampling import Sample, stratified_sample
from prompt_shields.evals.scoring import scorer_from_name
from prompt_shields.pricing import estimate_cost


def _pairs(values: list[str], flag: str) -> dict[str, str]:
    out = {}
    for value in values:
        key, sep, rest = value.partition("=")
        if not sep or not key or not rest:
            raise SystemExit(f"{flag} expects KEY=VALUE, got {value!r}")
        out[key] = rest
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ps-eval",
        description="Re-run a stratified sample of logged LLM calls on candidate "
                    "models and report quality parity and savings at parity.",
    )
    p.add_argument("--records", required=True, help="JSONL of logged calls")
    p.add_argument("--candidate", action="append", required=True,
                   help="candidate model as vendor:model (repeatable)")
    p.add_argument("--mode", choices=("dry-run", "mock", "replay", "live"),
                   default="dry-run")
    p.add_argument("--recordings", help="JSONL recordings for --mode replay")
    p.add_argument("--per-stratum", type=int, default=30)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--use-case", action="append",
                   help="only sample these use cases (repeatable)")
    p.add_argument("--scorer", default="exact",
                   help="default scorer: exact, schema, similarity or judge")
    p.add_argument("--scorer-for", action="append", default=[],
                   metavar="USE_CASE=SCORER", help="scorer for one use case")
    p.add_argument("--schema", action="append", default=[],
                   metavar="USE_CASE=FILE", help="JSON Schema for the schema scorer")
    p.add_argument("--similarity-threshold", type=float)
    p.add_argument("--judge", help="judge model as vendor:model")
    policy = p.add_mutually_exclusive_group(required=True)
    policy.add_argument("--policy", help="eval policy JSON (allowed models per level)")
    policy.add_argument("--allow-all-models", action="store_true",
                        help="no per-sensitivity model restriction")
    p.add_argument("--allow-prompt-reexecution", action="store_true",
                   help="required for --mode live: send logged prompts to providers")
    p.add_argument("--max-spend-usd", type=float,
                   help="spend cap for re-execution and judging (required for live)")
    p.add_argument("--rpm", type=float, help="max requests per minute")
    p.add_argument("--base-url", action="append", default=[], metavar="VENDOR=URL")
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--rerun-baseline", action="store_true",
                   help="re-run the logged model when a record has no logged answer")
    p.add_argument("--min-parity", type=float, default=0.95)
    p.add_argument("--min-cell-samples", type=int, default=30)
    p.add_argument("--min-cell-calls", type=int, default=10)
    p.add_argument("--bootstrap-resamples", type=int, default=1000)
    p.add_argument("--bootstrap-seed", type=int, default=0)
    p.add_argument("--out-json", help="write the report as JSON")
    p.add_argument("--out-md", help="write report sections 9-10 as Markdown")
    return p


def _policy(args) -> EvalPolicy:
    if args.allow_all_models:
        return EvalPolicy.allow_all()
    return EvalPolicy.from_dict(json.loads(Path(args.policy).read_text()))


def _scorers(args, judge: Candidate | None) -> dict:
    schemas = {k: json.loads(Path(v).read_text())
               for k, v in _pairs(args.schema, "--schema").items()}

    def make(name: str, use_case: str):
        return scorer_from_name(name, schema=schemas.get(use_case), judge=judge,
                                threshold=args.similarity_threshold)

    scorers = {"*": make(args.scorer, "*")}
    for use_case, name in _pairs(args.scorer_for, "--scorer-for").items():
        scorers[use_case] = make(name, use_case)
    for use_case in schemas:
        scorers.setdefault(use_case, make("schema", use_case))
    return scorers


def plan(sample: Sample, config: EvalConfig) -> dict:
    """What a run would do, without calling anything."""
    per_candidate = {}
    for candidate in config.candidates:
        calls = blocked = 0
        worst = 0.0
        unpriced = 0
        for record in sample.records:
            level = record.sensitivity(config.policy.unclassified_default)
            if not config.policy.allows(candidate, level):
                blocked += 1
                continue
            calls += 1
            max_tokens = (min(record.max_tokens, config.max_tokens)
                          if record.max_tokens else config.max_tokens)
            tokens_in = worst_case_input_tokens(record.messages or [])
            cost = (None if tokens_in is None else
                    estimate_cost(candidate.vendor, candidate.model, tokens_in,
                                  max_tokens, config.pricing_table))
            if cost is None:
                unpriced += 1
            else:
                worst += cost
        per_candidate[candidate.label] = {
            "calls": calls, "blocked_by_policy": blocked,
            "unpriced_calls": unpriced, "worst_case_usd": round(worst, 4),
        }
    return {
        "sampled_records": len(sample.records),
        "seed": sample.seed,
        "per_stratum": sample.per_stratum,
        "strata": [
            {"use_case": k[0], "sensitivity": k[1], "complexity": k[2],
             "population": v.population, "eligible": v.eligible, "sampled": v.sampled}
            for k, v in sorted(sample.strata.items())
        ],
        "candidates": per_candidate,
        "worst_case_total_usd": round(
            sum(c["worst_case_usd"] for c in per_candidate.values()), 4),
        "max_spend_usd": config.max_spend_usd,
        "note": "worst case excludes judge calls and baseline re-runs",
    }


def _echo_reference(records: list[EvalRecord]):
    by_messages = {json.dumps(r.messages, sort_keys=True): r.reference or ""
                   for r in records if r.messages}

    def respond(candidate, messages):
        return by_messages.get(json.dumps(messages, sort_keys=True), "")

    return respond


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for flag, value, low in (("--max-spend-usd", args.max_spend_usd, 0.0),
                             ("--rpm", args.rpm, None)):
        if value is not None and not (math.isfinite(value)
                                      and (value >= low if low is not None else value > 0)):
            print(f"ps-eval: {flag} must be a finite number "
                  f"{'>= 0' if low is not None else '> 0'}", file=sys.stderr)
            return 2
    try:
        candidates = [Candidate.parse(c) for c in args.candidate]
        judge = Candidate.parse(args.judge) if args.judge else None
        policy = _policy(args)
        scorers = _scorers(args, judge)
        records = load_records(args.records)
    except (ValueError, TypeError, OSError, ImportError) as exc:
        print(f"ps-eval: {exc}", file=sys.stderr)
        return 2

    sample = stratified_sample(
        records, per_stratum=args.per_stratum, seed=args.seed,
        unclassified_default=policy.unclassified_default, use_cases=args.use_case,
    )
    config = EvalConfig(
        candidates=candidates, scorers=scorers, policy=policy,
        allow_prompt_reexecution=args.allow_prompt_reexecution,
        max_spend_usd=args.max_spend_usd, requests_per_minute=args.rpm,
        temperature=args.temperature, max_tokens=args.max_tokens,
        rerun_baseline=args.rerun_baseline,
    )

    if args.mode == "dry-run":
        print(json.dumps(plan(sample, config), indent=2))
        return 0
    if args.mode == "live":
        if not args.allow_prompt_reexecution:
            print("ps-eval: --mode live sends logged prompts to providers; pass "
                  "--allow-prompt-reexecution to confirm", file=sys.stderr)
            return 2
        if args.max_spend_usd is None:
            print("ps-eval: --mode live needs --max-spend-usd", file=sys.stderr)
            return 2
        vendors = {c.vendor for c in candidates} | ({judge.vendor} if judge else set())
        if args.rerun_baseline:
            vendors |= {r.vendor for r in sample.records if r.vendor}
        try:
            executor = ProviderExecutor.for_vendors(
                vendors, base_urls=_pairs(args.base_url, "--base-url"))
        except (ValueError, ImportError) as exc:
            print(f"ps-eval: {exc}", file=sys.stderr)
            return 2
    elif args.mode == "replay":
        if not args.recordings:
            print("ps-eval: --mode replay needs --recordings", file=sys.stderr)
            return 2
        executor = RecordedExecutor.from_jsonl(args.recordings)
    else:
        executor = FakeExecutor(_echo_reference(sample.records))

    run = run_eval(sample, executor, config)
    if args.mode == "mock":
        run.notes.insert(0, "MOCK RUN: candidates echoed the logged answers; parity "
                            "figures are not measurements.")
    report = build_report(
        records, run, min_parity=args.min_parity,
        min_cell_samples=args.min_cell_samples, min_cell_calls=args.min_cell_calls,
        bootstrap_resamples=args.bootstrap_resamples,
        bootstrap_seed=args.bootstrap_seed,
    )
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(report.to_dict(), indent=2))
    markdown = report.to_markdown()
    if args.out_md:
        Path(args.out_md).write_text(markdown)
    if not args.out_json and not args.out_md:
        print(markdown)
    statuses = Counter(t.status for t in run.trials)
    print(f"ps-eval: {len(run.trials)} trials {dict(statuses)}, "
          f"spend USD {run.spend_usd:.4f}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
