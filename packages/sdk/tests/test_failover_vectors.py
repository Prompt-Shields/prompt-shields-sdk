"""Shared failover vectors (``schemas/failover/vectors.json``, #21).

The gateway runs the same file in
``gateway/src/middlewares/psFailover/vectors.test.ts``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from prompt_shields.failover import (
    BreakerConfig,
    BreakerRegistry,
    Candidate,
    CircuitBreaker,
    FailoverEngine,
    FailureInfo,
    NoCompliantCandidate,
    classify_failure,
)

VECTORS = json.loads(
    (
        Path(__file__).resolve().parents[3] / "schemas" / "failover" / "vectors.json"
    ).read_text()
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class VectorError(Exception):
    def __init__(self, info: FailureInfo) -> None:
        super().__init__("vector failure")
        self.info = info


def _info(spec: dict) -> FailureInfo:
    return FailureInfo(
        status=spec.get("status"),
        kind=spec.get("kind"),
        code=spec.get("code"),
        message=spec.get("message"),
        retry_after=spec.get("retry_after"),
    )


@pytest.mark.parametrize("vec", VECTORS["classify"], ids=lambda v: v["id"])
def test_classify_vector(vec: dict) -> None:
    got = classify_failure(_info(vec["input"]))
    exp = vec["expected"]
    assert (got.error_class, got.action, got.trips_breaker) == (
        exp["error_class"],
        exp["action"],
        exp["trips_breaker"],
    )


@pytest.mark.parametrize("vec", VECTORS["breaker"], ids=lambda v: v["id"])
def test_breaker_vector(vec: dict) -> None:
    clock = Clock()
    seen: list[str] = []
    breaker = CircuitBreaker(
        BreakerConfig(**vec["config"]), clock, lambda _a, b: seen.append(b)
    )
    for step in vec["steps"]:
        clock.now = float(step["t"])
        op = step["op"]
        if op == "allow":
            assert breaker.allow() is step["allowed"], step
        elif op == "failure":
            breaker.record_failure()
        elif op == "success":
            breaker.record_success()
        elif op == "release":
            breaker.release()
        else:  # pragma: no cover - a vector typo
            raise AssertionError(f"unknown op {op}")
        assert breaker.current_state() == step["state"], step
    if "transitions" in vec:
        assert seen == vec["transitions"]


@pytest.mark.parametrize("vec", VECTORS["failover"], ids=lambda v: v["id"])
def test_failover_vector(vec: dict) -> None:
    registry = BreakerRegistry(clock=Clock())
    for provider in vec.get("open", []):
        registry.force_open(provider)
    waits: list[float] = []
    outcomes = {k: list(v) for k, v in vec["outcomes"].items()}
    engine = FailoverEngine(
        breakers=registry,
        max_wait=vec.get("config", {}).get("max_wait", 10),
        failure_of=lambda exc: exc.info,
        sleep=waits.append,
    )
    candidates = [
        Candidate(c["provider"], c["model"], c.get("context_window"))
        for c in vec["candidates"]
    ]
    deny = set(vec.get("deny", []))
    called: list[str] = []

    def call(c: Candidate) -> str:
        called.append(c.provider)
        outcome = outcomes[c.provider].pop(0)
        if outcome == "ok":
            return c.provider
        raise VectorError(_info(outcome))

    exp = vec["expected"]
    allow = (lambda c: c.provider not in deny) if deny else None
    if exp.get("error") == "no_compliant_candidate":
        with pytest.raises(NoCompliantCandidate) as info:
            engine.run(candidates, call, allow=allow)
        err = info.value
        attempts, chain, skipped = err.attempts, err.fallback_chain, err.skipped
    elif exp.get("error") == "raise":
        with pytest.raises(VectorError) as info:
            engine.run(candidates, call, allow=allow)
        err = info.value
        attempts, chain, skipped = err.ps_attempts, err.ps_fallback_chain, ()
        assert attempts[-1].error_class == exp["error_class"]
    else:
        result = engine.run(candidates, call, allow=allow)
        assert result.response == exp["served"]
        assert result.candidate.provider == exp["served"]
        attempts, chain, skipped = (
            result.attempts,
            result.fallback_chain,
            result.skipped,
        )
    assert [a.provider for a in attempts] == exp["attempts"] == called
    assert chain == exp["fallback_chain"]
    assert [[s.provider, s.reason] for s in skipped] == exp.get("skipped", [])
    if "waits" in exp:
        assert waits == exp["waits"]
