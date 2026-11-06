"""ShieldsRouter failover (#21, PRD 0001 §7.5): the router hands the engine
only candidates that passed the data policy, and records what happened.

No network: each provider's instrumented client gets a fake upstream that
fails as told, and its telemetry sender records events instead of sending.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletion
from prompt_shields import (
    AsyncShieldsRouter,
    PolicyViolation,
    RouterError,
    ShieldsRouter,
)
from prompt_shields.failover import BreakerConfig, BreakerRegistry, NoCompliantCandidate

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "schemas" / "route-policy"
NORDIC = json.loads((SCHEMA_DIR / "examples" / "nordic-sovereign.json").read_text())
PROVIDERS = {
    "openai": {"api_key": "sk-openai"},
    "anthropic": {"api_key": "sk-ant"},
    "scaleway": {"api_key": "sk-scw", "base_url": "https://scw.example/v1"},
    "mistral-eu": {"api_key": "sk-mis", "base_url": "https://mis.example/v1"},
    "berget": {"api_key": "sk-berget", "base_url": "https://api.berget.ai/v1"},
}
HI = [{"role": "user", "content": "hi"}]


class ProviderError(Exception):
    """Duck-typed like an SDK APIStatusError (status_code, body, response)."""

    def __init__(self, status, code=None, message="boom", headers=None):
        super().__init__(message)
        self.status_code = status
        self.body = {"error": {"code": code, "message": message}}
        self.response = SimpleNamespace(headers=headers or {})


def _ok(model):
    return ChatCompletion.model_validate({
        "id": "chatcmpl-1", "object": "chat.completion", "created": 1700000000,
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    })


class _Upstream:
    def __init__(self, failures, is_async):
        self.calls = []
        self.failures = failures  # exceptions raised in order, then success

        def respond(**kwargs):
            self.calls.append(kwargs)
            if self.failures:
                raise self.failures.pop(0)
            return _ok(kwargs["model"])

        async def arespond(**kwargs):
            return respond(**kwargs)

        create = arespond if is_async else respond
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
        self.messages = SimpleNamespace(create=create)


def _wire(router, failures=None, is_async=False):
    events, upstreams = [], {}
    for name in router._providers:
        client = router._client(name)
        up = _Upstream(list((failures or {}).get(name, ())), is_async)
        client._upstream = up
        upstreams[name] = up
        client._telemetry.enqueue = events.append
        if is_async:
            async def noop():
                return None
            client._telemetry.flush = noop
        else:
            client._telemetry.flush_sync = lambda: None
    return events, upstreams


def _router(cls=ShieldsRouter, **kwargs):
    kwargs.setdefault("ps_api_key", "ps-test")
    kwargs.setdefault("providers", PROVIDERS)
    kwargs.setdefault("policy", NORDIC)
    # Each test gets its own breakers, so no state leaks between tests.
    if "breakers" not in kwargs and "breaker_config" not in kwargs:
        kwargs["breakers"] = BreakerRegistry()
    return cls(**kwargs)


def _called(upstreams):
    return {n: len(u.calls) for n, u in upstreams.items() if u.calls}


# --- failover within the constraint set ------------------------------------------


def test_server_error_fails_over_to_next_compliant_candidate():
    router = _router()
    events, upstreams = _wire(router, {"scaleway": [ProviderError(503)]})
    resp = router.chat.completions.create(
        messages=HI, data_classification="confidential"
    )
    # gpt-4o-mini is cheapest in the group but not allowed for confidential
    # data: it is never a candidate.
    assert _called(upstreams) == {"scaleway": 1, "mistral-eu": 1}
    assert resp.ps.route.model == "mistral-small-eu"
    assert resp.ps.route.provider == "mistral-eu"
    assert resp.ps.route.group == "cheap"
    assert resp.ps.route.jurisdiction == "EEA"
    assert resp.ps.fallback_chain == ("server_error",)
    first, second = resp.ps.attempts
    assert (first.provider, first.ok, first.status, first.error_class, first.error) == (
        "scaleway", False, 503, "server_error", "ProviderError"
    )
    assert (second.provider, second.model, second.ok) == (
        "mistral-eu", "mistral-small-eu", True
    )
    (event,) = events
    assert event["served_provider"] == "mistral-eu"
    assert event["served_model"] == "mistral-small-eu"
    assert event["fallback_chain"] == ["server_error"]
    assert event["retry_count"] == 1
    assert [a.get("error_class") for a in event["attempts"]] == ["server_error", None]
    assert "boom" not in json.dumps(event)  # classes only, never messages
    assert "fallback" in event["outcome_signals"]


def test_first_attempt_success_records_empty_chain():
    router = _router()
    events, _ = _wire(router)
    resp = router.chat.completions.create(messages=HI, data_classification="confidential")
    assert resp.ps.fallback_chain == ()
    assert len(resp.ps.attempts) == 1 and resp.ps.attempts[0].ok
    (event,) = events
    assert event["fallback_chain"] == []
    assert "attempts" not in event and "retry_count" not in event


def test_every_compliant_candidate_down_fails_closed():
    router = _router()
    events, upstreams = _wire(router, {
        "scaleway": [ProviderError(500)], "mistral-eu": [ProviderError(502)],
    })
    with pytest.raises(NoCompliantCandidate) as err:
        router.chat.completions.create(messages=HI, data_classification="confidential")
    assert isinstance(err.value, PolicyViolation)
    assert err.value.fallback_chain == ["server_error", "server_error"]
    assert isinstance(err.value.__cause__, ProviderError)
    assert _called(upstreams) == {"scaleway": 1, "mistral-eu": 1}
    assert events == []


def test_restricted_data_only_reaches_named_providers():
    router = _router()
    _, upstreams = _wire(router, {"mistral-eu": [ProviderError(503)]})
    with pytest.raises(NoCompliantCandidate):
        router.chat.completions.create(messages=HI, data_classification="restricted")
    # scaleway (EEA, ZDR) is in the group but not on restricted's allow-list.
    assert _called(upstreams) == {"mistral-eu": 1}


def test_pii_escalation_bounds_the_fallbacks():
    """Escalation is worked out before the list is built: declared public,
    but an IBAN makes it confidential, so the US model is never a fallback."""
    router = _router()
    _, upstreams = _wire(router, {
        "scaleway": [ProviderError(503)], "openai": [ProviderError(503)],
    })
    resp = router.chat.completions.create(
        messages=[{"role": "user", "content": "IBAN DE89370400440532013000"}],
        data_classification="public",
    )
    assert resp.ps.route.sensitivity_level == "confidential"
    assert resp.ps.route.sensitivity_source == "pii_escalated"
    assert _called(upstreams) == {"scaleway": 1, "mistral-eu": 1}


def test_each_attempt_uses_its_own_providers_credentials():
    """Never provider A's key on provider B's host."""
    router = _router()
    seen = []
    for name in router._providers:
        real = router._client(name)._upstream
        key, url = real.api_key, str(real.base_url)

        def create(_key=key, _url=url, _name=name, **kwargs):
            seen.append((kwargs["model"], _key, _url))
            if _name == "scaleway":
                raise ProviderError(503)
            return _ok(kwargs["model"])

        router._client(name)._upstream = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        router._client(name)._telemetry.enqueue = lambda event: None
        router._client(name)._telemetry.flush_sync = lambda: None
    router.chat.completions.create(messages=HI, data_classification="confidential")
    assert seen == [
        ("scaleway-small", "sk-scw", "https://scw.example/v1/"),
        ("mistral-small-eu", "sk-mis", "https://mis.example/v1/"),
    ]


def test_failover_in_a_session_is_not_a_caller_retry():
    router = _router()
    events, _ = _wire(router, {"scaleway": [ProviderError(503)]})
    router.chat.completions.create(
        messages=HI, data_classification="confidential",
        ps_metadata={"session_id": "s-failover-retry"},
    )
    (event,) = events
    assert "fallback" in event["outcome_signals"]
    assert "retry" not in event["outcome_signals"]


def test_engine_rechecks_compliance_before_each_call(monkeypatch):
    """Defence in depth: a non-compliant plan that reached the list anyway is
    skipped by the engine, never called."""
    router = _router()
    _, upstreams = _wire(router, {"scaleway": [ProviderError(503)]})
    real = router._fallback_plans

    def leaky(plan):
        plans = real(plan)
        tier = NORDIC["groups"]["cheap"][0]  # gpt-4o-mini, US
        bad = type(plan)(
            plan.decision, tier, router._providers["openai"], plan.requested_model,
            plan.data_classification, plan.use_case, plan.request,
        )
        return [plans[0], bad, *plans[1:]]

    monkeypatch.setattr(router, "_fallback_plans", leaky)
    resp = router.chat.completions.create(messages=HI, data_classification="confidential")
    assert "openai" not in _called(upstreams)
    assert resp.ps.route.provider == "mistral-eu"


def test_pinned_model_only_fails_over_to_the_same_model():
    router = _router()
    _, upstreams = _wire(router, {"scaleway": [ProviderError(503)]})
    with pytest.raises(NoCompliantCandidate):
        router.chat.completions.create(
            messages=HI, model="scaleway-small", data_classification="confidential"
        )
    assert _called(upstreams) == {"scaleway": 1}


def test_use_case_eligible_models_bound_the_fallbacks():
    router = _router()
    _, upstreams = _wire(router, {"mistral-eu": [ProviderError(503)]})
    with pytest.raises(NoCompliantCandidate):
        router.chat.completions.create(
            messages=HI, data_classification="confidential", use_case="claim-summary"
        )
    # scaleway-small complies but is not one of claim-summary's eligible models.
    assert _called(upstreams) == {"mistral-eu": 1}


# --- no fallback ---------------------------------------------------------------------


def test_content_filter_refusal_is_raised_not_retried():
    router = _router()
    _, upstreams = _wire(router, {
        "scaleway": [ProviderError(400, code="content_filter")],
    })
    with pytest.raises(ProviderError) as err:
        router.chat.completions.create(messages=HI, data_classification="confidential")
    assert err.value.ps_fallback_chain == ["content_filter"]
    assert _called(upstreams) == {"scaleway": 1}


def test_client_error_is_raised_not_retried():
    router = _router()
    _, upstreams = _wire(router, {"scaleway": [ProviderError(401)]})
    with pytest.raises(ProviderError):
        router.chat.completions.create(messages=HI, data_classification="confidential")
    assert _called(upstreams) == {"scaleway": 1}


def test_stream_still_refused_in_local_mode():
    router = _router()
    _, upstreams = _wire(router)
    with pytest.raises(RouterError, match="stream"):
        router.chat.completions.create(messages=HI, stream=True)
    assert not _called(upstreams)


def test_failover_false_makes_one_attempt():
    router = _router(failover=False)
    _, upstreams = _wire(router, {"scaleway": [ProviderError(503)]})
    with pytest.raises(ProviderError):
        router.chat.completions.create(messages=HI, data_classification="confidential")
    assert _called(upstreams) == {"scaleway": 1}


# --- 429, attempts budget, circuit breaker ---------------------------------------


def test_rate_limit_waits_retry_after_then_retries_same_candidate():
    router = _router()
    events, upstreams = _wire(router, {
        "scaleway": [ProviderError(429, headers={"retry-after": "0"})],
    })
    resp = router.chat.completions.create(messages=HI, data_classification="confidential")
    assert _called(upstreams) == {"scaleway": 2}
    assert resp.ps.fallback_chain == ("rate_limited",)
    assert events[0]["fallback_chain"] == ["rate_limited"]


def test_max_attempts_caps_provider_calls():
    router = _router(max_attempts=1)
    _, upstreams = _wire(router, {"scaleway": [ProviderError(503)]})
    with pytest.raises(NoCompliantCandidate, match="max_attempts=1") as err:
        router.chat.completions.create(messages=HI, data_classification="confidential")
    assert err.value.fallback_chain == ["server_error"]
    assert isinstance(err.value.__cause__, ProviderError)
    assert _called(upstreams) == {"scaleway": 1}


def test_max_attempts_counts_rate_limit_retries():
    router = _router(max_attempts=2)
    _, upstreams = _wire(router, {
        "scaleway": [ProviderError(503)],
        "mistral-eu": [ProviderError(429, headers={"retry-after": "0"})],
    })
    with pytest.raises(NoCompliantCandidate):
        router.chat.completions.create(messages=HI, data_classification="confidential")
    assert _called(upstreams) == {"scaleway": 1, "mistral-eu": 1}


def test_failover_settings_come_from_policy_then_kwargs():
    router = _router()
    assert router.max_attempts == 3
    assert router._engine.max_wait == 10.0
    policy = {**NORDIC, "fallback": {"max_attempts": 2, "retry_after_max_wait_ms": 500}}
    router = _router(policy=policy)
    assert (router.max_attempts, router._engine.max_wait) == (2, 0.5)
    router = _router(policy=policy, max_attempts=4, retry_after_max_wait=1)
    assert (router.max_attempts, router._engine.max_wait) == (4, 1)


def test_open_breaker_skips_provider_on_later_calls():
    router = _router(breaker_config=BreakerConfig(failure_threshold=1, open_seconds=60))
    _, upstreams = _wire(router, {"scaleway": [ProviderError(503)]})
    router.chat.completions.create(messages=HI, data_classification="confidential")
    assert router.breakers.state("scaleway") == "open"
    resp = router.chat.completions.create(messages=HI, data_classification="confidential")
    assert _called(upstreams) == {"scaleway": 1, "mistral-eu": 2}
    assert resp.ps.fallback_chain == ()  # skipped, not attempted
    assert len(resp.ps.attempts) == 1


@pytest.mark.parametrize("kwargs,match", [
    ({"breakers": BreakerRegistry(), "breaker_config": BreakerConfig()}, "not both"),
    ({"max_attempts": 0}, "max_attempts"),
    ({"retry_after_max_wait": -1}, "retry_after_max_wait"),
    ({"rate_limit_retries": -1}, "rate_limit_retries"),
])
def test_bad_failover_configuration_is_rejected(kwargs, match):
    with pytest.raises(RouterError, match=match):
        _router(**kwargs)


# --- async ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_router_fails_over():
    router = _router(AsyncShieldsRouter)
    events, upstreams = _wire(router, {"scaleway": [ProviderError(503)]}, is_async=True)
    resp = await router.chat.completions.create(
        messages=HI, data_classification="confidential"
    )
    assert _called(upstreams) == {"scaleway": 1, "mistral-eu": 1}
    assert resp.ps.fallback_chain == ("server_error",)
    assert events[0]["fallback_chain"] == ["server_error"]

    _wire(router, {"scaleway": [ProviderError(503)],
                   "mistral-eu": [ProviderError(503)]}, is_async=True)
    with pytest.raises(NoCompliantCandidate):
        await router.chat.completions.create(
            messages=HI, data_classification="confidential"
        )
