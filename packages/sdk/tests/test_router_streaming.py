"""ShieldsRouter local-mode streaming (``stream=True``).

The rules under test:

* route, data-policy gate, budgets and the learner's choice happen before
  anything is sent, as without streaming;
* failover only before the first chunk; an error after it is raised to the
  caller and never retried;
* telemetry and spend recorded exactly once, when the stream ends, is
  closed early or fails, with the provider's usage when it sends it, else
  estimated tokens (``tokens_estimated``);
* ``pseudonymise=True`` restores placeholders split across chunks and drops
  the mapping when the stream is done.

No network: each provider's instrumented client gets a fake upstream whose
streams are scripted, and telemetry is captured instead of sent.
"""

from __future__ import annotations

import asyncio
import gc
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletionChunk
from prompt_shields import (
    AsyncRoutedStream,
    AsyncShieldsRouter,
    BudgetExceeded,
    PolicyViolation,
    RoutedStream,
    ShieldsRouter,
)
from prompt_shields.budgets import BudgetEnforcer
from prompt_shields.failover import BreakerRegistry, NoCompliantCandidate
from prompt_shields.pseudonymise import CLOSE, OPEN

from tests.test_router_failover import ProviderError

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
HI = [{"role": "user", "content": "hi"}]
EMAIL = "kari.nordmann@example.no"

#: Two EEA tiers and one US tier in one group, so a confidential call has a
#: primary and one compliant fallback, and the US tier is never a candidate.
POLICY = {
    "schema_version": "route-policy.v1",
    "policy_version": "stream-test-1",
    "default_group": "cheap",
    "group_order": ["cheap"],
    "quality_to_group": {"draft": "cheap", "balanced": "cheap", "critical": "cheap"},
    "strategy": "heuristic",
    "groups": {
        "cheap": [
            {"model": "us-model", "provider": "us",
             "input_cost_per_1k": 0.00001, "output_cost_per_1k": 0.00001,
             "jurisdiction": "US", "zdr": True},
            {"model": "primary-model", "provider": "primary",
             "input_cost_per_1k": 0.0001, "output_cost_per_1k": 0.0004,
             "jurisdiction": "EEA", "zdr": True, "us_cloud_act_exposure": False},
            {"model": "backup-model", "provider": "backup",
             "input_cost_per_1k": 0.0002, "output_cost_per_1k": 0.0008,
             "jurisdiction": "EEA", "zdr": True, "us_cloud_act_exposure": False},
        ],
    },
    "sensitivity": {"unclassified_default": "internal"},
    "data_policy": {
        "public": {"jurisdictions": ["any"]},
        "internal": {"jurisdictions": ["EEA", "US"]},
        "confidential": {"jurisdictions": ["EEA"], "require_zdr": True,
                         "us_cloud_act_exposure": False},
        "restricted": {"jurisdictions": ["NONE"]},
    },
    "fallback": {"unknown_group": "default_group",
                 "budget_unsatisfiable": "cheapest",
                 "no_compliant_candidate": "fail_closed"},
}
PROVIDERS = {
    "us": {"api_key": "sk-us", "base_url": "https://us.example/v1"},
    "primary": {"api_key": "sk-p", "base_url": "https://primary.example/v1"},
    "backup": {"api_key": "sk-b", "base_url": "https://backup.example/v1"},
}


# --- scripted provider streams ---------------------------------------------------


def _chunk(model, content=None, finish=None, usage=None):
    data = {
        "id": "c1", "object": "chat.completion.chunk", "created": 1, "model": model,
        "choices": [] if usage is not None and content is None and finish is None
        else [{"index": 0, "delta": {} if content is None else {"content": content},
               "finish_reason": finish}],
    }
    if usage is not None:
        data["usage"] = usage
    return ChatCompletionChunk.model_validate(data)


def _chunks(model, texts, usage=True):
    out = [_chunk(model, t) for t in texts] + [_chunk(model, finish="stop")]
    if usage:
        out.append(_chunk(model, usage={"prompt_tokens": 100, "completion_tokens": 40,
                                        "total_tokens": 140}))
    return out


class Boom(Exception):
    pass


class _Stream:
    """A provider stream: yields ``items`` (raising any exception in it, as
    a stream failing at that point would), records being closed."""

    def __init__(self, items):
        self._items = list(items)
        self.closed = False
        self.read = 0

    def __iter__(self):
        for item in self._items:
            if isinstance(item, BaseException):
                raise item
            self.read += 1
            yield item

    def close(self):
        self.closed = True


class _AsyncStream(_Stream):
    def __iter__(self):
        raise TypeError("async only")

    def __aiter__(self):
        return self._agen()

    async def _agen(self):
        for item in self._items:
            if isinstance(item, BaseException):
                raise item
            self.read += 1
            yield item

    async def close(self):
        self.closed = True


class _Upstream:
    """Fake provider SDK. ``script`` is a list, one entry per request: an
    exception (the request raises) or a list of stream items."""

    def __init__(self, script, is_async):
        self.calls: list[dict] = []
        self.streams: list[_Stream] = []
        self.script = list(script)

        def respond(**kwargs):
            self.calls.append(kwargs)
            step = self.script.pop(0) if self.script else _chunks(kwargs["model"], ["ok"])
            if isinstance(step, BaseException):
                raise step
            stream = (_AsyncStream if is_async else _Stream)(
                step(kwargs["model"]) if callable(step) else step
            )
            self.streams.append(stream)
            return stream

        async def arespond(**kwargs):
            return respond(**kwargs)

        create = arespond if is_async else respond
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
        self.messages = SimpleNamespace(create=create)


def _router(cls=ShieldsRouter, scripts=None, **kwargs):
    kwargs.setdefault("ps_api_key", "ps-test")
    kwargs.setdefault("providers", PROVIDERS)
    kwargs.setdefault("policy", POLICY)
    kwargs.setdefault("breakers", BreakerRegistry())
    router = cls(**kwargs)
    is_async = cls is AsyncShieldsRouter
    events: list[dict] = []
    upstreams = {}
    for name in router._providers:
        client = router._client(name)
        up = _Upstream((scripts or {}).get(name, ()), is_async)
        client._upstream = up
        upstreams[name] = up
        client._telemetry.enqueue = events.append
        if is_async:
            async def noop():
                return None
            client._telemetry.flush = noop
        else:
            client._telemetry.flush_sync = lambda: None
    return router, events, upstreams


def _called(upstreams):
    return {n: len(u.calls) for n, u in upstreams.items() if u.calls}


def _text(chunks):
    return "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)


def _enforcer(limit=1.0, mode="block"):
    return BudgetEnforcer(
        {"schema_version": "budget.v1",
         "budgets": [{"id": "b", "scope": "global", "window": "day",
                      "limit_usd": limit, "mode": mode}]},
        clock=lambda: NOW,
    )


def _spent(enforcer):
    return enforcer.check({}).statuses[0].spent_usd


CONFIDENTIAL = {"data_classification": "confidential"}


# --- routing before the stream starts ------------------------------------------------


def test_stream_is_routed_and_yields_chunks_with_one_event():
    router, events, upstreams = _router()
    stream = router.chat.completions.create(messages=HI, stream=True, **CONFIDENTIAL)
    assert isinstance(stream, RoutedStream)
    # Routed under the data policy before anything was read by the caller.
    assert stream.ps.route.provider == "primary"
    assert stream.ps.route.model == "primary-model"
    assert stream.ps.route.sensitivity_level == "confidential"
    assert stream.ps.cost.usd is None  # not known until the stream ends
    assert stream.ps_call_id
    assert events == []
    chunks = list(stream)
    assert _text(chunks) == "ok"
    assert _called(upstreams) == {"primary": 1}
    assert upstreams["primary"].calls[0]["stream"] is True
    (event,) = events
    assert event["stream"] is True and event["stream_completed"] is True
    assert event["served_provider"] == "primary"
    assert event["route_mode"] == "local"
    assert event["sensitivity_level"] == "confidential"
    assert event["fallback_chain"] == []
    # Usage from the provider's final chunk, not estimated.
    assert (event["tokens_in"], event["tokens_out"]) == (100, 40)
    assert not event.get("tokens_estimated")
    assert event["call_id"] == stream.ps_call_id
    # ps is completed with the event's cost.
    assert stream.ps.cost.usd == pytest.approx(event["cost"])
    assert stream.ps.cost.usd > 0


def test_data_policy_fails_closed_before_any_stream_is_opened():
    router, events, upstreams = _router()
    with pytest.raises(PolicyViolation):
        router.chat.completions.create(
            messages=HI, stream=True, data_classification="restricted"
        )
    assert _called(upstreams) == {}
    assert events == []


def test_block_budget_refuses_the_stream_before_it_is_sent():
    enforcer = _enforcer(limit=1e-9)
    enforcer.commit(enforcer.check({}), 1.0)
    router, events, upstreams = _router(budgets=enforcer)
    with pytest.raises(BudgetExceeded):
        router.chat.completions.create(messages=HI, stream=True, **CONFIDENTIAL)
    assert _called(upstreams) == {}
    assert events == []


def test_non_compliant_tier_is_never_a_stream_candidate():
    router, _, upstreams = _router(scripts={
        "primary": [ProviderError(503)], "backup": [ProviderError(503)],
    })
    with pytest.raises(NoCompliantCandidate):
        router.chat.completions.create(messages=HI, stream=True, **CONFIDENTIAL)
    # The cheaper US tier is not allowed for confidential data.
    assert _called(upstreams) == {"primary": 1, "backup": 1}


# --- failover only before the first chunk -------------------------------------------


def test_request_error_fails_over_before_the_first_chunk():
    router, events, upstreams = _router(scripts={"primary": [ProviderError(503)]})
    stream = router.chat.completions.create(messages=HI, stream=True, **CONFIDENTIAL)
    assert stream.ps.route.provider == "backup"
    assert stream.ps.fallback_chain == ("server_error",)
    assert _text(list(stream)) == "ok"
    assert _called(upstreams) == {"primary": 1, "backup": 1}
    (event,) = events  # none for the failed attempt
    assert event["served_provider"] == "backup"
    assert event["fallback_chain"] == ["server_error"]
    assert event["retry_count"] == 1


@pytest.mark.parametrize("status,error_class", [(503, "server_error"),
                                                (429, "rate_limited")])
def test_stream_failing_before_its_first_chunk_fails_over(status, error_class):
    """A lazy stream whose request fails on the first read: nothing reached
    the caller, so this is still a failed attempt, with no event."""
    router, events, upstreams = _router(
        scripts={"primary": [[ProviderError(status)]] * 2},
        rate_limit_retries=0,
    )
    stream = router.chat.completions.create(messages=HI, stream=True, **CONFIDENTIAL)
    assert stream.ps.route.provider == "backup"
    assert stream.ps.fallback_chain == (error_class,)
    assert upstreams["primary"].streams[0].closed
    assert _text(list(stream)) == "ok"
    (event,) = events
    assert event["served_provider"] == "backup"


def test_no_retry_after_the_first_chunk():
    router, events, upstreams = _router(scripts={
        "primary": [[_chunk("primary-model", "Hel"), ProviderError(503)]],
    })
    stream = router.chat.completions.create(messages=HI, stream=True, **CONFIDENTIAL)
    first = next(stream)
    assert first.choices[0].delta.content == "Hel"
    with pytest.raises(ProviderError):
        next(stream)
    # Never retried mid-stream: the backup was not called.
    assert _called(upstreams) == {"primary": 1}
    (event,) = events
    assert event["stream_completed"] is False
    assert event["stream_error"] == "ProviderError"
    assert event["served_provider"] == "primary"
    # Further reads end; still one event.
    assert list(stream) == []
    stream.close()
    assert len(events) == 1


def test_failover_disabled_raises_the_first_error():
    router, events, upstreams = _router(
        scripts={"primary": [[ProviderError(503)]]}, failover=False
    )
    with pytest.raises(ProviderError):
        router.chat.completions.create(messages=HI, stream=True, **CONFIDENTIAL)
    assert _called(upstreams) == {"primary": 1}
    assert events == []


# --- recorded exactly once ------------------------------------------------------


def test_spend_recorded_exactly_once_on_completion():
    enforcer = _enforcer()
    router, events, _ = _router(budgets=enforcer,
                                scripts={"primary": [ProviderError(503)]})
    with router.chat.completions.create(messages=HI, stream=True,
                                        **CONFIDENTIAL) as stream:
        chunks = list(stream)
    assert _text(chunks) == "ok"
    (event,) = events
    assert event["budget_state"] == "ok"
    assert _spent(enforcer) == pytest.approx(event["cost"])
    # Closing again, iterating again: nothing more is recorded.
    stream.close()
    assert list(stream) == []
    assert len(events) == 1
    assert _spent(enforcer) == pytest.approx(event["cost"])
    assert stream.ps.budget.state == "ok"


def test_spend_recorded_once_when_closed_early():
    enforcer = _enforcer()
    router, events, upstreams = _router(budgets=enforcer, scripts={
        "primary": [lambda m: _chunks(m, ["a", "b", "c"], usage=False)],
    })
    stream = router.chat.completions.create(messages=HI, stream=True, **CONFIDENTIAL)
    next(stream)
    stream.close()
    stream.close()
    assert upstreams["primary"].streams[0].closed
    (event,) = events
    assert event["stream_completed"] is False
    # No usage chunk: estimated, and flagged.
    assert event["tokens_estimated"] is True
    assert _spent(enforcer) == pytest.approx(event["cost"])
    assert stream.ps.cost.usd == pytest.approx(event["cost"])


def test_spend_recorded_once_when_the_stream_fails_mid_way():
    enforcer = _enforcer()
    router, events, _ = _router(budgets=enforcer, scripts={
        "primary": [[_chunk("primary-model", "x"), Boom()]],
    })
    stream = router.chat.completions.create(messages=HI, stream=True, **CONFIDENTIAL)
    with pytest.raises(Boom):
        list(stream)
    stream.close()
    (event,) = events
    assert event["stream_error"] == "Boom"
    assert _spent(enforcer) == pytest.approx(event["cost"])


def test_failed_attempts_record_no_spend():
    enforcer = _enforcer()
    router, events, _ = _router(budgets=enforcer, scripts={
        "primary": [[ProviderError(503)]], "backup": [ProviderError(503)],
    })
    with pytest.raises(NoCompliantCandidate):
        router.chat.completions.create(messages=HI, stream=True, **CONFIDENTIAL)
    assert events == []
    assert _spent(enforcer) == 0


def test_abandoned_stream_is_recorded_once():
    enforcer = _enforcer()
    router, events, _ = _router(budgets=enforcer, scripts={
        "primary": [lambda m: _chunks(m, ["a", "b"], usage=False)],
    })
    stream = router.chat.completions.create(messages=HI, stream=True, **CONFIDENTIAL)
    next(stream)
    del stream
    gc.collect()
    (event,) = events
    assert event["stream_abandoned"] is True
    assert _spent(enforcer) == pytest.approx(event["cost"])


def test_empty_stream_ends_cleanly():
    router, events, _ = _router(scripts={"primary": [[]]})
    stream = router.chat.completions.create(messages=HI, stream=True, **CONFIDENTIAL)
    assert list(stream) == []
    (event,) = events
    assert event["stream_completed"] is True


# --- Anthropic events --------------------------------------------------------------


def _anthropic_events(model, text_parts):
    ns = SimpleNamespace
    out = [ns(type="message_start", message=ns(
        model=model, usage=ns(input_tokens=30, output_tokens=1)))]
    out.append(ns(type="content_block_start", index=0,
                  content_block=ns(type="text", text="")))
    for t in text_parts:
        out.append(ns(type="content_block_delta", index=0,
                      delta=ns(type="text_delta", text=t)))
    out.append(ns(type="content_block_stop", index=0))
    out.append(ns(type="message_delta", delta=ns(stop_reason="end_turn"),
                  usage=ns(output_tokens=12)))
    out.append(ns(type="message_stop"))
    return out


def test_anthropic_stream_usage_from_message_delta():
    providers = {**PROVIDERS, "primary": {"api_key": "sk-ant", "vendor": "anthropic"}}
    router, events, upstreams = _router(providers=providers, scripts={
        "primary": [lambda m: _anthropic_events(m, ["Hel", "lo"])],
    })
    stream = router.chat.completions.create(messages=HI, stream=True, **CONFIDENTIAL)
    parts = [e.delta.text for e in stream if e.type == "content_block_delta"]
    assert parts == ["Hel", "lo"]
    assert "stream_options" not in upstreams["primary"].calls[0]
    (event,) = events
    assert (event["tokens_in"], event["tokens_out"]) == (30, 12)
    assert not event.get("tokens_estimated")


# --- pseudonymisation ------------------------------------------------------------


def _ph(kind, n=1):
    return f"{OPEN}{kind}_{n}{CLOSE}"


@pytest.mark.parametrize("size", [1, 3, 7])
def test_pseudonymised_stream_restores_split_placeholders(size):
    answer = f"Hello {_ph('EMAIL')}, done."
    pieces = [answer[i:i + size] for i in range(0, len(answer), size)]
    router, events, upstreams = _router(
        pseudonymise=True,
        scripts={"primary": [lambda m: _chunks(m, pieces)]},
    )
    prompt = [{"role": "user", "content": f"Write to {EMAIL}."}]
    stream = router.chat.completions.create(messages=prompt, stream=True,
                                            **CONFIDENTIAL)
    chunks = list(stream)
    assert _text(chunks) == f"Hello {EMAIL}, done."
    # Only placeholders were sent.
    assert EMAIL not in repr(upstreams["primary"].calls[0]["messages"])
    (event,) = events
    assert EMAIL not in repr(event)
    info = stream.ps.pseudonymisation
    assert info.entities == {"email": 1} and info.restored == 1


def _capture_pseudo(router):
    """The Pseudonymisation of the next call, to check it was cleared."""
    seen = []
    original = router._plan

    def plan(*args, **kwargs):
        p = original(*args, **kwargs)
        seen.append(p.pseudo)
        return p

    router._plan = plan
    return seen


@pytest.mark.parametrize("how", ["finish", "close", "error"])
def test_pseudonymised_mapping_dropped_when_the_stream_is_done(how):
    script = (
        [_chunk("primary-model", "a"), Boom()] if how == "error"
        else _chunks("primary-model", ["a", "b"])
    )
    router, _, _ = _router(pseudonymise=True, scripts={"primary": [script]})
    seen = _capture_pseudo(router)
    stream = router.chat.completions.create(
        messages=[{"role": "user", "content": f"Mail {EMAIL}"}], stream=True,
        **CONFIDENTIAL,
    )
    (pseudo,) = seen
    assert pseudo.active  # held while the stream runs
    if how == "finish":
        list(stream)
    elif how == "close":
        next(stream)
        stream.close()
    else:
        with pytest.raises(Boom):
            list(stream)
    assert not pseudo.active


def test_pseudonymised_mapping_survives_failover_and_is_dropped_on_failure():
    answer = f"to {_ph('EMAIL')}"
    router, _, _ = _router(pseudonymise=True, scripts={
        "primary": [[ProviderError(503)]],
        "backup": [lambda m: _chunks(m, [answer[:5], answer[5:]])],
    })
    seen = _capture_pseudo(router)
    stream = router.chat.completions.create(
        messages=[{"role": "user", "content": f"Mail {EMAIL}"}], stream=True,
        **CONFIDENTIAL,
    )
    assert stream.ps.route.provider == "backup"
    assert _text(list(stream)) == f"to {EMAIL}"
    assert not seen[0].active

    router, _, _ = _router(pseudonymise=True, scripts={
        "primary": [ProviderError(503)], "backup": [ProviderError(503)],
    })
    seen = _capture_pseudo(router)
    with pytest.raises(NoCompliantCandidate):
        router.chat.completions.create(
            messages=[{"role": "user", "content": f"Mail {EMAIL}"}], stream=True,
            **CONFIDENTIAL,
        )
    assert not seen[0].active


# --- learner ----------------------------------------------------------------------


class _Learner:
    """Records observe() calls; stands in for AutoLearner's choose()."""

    def __init__(self):
        self.observed = []

    def observe(self, call_id, choice, signals):
        self.observed.append((call_id, choice, signals))


def test_learner_observes_the_stream_once_when_it_ends():
    from prompt_shields.learner_auto import LearnerChoice

    router, events, _ = _router()
    learner = _Learner()
    router.learner = learner
    choice = LearnerChoice(
        model="primary:primary-model", mode="exploit", propensity=1.0,
        explore_rate=0.0, policy_model="primary:primary-model",
        learner_version=None, use_case="support", sensitivity="confidential",
    )
    original = router._plan

    def plan(*args, **kwargs):
        from dataclasses import replace
        return replace(original(*args, **kwargs), learner=choice)

    router._plan = plan
    stream = router.chat.completions.create(messages=HI, stream=True, **CONFIDENTIAL)
    assert learner.observed == []
    list(stream)
    stream.close()
    (event,) = events
    assert [(c, ch) for c, ch, _ in learner.observed] == [(event["call_id"], choice)]


# --- async --------------------------------------------------------------------------


def test_async_stream_fails_over_and_records_once():
    async def main():
        enforcer = _enforcer()
        router, events, upstreams = _router(
            cls=AsyncShieldsRouter, budgets=enforcer,
            scripts={"primary": [[ProviderError(503)]]},
        )
        stream = await router.chat.completions.create(
            messages=HI, stream=True, **CONFIDENTIAL
        )
        assert isinstance(stream, AsyncRoutedStream)
        assert stream.ps.route.provider == "backup"
        chunks = [c async for c in stream]
        await stream.aclose()
        assert _text(chunks) == "ok"
        (event,) = events
        assert event["served_provider"] == "backup"
        assert _spent(enforcer) == pytest.approx(event["cost"])
        assert upstreams["primary"].streams[0].closed

    asyncio.run(main())


def test_async_no_retry_after_first_chunk_and_context_manager():
    async def main():
        router, events, upstreams = _router(cls=AsyncShieldsRouter, scripts={
            "primary": [[_chunk("primary-model", "x"), ProviderError(503)]],
        })
        async with await router.chat.completions.create(
            messages=HI, stream=True, **CONFIDENTIAL
        ) as stream:
            first = await stream.__anext__()
            assert first.choices[0].delta.content == "x"
            with pytest.raises(ProviderError):
                await stream.__anext__()
        assert _called(upstreams) == {"primary": 1}
        (event,) = events
        assert event["stream_error"] == "ProviderError"

    asyncio.run(main())


def test_async_pseudonymised_stream_restores_and_clears():
    async def main():
        answer = f"Hi {_ph('EMAIL')}!"
        router, _, _ = _router(cls=AsyncShieldsRouter, pseudonymise=True, scripts={
            "primary": [lambda m: _chunks(m, [answer[i:i + 2]
                                              for i in range(0, len(answer), 2)])],
        })
        seen = _capture_pseudo(router)
        stream = await router.chat.completions.create(
            messages=[{"role": "user", "content": f"Mail {EMAIL}"}], stream=True,
            **CONFIDENTIAL,
        )
        chunks = [c async for c in stream]
        assert _text(chunks) == f"Hi {EMAIL}!"
        assert not seen[0].active
        assert stream.ps.pseudonymisation.restored == 1

    asyncio.run(main())


def test_async_stream_closed_early_records_once():
    async def main():
        enforcer = _enforcer()
        router, events, upstreams = _router(
            cls=AsyncShieldsRouter, budgets=enforcer,
            scripts={"primary": [lambda m: _chunks(m, ["a", "b"], usage=False)]},
        )
        stream = await router.chat.completions.create(
            messages=HI, stream=True, **CONFIDENTIAL
        )
        await stream.__anext__()
        await stream.close()
        await stream.close()
        assert upstreams["primary"].streams[0].closed
        (event,) = events
        assert event["tokens_estimated"] is True
        assert _spent(enforcer) == pytest.approx(event["cost"])

    asyncio.run(main())


# --- finalisation of a stream dropped without close() --------------------------------


@pytest.fixture
def no_cyclic_gc():
    """Reference counting only: a dropped stream must be finalised at once,
    not when the cyclic garbage collector happens to run."""
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        gc.enable()


def _with_learner(router, learner=None):
    """Make every plan carry a learner choice, as auto mode would."""
    from dataclasses import replace

    from prompt_shields.learner_auto import LearnerChoice

    learner = learner or _Learner()
    router.learner = learner
    choice = LearnerChoice(
        model="primary:primary-model", mode="exploit", propensity=1.0,
        explore_rate=0.0, policy_model="primary:primary-model",
        learner_version=None, use_case="support", sensitivity="confidential",
    )
    original = router._plan

    def plan(*args, **kwargs):
        return replace(original(*args, **kwargs), learner=choice)

    router._plan = plan
    return learner, choice


def test_abandoned_stream_tells_the_learner_once(no_cyclic_gc):
    enforcer = _enforcer()
    router, events, upstreams = _router(budgets=enforcer, scripts={
        "primary": [lambda m: _chunks(m, ["a", "b"], usage=False)],
    })
    learner, choice = _with_learner(router)
    stream = router.chat.completions.create(messages=HI, stream=True, **CONFIDENTIAL)
    next(stream)
    del stream
    # Finalised by reference counting alone: event, spend, learner, close.
    (event,) = events
    assert event["stream_abandoned"] is True
    assert [(c, ch) for c, ch, _ in learner.observed] == [(event["call_id"], choice)]
    assert _spent(enforcer) == pytest.approx(event["cost"])
    assert upstreams["primary"].streams[0].closed
    gc.collect()
    assert len(events) == 1 and len(learner.observed) == 1


@pytest.mark.parametrize("how", ["exhausted", "closed", "error"])
def test_ended_stream_is_not_reported_again_when_collected(how, no_cyclic_gc):
    script = (
        [_chunk("primary-model", "a"), Boom()] if how == "error"
        else _chunks("primary-model", ["a", "b"])
    )
    enforcer = _enforcer()
    router, events, _ = _router(budgets=enforcer, scripts={"primary": [script]})
    learner, _ = _with_learner(router)
    stream = router.chat.completions.create(messages=HI, stream=True, **CONFIDENTIAL)
    if how == "exhausted":
        list(stream)
    elif how == "closed":
        next(stream)
        stream.close()
    else:
        with pytest.raises(Boom):
            list(stream)
    assert len(events) == 1 and len(learner.observed) == 1
    spent = _spent(enforcer)
    del stream
    gc.collect()
    assert len(events) == 1 and len(learner.observed) == 1
    assert _spent(enforcer) == spent


def test_never_read_stream_dropped_clears_mapping_and_closes_upstream(no_cyclic_gc):
    router, events, upstreams = _router(pseudonymise=True, scripts={
        "primary": [lambda m: _chunks(m, ["a", "b"])],
    })
    learner, _ = _with_learner(router)
    seen = _capture_pseudo(router)
    stream = router.chat.completions.create(
        messages=[{"role": "user", "content": f"Mail {EMAIL}"}], stream=True,
        **CONFIDENTIAL,
    )
    (pseudo,) = seen
    assert pseudo.active
    # The first chunk was read for failover: the upstream is open.
    assert upstreams["primary"].streams[0].read == 1
    del stream
    assert not pseudo.active
    assert upstreams["primary"].streams[0].closed
    (event,) = events
    assert event["stream_abandoned"] is True
    assert len(learner.observed) == 1


def test_stream_in_a_reference_cycle_is_finalised_by_the_collector(no_cyclic_gc):
    router, events, upstreams = _router(pseudonymise=True)
    learner, _ = _with_learner(router)
    seen = _capture_pseudo(router)
    stream = router.chat.completions.create(
        messages=[{"role": "user", "content": f"Mail {EMAIL}"}], stream=True,
        **CONFIDENTIAL,
    )
    stream.cycle = stream
    del stream
    assert learner.observed == [] and seen[0].active  # kept alive by the cycle
    gc.collect()
    assert not seen[0].active
    assert upstreams["primary"].streams[0].closed
    assert len(events) == 1 and len(learner.observed) == 1


def test_finaliser_never_raises_nor_logs_content(no_cyclic_gc, monkeypatch, caplog):
    import logging
    import sys

    class Raising(_Learner):
        def observe(self, call_id, choice, signals):
            raise RuntimeError(f"cannot learn from {EMAIL}")

    reported = []
    monkeypatch.setattr(sys, "excepthook", lambda *a: reported.append(a))
    monkeypatch.setattr(sys, "unraisablehook", lambda u: reported.append(u))
    router, events, _ = _router(pseudonymise=True)
    _with_learner(router, Raising())
    seen = _capture_pseudo(router)
    caplog.set_level(logging.DEBUG)
    stream = router.chat.completions.create(
        messages=[{"role": "user", "content": f"Mail {EMAIL}"}], stream=True,
        **CONFIDENTIAL,
    )
    del stream
    assert reported == []
    assert not seen[0].active  # dropped even though the learner raised
    assert len(events) == 1
    assert EMAIL not in caplog.text


def test_finaliser_does_nothing_at_interpreter_shutdown(no_cyclic_gc, monkeypatch):
    import sys

    router, events, upstreams = _router()
    learner, _ = _with_learner(router)
    stream = router.chat.completions.create(messages=HI, stream=True, **CONFIDENTIAL)
    assert stream._finalizer.atexit is False  # not run by the atexit hook
    monkeypatch.setattr(sys, "is_finalizing", lambda: True)
    del stream
    assert events == [] and learner.observed == []
    assert not upstreams["primary"].streams[0].closed


def test_async_abandoned_stream_is_finalised_once(no_cyclic_gc):
    async def main():
        enforcer = _enforcer()
        router, events, upstreams = _router(
            cls=AsyncShieldsRouter, budgets=enforcer, pseudonymise=True,
            scripts={"primary": [lambda m: _chunks(m, ["a", "b"], usage=False)]},
        )
        learner, choice = _with_learner(router)
        seen = _capture_pseudo(router)
        stream = await router.chat.completions.create(
            messages=[{"role": "user", "content": f"Mail {EMAIL}"}], stream=True,
            **CONFIDENTIAL,
        )
        await stream.__anext__()
        del stream
        (event,) = events
        assert event["stream_abandoned"] is True
        assert [(c, ch) for c, ch, _ in learner.observed] == [(event["call_id"], choice)]
        assert _spent(enforcer) == pytest.approx(event["cost"])
        assert not seen[0].active
        # The async close is scheduled on the running loop.
        for _ in range(3):
            await asyncio.sleep(0)
        assert upstreams["primary"].streams[0].closed
        gc.collect()
        assert len(events) == 1 and len(learner.observed) == 1

    asyncio.run(main())


def test_async_closed_stream_is_not_reported_again(no_cyclic_gc):
    async def main():
        router, events, _ = _router(cls=AsyncShieldsRouter)
        learner, _ = _with_learner(router)
        stream = await router.chat.completions.create(
            messages=HI, stream=True, **CONFIDENTIAL
        )
        async with stream:
            await stream.__anext__()
        del stream
        gc.collect()
        assert len(events) == 1 and len(learner.observed) == 1

    asyncio.run(main())
