"""Routing learner auto mode (#34): the enablement gate, exploration inside
the policy-safe set, auto-rollback, freezing on drift and poisoning, and the
router integration (local mode only, off by default). No network."""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletion
from prompt_shields import RouteHint, RouterError, ShieldsRouter
from prompt_shields.learner import (
    VERSION_PREFIX,
    LearnerConfig,
    policy_candidates,
    snapshot_digest,
    train,
)
from prompt_shields.learner_auto import (
    DEFAULT_EXPLORE_RATE,
    AutoGateError,
    AutoLearner,
    AutoModeError,
    GuardConfig,
    LearningFrozen,
    poisoning_signals,
    track_record,
)
from prompt_shields.route_policy import policy_safe_tiers

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "schemas" / "route-policy"
NORDIC = json.loads((SCHEMA_DIR / "examples" / "nordic-sovereign.json").read_text())
PROVIDERS = {
    "openai": {"api_key": "sk-openai"},
    "anthropic": {"api_key": "sk-ant"},
    "scaleway": {"api_key": "sk-scw", "base_url": "https://scw.example/v1"},
    "mistral-eu": {"api_key": "sk-mis", "base_url": "https://mis.example/v1"},
    "berget": {"api_key": "sk-berget", "base_url": "https://api.berget.ai/v1"},
}
SCALEWAY = "scaleway:scaleway-small"
MISTRAL_SMALL = "mistral-eu:mistral-small-eu"
CONFIDENTIAL_SAFE = {
    SCALEWAY, MISTRAL_SMALL, "berget:berget-llama-70b", "mistral-eu:mistral-large-eu",
}


def _rec(rid: str, *, status="accepted", calls=500, delta=1.0, fq=None, pq=0.9,
         use_case="support"):
    return {
        "id": rid, "use_case": use_case, "status": status,
        "realised": None if status != "accepted" else {
            "calls": calls, "delta_points": delta,
            "feedback_quality": fq, "projected_quality": pq,
        },
    }


GOOD_RECORD = {"data": [_rec("r1"), _rec("r2", delta=-3.5)]}


def _snapshot(recs=(), groups=(), **extra) -> dict:
    snap = {
        "schema": "ps-learner-snapshot.v1",
        "mode": "recommend",
        "algorithm": "linear-thompson.v1",
        "feature_schema": "ps-prompt-features.v1",
        "context_features": ["bias"],
        "config": {"ridge": 1.0},
        "policy_version": "nordic-sovereign-1",
        "parent_version": None,
        "data": {"fingerprint": "0" * 64, "rows": 0, "scored_rows": 0,
                 "skipped": {"no_model": 0}},
        "groups": list(groups),
        "recommendations": list(recs),
        **extra,
    }
    snap["learner_version"] = VERSION_PREFIX + snapshot_digest(snap)[:16]
    return snap


def _recommend(model=MISTRAL_SMALL, use_case="support", sensitivity="confidential",
               current_quality=0.9):
    return {
        "use_case": use_case, "sensitivity": sensitivity,
        "current_model": SCALEWAY, "recommended_model": model,
        "candidates": sorted(CONFIDENTIAL_SAFE),
        "projection": {"current": {"quality": current_quality},
                       "recommended": {"quality": current_quality}},
    }


def _learner(**kwargs) -> AutoLearner:
    kwargs.setdefault("explore_rate", 0.0)
    return AutoLearner({
        uc: [_rec(f"{uc}-1", use_case=uc), _rec(f"{uc}-2", use_case=uc)]
        for uc in ("support", "claim-summary", "legal-review")
    }, **kwargs)


# --- fake providers ------------------------------------------------------------


def _response(model: str) -> ChatCompletion:
    return ChatCompletion.model_validate({
        "id": "c1", "object": "chat.completion", "created": 1700000000, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    })


def _router(**kwargs):
    kwargs.setdefault("ps_api_key", "ps-test")
    kwargs.setdefault("providers", PROVIDERS)
    kwargs.setdefault("policy", NORDIC)
    router = ShieldsRouter(**kwargs)
    events: list[dict] = []
    for name in router._providers:
        client = router._client(name)
        calls: list = []

        def respond(_calls=calls, **kw):
            _calls.append(kw)
            return _response(kw["model"])

        client._upstream = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=respond)),
            messages=SimpleNamespace(create=respond),
        )
        client._telemetry.enqueue = events.append
        client._telemetry.flush_sync = lambda: None
    return router, events


_n = 0


def _msg():
    """A fresh prompt per call (a repeated one is a `regenerate` signal)."""
    global _n
    _n += 1
    return [{"role": "user", "content": f"question number {_n}"}]


# --- the gate --------------------------------------------------------------------


def test_track_record_counts_accurate_accepted_recommendations():
    tr = track_record(GOOD_RECORD, "support")
    assert (tr.accepted, tr.evaluated, tr.accurate, tr.eligible) == (2, 2, 2, True)
    # A list works too; duplicates, other use cases and rejections are counted apart.
    tr = track_record(
        [_rec("r1"), _rec("r1"), _rec("x", use_case="billing"), _rec("r3", status="rejected"),
         _rec("r4", status="pending")],
        "support",
    )
    assert (tr.accepted, tr.rejected, tr.evaluated, tr.eligible) == (1, 1, 1, False)


@pytest.mark.parametrize("records, why", [
    ({"data": []}, "0 accepted"),
    ({"data": [_rec("a"), _rec("b", calls=10)]}, "1 accepted"),  # too few realised calls
    ({"data": [_rec("a"), _rec("b", delta=None)]}, "1 accepted"),
    ({"data": [_rec("a"), _rec("b", delta=12.0)]}, "1 of 2"),  # savings off by 12 points
    ({"data": [_rec("a"), _rec("b", fq=0.7, pq=0.9)]}, "1 of 2"),  # quality below projection
    # An eligible flag in the input is not trusted: the counts decide.
    ({"data": [{"id": "z", "use_case": "support", "status": "accepted",
                "eligible": True}]}, "0 accepted"),
])
def test_gate_refuses_use_cases_without_an_accurate_track_record(records, why):
    with pytest.raises(AutoGateError, match=why):
        AutoLearner({"support": records})


def test_explore_rate_is_bounded_and_defaults_to_five_percent():
    assert AutoLearner({"support": GOOD_RECORD}).explore_rate == DEFAULT_EXPLORE_RATE == 0.05
    for bad in (-0.1, 0.5, float("nan")):
        with pytest.raises(AutoModeError):
            AutoLearner({"support": GOOD_RECORD}, explore_rate=bad)
    with pytest.raises(AutoModeError):
        AutoLearner({})


def test_explore_rate_is_zero_for_restricted_and_frontier_use_cases():
    al = AutoLearner({"support": GOOD_RECORD})
    assert al.explore_rate_for(NORDIC, "support", "confidential") == 0.05
    assert al.explore_rate_for(NORDIC, "support", "restricted") == 0.0
    assert al.explore_rate_for(NORDIC, "legal-review", "public") == 0.0  # min_group frontier
    custom = {**NORDIC, "sensitivity": {**NORDIC["sensitivity"],
                                        "levels": ["open", "secret"]}}
    assert al.explore_rate_for(custom, "support", "secret") == 0.0  # highest level
    al.freeze("test")
    assert al.explore_rate_for(NORDIC, "support", "confidential") == 0.0


# --- the policy-safe set ------------------------------------------------------------


def test_safe_set_is_shared_with_the_offline_learner():
    labels = {f"{t['provider']}:{t['model']}"
              for _, t in policy_safe_tiers(NORDIC, "confidential", "support")}
    assert labels == CONFIDENTIAL_SAFE
    assert set(policy_candidates(NORDIC, "support", "confidential")) == labels
    assert set(policy_candidates(NORDIC, "claim-summary", "confidential")) == {
        MISTRAL_SMALL, "berget:berget-llama-70b"}
    assert set(policy_candidates(NORDIC, "legal-review", "restricted")) == {
        "mistral-eu:mistral-large-eu"}


# --- router integration ---------------------------------------------------------------


def test_router_without_learner_is_unchanged_and_gateway_refuses_one():
    router, events = _router()
    resp = router.chat.completions.create(messages=_msg(), use_case="support",
                                          data_classification="confidential")
    assert resp.ps.route.learner is None and router.learner is None
    assert "route_propensity" not in events[0]
    with pytest.raises(RouterError, match="mode='local'"):
        ShieldsRouter(ps_api_key="k", mode="gateway", gateway_url="http://gw",
                      learner=_learner())
    with pytest.raises(RouterError, match="AutoLearner"):
        _router(learner=object())


def test_exploit_routes_to_the_recommendation_inside_the_safe_set():
    snap = _snapshot([_recommend()])
    router, events = _router(learner=_learner(snapshot=snap))
    resp = router.chat.completions.create(messages=_msg(), use_case="support",
                                          data_classification="confidential")
    r = resp.ps.route
    assert (r.provider, r.model) == ("mistral-eu", "mistral-small-eu")
    assert r.reason == "heuristic:simple+learner-exploit"
    assert r.learner.mode == "exploit" and r.learner.propensity == 1.0
    assert r.learner.policy_model == SCALEWAY
    assert r.learner.learner_version == snap["learner_version"]
    (ev,) = events
    assert ev["route_learner_mode"] == "exploit" and ev["route_propensity"] == 1.0
    assert ev["route_learner_version"] == snap["learner_version"]
    assert ev["route_learner_frozen"] is False and r.learner.frozen is False
    assert ev["served_provider"] == "mistral-eu"
    # Not enabled for this use case, or a pinned model, or a hint: untouched.
    other = router.chat.completions.create(messages=_msg(), use_case="billing",
                                           data_classification="confidential")
    assert other.ps.route.learner is None and other.ps.route.model == "scaleway-small"
    hinted = router.chat.completions.create(
        messages=_msg(), use_case="support", data_classification="confidential",
        route=RouteHint(quality="balanced"))
    assert hinted.ps.route.learner is None
    pinned = router.chat.completions.create(
        messages=_msg(), model="scaleway-small", use_case="support",
        data_classification="confidential")
    assert pinned.ps.route.learner is None
    concrete = router.chat.completions.create(
        messages=_msg(), model="scaleway-small", use_case="support",
        data_classification="confidential", route=RouteHint(max_cost=1.0))
    assert concrete.ps.route.learner is None


def test_async_router_routes_with_the_learner():
    import asyncio

    from prompt_shields import AsyncShieldsRouter

    al = _learner(snapshot=_snapshot([_recommend()]))
    router = AsyncShieldsRouter(ps_api_key="ps-test", providers=PROVIDERS, policy=NORDIC,
                                learner=al)
    events: list[dict] = []
    for name in router._providers:
        client = router._client(name)

        async def respond(**kw):
            return _response(kw["model"])

        async def noop():
            return None

        client._upstream = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=respond)))
        client._telemetry.enqueue = events.append
        client._telemetry.flush = noop
    resp = asyncio.run(router.chat.completions.create(
        messages=_msg(), use_case="support", data_classification="confidential"))
    assert resp.ps.route.model == "mistral-small-eu"
    assert resp.ps.route.learner.mode == "exploit"
    assert events[0]["route_learner_mode"] == "exploit"


def test_recommendation_outside_the_safe_set_is_ignored():
    # gpt-4o is US-hosted: never allowed for confidential, whatever the snapshot says.
    snap = _snapshot([_recommend(model="openai:gpt-4o")])
    router, _ = _router(learner=_learner(snapshot=snap))
    resp = router.chat.completions.create(messages=_msg(), use_case="support",
                                          data_classification="confidential")
    assert resp.ps.route.model == "scaleway-small"
    assert resp.ps.route.learner.mode == "policy"
    # A restricted call (PII escalation) only ever sees restricted-safe models.
    snap = _snapshot([_recommend(sensitivity="restricted")])
    router, _ = _router(learner=_learner(snapshot=snap, explore_rate=0.2, seed=1))
    for _ in range(30):
        resp = router.chat.completions.create(
            messages=[{"role": "user", "content": f"patient diagnosis {_n} " + "x" * _n}],
            use_case="support", data_classification="restricted")
        assert resp.ps.route.provider in {"berget", "mistral-eu"}
        assert resp.ps.route.learner.explore_rate == 0.0


def test_exploration_stays_in_the_safe_set_with_logged_propensities():
    router, _ = _router(learner=_learner(snapshot=_snapshot([_recommend()]),
                                         explore_rate=0.2, seed=7))
    modes = []
    for _ in range(200):
        resp = router.chat.completions.create(messages=_msg(), use_case="support",
                                              data_classification="confidential")
        lc = resp.ps.route.learner
        assert f"{resp.ps.route.provider}:{resp.ps.route.model}" in CONFIDENTIAL_SAFE
        expected = (0.8 if lc.model == MISTRAL_SMALL else 0.0) + 0.2 / 4
        assert lc.propensity == pytest.approx(expected)
        modes.append(lc.mode)
    assert 15 <= modes.count("explore") <= 65
    # The budget clamp bounds the options too.
    resp = router.chat.completions.create(
        messages=_msg(), use_case="support", data_classification="confidential",
        route=RouteHint(max_cost=0.0000001))
    assert resp.ps.route.model in {"scaleway-small", "mistral-small-eu"}


def test_claim_summary_explores_only_eligible_models():
    snap = _snapshot([_recommend(use_case="claim-summary", model="berget:berget-llama-70b")])
    router, _ = _router(learner=_learner(snapshot=snap, explore_rate=0.2, seed=3))
    seen = set()
    for _ in range(60):
        resp = router.chat.completions.create(messages=_msg(), use_case="claim-summary",
                                              data_classification="confidential")
        seen.add(resp.ps.route.model)
    assert seen <= {"mistral-small-eu", "berget-llama-70b"}
    assert "berget-llama-70b" in seen


# --- auto-rollback -----------------------------------------------------------------------


GUARD = GuardConfig(window=20, min_samples=5, promote_after=10)


def test_rollback_on_implicit_failures_restores_the_policy():
    snap = _snapshot([_recommend()])
    al = _learner(snapshot=snap, guard=GUARD)
    choices = [al.choose("support", "confidential", sorted(CONFIDENTIAL_SAFE), SCALEWAY,
                         NORDIC) for _ in range(5)]
    for i, c in enumerate(choices):
        assert c.mode == "exploit"
        al.observe(f"call{i}", c, ["refusal"] if i % 2 == 0 else [])
    assert al.active_version is None
    (rb,) = [e for e in al.events if e["event"] == "rollback"]
    assert rb["reason"] == "implicit_failure" and rb["version"] == snap["learner_version"]
    assert al.choose("support", "confidential", sorted(CONFIDENTIAL_SAFE), SCALEWAY,
                     NORDIC).mode == "policy"
    # A rolled-back version is never loaded again.
    with pytest.raises(AutoModeError, match="rolled back"):
        al.load_snapshot(snap)


def test_rollback_on_parity_goes_to_the_last_good_snapshot():
    first = _snapshot([_recommend()])
    al = _learner(snapshot=first, guard=GUARD)
    for i in range(10):
        c = al.choose("support", "confidential", sorted(CONFIDENTIAL_SAFE), SCALEWAY, NORDIC)
        al.observe(f"a{i}", c, [])
        al.record_outcome(f"a{i}", quality=0.9)
    assert al.good_versions == [first["learner_version"]]
    second = _snapshot([_recommend(model="berget:berget-llama-70b")])
    al.load_snapshot(second)
    assert al.active_version == second["learner_version"]
    for i in range(5):
        c = al.choose("support", "confidential", sorted(CONFIDENTIAL_SAFE), SCALEWAY, NORDIC)
        assert c.model == "berget:berget-llama-70b"
        al.observe(f"b{i}", c, [])
        al.record_outcome(f"b{i}", quality=0.6)
    assert al.active_version == first["learner_version"]
    rb = [e for e in al.events if e["event"] == "rollback"][-1]
    assert rb["reason"] == "parity" and rb["to"] == first["learner_version"]
    # Outcomes of the rolled-back snapshot's calls are no longer taken.
    assert al.record_outcome("b0", quality=0.0) is False


def test_router_rolls_back_from_recorded_outcomes():
    al = _learner(snapshot=_snapshot([_recommend()]), guard=GUARD)
    router, _ = _router(learner=al)
    for _ in range(5):
        resp = router.chat.completions.create(messages=_msg(), use_case="support",
                                              data_classification="confidential")
        assert resp.ps.route.model == "mistral-small-eu"
        al.record_outcome(resp.raw.ps_call_id, quality=0.2)
    resp = router.chat.completions.create(messages=_msg(), use_case="support",
                                          data_classification="confidential")
    assert al.active_version is None
    assert resp.ps.route.model == "scaleway-small" and resp.ps.route.learner.mode == "policy"


def test_router_takes_outcome_signals_from_the_call():
    al = _learner(snapshot=_snapshot([_recommend()]), guard=GUARD)
    router, _ = _router(learner=al)
    for _ in range(5):
        # The same prompt every time: `regenerate` signals (quality 0.5).
        router.chat.completions.create(messages=[{"role": "user", "content": "same"}],
                                       use_case="support",
                                       data_classification="confidential")
    rb = [e for e in al.events if e["event"] == "rollback"]
    assert rb and rb[0]["reason"] == "parity"



def test_learner_pick_the_request_cannot_go_to_falls_back_to_the_policy():
    # An Anthropic recommendation for a call with OpenAI-only parameters:
    # the call is routed as the policy would, not refused.
    claude = "anthropic:claude-sonnet-4-20250514"
    snap = _snapshot([_recommend(model=claude, sensitivity="internal")])
    al = _learner(snapshot=snap, guard=GUARD)
    router, _ = _router(learner=al)
    assert router.route(_msg(), use_case="support",
                        data_classification="internal").model == "claude-sonnet-4-20250514"
    resp = router.chat.completions.create(
        messages=_msg(), use_case="support", data_classification="internal",
        response_format={"type": "json_object"})
    assert resp.ps.route.provider != "anthropic" and resp.ps.route.learner is None
    # Without such parameters the recommendation is served.
    resp = router.chat.completions.create(messages=_msg(), use_case="support",
                                          data_classification="internal")
    assert resp.ps.route.provider == "anthropic"


def test_calls_that_fail_outright_count_against_the_learner():
    al = _learner(snapshot=_snapshot([_recommend()]), guard=GUARD)
    router, _ = _router(learner=al, failover=False)

    def down(**kw):
        raise RuntimeError("upstream down")

    router._client("mistral-eu")._upstream.chat.completions.create = down
    for _ in range(GUARD.min_samples):
        with pytest.raises(RuntimeError):
            router.chat.completions.create(messages=_msg(), use_case="support",
                                           data_classification="confidential")
    rb = [e for e in al.events if e["event"] == "rollback"]
    assert rb and rb[0]["reason"] == "implicit_failure"
    assert al.active_version is None


# --- freezing ------------------------------------------------------------------------------


def _arm(n: int, mean: list[float], quality: float = 0.9) -> dict:
    d = len(mean)
    a = [[1.0 if i == j else 0.0 for j in range(d)] for i in range(d)]
    a[0] = [1.0 + n] + [n * m for m in mean[1:]]
    for j in range(1, d):
        a[j][0] = a[0][j]
    b_q = [n * quality] + [n * quality * m for m in mean[1:]]
    return {"n": n, "weight": float(n), "A": a,
            "b": {"quality": b_q, "cost": [0.0] * d, "latency": [0.0] * d},
            "S": {"qq": 0.0, "ll": 0.0, "ql": 0.0}}


def test_drift_freezes_learning():
    # Trained on long prompts; live traffic is short ones.
    long_mean = [1.0, 0.9, 0.5, 1.0, 0.0, 1.0, 1.0]
    group = {"use_case": "support", "sensitivity": "confidential",
             "arms": {SCALEWAY: _arm(100, long_mean)}}
    al = _learner(snapshot=_snapshot([_recommend()], groups=[group]), guard=GUARD,
                  explore_rate=0.1)
    router, events = _router(learner=al)
    for _ in range(5):
        router.chat.completions.create(messages=_msg(), use_case="support",
                                       data_classification="confidential")
    assert al.frozen and al.freeze_reasons[0].startswith("drift:support/confidential")
    assert al.explore_rate_for(NORDIC, "support", "confidential") == 0.0
    with pytest.raises(LearningFrozen):
        al.load_snapshot(_snapshot([_recommend(model="berget:berget-llama-70b")]))
    with pytest.raises(LearningFrozen):
        al.retrain([], NORDIC)
    # The active snapshot keeps serving.
    resp = router.chat.completions.create(messages=_msg(), use_case="support",
                                          data_classification="confidential")
    assert resp.ps.route.model == "mistral-small-eu"
    assert resp.ps.route.learner.frozen and events[-1]["route_learner_frozen"] is True
    assert events[-1]["route_propensity"] == 1.0  # no exploration while frozen
    al.unfreeze()
    assert not al.frozen


def _tuples(n_scaleway: int, n_mistral: int, q_mistral: float, seed: int = 0):
    rng = random.Random(seed)
    rows = []
    for (p, m), n, q in (((("scaleway", "scaleway-small")), n_scaleway, 0.9),
                         ((("mistral-eu", "mistral-small-eu")), n_mistral, q_mistral)):
        for _ in range(n):
            rows.append({
                "use_case": "support", "data_classification": "confidential",
                "provider": p, "model": m,
                "features": {"approx_input_tokens": rng.randint(50, 500)},
                "feedback_score": min(max(rng.gauss(q, 0.02), 0), 1),
                "tokens_in": 300, "tokens_out": 100,
            })
    return rows


def test_poisoning_heuristics_freeze_learning_on_load():
    cfg = LearnerConfig(min_arm_samples=10)
    base = train(_tuples(60, 60, 0.9), NORDIC, cfg)
    # A warm-started retrain with ten times the rows on one arm.
    spike = train(_tuples(0, 600, 0.9, seed=1), NORDIC, cfg, prior=base)
    assert any(s.startswith("volume_spike:support/confidential:mistral-eu:mistral-small-eu")
               for s in poisoning_signals(base, spike))
    # A fresh snapshot whose arm quality jumped.
    jump = train(_tuples(60, 60, 0.3, seed=2), NORDIC, cfg)
    assert any(s.startswith("quality_jump:") for s in poisoning_signals(base, jump))
    # An ordinary retrain raises nothing.
    calm = train(_tuples(10, 10, 0.9, seed=3), NORDIC, cfg, prior=base)
    assert poisoning_signals(base, calm) == []

    al = _learner(snapshot=base)
    al.load_snapshot(calm)
    with pytest.raises(LearningFrozen, match="poisoning"):
        al.load_snapshot(jump)
    assert al.frozen and al.active_version == calm["learner_version"]


def test_retrain_warm_starts_from_the_active_snapshot():
    cfg = LearnerConfig(min_arm_samples=10)
    base = train(_tuples(60, 60, 0.9), NORDIC, cfg)
    al = _learner(snapshot=base)
    snap = al.retrain(_tuples(5, 5, 0.9, seed=4), NORDIC, cfg)
    assert snap["parent_version"] == base["learner_version"]
    assert al.active_version == snap["learner_version"]


def test_tampered_or_foreign_snapshots_are_refused():
    snap = _snapshot([_recommend()])
    bad = copy.deepcopy(snap)
    bad["recommendations"][0]["recommended_model"] = "openai:gpt-4o"
    with pytest.raises(AutoModeError, match="learner_version"):
        _learner(snapshot=bad)
    with pytest.raises(AutoModeError, match="recommend-mode"):
        _learner(snapshot=_snapshot(mode="auto"))
