"""Contextual-bandit routing learner, ``recommend`` mode (#47, #48; PRD §7.13).

An **offline** learner: it reads logged calls (the teardown's learning
tuples, or telemetry rows in the same shape), learns which policy-compliant
model gives each (use case, sensitivity) the best reward, and *recommends*
route changes with a projection of their savings and quality. It never
routes a call and never changes a policy: a person reviews each
recommendation (collector Partner API, ``/partner/v1/learner/...``) and, if
they accept it, edits the route policy themselves.

Model
-----
One linear-Gaussian bandit per (use case, sensitivity) group. The arms are
the models the route policy allows for that group (:func:`policy_candidates`:
the sensitivity level's ``data_policy`` rule, the use case's
``eligible_models`` and ``min_group``). The learner only re-ranks that safe
set; it never widens it. A logged model outside the set still counts as
the group's *current* route, so a non-compliant current route is reported.

The context is a small numeric vector from the local prompt features
(:data:`CONTEXT_FEATURES`; no prompt text, no embedding). Per arm it keeps
ridge-regression sufficient statistics (``A = ridge·I + Σ w·x·xᵀ`` and
``b = Σ w·y·x`` for y in quality, cost and latency), so a snapshot can
warm-start a later run (``prior=``), for example from the teardown's
tuples (PRD: offline/online symmetry).

Reward = quality − λ·cost_usd − μ·latency_s (:class:`LearnerConfig`, with
per-use-case overrides). The recommended arm is the one that Thompson
sampling picks most often for the group's logged traffic: each draw samples
every arm's parameters from its posterior and picks the arm with the best
mean reward over the group's contexts. ``prob_best`` is that arm's share of
draws. The draws use a seeded RNG keyed by the group, so a run is
reproducible.

Off-policy evaluation
---------------------
The projection replays the group's logged contexts under "always route to
arm a": quality and latency from each arm's regression (the direct method,
``dm``), corrected with inverse propensity weights when every logged row
carries a ``propensity`` (doubly robust, ``dr``). Cost is the counterfactual
price of the logged token counts at that arm (policy tier prices, else the
SDK price table) when every row can be priced (``priced``), else the cost
regression. Recommendations need enough scored rows on both arms, a
confident ``prob_best``, quality no worse than the current route's by more
than ``quality_tolerance`` and a better reward.

Quality per row, strongest first: ``shadow_eval_score``, ``feedback_score``,
``quality_score`` (eval harness), then implicit outcome signals (a strong
failure class scores 0, a weak one 0.5, an empty list 1; weighted below
explicit scores, as the PRD asks). A row with none of these is unscored: it
counts as traffic but does not train the arms.

Teardown tuple fields (#42)
---------------------------
An eval ``quality_score`` is the parity of a whole (use case, model) eval
cell, copied onto every call in it, so its weight scales with the cell size
``quality_samples``: ``min(1, n / full_weight_quality_samples)``
(:class:`TupleInputs`). A cell below ``min_quality_samples`` (ps-eval's
``insufficient_sample``; default 30, as in ps-eval) is **ignored**, not
down-weighted: its parity is too noisy to move a route, and repeating it on
every call in the cell would let a handful of eval items look like
thousands of observations. The row falls back to its implicit outcome
signals, if any, else it is unscored (traffic only). Tuples without
``quality_samples`` keep weight 1, as before.

A tuple's ``replay`` object (the route policy's replayed decision for the
same call) is a **counterfactual** row: the replayed model, the logged
context and latency, the replayed projected cost and that model's eval
parity as the quality, weighted ``replay_weight`` times the sample weight
above. It trains the replayed arm but is not traffic: it adds nothing to
``calls``, the current route, the context mean or the doubly robust
(propensity) term, and it counts in the arm's ``replay_n``, not ``n``; the
``min_arm_samples`` gate counts both. A replay row is used only when the
replayed ``provider:model`` is one of the group's policy candidates,
differs from the logged model, has a measured parity from a sufficient
cell and a projected cost (an unpriced replay would train a cost of 0).
A recommendation whose recommended model has no logged rows is marked
``counterfactual_only``. Without these fields nothing in the snapshot changes, so an existing
snapshot's version is unchanged; with them the snapshot carries an
``inputs`` block with these settings and the rows used and skipped.

Snapshots
---------
:func:`train` returns a snapshot dict (:data:`SNAPSHOT_SCHEMA`) holding the
config, the per-arm statistics, per-group outcome and the recommendations.
``learner_version`` is ``lv1-`` plus the first 16 hex characters of the
SHA-256 of the canonical JSON of everything else (:func:`snapshot_digest`),
so the same input and config always give the same version, and the
collector can check a snapshot was not altered. There is no timestamp in
the hashed content. :func:`upload_snapshot` sends it to
``POST /ingest/learner-snapshots``.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import zlib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any

import httpx

from prompt_shields import pricing
from prompt_shields.prompt_features import FEATURE_SCHEMA_VERSION
from prompt_shields.route_policy import (
    classify_sensitivity,
    data_policy_rule,
    tier_complies,
)

SNAPSHOT_SCHEMA = "ps-learner-snapshot.v1"
ALGORITHM = "linear-thompson.v1"
MODE = "recommend"
VERSION_PREFIX = "lv1-"
INGEST_PATH = "/ingest/learner-snapshots"
#: Group label when the policy has no sensitivity levels and none was declared.
UNCLASSIFIED = "unclassified"

#: Context vector, in order. Bump ALGORITHM if this changes.
CONTEXT_FEATURES: tuple[str, ...] = (
    "bias",
    "log_input_tokens",
    "message_count",
    "has_code",
    "has_json_schema",
    "complex",
    "language_en",
)

#: Outcome signal classes (prompt_shields.outcome_signals) by strength.
STRONG_FAILURES = frozenset({
    "fallback", "schema_invalid", "tool_call_invalid", "refusal",
    "max_tokens", "stream_error",
})
WEAK_FAILURES = frozenset({"regenerate", "retry", "abandoned"})

# Row weight by quality source: implicit signals count less than explicit
# scores (PRD §7.13, poisoning resistance).
SOURCE_WEIGHTS: dict[str, float] = {
    "shadow_eval": 1.0,
    "feedback": 1.0,
    "eval": 1.0,
    "implicit": 0.25,
}

_ROUND = 10


@dataclass(frozen=True)
class RewardWeights:
    """λ (per USD per call) and μ (per second of latency)."""

    lambda_cost: float = 20.0
    mu_latency: float = 0.02


@dataclass(frozen=True)
class TupleInputs:
    """How a tuple's ``quality_samples`` and ``replay`` fields train the arms."""

    #: Eval cells smaller than this are ``insufficient_sample``: ignored.
    min_quality_samples: int = 30
    #: Cell size at which an eval score gets its full weight.
    full_weight_quality_samples: int = 100
    #: Weight of a counterfactual replay row relative to a logged eval row.
    replay_weight: float = 0.5


@dataclass(frozen=True)
class LearnerConfig:
    reward: RewardWeights = field(default_factory=RewardWeights)
    #: Use case -> its own weights (e.g. a ``critical`` one with λ near 0).
    use_case_rewards: Mapping[str, RewardWeights] = field(default_factory=dict)
    ridge: float = 1.0
    thompson_draws: int = 400
    #: Posterior spread multiplier (1 = the fitted residual noise).
    exploration: float = 1.0
    seed: int = 0
    #: Scored rows needed on the current and the recommended arm.
    min_arm_samples: int = 30
    min_prob_best: float = 0.8
    #: Largest projected quality drop accepted with a recommendation.
    quality_tolerance: float = 0.02
    #: Teardown tuple fields; reported in the snapshot's ``inputs`` block
    #: (only when the tuples carry them), not in ``config``.
    inputs: TupleInputs = field(default_factory=TupleInputs)

    def weights_for(self, use_case: str | None) -> RewardWeights:
        return self.use_case_rewards.get(use_case or "", self.reward)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("inputs")
        d["use_case_rewards"] = {
            k: asdict(v) for k, v in sorted(self.use_case_rewards.items())
        }
        return d


# ─── policy-safe candidates ───────────────────────────────────────────


def _label(provider: Any, model: Any) -> str | None:
    if not model:
        return None
    return f"{provider}:{model}" if provider else str(model)


def _tiers(policy: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    out = []
    for group, tiers in (policy.get("groups") or {}).items():
        if isinstance(tiers, list):
            out.extend(
                (group, t) for t in tiers
                if isinstance(t, Mapping) and t.get("provider") and t.get("model")
            )
    return out


def policy_candidates(
    policy: Mapping[str, Any], use_case: str | None, sensitivity: str
) -> list[str]:
    """``provider:model`` labels the route policy allows for this group.

    A tier is a candidate when it meets the sensitivity level's
    ``data_policy`` rule (the router's own check) and, when the use case has
    a quality bar, is in its ``eligible_models`` (with an ``eval_set``) and in
    a group at or above its ``min_group`` (by ``group_order``). Tiers without
    a ``provider`` are left out, so a bare model id never admits a vendor.
    """
    # The group label is looked up as is: a policy can name a level
    # "unclassified" with its own rule, and the router applies that rule. With
    # no such rule the lookup gives None, as the router's level None does.
    rule = data_policy_rule(policy, sensitivity)
    bar = (policy.get("use_cases") or {}).get(use_case) if use_case else None
    eligible = (
        set(bar.get("eligible_models") or ())
        if bar and bar.get("eval_set") else None
    )
    order = list(policy.get("group_order") or [])
    min_rank = (
        order.index(bar["min_group"])
        if bar and bar.get("min_group") in order else None
    )
    out = set()
    for group, tier in _tiers(policy):
        if eligible is not None and tier["model"] not in eligible:
            continue
        if min_rank is not None and (group not in order or order.index(group) < min_rank):
            continue
        if tier_complies(tier, rule):
            out.add(f"{tier['provider']}:{tier['model']}")
    return sorted(out)


def _tier_prices(policy: Mapping[str, Any]) -> dict[str, tuple[float, float]]:
    prices = {}
    for _, t in _tiers(policy):
        i, o = t.get("input_cost_per_1k"), t.get("output_cost_per_1k")
        if isinstance(i, (int, float)) and isinstance(o, (int, float)):
            prices.setdefault(f"{t['provider']}:{t['model']}", (float(i), float(o)))
    return prices


# ─── rows ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Row:
    use_case: str | None
    sensitivity: str
    arm: str
    x: tuple[float, ...]
    quality: float | None
    weight: float
    source: str | None
    cost: float | None
    latency_s: float | None
    tokens_in: int | None
    tokens_out: int | None
    propensity: float | None


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _unit(v: Any) -> float | None:
    f = _num(v)
    return f if f is not None and 0.0 <= f <= 1.0 else None


def context_vector(features: Mapping[str, Any] | None) -> tuple[float, ...]:
    """The learner's context for one call's prompt features (no text)."""
    f = features or {}
    tokens = _num(f.get("approx_input_tokens")) or 0.0
    msgs = _num(f.get("message_count")) or 0.0
    return (
        1.0,
        round(math.log1p(max(tokens, 0.0)) / 10.0, 6),
        min(max(msgs, 0.0), 20.0) / 20.0,
        1.0 if f.get("has_code") is True else 0.0,
        1.0 if f.get("has_json_schema") is True else 0.0,
        1.0 if f.get("complexity") == "complex" else 0.0,
        1.0 if f.get("language") == "en" else 0.0,
    )


def row_quality(t: Mapping[str, Any]) -> tuple[float | None, str | None]:
    """(quality in [0, 1], source) of one tuple, or (None, None)."""
    for key, source in (
        ("shadow_eval_score", "shadow_eval"),
        ("feedback_score", "feedback"),
        ("quality_score", "eval"),
    ):
        q = _unit(t.get(key))
        if q is not None:
            return q, source
    signals = t.get("outcome_signals")
    if isinstance(signals, list):
        names = {s for s in signals if isinstance(s, str)}
        if names & STRONG_FAILURES:
            return 0.0, "implicit"
        if names & WEAK_FAILURES:
            return 0.5, "implicit"
        return 1.0, "implicit"
    return None, None


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _sample_weight(n: Any, inputs: TupleInputs) -> float | None:
    """Weight factor of an eval score from a cell of ``n`` samples, or None
    when the cell is too small (or ``n`` is not a count)."""
    n = _int(n)
    if n is None or n < inputs.min_quality_samples:
        return None
    full = inputs.full_weight_quality_samples
    return min(1.0, n / full) if full > 0 else 1.0


def _eval_insufficient(t: Mapping[str, Any], inputs: TupleInputs) -> bool:
    """True when the tuple's quality is an eval score from a too-small cell."""
    return (
        row_quality(t)[1] == "eval" and t.get("quality_samples") is not None
        and _sample_weight(t["quality_samples"], inputs) is None
    )


def _parse(
    t: Mapping[str, Any], policy: Mapping[str, Any], inputs: TupleInputs | None = None,
) -> _Row | None:
    arm = _label(t.get("provider"), t.get("model"))
    if arm is None:
        return None
    declared = (
        t.get("sensitivity_level") or t.get("sensitivity") or t.get("data_classification")
    )
    pii = t.get("pii_detected")
    # The router's own rule: an unknown label counts as unclassified, PII
    # only escalates.
    level, _ = classify_sensitivity(
        policy,
        declared if isinstance(declared, str) else None,
        [c for c in pii if isinstance(c, str)] if isinstance(pii, list) else (),
    )
    level = level or UNCLASSIFIED
    features = t.get("features") if isinstance(t.get("features"), Mapping) else {}
    use_case = t.get("use_case") or features.get("use_case")
    quality, source = row_quality(t)
    weight = SOURCE_WEIGHTS[source] if source else 0.0
    if source == "eval" and t.get("quality_samples") is not None:
        factor = _sample_weight(t["quality_samples"], inputs or TupleInputs())
        if factor is None:
            # insufficient_sample: ignore the eval score (see module doc).
            quality, source = row_quality({k: v for k, v in t.items() if k != "quality_score"})
            weight = SOURCE_WEIGHTS[source] if source else 0.0
        else:
            weight *= factor
    latency = _num(t.get("latency_ms"))
    prop = _num(t.get("propensity"))
    return _Row(
        use_case=str(use_case) if use_case is not None else None,
        sensitivity=level,
        arm=arm,
        x=context_vector(features),
        quality=quality,
        weight=weight,
        source=source,
        cost=_num(t.get("cost_usd", t.get("cost"))),
        latency_s=latency / 1000.0 if latency is not None and latency >= 0 else None,
        tokens_in=_int(t.get("tokens_in")),
        tokens_out=_int(t.get("tokens_out")),
        propensity=prop if prop is not None and 0.0 < prop <= 1.0 else None,
    )


def _replay_row(
    t: Mapping[str, Any], row: _Row, inputs: TupleInputs,
) -> tuple[_Row | None, str | None]:
    """(counterfactual row, None) for a tuple's ``replay``, or (None, why not).

    (None, None) when the tuple has no replay. The policy-candidate check is
    per group, in :func:`train`.
    """
    rp = t.get("replay")
    if not isinstance(rp, Mapping):
        return None, None
    if not inputs.replay_weight > 0:
        return None, "disabled"
    # A bare model id never names a vendor, so it can never be a candidate.
    arm = _label(rp.get("provider"), rp.get("model")) if rp.get("provider") else None
    if arm is None:
        return None, "no_model"
    if arm == row.arm or rp.get("outcome") == "unchanged":
        return None, "same_as_logged"
    quality = _unit(rp.get("quality_score"))
    if quality is None:
        return None, "not_measured"
    factor = _sample_weight(rp.get("quality_samples"), inputs)
    if factor is None:
        return None, "insufficient_sample"
    cost = _num(rp.get("cost_usd"))
    if cost is None or cost < 0:
        # An unpriced replay would train the arm's cost regression at 0 and
        # project the whole current cost as savings.
        return None, "no_cost"
    return replace(
        row, arm=arm, quality=quality, source="replay",
        weight=SOURCE_WEIGHTS["eval"] * inputs.replay_weight * factor,
        cost=cost, propensity=None,
    ), None


def _row_key(r: _Row) -> str:
    return json.dumps(asdict(r), sort_keys=True, default=str)


# ─── small linear algebra (d is tiny) ─────────────────────────────────


def _cholesky(a: list[list[float]]) -> list[list[float]]:
    n = len(a)
    low = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = a[i][j] - sum(low[i][k] * low[j][k] for k in range(j))
            if i == j:
                low[i][j] = math.sqrt(max(s, 1e-12))
            else:
                low[i][j] = s / low[j][j]
    return low


def _solve(low: list[list[float]], b: Sequence[float]) -> list[float]:
    """Solve (L Lᵀ) x = b."""
    n = len(b)
    y = [0.0] * n
    for i in range(n):
        y[i] = (b[i] - sum(low[i][k] * y[k] for k in range(i))) / low[i][i]
    x = [0.0] * n
    for i in reversed(range(n)):
        x[i] = (y[i] - sum(low[k][i] * x[k] for k in range(i + 1, n))) / low[i][i]
    return x


def _back(low: list[list[float]], z: Sequence[float]) -> list[float]:
    """Solve Lᵀ y = z: y ~ N(0, A⁻¹) when z ~ N(0, I)."""
    n = len(z)
    y = [0.0] * n
    for i in reversed(range(n)):
        y[i] = (z[i] - sum(low[k][i] * y[k] for k in range(i + 1, n))) / low[i][i]
    return y


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


# ─── arm statistics ───────────────────────────────────────────────────


@dataclass
class ArmStats:
    """Ridge sufficient statistics of one arm (targets: quality, cost, latency)."""

    d: int
    ridge: float
    n: int = 0
    weight: float = 0.0
    A: list[list[float]] = field(default_factory=list)
    b: dict[str, list[float]] = field(default_factory=dict)
    #: Σ w·yᵢ·yⱼ over the targets, for the residual noise.
    S: dict[str, float] = field(default_factory=dict)
    #: Counterfactual (replay) rows added; ``n`` counts logged rows only.
    replay_n: int = 0

    def __post_init__(self):
        if not self.A:
            self.A = [[self.ridge if i == j else 0.0 for j in range(self.d)]
                      for i in range(self.d)]
        for k in ("quality", "cost", "latency"):
            self.b.setdefault(k, [0.0] * self.d)
        for k in ("qq", "ll", "ql"):
            self.S.setdefault(k, 0.0)

    def add(self, x: Sequence[float], w: float, q: float, c: float, lat: float,
            *, counterfactual: bool = False) -> None:
        if counterfactual:
            self.replay_n += 1
        else:
            self.n += 1
        self.weight += w
        for i in range(self.d):
            wxi = w * x[i]
            row = self.A[i]
            for j in range(self.d):
                row[j] += wxi * x[j]
            self.b["quality"][i] += wxi * q
            self.b["cost"][i] += wxi * c
            self.b["latency"][i] += wxi * lat
        self.S["qq"] += w * q * q
        self.S["ll"] += w * lat * lat
        self.S["ql"] += w * q * lat

    def to_dict(self) -> dict[str, Any]:
        r = _round
        out: dict[str, Any] = {
            "n": self.n,
            "weight": r(self.weight),
            "A": [[r(v) for v in row] for row in self.A],
            "b": {k: [r(v) for v in vs] for k, vs in sorted(self.b.items())},
            "S": {k: r(v) for k, v in sorted(self.S.items())},
        }
        if self.replay_n:  # absent when zero: older snapshots hash the same
            out["replay_n"] = self.replay_n
        return out

    @property
    def samples(self) -> int:
        """Logged plus counterfactual rows (the ``min_arm_samples`` gate)."""
        return self.n + self.replay_n

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], ridge: float) -> ArmStats:
        return cls(d=len(d["A"]), ridge=ridge, n=int(d["n"]), weight=float(d["weight"]),
                   A=[list(map(float, row)) for row in d["A"]],
                   b={k: list(map(float, v)) for k, v in d["b"].items()},
                   S={k: float(v) for k, v in d["S"].items()},
                   replay_n=int(d.get("replay_n", 0)))


def _round(v: float) -> float:
    r = round(float(v), _ROUND)
    return 0.0 if r == 0 else r


class _Fit:
    """Posterior of one arm under one reward weighting."""

    def __init__(self, s: ArmStats, mu: float, exploration: float):
        self.low = _cholesky(s.A)
        self.theta_q = _solve(self.low, s.b["quality"])
        self.theta_c = _solve(self.low, s.b["cost"])
        self.theta_l = _solve(self.low, s.b["latency"])
        # y = quality − μ·latency; cost enters the reward separately.
        b_y = [q - mu * lat for q, lat in zip(s.b["quality"], s.b["latency"], strict=True)]
        self.theta_y = _solve(self.low, b_y)
        yy = s.S["qq"] - 2 * mu * s.S["ql"] + mu * mu * s.S["ll"]
        rss = max(yy - _dot(b_y, self.theta_y), 0.0)
        dof = max(s.weight - s.d, 1.0)
        self.sigma = exploration * math.sqrt(max(rss / dof, 1e-4))


# ─── training ─────────────────────────────────────────────────────────


def _group_key(use_case: str | None, sensitivity: str) -> str:
    return f"{use_case or ''}\x1f{sensitivity}"


def _arm_cost(
    arm: str, rows: Sequence[_Row], prices: Mapping[str, tuple[float, float]],
    pricing_table: dict | None,
) -> float | None:
    """Mean counterfactual cost of the rows' token counts at ``arm``."""
    if not rows:
        return None
    provider, _, model = arm.partition(":")
    total = 0.0
    for r in rows:
        if r.tokens_in is None or r.tokens_out is None:
            return None
        if arm in prices:
            pi, po = prices[arm]
            total += r.tokens_in / 1000 * pi + r.tokens_out / 1000 * po
            continue
        c = pricing.estimate_cost(provider, model or None, r.tokens_in, r.tokens_out,
                                  pricing_table)
        if c is None:
            return None
        total += c
    return total / len(rows)


def _projection(
    arm: str, fit: _Fit, rows: Sequence[_Row], xbar: Sequence[float],
    cost: float | None, w: RewardWeights,
) -> dict[str, Any]:
    quality = _dot(xbar, fit.theta_q)
    method = "dm"
    scored = [r for r in rows if r.quality is not None]
    if scored and all(r.propensity is not None for r in scored):
        # Doubly robust: regression plus IPS-weighted residual on the arm.
        method = "dr"
        quality = sum(
            _dot(r.x, fit.theta_q)
            + ((r.quality - _dot(r.x, fit.theta_q)) / r.propensity if r.arm == arm else 0.0)
            for r in scored
        ) / len(scored)
    cost_method = "priced"
    if cost is None:
        cost, cost_method = _dot(xbar, fit.theta_c), "regression"
    latency = max(_dot(xbar, fit.theta_l), 0.0)
    quality = min(max(quality, 0.0), 1.0)
    cost = max(cost, 0.0)
    return {
        "model": arm,
        "quality": _round(quality),
        "cost_per_call": _round(cost),
        "latency_ms": _round(latency * 1000.0),
        "reward": _round(quality - w.lambda_cost * cost - w.mu_latency * latency),
        "quality_method": method,
        "cost_method": cost_method,
    }


def _thompson(
    fits: Mapping[str, _Fit], costs: Mapping[str, float], xbar: Sequence[float],
    w: RewardWeights, draws: int, rng: random.Random,
) -> dict[str, float]:
    arms = sorted(fits)
    wins = dict.fromkeys(arms, 0)
    for _ in range(draws):
        best, best_r = None, -math.inf
        for arm in arms:
            f = fits[arm]
            z = [rng.gauss(0.0, 1.0) for _ in xbar]
            noise = _back(f.low, z)
            theta = [t + f.sigma * e for t, e in zip(f.theta_y, noise, strict=True)]
            r = _dot(xbar, theta) - w.lambda_cost * costs[arm]
            if r > best_r:
                best, best_r = arm, r
        wins[best] += 1
    return {a: wins[a] / draws for a in arms}


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    """SHA-256 of the snapshot's canonical JSON without ``learner_version``."""
    body = {k: v for k, v in snapshot.items() if k != "learner_version"}
    return hashlib.sha256(canonical_json(body).encode()).hexdigest()


def train(
    tuples: Iterable[Mapping[str, Any]],
    route_policy: Mapping[str, Any],
    config: LearnerConfig | None = None,
    *,
    prior: Mapping[str, Any] | None = None,
    pricing_table: dict | None = None,
) -> dict[str, Any]:
    """Learn from logged calls and return a snapshot with recommendations.

    ``route_policy`` (route-policy.v1) decides each group's candidates; it is
    required, since a recommendation must stay inside the policy. ``prior``
    is an earlier snapshot to warm-start from (its arm statistics are added;
    its config must match). Pure: no I/O, no clock, deterministic.
    """
    cfg = config or LearnerConfig()
    if not isinstance(route_policy, Mapping) or not route_policy.get("groups"):
        raise ValueError("route_policy must be a route-policy.v1 document with groups")
    d = len(CONTEXT_FEATURES)

    rows: list[_Row] = []
    skipped = {"no_model": 0}
    data_hash = hashlib.sha256()
    parsed = []
    replay_parsed = []
    replay_skipped: dict[str, int] = defaultdict(int)
    uses_inputs = False
    insufficient = 0
    for t in tuples:
        r = _parse(t, route_policy, cfg.inputs) if isinstance(t, Mapping) else None
        if r is None:
            skipped["no_model"] += 1
            continue
        parsed.append((_row_key(r), r))
        if "replay" in t or t.get("quality_samples") is not None:
            uses_inputs = True
        insufficient += _eval_insufficient(t, cfg.inputs)
        rp, why = _replay_row(t, r, cfg.inputs)
        if rp is not None:
            replay_parsed.append((_row_key(rp), rp))
        elif why is not None:
            replay_skipped[why] += 1
    # Sorted, so the input order changes nothing (sums included).
    parsed.sort(key=lambda kr: kr[0])
    for key, r in parsed:
        data_hash.update(key.encode())
        data_hash.update(b"\n")
        rows.append(r)
    # Counterfactual rows hash after the logged ones, and only when present.
    replay_parsed.sort(key=lambda kr: kr[0])
    if replay_parsed:
        data_hash.update(b"replay\n")
    for key, _ in replay_parsed:
        data_hash.update(key.encode())
        data_hash.update(b"\n")

    by_group: dict[str, list[_Row]] = defaultdict(list)
    for r in rows:
        by_group[_group_key(r.use_case, r.sensitivity)].append(r)
    replay_by_group: dict[str, list[_Row]] = defaultdict(list)
    for _, r in replay_parsed:
        replay_by_group[_group_key(r.use_case, r.sensitivity)].append(r)
    replay_used = 0

    prior_arms: dict[str, dict[str, ArmStats]] = {}
    parent = None
    if prior is not None:
        if prior.get("schema") != SNAPSHOT_SCHEMA or prior.get("algorithm") != ALGORITHM:
            raise ValueError("prior snapshot has another schema or algorithm")
        if prior.get("context_features") != list(CONTEXT_FEATURES):
            raise ValueError("prior snapshot has another context feature set")
        if float(prior["config"]["ridge"]) != cfg.ridge:
            raise ValueError("prior snapshot used another ridge")
        parent = prior.get("learner_version")
        for g in prior.get("groups", []):
            key = _group_key(g.get("use_case"), g["sensitivity"])
            prior_arms[key] = {
                arm: ArmStats.from_dict(s, cfg.ridge) for arm, s in g["arms"].items()
            }
            by_group.setdefault(key, [])

    prices = _tier_prices(route_policy)
    groups_out: list[dict[str, Any]] = []
    recommendations: list[dict[str, Any]] = []

    for key in sorted(by_group):
        g_rows = by_group[key]
        use_case_s, _, sensitivity = key.partition("\x1f")
        use_case = use_case_s or None
        w = cfg.weights_for(use_case)
        candidates = policy_candidates(route_policy, use_case, sensitivity)
        candidate_set = set(candidates)

        arms: dict[str, ArmStats] = {}
        for arm, s in prior_arms.get(key, {}).items():
            arms[arm] = s
        for r in g_rows:
            if r.quality is None:
                continue
            s = arms.setdefault(r.arm, ArmStats(d=d, ridge=cfg.ridge))
            s.add(r.x, r.weight, r.quality, r.cost or 0.0, r.latency_s or 0.0)
        # Counterfactual replay rows: only for the group's policy candidates,
        # and never traffic (calls, current route, context mean, DR term).
        for r in replay_by_group.get(key, ()):
            if r.arm not in candidate_set:
                replay_skipped["outside_policy"] += 1
                continue
            replay_used += 1
            s = arms.setdefault(r.arm, ArmStats(d=d, ridge=cfg.ridge))
            s.add(r.x, r.weight, r.quality, r.cost or 0.0, r.latency_s or 0.0,
                  counterfactual=True)

        traffic: dict[str, int] = defaultdict(int)
        for r in g_rows:
            traffic[r.arm] += 1
        current = (
            max(sorted(traffic), key=lambda a: traffic[a]) if traffic else None
        )
        n_calls = len(g_rows)
        xbar = (
            [sum(r.x[i] for r in g_rows) / n_calls for i in range(d)]
            if n_calls else [0.0] * d
        )

        group: dict[str, Any] = {
            "use_case": use_case,
            "sensitivity": sensitivity,
            "candidates": candidates,
            "current_model": current,
            "current_compliant": current in candidate_set if current else None,
            "calls": n_calls,
            "scored_calls": sum(1 for r in g_rows if r.quality is not None),
            "reward": asdict(w),
            "arms": {a: arms[a].to_dict() for a in sorted(arms)},
        }

        evaluable = sorted(
            a for a in candidate_set
            if a in arms and arms[a].samples >= cfg.min_arm_samples
        )
        group["unevaluated_candidates"] = [a for a in candidates if a not in evaluable]
        if not n_calls or not evaluable:
            group["status"] = "insufficient_data"
            groups_out.append(group)
            continue

        fits = {a: _Fit(arms[a], w.mu_latency, cfg.exploration) for a in arms}
        costs = {a: _arm_cost(a, g_rows, prices, pricing_table) for a in arms}
        draw_costs = {
            a: costs[a] if costs[a] is not None else max(_dot(xbar, fits[a].theta_c), 0.0)
            for a in evaluable
        }
        rng = random.Random(cfg.seed * 1_000_003 + zlib.crc32(key.encode()))
        prob = _thompson({a: fits[a] for a in evaluable}, draw_costs, xbar, w,
                         cfg.thompson_draws, rng)
        best = max(evaluable, key=lambda a: (prob[a], a == current, a))
        group["prob_best"] = {a: _round(p) for a, p in sorted(prob.items())}
        group["best_model"] = best

        proj_best = _projection(best, fits[best], g_rows, xbar, costs[best], w)
        cur_ok = current in arms and arms[current].samples >= cfg.min_arm_samples
        if best == current:
            group["status"] = "keep_current"
        elif not cur_ok:
            group["status"] = "insufficient_data"
        else:
            proj_cur = _projection(current, fits[current], g_rows, xbar, costs[current], w)
            quality_delta = proj_best["quality"] - proj_cur["quality"]
            if prob[best] < cfg.min_prob_best:
                group["status"] = "not_confident"
            elif quality_delta < -cfg.quality_tolerance:
                group["status"] = "quality_below_parity"
            elif proj_best["reward"] <= proj_cur["reward"]:
                group["status"] = "keep_current"
            else:
                group["status"] = "recommend"
                saving = proj_cur["cost_per_call"] - proj_best["cost_per_call"]
                replay_samples = {
                    a: arms[a].replay_n for a in (current, best) if arms[a].replay_n
                }
                recommendations.append({
                    "use_case": use_case,
                    "sensitivity": sensitivity,
                    "current_model": current,
                    "recommended_model": best,
                    "candidates": candidates,
                    "projection": {
                        "calls": n_calls,
                        "current": proj_cur,
                        "recommended": proj_best,
                        "quality_delta": _round(quality_delta),
                        "reward_delta": _round(proj_best["reward"] - proj_cur["reward"]),
                        "savings_per_call": _round(saving),
                        "savings_total": _round(saving * n_calls),
                        "savings_pct": _round(
                            saving / proj_cur["cost_per_call"]
                            if proj_cur["cost_per_call"] > 0 else 0.0
                        ),
                        "prob_best": _round(prob[best]),
                        "arm_samples": {
                            current: arms[current].n, best: arms[best].n,
                        },
                        **({"counterfactual_samples": replay_samples}
                           if replay_samples else {}),
                        # No logged call on the recommended model: the whole
                        # case rests on replayed (eval parity) rows.
                        **({"counterfactual_only": True}
                           if arms[best].n == 0 else {}),
                    },
                })
        groups_out.append(group)

    snapshot: dict[str, Any] = {
        "schema": SNAPSHOT_SCHEMA,
        "mode": MODE,
        "algorithm": ALGORITHM,
        "feature_schema": FEATURE_SCHEMA_VERSION,
        "context_features": list(CONTEXT_FEATURES),
        "config": cfg.to_dict(),
        "policy_version": route_policy.get("version") or route_policy.get("policy_version"),
        "parent_version": parent,
        "data": {
            "fingerprint": data_hash.hexdigest(),
            "rows": len(rows),
            "scored_rows": sum(1 for r in rows if r.quality is not None),
            "skipped": skipped,
        },
        "groups": groups_out,
        "recommendations": recommendations,
    }
    if uses_inputs:  # absent for tuples without these fields: same version
        snapshot["inputs"] = {
            **asdict(cfg.inputs),
            "insufficient_sample_rows": insufficient,
            "replay": {"rows": replay_used, "skipped": dict(sorted(replay_skipped.items()))},
        }
    snapshot["learner_version"] = VERSION_PREFIX + snapshot_digest(snapshot)[:16]
    return snapshot


def verify_snapshot(snapshot: Mapping[str, Any]) -> bool:
    """True when ``learner_version`` matches the snapshot's content."""
    v = snapshot.get("learner_version")
    return isinstance(v, str) and v == VERSION_PREFIX + snapshot_digest(snapshot)[:16]


def load_tuples(lines: Iterable[str]) -> list[dict[str, Any]]:
    """Parse JSONL tuples, skipping blank lines."""
    out = []
    for n, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError as e:
            raise ValueError(f"line {n}: not JSON") from e
        if isinstance(obj, dict):
            out.append(obj)
    return out


def join_feedback(
    tuples: Iterable[Mapping[str, Any]], feedback: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Copy explicit feedback scores (``{call_id, score}``) onto tuples by
    ``call_id``; a tuple's own ``feedback_score`` wins."""
    scores = {
        f["call_id"]: f["score"] for f in feedback
        if isinstance(f.get("call_id"), str) and _unit(f.get("score")) is not None
    }
    out = []
    for t in tuples:
        t = dict(t)
        cid = t.get("call_id")
        if t.get("feedback_score") is None and cid in scores:
            t["feedback_score"] = scores[cid]
        out.append(t)
    return out


def shadow_tuples(results: Iterable[Any]) -> list[dict[str, Any]]:
    """Learning rows from shadow-eval results (``ShadowResult`` or dicts).

    Each judged result gives the alternative model a quality of 1 (win),
    0.5 (tie) or 0 (loss) against the primary, with its cost and latency.
    Shadow results carry no prompt features, so the context is the bias only.
    """
    score = {"win": 1.0, "tie": 0.5, "loss": 0.0}
    out = []
    for r in results:
        get = r.get if isinstance(r, Mapping) else (lambda k, _r=r: getattr(_r, k, None))
        if get("status") != "judged" or get("outcome") not in score:
            continue
        provider, _, model = str(get("candidate")).rpartition(":")
        out.append({
            "use_case": get("use_case"),
            "sensitivity_level": get("sensitivity"),
            "provider": provider or None,
            "model": model,
            "shadow_eval_score": score[get("outcome")],
            "cost_usd": get("candidate_cost"),
            "latency_ms": get("candidate_latency_ms"),
            "features": {},
        })
    return out


def upload_snapshot(
    snapshot: Mapping[str, Any], *, ps_api_key: str,
    ps_collector_url: str = "http://localhost:8000", timeout: float = 30.0,
) -> dict:
    """Send a snapshot to the collector (``POST /ingest/learner-snapshots``).

    Returns the collector's answer; raises ``httpx.HTTPStatusError`` on a
    refusal.
    """
    resp = httpx.post(
        f"{ps_collector_url.rstrip('/')}{INGEST_PATH}",
        content=canonical_json(snapshot),
        headers={"Authorization": f"Bearer {ps_api_key}",
                 "Content-Type": "application/json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def summarize(snapshot: Mapping[str, Any]) -> str:
    """A short plain-text summary for the CLI."""
    lines = [
        (
            f"learner_version {snapshot['learner_version']} ({snapshot['mode']} mode, "
            f"{snapshot['data']['rows']} rows, {snapshot['data']['scored_rows']} scored)"
        ),
    ]
    for g in snapshot["groups"]:
        lines.append(
            f"  {g['use_case'] or '(no use case)'} / {g['sensitivity']}: "
            f"{g['status']} (current {g['current_model']}, {g['calls']} calls)"
        )
    for r in snapshot["recommendations"]:
        p = r["projection"]
        lines.append(
            f"  recommend {r['current_model']} -> {r['recommended_model']} for "
            f"{r['use_case'] or '(no use case)'} / {r['sensitivity']}: "
            f"saves {p['savings_pct']:.1%} per call, quality {p['quality_delta']:+.3f}, "
            f"P(best) {p['prob_best']:.2f}"
        )
    return "\n".join(lines)



def main(argv: Sequence[str] | None = None) -> int:
    """``ps-learn``: train on tuples, write a snapshot, optionally upload it."""
    import argparse
    import os
    import sys

    ap = argparse.ArgumentParser(
        prog="ps-learn",
        description="Offline routing learner (recommend mode): learns from logged "
        "calls and proposes policy-compliant route changes. It never routes traffic.",
    )
    ap.add_argument("--tuples", required=True, action="append",
                    help="learning tuples JSONL (ps-teardown --learning-tuples); repeatable")
    ap.add_argument("--policy", required=True, help="route-policy.v1 JSON")
    ap.add_argument("--feedback", help="JSONL of {call_id, score} to join onto the tuples")
    ap.add_argument("--prior", help="earlier snapshot JSON to warm-start from")
    ap.add_argument("--out", required=True, help="where to write the snapshot JSON")
    ap.add_argument("--lambda-cost", type=float, default=RewardWeights.lambda_cost)
    ap.add_argument("--mu-latency", type=float, default=RewardWeights.mu_latency)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--min-arm-samples", type=int, default=LearnerConfig.min_arm_samples)
    ap.add_argument("--min-quality-samples", type=int,
                    default=TupleInputs.min_quality_samples,
                    help="eval cells smaller than this are insufficient_sample and ignored")
    ap.add_argument("--replay-weight", type=float, default=TupleInputs.replay_weight,
                    help="weight of a tuple's counterfactual replay row (0 turns it off)")
    ap.add_argument("--upload", action="store_true",
                    help="send the snapshot to the collector (PS_API_KEY, PS_COLLECTOR_URL)")
    args = ap.parse_args(argv)

    tuples: list[dict[str, Any]] = []
    for path in args.tuples:
        with open(path, encoding="utf-8") as fh:
            tuples.extend(load_tuples(fh))
    if args.feedback:
        with open(args.feedback, encoding="utf-8") as fh:
            tuples = join_feedback(tuples, load_tuples(fh))
    with open(args.policy, encoding="utf-8") as fh:
        policy = json.load(fh)
    prior = None
    if args.prior:
        with open(args.prior, encoding="utf-8") as fh:
            prior = json.load(fh)
    cfg = LearnerConfig(
        reward=RewardWeights(args.lambda_cost, args.mu_latency),
        seed=args.seed, min_arm_samples=args.min_arm_samples,
        inputs=TupleInputs(min_quality_samples=args.min_quality_samples,
                           replay_weight=args.replay_weight),
    )
    try:
        snap = train(tuples, policy, cfg, prior=prior)
    except ValueError as e:
        print(f"ps-learn: {e}", file=sys.stderr)
        return 2
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, sort_keys=True, indent=1)
        fh.write("\n")
    print(summarize(snap))
    if args.upload:
        key = os.environ.get("PS_API_KEY")
        if not key:
            print("ps-learn: --upload needs PS_API_KEY", file=sys.stderr)
            return 2
        url = os.environ.get("PS_COLLECTOR_URL", "http://localhost:8000")
        try:
            print(json.dumps(upload_snapshot(snap, ps_api_key=key, ps_collector_url=url)))
        except httpx.HTTPError as e:
            print(f"ps-learn: upload failed: {type(e).__name__}", file=sys.stderr)
            return 1
    return 0


__all__ = [
    "ALGORITHM",
    "CONTEXT_FEATURES",
    "MODE",
    "SNAPSHOT_SCHEMA",
    "ArmStats",
    "LearnerConfig",
    "RewardWeights",
    "TupleInputs",
    "canonical_json",
    "context_vector",
    "join_feedback",
    "load_tuples",
    "main",
    "policy_candidates",
    "row_quality",
    "shadow_tuples",
    "snapshot_digest",
    "summarize",
    "train",
    "upload_snapshot",
    "verify_snapshot",
]
