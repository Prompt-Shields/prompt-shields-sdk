"""Learning-tuple export for a warm-started router (#42, PRD §7.13).

One JSON object per logged call: ``(prompt features, model, quality score,
cost)``. Features are computed on this machine (``records.prompt_features``,
i.e. :mod:`prompt_shields.prompt_features`)
from message text when the input has it, else from the reported token counts
only; ``features_source`` says which. The export holds no prompt text,
response text, request hash, session id or user id.

``quality_score`` is null with ``quality_source: "not_measured"`` until the
eval harness (#14) scores sampled calls; the replayed decision per call
arrives with policy replay (#13). Aggregated usage-export rows are not
calls and are not exported.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import IO, Any

from prompt_shields.prompt_features import FEATURE_SCHEMA_VERSION
from prompt_shields.route_policy import COMPLEX_TOKEN_THRESHOLD
from prompt_shields.teardown.engine import PricedRecord

TUPLE_SCHEMA = "ps-learning-tuple.v0"


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


def learning_tuples(priced: Iterable[PricedRecord]) -> Iterator[dict[str, Any]]:
    """Yield one tuple per non-aggregated call."""
    for p in priced:
        r = p.record
        if r.aggregated:
            continue
        features, features_source = _features(p)
        yield {
            "schema": TUPLE_SCHEMA,
            "timestamp": r.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ") if r.timestamp else None,
            "business_unit": r.business_unit,
            "use_case": r.use_case,
            "data_classification": r.data_classification,
            "features": features,
            "features_source": features_source,
            "provider": p.provider,
            "model": p.model,
            "quality_score": None,
            "quality_source": "not_measured",
            "cost_usd": p.cost,
            "cost_source": p.cost_source,
            "tokens_in": r.tokens_in,
            "tokens_out": r.tokens_out,
        }


def write_learning_tuples(priced: Iterable[PricedRecord], out: IO[str]) -> int:
    """Write tuples as JSONL to ``out``; returns how many were written."""
    n = 0
    for t in learning_tuples(priced):
        out.write(json.dumps(t, sort_keys=True) + "\n")
        n += 1
    return n
