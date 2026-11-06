"""Routing learner, ``auto`` mode (#34; PRD 0001 §7.13). Opt-in, off by default.

In ``recommend`` mode (:mod:`prompt_shields.learner`) the learner only
proposes route changes and a person applies them. In ``auto`` mode the
:class:`AutoLearner` routes live calls itself: it applies the active
snapshot's recommendations and explores a little, with guard rails.

Where it runs
-------------
In the SDK router, local mode only: ``ShieldsRouter(mode="local",
learner=AutoLearner(...))``. ``mode="gateway"`` refuses a learner; the
gateway does not run auto mode yet. Without ``learner=`` (the default) the
router routes exactly as before.

What it may change
------------------
Only the *strategy* step of ``resolve_policy_route`` (the heuristic's group
choice). The router first decides the call as usual (data policy, use-case
bar, budget clamp); then, for a call with ``model="auto"`` and no explicit
group or quality hint, the learner picks one model from the policy-safe set
(:func:`prompt_shields.route_policy.policy_safe_tiers`, the same function
the offline learner takes its arms from: the sensitivity level's
``data_policy`` rule, the use case's ``eligible_models`` and ``min_group``),
limited to configured providers and to the ``max_cost`` / budget clamp, plus
the policy's own pick. A pinned model, an explicit group (``X-PS-Route``) or
a quality hint is the caller's choice and is never changed. Failover then
works as before from the chosen candidate, and the engine re-checks every
candidate against the data policy. When the learner's model cannot take the
request (OpenAI-only parameters on an Anthropic model), the call is routed
as the policy would and not recorded as learner-routed.

Per call (for an enabled use case):

* **exploit**: the active snapshot's recommendation for the call's
  (use case, sensitivity), when its model is in the safe set; else the
  policy's pick (``policy``).
* **explore**: with probability ``explore_rate`` (default 5%, at most 20%),
  a uniformly random model of the safe set instead. The rate is forced to 0
  for the ``restricted`` level (or the policy's highest sensitivity level),
  for a use case whose ``min_group`` is ``frontier`` (or the top of
  ``group_order``), and while learning is frozen.

Each routed call records the choice (``resp.ps.route.learner``, the route
reason suffix ``+learner-exploit`` / ``+learner-explore``, and the event's
``route_learner_version``, ``route_learner_mode`` and ``route_propensity``),
so later training can weight it by its propensity.

The enablement gate
-------------------
A use case can be enabled only with a recorded, accurate ``recommend``
track record: the Partner API's ``GET /partner/v1/learner/recommendations``
answer for it (:func:`fetch_recommendations`). :func:`track_record` counts
the accepted recommendations whose realised savings were measured on at
least :data:`GATE_MIN_REALISED_CALLS` calls (*evaluated*), and among them
those within :data:`GATE_MAX_DELTA_POINTS` percentage points of the
projection whose feedback quality, when known, is no more than
:data:`GATE_QUALITY_TOLERANCE` under the projected quality (*accurate*). The
use case is eligible with at least :data:`GATE_MIN_EVALUATED` evaluated and
an accurate share of at least :data:`GATE_MIN_ACCURACY`. The counts are
recomputed here from the records; an ``eligible`` flag in the input is not
trusted. Duplicate records count once: by ``id``, or for a record without
one by its snapshot, use case, sensitivity and models. The thresholds are
constants, not options. The gate guards against
enabling auto mode by mistake; it is not an access control (whoever runs the
router controls its code).

Auto-rollback
-------------
Every call the learner routed is observed: its outcome signals right away
(from the telemetry event; a call no candidate served counts as a strong
failure, :data:`CALL_FAILED`), explicit quality later through
:meth:`AutoLearner.record_outcome` (feedback, eval-parity or shadow-eval
scores). Per (use case, sensitivity), over the last ``window`` observations
of the active snapshot, *treated* calls are the exploit calls the learner
moved off the policy's pick, *control* calls those it left on it. With at
least ``min_samples`` treated calls the snapshot is rolled back when:

* **implicit failure**: the treated share of strong failure classes
  (``fallback``, ``refusal``, ``schema_invalid`` ...) is above the control
  share plus ``max_failure_increase``, or above ``max_failure_rate`` while
  there are fewer than ``min_samples`` control calls;
* **parity**: the treated mean quality is more than ``parity_tolerance``
  below the control mean, or, with too few control calls, below the
  recommendation's projected quality for the current route.

Rollback restores the last good snapshot, or no snapshot, which is the
route policy's own decision. A snapshot is good once *every* group that it
moved calls in has served at least ``promote_after`` (and ``min_samples``)
treated calls of its own without a breach: the evidence is counted per
(use case, sensitivity), so one busy group cannot vouch for another. A
rolled-back version is never loaded again.

If the learner itself raises while choosing or observing, the router keeps
the policy's pick and serves the call; it logs the error type only and
counts the fallback in :attr:`AutoLearner.fallbacks`.

Freezing
--------
Simple, documented heuristics; any of them freezes learning (no
exploration, no new snapshot, :meth:`AutoLearner.retrain` refused) until
:meth:`AutoLearner.unfreeze`. The active snapshot keeps serving, and the
rollback guard keeps running.

* **drift**: the mean context vector of the last ``window`` calls of a group
  moves more than ``drift_threshold`` (on any feature but the bias) from the
  mean of the rows the snapshot was trained on (recovered from its arms'
  statistics);
* **poisoning**, when a new snapshot is loaded over the active one: an arm's
  scored-row count grew more than ``poison_volume_ratio`` times in one
  warm-started retrain, or an arm's fitted quality at the group's training
  context moved more than ``poison_quality_jump``.

Not built yet (#34): the gateway, a periodic RouteLLM-style retrain as the
cold-start prior (:meth:`AutoLearner.retrain` is the manual hook), and
storing the propensity in the collector.
"""

from __future__ import annotations

import json
import logging
import math
import random
import threading
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from prompt_shields.learner import (
    ALGORITHM,
    SNAPSHOT_SCHEMA,
    STRONG_FAILURES,
    UNCLASSIFIED,
    LearnerConfig,
    _cholesky,
    _dot,
    _solve,
    row_quality,
    train,
    verify_snapshot,
)
from prompt_shields.route_policy import DEFAULT_LEVELS, use_case_bar

logger = logging.getLogger("prompt_shields.learner_auto")

MODE = "auto"
DEFAULT_EXPLORE_RATE = 0.05
MAX_EXPLORE_RATE = 0.2

#: Enablement gate (see the module docstring). Constants on purpose.
GATE_MIN_EVALUATED = 2
GATE_MIN_ACCURACY = 0.8
GATE_MIN_REALISED_CALLS = 50
GATE_MAX_DELTA_POINTS = 5.0
GATE_QUALITY_TOLERANCE = 0.05

RECOMMENDATIONS_PATH = "/partner/v1/learner/recommendations"
#: Outcome signal the router reports for a learner-routed call that failed
#: outright (every candidate failed or was skipped): a strong failure.
CALL_FAILED = "call_failed"
_MAX_PENDING = 10_000

ChoiceMode = Literal["policy", "exploit", "explore"]


class AutoModeError(ValueError):
    """Auto mode cannot do what was asked."""


class AutoGateError(AutoModeError):
    """A use case has no accurate ``recommend`` track record yet."""


class LearningFrozen(AutoModeError):
    """Learning is frozen (drift or poisoning); see ``freeze_reasons``."""


# ─── the enablement gate ──────────────────────────────────────────────


@dataclass(frozen=True)
class TrackRecord:
    """A use case's ``recommend`` track record, as the gate counts it."""

    use_case: str
    accepted: int
    rejected: int
    evaluated: int
    accurate: int
    eligible: bool
    reasons: tuple[str, ...] = ()


def _items(recommendations: Any) -> list[Mapping[str, Any]]:
    if isinstance(recommendations, Mapping):
        recommendations = recommendations.get("data") or []
    if not isinstance(recommendations, Iterable) or isinstance(recommendations, (str, bytes)):
        return []
    return [r for r in recommendations if isinstance(r, Mapping)]


def _finite(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _record_key(r: Mapping[str, Any]) -> Any:
    """A stable identity for one recommendation record: its ``id``, else
    what identifies a recommendation (snapshot, use case, sensitivity and
    models; the learner makes one per group and snapshot). Records without
    any of those collapse together, which only makes the gate stricter."""
    rid = r.get("id")
    if isinstance(rid, str) and rid:
        return ("id", rid)
    fields = [
        r.get(k) for k in ("snapshot_id", "learner_version", "use_case",
                           "sensitivity_level", "current_model", "recommended_model")
    ]
    return ("fields", json.dumps(fields, sort_keys=True, default=str))


def track_record(recommendations: Any, use_case: str) -> TrackRecord:
    """Count ``use_case``'s reviewed recommendations and decide the gate.

    ``recommendations`` is the Partner API's list answer (``{"data": [...]}``)
    or its ``data`` list, as ``GET /partner/v1/learner/recommendations``
    returns it (each accepted one with ``realised``). Duplicates count once
    (:func:`_record_key`).
    """
    seen: set[Any] = set()
    accepted = rejected = evaluated = accurate = 0
    for r in _items(recommendations):
        if r.get("use_case") != use_case:
            continue
        key = _record_key(r)
        if key in seen:
            continue
        seen.add(key)
        status = r.get("status")
        if status == "rejected":
            rejected += 1
            continue
        if status != "accepted":
            continue
        accepted += 1
        realised = r.get("realised") if isinstance(r.get("realised"), Mapping) else {}
        calls = realised.get("calls")
        delta = _finite(realised.get("delta_points"))
        if not isinstance(calls, int) or calls < GATE_MIN_REALISED_CALLS or delta is None:
            continue
        evaluated += 1
        ok = abs(delta) <= GATE_MAX_DELTA_POINTS
        fq = _finite(realised.get("feedback_quality"))
        pq = _finite(realised.get("projected_quality"))
        if fq is not None and pq is not None and fq < pq - GATE_QUALITY_TOLERANCE:
            ok = False
        accurate += ok
    reasons = []
    if evaluated < GATE_MIN_EVALUATED:
        reasons.append(
            f"{evaluated} accepted recommendation(s) with realised savings over "
            f"{GATE_MIN_REALISED_CALLS}+ calls; {GATE_MIN_EVALUATED} needed"
        )
    elif accurate / evaluated < GATE_MIN_ACCURACY:
        reasons.append(
            f"{accurate} of {evaluated} evaluated recommendations were accurate; "
            f"{GATE_MIN_ACCURACY:.0%} needed"
        )
    return TrackRecord(use_case, accepted, rejected, evaluated, accurate,
                       not reasons, tuple(reasons))


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _collector_base_url(url: Any) -> str:
    """The collector base URL for :func:`fetch_recommendations`, or
    :class:`AutoModeError`. It must be given, and be ``https://`` (plain
    ``http://`` only to a loopback host): the request carries the partner
    token."""
    if not isinstance(url, str) or not url.strip():
        raise AutoModeError(
            "fetch_recommendations needs ps_collector_url, the collector's "
            "base URL (https://...); there is no default"
        )
    url = url.strip()
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        parts, host = None, None
    if (parts is None or parts.scheme not in ("https", "http") or not host
            or parts.query or parts.fragment or "?" in url or "#" in url):
        raise AutoModeError(
            "ps_collector_url must be an https:// base URL with a host and "
            "no query or fragment"
        )
    if parts.scheme == "http" and host not in _LOOPBACK_HOSTS:
        raise AutoModeError(
            "ps_collector_url must use https:// (plain http:// is allowed only "
            "for localhost): the request carries the partner token"
        )
    return url.rstrip("/")


def fetch_recommendations(
    use_case: str, *, partner_token: str,
    ps_collector_url: str | None = None, timeout: float = 30.0,
) -> dict:
    """The Partner API's recommendations for ``use_case`` (scope
    ``learner:read``), to pass to :class:`AutoLearner` as its track record.

    ``ps_collector_url`` is required and must be ``https://`` (``http://``
    only to localhost); anything else raises :class:`AutoModeError` before
    any request is sent."""
    base = _collector_base_url(ps_collector_url)
    resp = httpx.get(
        f"{base}{RECOMMENDATIONS_PATH}",
        params={"use_case": use_case, "limit": 200},
        headers={"Authorization": f"Bearer {partner_token}"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


# ─── configuration and choices ────────────────────────────────────────


@dataclass(frozen=True)
class GuardConfig:
    """Thresholds of the rollback guard and the freeze heuristics."""

    #: Observations kept per (use case, sensitivity), and calls in the
    #: drift window.
    window: int = 200
    #: Treated (and control) calls needed before a rate is compared.
    min_samples: int = 50
    parity_tolerance: float = 0.05
    #: Strong-failure share allowed without enough control calls.
    max_failure_rate: float = 0.15
    #: Allowed treated share above the control share.
    max_failure_increase: float = 0.05
    #: Treated calls without a breach before a snapshot counts as good.
    promote_after: int = 200
    drift_threshold: float = 0.25
    poison_volume_ratio: float = 5.0
    poison_quality_jump: float = 0.2

    def __post_init__(self):
        if self.window < 1 or self.min_samples < 1 or self.promote_after < 1:
            raise AutoModeError("window, min_samples and promote_after must be >= 1")
        if self.min_samples > self.window:
            raise AutoModeError("min_samples must not exceed window")


@dataclass(frozen=True)
class LearnerChoice:
    """What the learner did for one call (``resp.ps.route.learner``)."""

    model: str  # provider:model
    mode: ChoiceMode
    propensity: float
    explore_rate: float
    policy_model: str
    learner_version: str | None
    use_case: str
    sensitivity: str


@dataclass
class _Obs:
    version: str | None
    mode: ChoiceMode
    changed: bool
    quality: float | None = None
    explicit: bool = False
    failure: bool = False


@dataclass
class _Group:
    obs: OrderedDict = field(default_factory=OrderedDict)  # call_id -> _Obs
    contexts: deque = field(default_factory=deque)
    #: Treated calls this group served under the active snapshot (all of
    #: them, not just the window): its promotion evidence.
    treated: int = 0


def _level(sensitivity: str | None) -> str:
    return sensitivity or UNCLASSIFIED


# ─── the learner ──────────────────────────────────────────────────────


class AutoLearner:
    """Live routing by the learner, for the use cases enabled here.

    ``use_cases`` maps each use case to enable to its track record (the
    Partner API recommendations answer, see :func:`track_record`); a use
    case without an eligible record raises :class:`AutoGateError`.
    ``snapshot`` is the first snapshot to serve (``learner.train`` output);
    without one every call keeps the policy's pick until
    :meth:`load_snapshot`. ``on_event`` gets a dict for every load, refusal,
    rollback, promotion and freeze (also kept in ``events``).
    """

    def __init__(
        self,
        use_cases: Mapping[str, Any],
        *,
        snapshot: Mapping[str, Any] | None = None,
        explore_rate: float = DEFAULT_EXPLORE_RATE,
        guard: GuardConfig | None = None,
        seed: int | None = None,
        on_event: Callable[[dict], Any] | None = None,
    ):
        rate = _finite(explore_rate)
        if rate is None or not 0.0 <= rate <= MAX_EXPLORE_RATE:
            raise AutoModeError(f"explore_rate must be between 0 and {MAX_EXPLORE_RATE}")
        if not isinstance(use_cases, Mapping) or not use_cases:
            raise AutoModeError("auto mode needs at least one use case to enable")
        self.track_records: dict[str, TrackRecord] = {}
        for uc, records in use_cases.items():
            if not isinstance(uc, str) or not uc:
                raise AutoModeError("use case names must be non-empty strings")
            tr = track_record(records, uc)
            if not tr.eligible:
                raise AutoGateError(
                    f"auto mode cannot be enabled for use case {uc!r}: "
                    + "; ".join(tr.reasons)
                )
            self.track_records[uc] = tr
        self.explore_rate = rate
        self.guard = guard or GuardConfig()
        self._rng = random.Random(seed)
        self._on_event = on_event
        self._lock = threading.RLock()
        self.events: deque = deque(maxlen=200)
        self._active: Mapping[str, Any] | None = None
        self._good: list[Mapping[str, Any]] = []
        self._bad: set[str] = set()
        self._recs: dict[tuple[str | None, str], Mapping[str, Any]] = {}
        self._train_means: dict[tuple[str | None, str], list[float] | None] = {}
        self._groups: dict[tuple[str, str], _Group] = {}
        self._pending: OrderedDict = OrderedDict()  # call_id -> (key, _Obs)
        self.freeze_reasons: list[str] = []
        #: Calls routed by the policy because the learner raised, by stage
        #: (``choose`` / ``observe``); the router reports them here.
        self.fallbacks: dict[str, int] = {}
        if snapshot is not None:
            self.load_snapshot(snapshot)

    # --- state ---------------------------------------------------------

    @property
    def use_cases(self) -> frozenset[str]:
        return frozenset(self.track_records)

    @property
    def active_version(self) -> str | None:
        return self._active.get("learner_version") if self._active else None

    @property
    def good_versions(self) -> list[str]:
        return [s["learner_version"] for s in self._good]

    @property
    def frozen(self) -> bool:
        return bool(self.freeze_reasons)

    def enabled_for(self, use_case: str | None) -> bool:
        return use_case in self.track_records

    def _event(self, kind: str, **details: Any) -> None:
        ev = {"event": kind, **details}
        self.events.append(ev)
        if kind in ("rollback", "frozen", "snapshot_refused"):
            logger.warning("learner auto mode: %s %s", kind, details)
        if self._on_event is not None:
            try:
                self._on_event(ev)
            except Exception:  # a callback never breaks routing
                logger.exception("learner auto mode: on_event failed")

    # --- snapshots -----------------------------------------------------

    def load_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        """Serve ``snapshot`` from now on (on probation until promoted).

        Refused (:class:`AutoModeError`) when it fails its version check,
        is not a ``recommend`` snapshot of this algorithm, or was rolled
        back before; refused with :class:`LearningFrozen` while frozen, or
        when the poisoning heuristics fire against the active snapshot
        (which also freezes learning).
        """
        with self._lock:
            version = snapshot.get("learner_version") if isinstance(snapshot, Mapping) else None
            try:
                if not isinstance(snapshot, Mapping) or not verify_snapshot(snapshot):
                    raise AutoModeError("snapshot fails its learner_version check")
                if (snapshot.get("schema") != SNAPSHOT_SCHEMA
                        or snapshot.get("algorithm") != ALGORITHM
                        or snapshot.get("mode") != "recommend"):
                    raise AutoModeError("not a recommend-mode snapshot of this learner")
                if version in self._bad:
                    raise AutoModeError(f"snapshot {version} was rolled back")
                if self.frozen:
                    raise LearningFrozen(
                        "learning is frozen: " + "; ".join(self.freeze_reasons)
                    )
                if version == self.active_version:
                    return
                if self._active is not None:
                    signals = poisoning_signals(self._active, snapshot, self.guard)
                    if signals:
                        self._freeze(signals)
                        raise LearningFrozen("possible poisoning: " + "; ".join(signals))
            except AutoModeError as e:
                self._event("snapshot_refused", version=version, reason=str(e))
                raise
            self._activate(snapshot)
            self._event("snapshot_loaded", version=version,
                        parent=snapshot.get("parent_version"))

    def _activate(self, snapshot: Mapping[str, Any] | None) -> None:
        self._active = snapshot
        self._recs = {}
        self._train_means = {}
        self._groups.clear()
        self._pending.clear()
        if snapshot is None:
            return
        for r in snapshot.get("recommendations") or ():
            self._recs[(r.get("use_case"), r.get("sensitivity"))] = r
        ridge = float((snapshot.get("config") or {}).get("ridge", 1.0))
        for g in snapshot.get("groups") or ():
            self._train_means[(g.get("use_case"), g.get("sensitivity"))] = (
                _training_mean(g, ridge)
            )

    def retrain(
        self, tuples: Iterable[Mapping[str, Any]], route_policy: Mapping[str, Any],
        config: LearnerConfig | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        """Train on new tuples warm-started from the active snapshot, then
        load the result. Refused while frozen."""
        if self.frozen:
            raise LearningFrozen("learning is frozen: " + "; ".join(self.freeze_reasons))
        snap = train(tuples, route_policy, config, prior=self._active, **kwargs)
        self.load_snapshot(snap)
        return snap

    def rollback(self, reason: str = "manual", **details: Any) -> str | None:
        """Drop the active snapshot for the last good one (or none: the
        policy's own decisions). Returns the version now active."""
        with self._lock:
            bad = self.active_version
            if bad is None:
                return None
            self._bad.add(bad)
            self._good = [s for s in self._good if s.get("learner_version") != bad]
            self._activate(self._good[-1] if self._good else None)
            self._event("rollback", version=bad, to=self.active_version,
                        reason=reason, **details)
            return self.active_version

    # --- freezing ------------------------------------------------------

    def _freeze(self, reasons: Iterable[str]) -> None:
        new = [r for r in reasons if r not in self.freeze_reasons]
        if new:
            self.freeze_reasons.extend(new)
            self._event("frozen", reasons=new)

    def freeze(self, reason: str) -> None:
        with self._lock:
            self._freeze([reason])

    def unfreeze(self) -> None:
        """Resume learning after a person has looked at the freeze reasons."""
        with self._lock:
            if self.freeze_reasons:
                self._event("unfrozen", reasons=list(self.freeze_reasons))
            self.freeze_reasons = []
            for g in self._groups.values():
                g.contexts.clear()

    # --- routing -------------------------------------------------------

    def explore_rate_for(
        self, policy: Mapping[str, Any], use_case: str | None, sensitivity: str | None
    ) -> float:
        """The exploration rate for one call: 0 when frozen, for the
        ``restricted`` (or highest) sensitivity level, and for a use case
        whose ``min_group`` is ``frontier`` (or the top of ``group_order``)."""
        if self.frozen or self.explore_rate == 0:
            return 0.0
        sens = policy.get("sensitivity") or {}
        levels = list(sens.get("levels") or DEFAULT_LEVELS)
        if sensitivity == "restricted" or (levels and sensitivity == levels[-1]):
            return 0.0
        bar = use_case_bar(policy, use_case) or {}
        order = list(policy.get("group_order") or [])
        mg = bar.get("min_group")
        if mg == "frontier" or (order and mg == order[-1]):
            return 0.0
        return self.explore_rate

    def choose(
        self,
        use_case: str | None,
        sensitivity: str | None,
        candidates: Sequence[str],
        policy_model: str,
        policy: Mapping[str, Any],
        *,
        context: Sequence[float] | None = None,
    ) -> LearnerChoice | None:
        """Pick one of ``candidates`` (``provider:model`` labels, all
        policy-safe; ``policy_model`` is the policy's own pick). None when
        the use case is not enabled."""
        if not self.enabled_for(use_case):
            return None
        assert use_case is not None
        level = _level(sensitivity)
        cands = sorted(set(candidates) | {policy_model})
        with self._lock:
            if context is not None:
                self._observe_context(use_case, level, context)
            rate = self.explore_rate_for(policy, use_case, sensitivity)
            rec = self._recs.get((use_case, level))
            target = policy_model
            if rec is not None and rec.get("recommended_model") in cands:
                target = rec["recommended_model"]
            pick: str = target
            mode: ChoiceMode = "exploit" if target != policy_model else "policy"
            n = len(cands)
            if n < 2:
                rate = 0.0
            if rate > 0 and self._rng.random() < rate:
                pick, mode = self._rng.choice(cands), "explore"
            propensity = (1.0 - rate) * (pick == target) + (rate / n if rate else 0.0)
            return LearnerChoice(
                model=pick, mode=mode, propensity=round(propensity, 6),
                explore_rate=rate, policy_model=policy_model,
                learner_version=self.active_version, use_case=use_case,
                sensitivity=level,
            )

    def note_fallback(self, stage: str, error: BaseException | None = None) -> None:
        """Count a call the router served on the policy's pick because the
        learner raised in ``stage``. Only the error's type is kept."""
        with self._lock:
            self.fallbacks[stage] = self.fallbacks.get(stage, 0) + 1
            self._event("learner_error", stage=stage,
                        error=type(error).__name__ if error is not None else None)

    # --- observations --------------------------------------------------

    def observe(
        self, call_id: str | None, choice: LearnerChoice,
        outcome_signals: Sequence[str] | None = None,
    ) -> None:
        """Record a call the learner routed (the router calls this)."""
        with self._lock:
            if choice.learner_version != self.active_version:
                return  # routed by a snapshot that is gone
            key = (choice.use_case, choice.sensitivity)
            obs = _Obs(choice.learner_version, choice.mode,
                       changed=choice.model != choice.policy_model)
            g = self._groups.setdefault(key, _Group())
            cid = call_id if isinstance(call_id, str) else f"_anon{id(obs)}"
            g.obs[cid] = obs
            while len(g.obs) > self.guard.window:
                g.obs.popitem(last=False)
            self._pending[cid] = (key, obs)
            while len(self._pending) > _MAX_PENDING:
                self._pending.popitem(last=False)
            if obs.changed and obs.mode == "exploit":
                g.treated += 1
            self._apply(key, obs, None, outcome_signals)

    def record_outcome(
        self, call_id: str, *, quality: float | None = None,
        outcome_signals: Sequence[str] | None = None,
    ) -> bool:
        """Add a later signal for a call the learner routed: an explicit
        quality in [0, 1] (feedback, eval parity, shadow eval) and/or more
        outcome signals (``regenerate``, ``retry`` ...). False when the call
        is unknown (not learner-routed, too old, or another snapshot's)."""
        with self._lock:
            entry = self._pending.get(call_id)
            if entry is None:
                return False
            key, obs = entry
            if obs.version != self.active_version:
                return False
            self._apply(key, obs, quality, outcome_signals)
            return True

    def _apply(self, key, obs: _Obs, quality, signals) -> None:
        q = _finite(quality)
        if q is not None and 0.0 <= q <= 1.0:
            obs.quality, obs.explicit = q, True
        if signals is not None:
            names = {s for s in signals if isinstance(s, str)}
            if names & STRONG_FAILURES or CALL_FAILED in names:
                obs.failure = True
            if not obs.explicit:
                implicit = (
                    0.0 if CALL_FAILED in names
                    else row_quality({"outcome_signals": sorted(names)})[0]
                )
                obs.quality = implicit if obs.quality is None else min(obs.quality, implicit)
        self._evaluate(key)

    def _evaluate(self, key) -> None:
        g = self._groups.get(key)
        if g is None:
            return
        cfg = self.guard
        version = self.active_version
        obs = [o for o in g.obs.values() if o.version == version]
        treated = [o for o in obs if o.changed and o.mode == "exploit"]
        if len(treated) < cfg.min_samples:
            return
        control = [o for o in obs if not o.changed]
        fail_t = sum(o.failure for o in treated) / len(treated)
        q_vals = [o.quality for o in treated if o.quality is not None]
        q_t = sum(q_vals) / len(q_vals) if q_vals else None
        ref_q: float | None = None
        if len(control) >= cfg.min_samples:
            fail_ref = sum(o.failure for o in control) / len(control) + cfg.max_failure_increase
            cq = [o.quality for o in control if o.quality is not None]
            ref_q = sum(cq) / len(cq) if cq else None
        else:
            fail_ref = cfg.max_failure_rate
            rec = self._recs.get(key)
            proj = ((rec or {}).get("projection") or {}).get("current") or {}
            ref_q = _finite(proj.get("quality"))
        details = {"use_case": key[0], "sensitivity": key[1],
                   "treated": len(treated), "control": len(control)}
        if fail_t > fail_ref:
            self.rollback("implicit_failure", failure_rate=round(fail_t, 4),
                          threshold=round(fail_ref, 4), **details)
            return
        if q_t is not None and ref_q is not None and q_t < ref_q - cfg.parity_tolerance:
            self.rollback("parity", quality=round(q_t, 4), reference=round(ref_q, 4),
                          **details)
            return
        if self._active is None or version in self.good_versions:
            return
        # Promotion needs evidence from every group the snapshot moved calls
        # in, each on its own: one busy group cannot promote it for others.
        need = max(cfg.promote_after, cfg.min_samples)
        moved = {k: grp.treated for k, grp in self._groups.items() if grp.treated}
        if moved and all(n >= need for n in moved.values()):
            self._good.append(self._active)
            self._event("promoted", version=version, treated=sum(moved.values()),
                        groups={f"{k[0]}/{k[1]}": n for k, n in sorted(moved.items())})

    def _observe_context(self, use_case: str, level: str, x: Sequence[float]) -> None:
        g = self._groups.setdefault((use_case, level), _Group())
        g.contexts.append(tuple(float(v) for v in x))
        while len(g.contexts) > self.guard.window:
            g.contexts.popleft()
        if self.frozen or len(g.contexts) < self.guard.min_samples:
            return
        mean = self._train_means.get((use_case, level))
        if not mean:
            return
        n = len(g.contexts)
        live = [sum(c[j] for c in g.contexts) / n for j in range(len(mean))]
        shift = max((abs(live[j] - mean[j]) for j in range(1, len(mean))), default=0.0)
        if shift > self.guard.drift_threshold:
            self._freeze([f"drift:{use_case}/{level}:{shift:.3f}"])


# ─── heuristics on snapshots ──────────────────────────────────────────


def _training_mean(group: Mapping[str, Any], ridge: float) -> list[float] | None:
    """Weighted mean context of a group's scored training rows, from its
    arms' ``A = ridge·I + Σ w·x·xᵀ`` (the bias feature is always 1, so row 0
    holds Σw and Σw·x)."""
    arms = (group.get("arms") or {}).values()
    total = None
    weight = 0.0
    for s in arms:
        a = s.get("A") or []
        if not a:
            continue
        row = [float(v) for v in a[0]]
        if total is None:
            total = [0.0] * len(row)
        weight += row[0] - ridge
        for j in range(1, len(row)):
            total[j] += row[j]
    if total is None or weight <= 1e-9:
        return None
    total[0] = weight
    return [v / weight for v in total]


def _arm_quality(stats: Mapping[str, Any], x: Sequence[float]) -> float | None:
    try:
        low = _cholesky([[float(v) for v in row] for row in stats["A"]])
        theta = _solve(low, [float(v) for v in stats["b"]["quality"]])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    return _dot(x, theta)


def poisoning_signals(
    old: Mapping[str, Any], new: Mapping[str, Any], guard: GuardConfig | None = None
) -> list[str]:
    """Reasons to distrust ``new`` against the active ``old`` snapshot."""
    cfg = guard or GuardConfig()
    out = []
    ridge = float((old.get("config") or {}).get("ridge", 1.0))
    warm = new.get("parent_version") == old.get("learner_version")
    old_groups = {(g.get("use_case"), g.get("sensitivity")): g for g in old.get("groups") or ()}
    for g in new.get("groups") or ():
        key = (g.get("use_case"), g.get("sensitivity"))
        og = old_groups.get(key)
        if og is None:
            continue
        x = _training_mean(og, ridge)
        label = f"{key[0] or ''}/{key[1]}"
        for arm, ns in (g.get("arms") or {}).items():
            os_ = (og.get("arms") or {}).get(arm)
            if not os_:
                continue
            old_n, new_n = int(os_.get("n", 0)), int(ns.get("n", 0))
            if warm and old_n >= cfg.min_samples and new_n > cfg.poison_volume_ratio * old_n:
                out.append(f"volume_spike:{label}:{arm}:{old_n}->{new_n}")
            if x is None:
                continue
            q_old, q_new = _arm_quality(os_, x), _arm_quality(ns, x)
            if q_old is not None and q_new is not None and abs(q_new - q_old) > cfg.poison_quality_jump:
                out.append(f"quality_jump:{label}:{arm}:{q_old:.3f}->{q_new:.3f}")
    return out


__all__ = [
    "CALL_FAILED",
    "DEFAULT_EXPLORE_RATE",
    "GATE_MAX_DELTA_POINTS",
    "GATE_MIN_ACCURACY",
    "GATE_MIN_EVALUATED",
    "GATE_MIN_REALISED_CALLS",
    "GATE_QUALITY_TOLERANCE",
    "MAX_EXPLORE_RATE",
    "AutoGateError",
    "AutoLearner",
    "AutoModeError",
    "GuardConfig",
    "LearnerChoice",
    "LearningFrozen",
    "TrackRecord",
    "fetch_recommendations",
    "poisoning_signals",
    "track_record",
]
