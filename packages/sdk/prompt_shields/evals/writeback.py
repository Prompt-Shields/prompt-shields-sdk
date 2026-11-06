"""Write measured parity back into a route policy's per-use-case quality bar (#23).

``ps-eval`` measures, per use case, how often each candidate model matches the
logged answer (its parity cells). The routers (SDK ``resolve_policy_route`` and
the gateway) only let a use case with an ``eval_set`` be served by the models
in its ``eligible_models``. This module turns the first into the second, so a
model that has not passed parity is never picked for that use case.

A candidate passes a use case when its cell has at least ``min_cell_samples``
scored samples (from the report's params) and its parity reaches the use
case's own ``min_parity`` (from the policy's bar, default 0.95). With
``conservative`` the Wilson lower bound must reach it instead of the point
estimate. The logged model (the report's ``from_model``) stays eligible: it is
the reference the candidates were measured against.

``ps-eval-apply --report eval.json --route-policy policy.json --eval-set ID
--out policy.new.json`` writes the updated policy and prints what changed. It
refuses a mock run's report, whose parity figures are not measurements.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

#: The schema's default for ``use_cases.*.min_parity``.
DEFAULT_MIN_PARITY = 0.95
#: ``ps-eval``'s default ``--min-cell-samples``.
DEFAULT_MIN_CELL_SAMPLES = 30
MOCK_NOTE = "MOCK RUN"


def _model_id(label: str) -> str:
    """``vendor:model`` → ``model`` (policy tiers name models by id)."""
    return label.split(":", 1)[1] if ":" in label else label


def policy_sha256(policy: Mapping[str, Any]) -> str:
    """The hash ``ps-eval --route-policy`` records for the policy it ran under."""
    canonical = json.dumps(policy, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _cell_passes(cell: Mapping[str, Any], min_parity: float, min_samples: int,
                 conservative: bool) -> tuple[bool, str]:
    n, passed = cell.get("n") or 0, cell.get("passed") or 0
    if n < min_samples:
        return False, f"n={n} < min_cell_samples={min_samples}"
    score = cell.get("ci_low") if conservative else passed / n
    if score is None:
        return False, "no confidence interval"
    if score < min_parity:
        what = "ci_low" if conservative else "parity"
        return False, f"{what}={round(score, 4)} < min_parity={min_parity}"
    return True, "passed"


def apply_parity(
    policy: Mapping[str, Any],
    report: Mapping[str, Any],
    *,
    eval_set: str,
    use_cases: Iterable[str] | None = None,
    conservative: bool = False,
    keep_models: Iterable[str] = (),
) -> tuple[dict, dict]:
    """Return ``(new_policy, summary)`` with each evaluated use case's bar set.

    Every use case in the report (or only those in ``use_cases``) gets
    ``eval_set`` and ``eligible_models``: the candidates that passed at that
    use case's ``min_parity``, the logged ``from_model`` and ``keep_models``.
    ``min_group`` and ``min_parity`` already on the bar are kept; a bar without
    ``min_parity`` gets the default written out so the threshold is explicit.
    Use cases the report does not cover are left unchanged. ``policy`` is not
    modified.

    Raises ``ValueError`` for a mock run's report or an empty ``eval_set``.
    """
    if not eval_set:
        raise ValueError("eval_set must be a non-empty eval set id")
    notes = [str(n) for n in report.get("notes") or []]
    if any(n.startswith(MOCK_NOTE) for n in notes):
        raise ValueError("the report is from a mock run: its parity figures are "
                         "not measurements and cannot make a model eligible")
    params = report.get("params") or {}
    min_samples = int(params.get("min_cell_samples") or DEFAULT_MIN_CELL_SAMPLES)
    wanted = set(use_cases) if use_cases is not None else None

    cells_by_uc: dict[str, list[Mapping[str, Any]]] = {}
    for cell in report.get("cells") or []:
        uc = cell.get("use_case")
        if uc and (wanted is None or uc in wanted):
            cells_by_uc.setdefault(uc, []).append(cell)
    reference = {u.get("use_case"): u.get("from_model")
                 for u in report.get("use_cases") or [] if u.get("from_model")}

    out = copy.deepcopy(dict(policy))
    bars = out.setdefault("use_cases", {})
    known_models = {t.get("model") for tiers in (policy.get("groups") or {}).values()
                    for t in tiers or ()}
    summary: dict[str, Any] = {"eval_set": eval_set, "conservative": conservative,
                               "min_cell_samples": min_samples, "use_cases": [],
                               "warnings": []}
    ran_under = (((report.get("methodology") or {}).get("policy") or {})
                 .get("route_policy") or {}).get("sha256")
    if ran_under is None:
        summary["warnings"].append(
            "the eval did not record a route policy (ps-eval --route-policy): its "
            "candidates were not checked against this policy's data rules")
    elif ran_under != policy_sha256(policy):
        summary["warnings"].append(
            "the eval ran under a different version of this route policy")
    for uc in sorted(wanted - set(cells_by_uc)) if wanted is not None else ():
        summary["warnings"].append(f"use case {uc!r} has no cells in the report")

    for uc, cells in sorted(cells_by_uc.items()):
        bar = dict(bars.get(uc) or {})
        min_parity = float(bar.get("min_parity", DEFAULT_MIN_PARITY))
        eligible = {_model_id(m) for m in keep_models}
        if reference.get(uc):
            eligible.add(_model_id(reference[uc]))
        passed, excluded = [], []
        for cell in sorted(cells, key=lambda c: str(c.get("candidate"))):
            model = _model_id(str(cell.get("candidate")))
            ok, why = _cell_passes(cell, min_parity, min_samples, conservative)
            if ok:
                eligible.add(model)
                passed.append(model)
            else:
                excluded.append({"model": model, "parity": cell.get("parity"),
                                 "ci_low": cell.get("ci_low"), "n": cell.get("n"),
                                 "why": why})
        bar.update({"eval_set": eval_set, "min_parity": min_parity,
                    "eligible_models": sorted(eligible)})
        bars[uc] = bar
        unknown = sorted(m for m in eligible if m not in known_models)
        if unknown:
            summary["warnings"].append(
                f"use case {uc!r}: eligible models not in any policy group: "
                + ", ".join(unknown))
        summary["use_cases"].append({
            "use_case": uc, "min_parity": min_parity,
            "eligible_models": bar["eligible_models"], "passed": passed,
            "excluded": excluded,
        })
    return out, summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ps-eval-apply",
        description="Write ps-eval parity results into a route policy's per-use-case "
                    "quality bar (use_cases.*.eligible_models).",
    )
    p.add_argument("--report", required=True, help="ps-eval --out-json results")
    p.add_argument("--route-policy", required=True, help="route-policy.v1 JSON")
    p.add_argument("--eval-set", required=True, help="eval set id to record")
    p.add_argument("--out", help="write the updated policy here (default: stdout)")
    p.add_argument("--use-case", action="append",
                   help="only update this use case (repeatable)")
    p.add_argument("--conservative", action="store_true",
                   help="require the Wilson lower bound to reach min_parity")
    p.add_argument("--keep-model", action="append", default=[],
                   help="model id that stays eligible regardless (repeatable)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = json.loads(Path(args.report).read_text())
        policy = json.loads(Path(args.route_policy).read_text())
        new_policy, summary = apply_parity(
            policy, report, eval_set=args.eval_set, use_cases=args.use_case,
            conservative=args.conservative, keep_models=args.keep_model)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        print(f"ps-eval-apply: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(new_policy, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text)
    else:
        sys.stdout.write(text)
    for w in summary["warnings"]:
        print(f"ps-eval-apply: warning: {w}", file=sys.stderr)
    for row in summary["use_cases"]:
        print(f"ps-eval-apply: {row['use_case']}: eligible "
              f"{', '.join(row['eligible_models']) or '(none)'}; excluded "
              f"{', '.join(e['model'] for e in row['excluded']) or '(none)'}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
