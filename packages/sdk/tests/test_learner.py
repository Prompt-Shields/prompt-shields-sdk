"""Contextual-bandit learner, recommend mode (#47) and snapshots (#48).

Deterministic synthetic traffic: each test builds logged calls from a seeded
RNG with a known best arm and checks the learner finds it, stays inside the
route policy and is reproducible.
"""

from __future__ import annotations

import json
import random

import pytest
from prompt_shields import learner
from prompt_shields.evals.shadow import ShadowResult
from prompt_shields.learner import (
    LearnerConfig,
    RewardWeights,
    context_vector,
    policy_candidates,
    row_quality,
    train,
    verify_snapshot,
)

FRONTIER = "openai:gpt-4o"
SMALL = "mistral:mistral-small"
CHEAP_US = "openai:gpt-4o-mini"

POLICY = {
    "version": "test-policy-1",
    "sensitivity": {"unclassified_default": "internal"},
    "data_policy": {
        "confidential": {"jurisdictions": ["EEA"]},
        "restricted": {"jurisdictions": ["DE"], "require_zdr": True},
    },
    "group_order": ["cheap", "frontier"],
    "default_group": "cheap",
    "quality_to_group": {},
    "groups": {
        "cheap": [
            {"provider": "openai", "model": "gpt-4o-mini", "jurisdiction": "US",
             "input_cost_per_1k": 0.00015, "output_cost_per_1k": 0.0006},
            {"provider": "mistral", "model": "mistral-small", "jurisdiction": "FR",
             "input_cost_per_1k": 0.0002, "output_cost_per_1k": 0.0006},
        ],
        "frontier": [
            {"provider": "openai", "model": "gpt-4o", "jurisdiction": "IE",
             "input_cost_per_1k": 0.0025, "output_cost_per_1k": 0.01},
        ],
    },
    "use_cases": {
        "legal-review": {"min_group": "frontier"},
    },
}


def _features(rng: random.Random, code_share: float) -> dict:
    code = rng.random() < code_share
    tokens = rng.randint(200, 3000)
    return {
        "feature_schema": "ps-prompt-features.v1",
        "approx_input_tokens": tokens,
        "message_count": rng.randint(1, 6),
        "has_code": code,
        "has_json_schema": False,
        "language": "en",
        "use_case": "support",
        "complexity": "complex" if code or tokens > 2000 else "simple",
    }


def _traffic(
    *,
    seed: int = 7,
    n: int = 1000,
    arms: dict[str, float],
    code_quality: dict[str, float] | None = None,
    code_share: float = 0.2,
    use_case: str = "support",
    classification: str = "confidential",
    score_key: str = "feedback_score",
) -> list[dict]:
    """``arms`` maps a label to its share of traffic; quality per arm is
    ``base`` for plain prompts and ``code_quality`` for code prompts."""
    base = {FRONTIER: 0.92, SMALL: 0.92, CHEAP_US: 0.95}
    code_q = code_quality or {}
    rng = random.Random(seed)
    labels = sorted(arms)
    weights = [arms[a] for a in labels]
    out = []
    for i in range(n):
        arm = rng.choices(labels, weights)[0]
        f = _features(rng, code_share)
        f["use_case"] = use_case
        q = code_q.get(arm, base[arm]) if f["has_code"] else base[arm]
        provider, model = arm.split(":")
        out.append({
            "schema": "ps-learning-tuple.v0",
            "call_id": f"c{i}",
            "use_case": use_case,
            "data_classification": classification,
            "features": f,
            "provider": provider,
            "model": model,
            # A graded score around the arm's quality (low noise, so a
            # few hundred rows settle the comparison).
            score_key: round(min(max(rng.gauss(q, 0.05), 0.0), 1.0), 4),
            "tokens_in": f["approx_input_tokens"],
            "tokens_out": rng.randint(100, 600),
            "latency_ms": rng.randint(400, 1500),
        })
    return out


def test_policy_candidates_follow_data_policy_and_use_case_bar():
    assert policy_candidates(POLICY, "support", "internal") == [SMALL, FRONTIER, CHEAP_US]
    # Confidential: EEA only, so the US tier is out.
    assert policy_candidates(POLICY, "support", "confidential") == [SMALL, FRONTIER]
    # min_group frontier.
    assert policy_candidates(POLICY, "legal-review", "confidential") == [FRONTIER]
    assert policy_candidates(POLICY, "support", "restricted") == []


def test_level_named_unclassified_keeps_its_data_policy_rule():
    # A custom level literally called "unclassified" must keep its rule; it
    # must not be read as "no sensitivity section" (no constraint).
    policy = json.loads(json.dumps(POLICY))
    policy["sensitivity"] = {
        "levels": ["unclassified", "public"], "unclassified_default": "unclassified",
    }
    policy["data_policy"] = {
        "unclassified": {"jurisdictions": ["DE"], "require_zdr": True},
    }
    assert policy_candidates(policy, None, "unclassified") == []
    policy["data_policy"]["unclassified"] = {"jurisdictions": ["EEA"]}
    assert policy_candidates(policy, None, "unclassified") == [SMALL, FRONTIER]


def test_finds_the_cheaper_arm_at_parity():
    rows = _traffic(arms={FRONTIER: 0.7, SMALL: 0.3})
    snap = train(rows, POLICY)
    (rec,) = snap["recommendations"]
    assert rec["current_model"] == FRONTIER
    assert rec["recommended_model"] == SMALL
    assert rec["use_case"] == "support" and rec["sensitivity"] == "confidential"
    p = rec["projection"]
    assert p["prob_best"] >= 0.8
    assert p["savings_pct"] > 0.8  # tier prices: mistral-small is far cheaper
    assert p["savings_total"] == pytest.approx(p["savings_per_call"] * p["calls"], rel=1e-6)
    assert p["quality_delta"] > -0.02
    assert p["recommended"]["cost_method"] == "priced"
    assert p["recommended"]["quality_method"] == "dm"
    assert rec["candidates"] == [SMALL, FRONTIER]
    (group,) = snap["groups"]
    assert group["status"] == "recommend" and group["best_model"] == SMALL


def test_keeps_the_better_arm_when_the_cheap_one_is_worse():
    rows = _traffic(arms={FRONTIER: 0.7, SMALL: 0.3}, code_quality={SMALL: 0.3},
                    code_share=0.8)
    snap = train(rows, POLICY)
    assert snap["recommendations"] == []
    (group,) = snap["groups"]
    assert group["best_model"] == FRONTIER
    assert group["status"] == "keep_current"


def test_context_matters():
    """Same arms and qualities, different prompt mix: the small model is
    weaker on code, so code-heavy traffic keeps the frontier model and plain
    traffic moves to the small one."""
    weak_on_code = {SMALL: 0.85}
    plain = _traffic(arms={FRONTIER: 0.6, SMALL: 0.4}, code_quality=weak_on_code,
                     code_share=0.1)
    code = _traffic(arms={FRONTIER: 0.6, SMALL: 0.4}, code_quality=weak_on_code,
                    code_share=0.9)
    assert [r["recommended_model"] for r in train(plain, POLICY)["recommendations"]] == [SMALL]
    snap = train(code, POLICY)
    assert snap["recommendations"] == []
    assert snap["groups"][0]["status"] == "quality_below_parity"


def test_never_recommends_outside_the_policy():
    # The US model is best and cheapest but not allowed for confidential data.
    rows = _traffic(arms={FRONTIER: 0.5, CHEAP_US: 0.5})
    snap = train(rows, POLICY)
    assert snap["recommendations"] == []
    (group,) = snap["groups"]
    assert CHEAP_US not in group["candidates"]
    assert group["status"] == "keep_current"
    # Internal data may use it.
    rows = _traffic(arms={FRONTIER: 0.5, CHEAP_US: 0.5}, classification="internal")
    (rec,) = train(rows, POLICY)["recommendations"]
    assert rec["recommended_model"] == CHEAP_US
    # A use case with min_group frontier never moves down.
    rows = _traffic(arms={FRONTIER: 0.6, SMALL: 0.4}, use_case="legal-review")
    snap = train(rows, POLICY)
    assert snap["recommendations"] == []
    assert snap["groups"][0]["candidates"] == [FRONTIER]


def test_sensitivity_follows_the_router_rules():
    # An unknown label is unclassified (internal here); PII escalates it.
    rows = _traffic(arms={FRONTIER: 0.5, CHEAP_US: 0.5}, classification="top-secret", n=200)
    assert {g["sensitivity"] for g in train(rows, POLICY)["groups"]} == {"internal"}
    policy = {**POLICY, "sensitivity": {"unclassified_default": "internal",
                                        "pii_escalation": {"email": "confidential"}}}
    for r in rows:
        r["pii_detected"] = ["email"]
    (group,) = train(rows, policy)["groups"]
    assert group["sensitivity"] == "confidential"
    assert CHEAP_US not in group["candidates"]


def test_non_compliant_current_route_is_flagged():
    rows = _traffic(arms={CHEAP_US: 0.8, SMALL: 0.2})
    (group,) = train(rows, POLICY)["groups"]
    assert group["current_model"] == CHEAP_US
    assert group["current_compliant"] is False


def test_too_little_data_recommends_nothing():
    rows = _traffic(arms={FRONTIER: 0.95, SMALL: 0.05}, n=200)
    snap = train(rows, POLICY, LearnerConfig(min_arm_samples=30))
    assert snap["recommendations"] == []
    # The small model has too few rows to be compared: keep the current one.
    assert snap["groups"][0]["status"] == "keep_current"
    assert snap["groups"][0]["unevaluated_candidates"] == [SMALL]


def test_unscored_rows_count_as_traffic_only():
    rows = _traffic(arms={FRONTIER: 0.7, SMALL: 0.3})
    for r in rows:
        del r["feedback_score"]
    snap = train(rows, POLICY)
    assert snap["data"]["scored_rows"] == 0
    assert snap["groups"][0]["calls"] == 1000
    assert snap["groups"][0]["status"] == "insufficient_data"


def test_lambda_trades_cost_against_quality():
    # Small is a bit worse (0.85 vs 0.92): recommended when cost matters,
    # not when quality dominates (λ near zero) or parity is strict.
    rows = _traffic(arms={FRONTIER: 0.6, SMALL: 0.4}, n=1500,
                    code_quality={SMALL: 0.85, FRONTIER: 0.92}, code_share=1.0)
    loose = LearnerConfig(reward=RewardWeights(lambda_cost=200.0), quality_tolerance=0.2)
    assert [r["recommended_model"] for r in train(rows, POLICY, loose)["recommendations"]] == [SMALL]
    quality_first = LearnerConfig(reward=RewardWeights(lambda_cost=0.01),
                                  quality_tolerance=0.2)
    assert train(rows, POLICY, quality_first)["recommendations"] == []
    strict = LearnerConfig(reward=RewardWeights(lambda_cost=200.0))
    snap = train(rows, POLICY, strict)
    assert snap["recommendations"] == []
    assert snap["groups"][0]["status"] == "quality_below_parity"
    # Per-use-case weights override the default.
    per_uc = LearnerConfig(reward=RewardWeights(lambda_cost=0.01), quality_tolerance=0.2,
                           use_case_rewards={"support": RewardWeights(lambda_cost=200.0)})
    assert len(train(rows, POLICY, per_uc)["recommendations"]) == 1


def test_doubly_robust_with_propensities():
    rows = _traffic(arms={FRONTIER: 0.7, SMALL: 0.3})
    for r in rows:
        r["propensity"] = 0.7 if r["model"] == "gpt-4o" else 0.3
    (rec,) = train(rows, POLICY)["recommendations"]
    assert rec["projection"]["recommended"]["quality_method"] == "dr"
    assert rec["recommended_model"] == SMALL


def test_snapshot_is_reproducible_and_versioned():
    rows = _traffic(arms={FRONTIER: 0.7, SMALL: 0.3})
    a = train(rows, POLICY)
    b = train(list(reversed(rows)), POLICY)
    assert a == b
    assert a["learner_version"].startswith("lv1-") and len(a["learner_version"]) == 20
    assert verify_snapshot(a)
    assert a["mode"] == "recommend"
    assert a["policy_version"] == "test-policy-1"
    # JSON round trip keeps the version valid; any edit breaks it.
    c = json.loads(learner.canonical_json(a))
    assert verify_snapshot(c)
    c["recommendations"][0]["recommended_model"] = CHEAP_US
    assert not verify_snapshot(c)
    # Another seed or another config is another version.
    assert train(rows, POLICY, LearnerConfig(seed=1))["learner_version"] != a["learner_version"]
    # No prompt text or call ids in the snapshot.
    assert "c0" not in json.dumps(a)


def test_warm_start_from_a_prior_snapshot():
    cfg = LearnerConfig(min_arm_samples=50)
    first = train(_traffic(arms={FRONTIER: 0.7, SMALL: 0.3}, n=120, seed=1), POLICY, cfg)
    assert first["recommendations"] == []  # too few small-model rows alone
    second = train(_traffic(arms={FRONTIER: 0.7, SMALL: 0.3}, n=120, seed=2), POLICY, cfg,
                   prior=first)
    assert second["parent_version"] == first["learner_version"]
    arms = second["groups"][0]["arms"]
    assert arms[SMALL]["n"] == (
        first["groups"][0]["arms"][SMALL]["n"]
        + sum(1 for r in _traffic(arms={FRONTIER: 0.7, SMALL: 0.3}, n=120, seed=2)
              if r["model"] == "mistral-small")
    )
    assert [r["recommended_model"] for r in second["recommendations"]] == [SMALL]
    with pytest.raises(ValueError, match="ridge"):
        train([], POLICY, LearnerConfig(ridge=2.0), prior=first)


def test_quality_sources_and_weights():
    assert row_quality({"shadow_eval_score": 0.2, "feedback_score": 1}) == (0.2, "shadow_eval")
    assert row_quality({"feedback_score": 1}) == (1.0, "feedback")
    assert row_quality({"quality_score": 0.7}) == (0.7, "eval")
    assert row_quality({"outcome_signals": ["refusal"]}) == (0.0, "implicit")
    assert row_quality({"outcome_signals": ["regenerate"]}) == (0.5, "implicit")
    assert row_quality({"outcome_signals": []}) == (1.0, "implicit")
    assert row_quality({"quality_score": None}) == (None, None)
    assert row_quality({"feedback_score": 3}) == (None, None)
    assert learner.SOURCE_WEIGHTS["implicit"] < learner.SOURCE_WEIGHTS["shadow_eval"]


def test_context_vector_has_no_text_and_fixed_size():
    v = context_vector({"approx_input_tokens": 100, "has_code": True, "language": "en"})
    assert len(v) == len(learner.CONTEXT_FEATURES)
    assert v[0] == 1.0 and v[3] == 1.0 and v[6] == 1.0
    assert context_vector(None) == (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def test_shadow_results_and_feedback_join():
    shadow = [
        ShadowResult(use_case="support", sensitivity="confidential", primary=FRONTIER,
                     candidate=SMALL, status="judged", outcome="win", candidate_cost=0.001,
                     candidate_latency_ms=300),
        ShadowResult(use_case="support", sensitivity="confidential", primary=FRONTIER,
                     candidate=SMALL, status="error"),
    ]
    (t,) = learner.shadow_tuples(shadow)
    assert (t["provider"], t["model"], t["shadow_eval_score"]) == ("mistral", "mistral-small", 1.0)
    joined = learner.join_feedback(
        [{"call_id": "a"}, {"call_id": "b", "feedback_score": 0.0}],
        [{"call_id": "a", "score": 1}, {"call_id": "b", "score": 1}],
    )
    assert joined[0]["feedback_score"] == 1 and joined[1]["feedback_score"] == 0.0


def test_requires_a_policy():
    with pytest.raises(ValueError, match="route_policy"):
        train([], {})


def test_cli_writes_a_snapshot(tmp_path, capsys):
    tuples = tmp_path / "t.jsonl"
    tuples.write_text("".join(json.dumps(r) + "\n" for r in
                              _traffic(arms={FRONTIER: 0.7, SMALL: 0.3})))
    policy = tmp_path / "p.json"
    policy.write_text(json.dumps(POLICY))
    out = tmp_path / "snap.json"
    assert learner.main(["--tuples", str(tuples), "--policy", str(policy),
                         "--out", str(out)]) == 0
    snap = json.loads(out.read_text())
    assert verify_snapshot(snap)
    assert f"recommend {FRONTIER} -> {SMALL}" in capsys.readouterr().out
