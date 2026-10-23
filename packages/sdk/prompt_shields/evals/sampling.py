"""Stratified sampling (M§7.2).

Strata are use case × sensitivity level × complexity band. Within each
stratum, up to ``per_stratum`` records with prompt text are drawn at random
with a recorded seed. The sample keeps, per stratum, the population it stands
for, so a report can say what each sampled call represents.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass

from prompt_shields.evals.records import EvalRecord, complexity_band

StratumKey = tuple[str, str, str]  # (use_case, sensitivity, complexity)


def stratum_of(record: EvalRecord, unclassified_default: str = "confidential") -> StratumKey:
    return (
        record.use_case,
        record.sensitivity(unclassified_default),
        complexity_band(record.input_tokens()),
    )


@dataclass(frozen=True)
class StratumInfo:
    population: int  # every record in the stratum
    eligible: int  # records with prompt text (can be re-executed)
    sampled: int


@dataclass(frozen=True)
class Sample:
    records: list[EvalRecord]
    strata: dict[StratumKey, StratumInfo]
    seed: int
    per_stratum: int
    unclassified_default: str

    def stratum(self, record: EvalRecord) -> StratumKey:
        return stratum_of(record, self.unclassified_default)


def stratified_sample(
    records: Sequence[EvalRecord],
    *,
    per_stratum: int,
    seed: int,
    unclassified_default: str = "confidential",
    use_cases: Sequence[str] | None = None,
) -> Sample:
    """Draw up to ``per_stratum`` records with prompt text from each stratum.

    The draw is deterministic for a given ``seed`` and set of records: strata
    and records are ordered by key and ``record_id`` before sampling, so the
    input order does not matter. ``use_cases`` limits sampling to those use
    cases (the population counts still cover every record).
    """
    if per_stratum < 1:
        raise ValueError("per_stratum must be at least 1")
    groups: dict[StratumKey, list[EvalRecord]] = {}
    for record in records:
        groups.setdefault(stratum_of(record, unclassified_default), []).append(record)

    rng = random.Random(seed)
    chosen: list[EvalRecord] = []
    strata: dict[StratumKey, StratumInfo] = {}
    for key in sorted(groups):
        members = groups[key]
        eligible = sorted(
            (r for r in members if r.has_prompt), key=lambda r: r.record_id
        )
        if use_cases is not None and key[0] not in use_cases:
            eligible_for_draw: list[EvalRecord] = []
        else:
            eligible_for_draw = eligible
        if len(eligible_for_draw) <= per_stratum:
            drawn = list(eligible_for_draw)
        else:
            drawn = rng.sample(eligible_for_draw, per_stratum)
        chosen.extend(drawn)
        strata[key] = StratumInfo(
            population=len(members), eligible=len(eligible), sampled=len(drawn)
        )
    return Sample(
        records=chosen,
        strata=strata,
        seed=seed,
        per_stratum=per_stratum,
        unclassified_default=unclassified_default,
    )
