"""Failover engine and circuit breaker behaviour beyond the shared vectors (#21)."""

from __future__ import annotations

import asyncio
import threading

import pytest
from prompt_shields.failover import (
    BreakerConfig,
    BreakerRegistry,
    Candidate,
    FailoverEngine,
    NoCompliantCandidate,
    failure_from_exception,
)
from prompt_shields.route_policy import PolicyViolation


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


# Shapes of the OpenAI / Anthropic / httpx exceptions, without importing them.
class FakeResponse:
    def __init__(self, status: int, headers: dict | None = None) -> None:
        self.status_code = status
        self.headers = headers or {}


class APIStatusError(Exception):
    def __init__(self, status: int, body=None, headers=None, code=None) -> None:
        super().__init__(f"status {status}")
        self.status_code = status
        self.response = FakeResponse(status, headers)
        self.body = body
        self.code = code


class APIConnectionError(Exception):
    pass


class APITimeoutError(APIConnectionError):
    pass


A = Candidate("berget", "llama-70b")
B = Candidate("mistral-eu", "mistral-large")


def engine(**kw) -> FailoverEngine:
    kw.setdefault("breakers", BreakerRegistry(clock=Clock()))
    kw.setdefault("sleep", lambda s: None)
    return FailoverEngine(**kw)


# --- exception mapping ---------------------------------------------------------


def test_maps_openai_style_errors() -> None:
    info = failure_from_exception(
        APIStatusError(
            400, body={"code": "context_length_exceeded", "message": "too long"}
        )
    )
    assert (info.status, info.code) == (400, "context_length_exceeded")
    nested = failure_from_exception(
        APIStatusError(
            400,
            body={
                "error": {
                    "type": "invalid_request_error",
                    "message": "prompt is too long: 1 > 0",
                }
            },
        )
    )
    assert nested.message == "prompt is too long: 1 > 0"
    assert (
        failure_from_exception(APIStatusError(400, code="content_filter")).code
        == "content_filter"
    )


def test_maps_retry_after_headers() -> None:
    assert (
        failure_from_exception(
            APIStatusError(429, headers={"retry-after": "3"})
        ).retry_after
        == 3
    )
    assert (
        failure_from_exception(
            APIStatusError(429, headers={"retry-after-ms": "250"})
        ).retry_after
        == 0.25
    )
    assert (
        failure_from_exception(
            APIStatusError(429, headers={"retry-after": "soon"})
        ).retry_after
        is None
    )
    past = failure_from_exception(
        APIStatusError(429, headers={"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"})
    )
    assert past.retry_after == 0


def test_maps_transport_errors() -> None:
    assert failure_from_exception(APITimeoutError()).kind == "timeout"
    assert failure_from_exception(TimeoutError()).kind == "timeout"
    assert failure_from_exception(APIConnectionError()).kind == "network"
    assert failure_from_exception(ConnectionResetError()).kind == "network"
    assert failure_from_exception(ValueError("bug")).kind is None


def test_real_exception_path_fails_over_and_raises() -> None:
    calls = []

    def call(c: Candidate):
        calls.append(c.provider)
        if c is A:
            raise APIStatusError(503)
        return "ok"

    result = engine().run([A, B], call)
    assert result.response == "ok" and result.fallback_chain == ["server_error"]

    def refused(c: Candidate):
        raise APIStatusError(400, code="content_filter")

    with pytest.raises(APIStatusError) as info:
        engine().run([A, B], refused)
    assert info.value.ps_fallback_chain == ["content_filter"]


def test_a_bug_in_the_call_is_not_failed_over() -> None:
    def call(c: Candidate):
        raise KeyError("oops")

    with pytest.raises(KeyError):
        engine().run([A, B], call)


# --- fail closed ---------------------------------------------------------------


def test_exhaustion_is_a_policy_violation_with_cause() -> None:
    cause = APIStatusError(502)

    def call(c: Candidate):
        raise cause

    with pytest.raises(PolicyViolation) as info:
        engine().run([A, B], call)
    err = info.value
    assert isinstance(err, NoCompliantCandidate)
    assert err.__cause__ is cause
    assert "failing closed" in str(err)
    assert "berget/llama-70b: server_error" in str(err)
    # Classes only: the provider's message is not in the error.
    assert "status 502" not in str(err)


# --- breaker -------------------------------------------------------------------


def test_breaker_opens_and_engine_skips_provider() -> None:
    clock = Clock()
    registry = BreakerRegistry(
        BreakerConfig(failure_threshold=2, open_seconds=10), clock=clock
    )
    eng = engine(breakers=registry)
    calls: list[str] = []

    def call(c: Candidate):
        calls.append(c.provider)
        if c is A:
            raise APIStatusError(500)
        return c.provider

    eng.run([A, B], call)
    eng.run([A, B], call)
    assert registry.state("berget") == "open"
    calls.clear()
    result = eng.run([A, B], call)
    assert calls == ["mistral-eu"]
    assert [(s.provider, s.reason) for s in result.skipped] == [
        ("berget", "circuit_open")
    ]

    # After open_seconds one probe goes through; its success closes the breaker.
    clock.now = 10
    calls.clear()
    ok = engine(breakers=registry).run(
        [A, B], lambda c: calls.append(c.provider) or "ok"
    )
    assert ok.candidate is A and registry.state("berget") == "closed"
    assert registry.metrics()["opened"] == 1
    assert registry.metrics()["half_opened"] == 1
    assert registry.metrics()["closed"] == 1


def test_client_errors_do_not_trip_the_breaker() -> None:
    registry = BreakerRegistry(BreakerConfig(failure_threshold=1), clock=Clock())

    def call(c: Candidate):
        raise APIStatusError(401)

    for _ in range(3):
        with pytest.raises(APIStatusError):
            engine(breakers=registry).run([A], call)
    assert registry.state("berget") == "closed"


def test_registry_is_bounded_and_reports_transitions() -> None:
    seen = []
    registry = BreakerRegistry(
        BreakerConfig(failure_threshold=1),
        max_entries=2,
        clock=Clock(),
        on_transition=lambda p, a, b: seen.append((p, a, b)),
    )
    for p in ("a", "b", "c"):
        registry.record_failure(p)
    assert set(registry.snapshot()) == {"b", "c"}
    assert registry.metrics()["evicted"] == 1
    assert registry.metrics()["tracked"] == 2
    assert seen == [
        ("a", "closed", "open"),
        ("b", "closed", "open"),
        ("c", "closed", "open"),
    ]
    # An evicted provider starts closed again (documented).
    assert registry.state("a") == "closed"


def test_on_transition_errors_are_swallowed() -> None:
    def boom(*_):
        raise RuntimeError("metrics backend down")

    registry = BreakerRegistry(
        BreakerConfig(failure_threshold=1), clock=Clock(), on_transition=boom
    )
    registry.record_failure("a")
    assert registry.state("a") == "open"


def test_registry_is_thread_safe() -> None:
    registry = BreakerRegistry(BreakerConfig(failure_threshold=10_000), clock=Clock())

    def work() -> None:
        for _ in range(500):
            registry.record_failure("p")
            registry.allow("p")

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert registry._breakers["p"].failures == 4000


def test_config_validation() -> None:
    with pytest.raises(ValueError):
        BreakerConfig(failure_threshold=0)
    with pytest.raises(ValueError):
        BreakerConfig(half_open_max_probes=0)
    with pytest.raises(ValueError):
        BreakerRegistry(max_entries=0)


# --- streaming -----------------------------------------------------------------


def test_stream_fails_over_before_first_chunk() -> None:
    def call(c: Candidate):
        if c is A:

            def broken():
                raise APIStatusError(503)
                yield  # pragma: no cover

            return broken()
        return iter(["x", "y"])

    stream = engine().run_stream([A, B], call)
    assert stream.candidate is B and stream.fallback_chain == ["server_error"]
    assert list(stream) == ["x", "y"]


def test_stream_is_never_retried_after_first_chunk() -> None:
    registry = BreakerRegistry(BreakerConfig(failure_threshold=1), clock=Clock())
    calls: list[str] = []

    def call(c: Candidate):
        calls.append(c.provider)

        def chunks():
            yield "x"
            raise APIConnectionError()

        return chunks()

    stream = engine(breakers=registry).run_stream([A, B], call)
    got = []
    with pytest.raises(APIConnectionError) as info:
        got.extend(stream)
    assert got == ["x"] and calls == ["berget"]
    assert stream.interrupted and info.value.ps_error_class == "stream_interrupted"
    assert registry.state("berget") == "open"


def test_empty_stream_is_a_success() -> None:
    stream = engine().run_stream([A], lambda c: iter([]))
    assert list(stream) == [] and stream.candidate is A


# --- async ---------------------------------------------------------------------


def test_async_run_and_retry_after() -> None:
    waits: list[float] = []

    async def sleep(s: float) -> None:
        waits.append(s)

    seq = [APIStatusError(429, headers={"retry-after": "1"}), "ok"]

    async def call(c: Candidate):
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    result = asyncio.run(engine(async_sleep=sleep).arun([A, B], call))
    assert result.response == "ok" and result.candidate is A
    assert waits == [1] and result.fallback_chain == ["rate_limited"]


def test_async_exhaustion_fails_closed() -> None:
    async def call(c: Candidate):
        raise APITimeoutError()

    with pytest.raises(NoCompliantCandidate) as info:
        asyncio.run(engine().arun([A, B], call))
    assert info.value.fallback_chain == ["timeout", "timeout"]


def test_async_stream() -> None:
    async def gen(items, fail=False):
        for i in items:
            yield i
        if fail:
            raise APIConnectionError()

    async def call(c: Candidate):
        if c is A:
            return gen([], fail=True)
        return gen(["a", "b"])

    async def main():
        stream = await engine().arun_stream([A, B], call)
        return stream, [c async for c in stream]

    stream, chunks = asyncio.run(main())
    assert chunks == ["a", "b"] and stream.fallback_chain == ["network_error"]


def test_async_empty_stream() -> None:
    async def gen():
        return
        yield  # pragma: no cover

    async def call(c: Candidate):
        return gen()

    async def main():
        stream = await engine().arun_stream([A], call)
        return [c async for c in stream]

    assert asyncio.run(main()) == []
