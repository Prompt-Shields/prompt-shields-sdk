"""Learning-tuple export for a warm-started router (#42, PRD §7.13).

One JSON object per logged call: ``(prompt features, model, quality score,
cost)``. Features are computed on this machine (``records.prompt_features``,
i.e. :mod:`prompt_shields.prompt_features`)
from message text when the input has it, else from the reported token counts
only; ``features_source`` says which. The export holds no prompt text,
response text, request hash, session id or user id.

``quality_score`` is the parity a prior ``ps-eval`` run (#14) measured for
the call's (use case, model) cell, read from ``--eval-results``
(``quality_source: "eval_parity"``, with the cell size in
``quality_samples``). It is null with ``quality_source: "not_measured"``
when no eval results were given or that cell was not evaluated; it is
never estimated.

With a route policy (``--policy``), each tuple also carries the call's
effective ``sensitivity_level`` and a ``replay`` object: the model the
policy replay (#13) routes the call to, its projected cost and, from the
eval results, that model's parity for the use case. ``replay`` is a
counterfactual arm for the same call, not a second logged call, so it adds
no traffic. Aggregated usage-export rows are not calls and are not
exported.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import IO, Any

import prompt_shields.teardown_replay as tr
from prompt_shields.prompt_features import FEATURE_SCHEMA_VERSION
from prompt_shields.route_policy import COMPLEX_TOKEN_THRESHOLD
from prompt_shields.teardown.engine import PricedRecord

TUPLE_SCHEMA = "ps-learning-tuple.v0"

#: Use-case label ps-eval gives a record without one.
UNATTRIBUTED = "(unattributed)"

NOT_MEASURED = "not_measured"
EVAL_PARITY = "eval_parity"


def _features(p: PricedRecord) -> tuple[dict[str, Any], str]:
    r = p.record
    if r.features is not None:
        return dict(r.features), "messages"
    tokens_in = r.tokens_in
    return {
        "feature_schema": FEATURE_SCHEMA_VERSION,
        "approx_input_tokens": tokens_in,
        "message_count": None,
        "has_code": None,
        "has_json_schema": None,
        "language": None,
        "use_case": r.use_case,
        "complexity": (
            None if tokens_in is None
            else "complex" if tokens_in > COMPLEX_TOKEN_THRESHOLD else "simple"
        ),
    }, "token_counts"


class _Quality:
    """Parity per (use case, model) from ``read_eval_results`` output."""

    def __init__(self, eval_results: Mapping[str, Any] | None):
        self.cells: dict[str, list[tuple[str | None, str, float, int]]] = {}
        for c in (eval_results or {}).get("cells", ()):
            if c.parity is None or c.n <= 0:
                continue
            # ps-eval labels are ``vendor:model`` split at the first colon
            # (``Candidate.parse``): Bedrock ids such as ``...-v1:0`` hold one.
            vendor, sep, model = c.candidate.partition(":")
            if not sep:
                vendor, model = "", vendor
            self.cells.setdefault(c.use_case, []).append(
                (vendor or None, model, c.parity, c.n))

    def score(self, use_case: str | None, provider: str | None,
              *models: str | None) -> dict[str, Any]:
        for vendor, model, parity, n in self.cells.get(use_case or UNATTRIBUTED, ()):
            if vendor and provider and vendor != provider:
                continue
            if any(tr._same_model(model, m) for m in models):
                return {"quality_score": parity, "quality_source": EVAL_PARITY,
                        "quality_samples": n}
        return {"quality_score": None, "quality_source": NOT_MEASURED,
                "quality_samples": None}


def _replay(p: PricedRecord, c: Any, policy: Mapping[str, Any],
            quality: _Quality) -> dict[str, Any]:
    if c.outcome == tr.OUTCOME_UNCHANGED:
        provider = p.provider
    else:
        tier = tr._find_tier(policy, c.routed_model)
        provider = (tier or {}).get("provider")
    return {
        "outcome": c.outcome,
        "group": c.routed_group,
        "provider": provider,
        "model": c.routed_model,
        "cost_usd": c.projected_cost,
        "cost_source": c.projected_pricing,
        **quality.score(p.record.use_case, provider, c.routed_model),
    }


def learning_tuples(
    priced: Iterable[PricedRecord],
    *,
    eval_results: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
    replays: Sequence[Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield one tuple per non-aggregated call.

    ``eval_results`` is :func:`prompt_shields.teardown.policy.read_eval_results`
    output. ``replays`` are the ``teardown_replay.CallReplay`` results of
    ``priced`` under ``policy``, in the same order
    (:func:`prompt_shields.teardown.policy.replay_priced`).
    """
    priced = list(priced)
    if replays is not None and (policy is None or len(replays) != len(priced)):
        raise ValueError("replays need the policy and one result per priced record")
    quality = _Quality(eval_results)
    for i, p in enumerate(priced):
        r = p.record
        if r.aggregated:
            continue
        features, features_source = _features(p)
        t = {
            "schema": TUPLE_SCHEMA,
            "timestamp": r.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ") if r.timestamp else None,
            "business_unit": r.business_unit,
            "use_case": r.use_case,
            "data_classification": r.data_classification,
            "features": features,
            "features_source": features_source,
            "provider": p.provider,
            "model": p.model,
            **quality.score(r.use_case, p.provider, p.model, r.baseline_model()),
            "cost_usd": p.cost,
            "cost_source": p.cost_source,
            "tokens_in": r.tokens_in,
            "tokens_out": r.tokens_out,
        }
        if replays is not None:
            c = replays[i]
            t["sensitivity_level"] = c.sensitivity_level
            t["replay"] = _replay(p, c, policy, quality)  # type: ignore[arg-type]
        yield t


def write_learning_tuples(
    priced: Iterable[PricedRecord],
    out: IO[str],
    *,
    eval_results: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
    replays: Sequence[Any] | None = None,
) -> int:
    """Write tuples as JSONL to ``out``; returns how many were written."""
    n = 0
    for t in learning_tuples(priced, eval_results=eval_results, policy=policy,
                             replays=replays):
        out.write(json.dumps(t, sort_keys=True) + "\n")
        n += 1
    return n
