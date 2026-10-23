"""Shadow-eval sampler (issue #46): sampling, policy gate, spend caps, queue,
judging, and that no prompt or answer text leaves the sampler."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest
from prompt_shields.evals import (
    Candidate,
    EvalPolicy,
    FakeExecutor,
    PromptReexecutionNotAllowed,
    ShadowConfig,
    ShadowSampler,
    policy_from_route_policy,
)

PRIMARY = Candidate("openai", "gpt-4o")
MINI = Candidate("openai", "gpt-4o-mini")
HAIKU = Candidate("anthropic", "claude-3-5-haiku-20241022")
JUDGE = Candidate("anthropic", "claude-sonnet-4-20250514")

SECRET_PROMPT = "SECRET-PROMPT-7f3a"
SECRET_ANSWER = "SECRET-ANSWER-91bc"

POLICY = EvalPolicy.from_dict({
    "allowed_models": {
        "public": ["*"],
        "internal": ["openai:gpt-4o-mini", "claude-sonnet-4-20250514"],
        "confidential": ["openai:gpt-4o-mini"],
        "restricted": [],
    }
})


def _responder(candidate, messages):
    if candidate == JUDGE:
        return "VERDICT: TIE"
    return "shadow answer"


def _config(**overrides) -> ShadowConfig:
    base = {"candidates": [MINI, HAIKU], "judge": JUDGE, "policy": POLICY,
            "sample_rate": 1.0, "seed": 7}
    base.update(overrides)
    return ShadowConfig(**base)


def _sampler(executor=None, *, start=False, **overrides) -> ShadowSampler:
    return ShadowSampler(executor or FakeExecutor(_responder), _config(**overrides),
                         start=start, sleep=lambda s: None)


def _offer(sampler, level="public", cost=100.0, text=SECRET_ANSWER, **kw):
    return sampler.offer(
        [{"role": "user", "content": SECRET_PROMPT}],
        model=PRIMARY.model, vendor=PRIMARY.vendor, response_text=text,
        sensitivity_level=level, use_case="support", cost_usd=cost, **kw,
    )


class _Sends(FakeExecutor):
    sends_prompts = True


# --- opt-in and config ------------------------------------------------------


def test_real_executor_needs_opt_in_and_cap():
    with pytest.raises(PromptReexecutionNotAllowed):
        ShadowSampler(_Sends(_responder), _config(), start=False)
    with pytest.raises(ValueError, match="max_spend_usd"):
        ShadowSampler(_Sends(_responder), _config(allow_prompt_reexecution=True),
                      start=False)
    ShadowSampler(_Sends(_responder),
                  _config(allow_prompt_reexecution=True, max_spend_usd=1.0),
                  start=False)


def test_allow_all_policy_refused():
    with pytest.raises(ValueError, match="allowlist"):
        _sampler(policy=EvalPolicy.allow_all())


@pytest.mark.parametrize("share", [0.03, -0.1, float("nan")])
def test_spend_share_capped_at_two_percent(share):
    with pytest.raises(ValueError, match="max_spend_share"):
        _sampler(max_spend_share=share)


def test_defaults():
    cfg = ShadowConfig(candidates=[MINI], judge=JUDGE, policy=POLICY)
    assert cfg.sample_rate == 0.01
    assert cfg.max_spend_share == 0.02
    assert cfg.allow_prompt_reexecution is False


# --- sampling ---------------------------------------------------------------


def test_samples_configured_share():
    sampler = _sampler(sample_rate=0.02, seed=1, queue_size=10_000)
    queued = sum(_offer(sampler) for _ in range(20_000))
    stats = sampler.stats()
    assert stats["offered"] == 20_000
    assert 300 <= stats["sampled"] <= 500
    assert queued == stats["enqueued"] == stats["sampled"]


def test_zero_rate_never_samples():
    sampler = _sampler(sample_rate=0.0)
    assert not any(_offer(sampler) for _ in range(100))
    assert sampler.stats()["sampled"] == 0


# --- end to end: judged pair, primary answer untouched ----------------------


def test_judges_one_alternative_against_primary():
    executor = FakeExecutor(_responder)
    results = []
    sampler = ShadowSampler(executor, _config(), start=False,
                            on_result=results.append, sleep=lambda s: None)
    assert _offer(sampler) is True
    sampler.drain()
    assert len(results) == 1
    result = results[0]
    assert result.status == "judged" and result.outcome == "tie"
    assert result.primary == "openai:gpt-4o"
    assert result.candidate in (MINI.label, HAIKU.label)
    # One alternative call and two judge calls (both orders).
    calls = [c for c, _ in executor.calls]
    assert calls.count(JUDGE) == 2 and len(calls) == 3
    # The judge compared the alternative with the served answer.
    judge_text = json.dumps(executor.calls[1][1])
    assert SECRET_ANSWER in judge_text and "shadow answer" in judge_text
    stats = sampler.stats()
    cell = stats["cells"][0]
    assert cell["judged"] == 1 and cell["ties"] == 1 and cell["acceptable_rate"] == 1.0
    assert result.candidate_cost > 0 and result.judge_cost > 0
    # Shadow spend is reported apart from primary spend.
    assert stats["primary_spend_usd"] == 100.0
    assert stats["shadow_spend_usd"] == pytest.approx(
        result.candidate_cost + result.judge_cost, abs=1e-6)
    assert cell["shadow_cost_usd"] == pytest.approx(stats["shadow_spend_usd"], abs=1e-6)


def test_never_shadows_on_the_primary_model():
    sampler = _sampler(candidates=[PRIMARY])
    assert _offer(sampler) is False
    assert sampler.stats()["skipped_policy"] == 1


# --- sensitivity gate -------------------------------------------------------


@pytest.mark.parametrize("level", ["restricted", "top-secret", "RESTRICTED"])
def test_restricted_and_unknown_labels_never_shadowed(level):
    executor = FakeExecutor(_responder)
    sampler = _sampler(executor)
    assert _offer(sampler, level=level) is False
    sampler.drain()
    assert executor.calls == []
    assert sampler.stats()["skipped_policy"] == 1


def test_unlabelled_uses_unclassified_default():
    policy = EvalPolicy.from_dict({
        "unclassified_default": "internal",
        "allowed_models": {"internal": ["openai:gpt-4o-mini", JUDGE.label]},
    })
    executor = FakeExecutor(_responder)
    sampler = _sampler(executor, policy=policy)
    assert _offer(sampler, level=None) is True  # internal: gpt-4o-mini only
    sampler.drain()
    assert executor.calls[0][0] == MINI


def test_judge_must_be_allowed_at_level():
    executor = FakeExecutor(_responder)
    results = []
    sampler = ShadowSampler(executor, _config(), start=False,
                            on_result=results.append, sleep=lambda s: None)
    assert _offer(sampler, level="confidential") is True  # judge not allowed there
    sampler.drain()
    assert executor.calls == []  # no alternative call that cannot be judged
    assert results[0].status == "skipped_judge_policy"


# --- spend ------------------------------------------------------------------


def test_shadow_spend_capped_at_share_of_primary_spend():
    executor = FakeExecutor(_responder)
    sampler = _sampler(executor)
    # Tiny primary spend: 2% of it cannot cover a worst-case shadow call.
    assert _offer(sampler, cost=0.0001) is True
    sampler.drain()
    assert executor.calls == []
    stats = sampler.stats()
    assert stats["refused_spend_share"] == 1
    assert stats["cells"][0]["skipped"] == 1
    assert stats["shadow_spend_usd"] == 0


def test_spend_share_never_exceeded():
    executor = FakeExecutor(_responder)
    sampler = _sampler(executor, queue_size=1000)
    for _ in range(200):
        _offer(sampler, cost=0.5)
        sampler.drain()
    stats = sampler.stats()
    assert stats["shadow_spend_usd"] > 0
    assert stats["shadow_spend_usd"] <= 0.02 * stats["primary_spend_usd"] + 1e-9


def test_absolute_cap():
    executor = _Sends(_responder)
    sampler = _sampler(executor, allow_prompt_reexecution=True, max_spend_usd=0.0)
    _offer(sampler)
    sampler.drain()
    assert executor.calls == []
    assert sampler.stats()["refused_spend_cap"] == 1


def test_unbounded_prompt_not_sent():
    executor = _Sends(_responder)
    sampler = _sampler(executor, allow_prompt_reexecution=True, max_spend_usd=10.0)
    sampler.offer(
        [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}],
        model="gpt-4o", vendor="openai", response_text="ok",
        sensitivity_level="public", cost_usd=100.0,
    )
    sampler.drain()
    assert executor.calls == []
    assert sampler.stats()["cells"][0]["skipped"] == 1


def test_rate_limited():
    waits = []
    clock = [0.0]
    sampler = ShadowSampler(FakeExecutor(_responder),
                            _config(requests_per_minute=6), start=False,
                            clock=lambda: clock[0], sleep=waits.append)
    _offer(sampler)
    sampler.drain()
    assert len(waits) == 2 and waits[0] == 10.0  # three calls, 10 s apart


# --- never blocks or breaks the primary call --------------------------------


def test_full_queue_drops_and_counts():
    sampler = _sampler(queue_size=2)
    results = [_offer(sampler) for _ in range(5)]
    assert results == [True, True, False, False, False]
    stats = sampler.stats()
    assert stats["dropped_queue_full"] == 3 and stats["queue_depth"] == 2


def test_offer_does_not_wait_for_the_shadow_call():
    release = threading.Event()

    def slow(candidate, messages):
        release.wait(5)
        return _responder(candidate, messages)

    sampler = ShadowSampler(FakeExecutor(slow), _config(queue_size=1),
                            sleep=lambda s: None)
    try:
        assert _offer(sampler) is True
        # The worker is now blocked in the shadow call; offers return at once.
        for _ in range(5):
            _offer(sampler)
        assert sampler.stats()["dropped_queue_full"] >= 1
    finally:
        release.set()
        sampler.close()
    assert sampler.stats()["completed"] >= 1


def test_offer_never_raises():
    sampler = _sampler()
    assert sampler.offer(None, model="gpt-4o", response_text="x",  # type: ignore[arg-type]
                         sensitivity_level="public") is False
    assert sampler.offer_routed([], object()) is False
    assert sampler.stats()["offer_errors"] >= 1


def test_executor_and_callback_failures_are_contained():
    def boom(candidate, messages):
        raise RuntimeError(SECRET_PROMPT)

    def bad_callback(result):
        raise RuntimeError("callback")

    sampler = ShadowSampler(FakeExecutor(boom), _config(), start=False,
                            on_result=bad_callback)
    _offer(sampler)
    sampler.drain()
    stats = sampler.stats()
    assert stats["cells"][0]["errors"] == 1
    assert stats["worker_errors"] == 1
    assert SECRET_PROMPT not in json.dumps(stats)


def test_background_worker_and_close():
    sampler = ShadowSampler(FakeExecutor(_responder), _config(), sleep=lambda s: None)
    _offer(sampler)
    sampler.close()
    assert sampler.stats()["completed"] == 1
    assert _offer(sampler) is False  # closed


# --- no text out ------------------------------------------------------------


def test_no_prompt_or_answer_text_in_stats_or_results():
    results = []
    sampler = ShadowSampler(FakeExecutor(_responder), _config(), start=False,
                            on_result=results.append, sleep=lambda s: None)
    for level in ("public", "internal", "restricted", None):
        _offer(sampler, level=level)
    sampler.drain()
    blob = json.dumps(sampler.stats()) + repr(results)
    for secret in (SECRET_PROMPT, SECRET_ANSWER, "shadow answer"):
        assert secret not in blob


# --- router hook ------------------------------------------------------------


def _routed(text=SECRET_ANSWER, level="internal", cost=50.0):
    route = SimpleNamespace(model="gpt-4o", provider="openai", sensitivity_level=level)
    return SimpleNamespace(
        model="gpt-4o",
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        ps=SimpleNamespace(route=route, cost=SimpleNamespace(usd=cost)),
    )


def test_offer_routed_reads_route_and_cost():
    executor = FakeExecutor(_responder)
    results = []
    sampler = ShadowSampler(executor, _config(), start=False,
                            on_result=results.append, sleep=lambda s: None)
    assert sampler.offer_routed([{"role": "user", "content": "hi"}], _routed(),
                                use_case="faq") is True
    sampler.drain()
    assert results[0].sensitivity == "internal" and results[0].use_case == "faq"
    assert executor.calls[0][0] == MINI  # only allowed alternative at internal
    assert sampler.stats()["primary_spend_usd"] == 50.0


def test_offer_routed_without_text_is_skipped():
    sampler = _sampler()
    assert sampler.offer_routed([{"role": "user", "content": "hi"}],
                                _routed(text=None)) is False
    assert sampler.stats()["skipped_no_answer"] == 1


# --- route-policy.v1 allowlist ----------------------------------------------


def test_policy_from_route_policy_uses_data_policy():
    route_policy = {
        "sensitivity": {"unclassified_default": "internal"},
        "data_policy": {
            "confidential": {"jurisdictions": ["EEA"]},
            "restricted": {"jurisdictions": ["DE"], "require_zdr": True},
        },
        "groups": {
            "cheap": [
                {"provider": "openai", "model": "gpt-4o-mini", "jurisdiction": "US"},
                {"provider": "mistral", "model": "mistral-small", "jurisdiction": "FR"},
                {"model": "no-provider", "jurisdiction": "FR"},
            ],
            "frontier": [
                {"provider": "anthropic", "model": "claude-sonnet-4-20250514",
                 "jurisdiction": "US"},
            ],
        },
    }
    policy = policy_from_route_policy(route_policy)
    assert policy.unclassified_default == "internal"
    assert policy.allowed["public"] == {
        "openai:gpt-4o-mini", "mistral:mistral-small",
        "anthropic:claude-sonnet-4-20250514",
    }
    assert policy.allowed["confidential"] == {"mistral:mistral-small"}
    assert policy.allowed["restricted"] == frozenset()
    sampler = _sampler(policy=policy)
    assert _offer(sampler, level="restricted") is False
    assert _offer(sampler, level="confidential") is False  # only mistral, not a candidate
