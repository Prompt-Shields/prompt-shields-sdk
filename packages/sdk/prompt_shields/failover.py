"""Failover engine and per-provider circuit breaker (PRD 0001 §7.5, #21).

The engine tries an ordered list of candidates and moves down the list only
when the failure says another provider may succeed:

=============================  ==============================================
Failure                        Action
=============================  ==============================================
429                            Wait ``Retry-After`` when it is at most
                               ``max_wait`` (``rate_limit_retries`` times,
                               default once), then the next candidate
5xx, timeout, connection       The next candidate; counts against the
                               provider's circuit breaker
Context length exceeded        The next candidate whose ``context_window`` is
                               known and larger than the failed one's
400 / 401 / 403 / other 4xx    Raise. No fallback
Content-filter refusal         Raise. Falling back to a laxer model would be
                               a policy-evasion path (P7)
Stream already emitting        Never retried (``run_stream``)
No candidate left              ``NoCompliantCandidate`` (a ``PolicyViolation``):
                               fail closed
=============================  ==============================================

**The engine never widens the constraint set.** It only calls the candidates
it is given, in order, and every rule above can only remove candidates. The
caller passes candidates that already passed the data policy (sensitivity,
jurisdiction, ZDR; ``route_policy``). ``allow=`` re-checks each candidate
before it is called, as defence in depth: a candidate it rejects is skipped
as ``non_compliant`` and is never called, not even as a last resort.

Circuit breakers are per provider and per process. Each process keeps its own
state in a ``BreakerRegistry`` (a module default, or one passed in), bounded
to ``max_entries`` providers with least-recently-used eviction. State is not
shared between processes or replicas: each one learns a provider is down
from its own failures. Evicting a breaker forgets its state, so an evicted
open breaker lets traffic through again.

Breaker states: ``closed`` (calls pass; ``failure_threshold`` consecutive
tripping failures open it), ``open`` (calls are skipped for ``open_seconds``),
``half_open`` (up to ``half_open_max_probes`` calls probe the provider; a
success closes the breaker, a tripping failure reopens it). Only failures
that say the provider is unhealthy trip it: 429, 5xx, timeouts, connection
errors and interrupted streams. A 4xx, a context-length error or a content
filter says nothing about the provider's health.

Every transition is counted (``BreakerRegistry.metrics()``) and passed to
``on_transition(provider, from_state, to_state)`` for export to a metrics
backend. Attempts and ``fallback_chain`` hold error classes only, never
error messages or content (PRD §7.12).

The gateway has the same engine in TypeScript
(``gateway/src/middlewares/psFailover``). Both pass the shared vectors in
``schemas/failover/vectors.json``.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import threading
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Generic, TypeVar

from .route_policy import PolicyViolation

T = TypeVar("T")

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

# Actions a classified failure leads to.
RETRY_AFTER = "retry_after"
NEXT = "next"
NEXT_LARGER_CONTEXT = "next_larger_context"
RAISE = "raise"

# Why a candidate was skipped without being called.
SKIP_CIRCUIT_OPEN = "circuit_open"
SKIP_CONTEXT_WINDOW = "context_window"
SKIP_NON_COMPLIANT = "non_compliant"

_CONTENT_FILTER_CODES = frozenset({"content_filter", "content_policy_violation"})
_CONTENT_FILTER_RE = re.compile(
    r"content[ _-]?(management )?(filter|policy)", re.IGNORECASE
)
_CONTEXT_CODES = frozenset({"context_length_exceeded"})
_CONTEXT_RE = re.compile(
    r"context[ _-]?(length|window)|prompt is too long|too many (input )?tokens",
    re.IGNORECASE,
)


# --- classification ------------------------------------------------------------


@dataclass(frozen=True)
class FailureInfo:
    """A provider failure, reduced to what classification needs.

    ``kind`` is ``timeout``, ``network`` or ``stream_interrupted`` for a
    failure without an HTTP status. ``code`` and ``message`` are the
    provider's error code and message; they are read here and never stored.
    """

    status: int | None = None
    kind: str | None = None
    code: str | None = None
    message: str | None = None
    retry_after: float | None = None


@dataclass(frozen=True)
class Classification:
    error_class: str
    action: str
    trips_breaker: bool


def classify_failure(info: FailureInfo) -> Classification:
    """Maps a failure to its error class and the §7.5 action."""
    if info.kind == "stream_interrupted":
        return Classification("stream_interrupted", RAISE, True)
    if info.kind == "timeout":
        return Classification("timeout", NEXT, True)
    if info.kind == "network":
        return Classification("network_error", NEXT, True)
    status = info.status
    if status is None:
        return Classification("unknown_error", RAISE, False)
    if status < 500:
        code = (info.code or "").lower()
        message = info.message or ""
        if code in _CONTENT_FILTER_CODES or _CONTENT_FILTER_RE.search(message):
            return Classification("content_filter", RAISE, False)
        if status == 413 or (
            status == 400 and (code in _CONTEXT_CODES or _CONTEXT_RE.search(message))
        ):
            return Classification("context_length_exceeded", NEXT_LARGER_CONTEXT, False)
    if status in (408, 504):
        return Classification("timeout", NEXT, True)
    if status == 429:
        return Classification("rate_limited", RETRY_AFTER, True)
    if status >= 500:
        return Classification("server_error", NEXT, True)
    if status >= 400:
        return Classification("client_error", RAISE, False)
    return Classification("unknown_error", RAISE, False)


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _retry_after(headers: Any) -> float | None:
    if headers is None:
        return None
    try:
        ms = headers.get("retry-after-ms")
        if ms is not None:
            return max(0.0, float(ms) / 1000)
        value = headers.get("retry-after")
    except (AttributeError, TypeError, ValueError):
        return None
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        when = parsedate_to_datetime(str(value))
        return max(0.0, when.timestamp() - time.time())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def _status_of(exc: BaseException) -> int | None:
    for obj in (exc, getattr(exc, "response", None)):
        for name in ("status_code", "status"):
            value = getattr(obj, name, None) if obj is not None else None
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


def failure_from_exception(exc: BaseException) -> FailureInfo:
    """Reads a provider SDK exception (OpenAI, Anthropic, httpx, builtins).

    Duck-typed, so it imports no provider SDK: the status comes from
    ``status_code`` / ``status`` (on the exception or its ``response``), the
    error code from ``code`` or the body, ``Retry-After`` from the response
    headers. An exception without a status is a timeout or a connection
    error when its class name says so.
    """
    status = _status_of(exc)
    if status is None:
        names = [cls.__name__ for cls in type(exc).__mro__]
        if isinstance(exc, TimeoutError) or any("Timeout" in n for n in names):
            return FailureInfo(kind="timeout")
        if isinstance(exc, ConnectionError) or any(
            n
            in (
                "APIConnectionError",
                "ConnectError",
                "NetworkError",
                "RemoteProtocolError",
            )
            for n in names
        ):
            return FailureInfo(kind="network")
        return FailureInfo()
    body = getattr(exc, "body", None)
    error = _get(body, "error") if body is not None else None
    detail = error if isinstance(error, dict) else body
    code = getattr(exc, "code", None) or _get(detail, "code") or _get(detail, "type")
    message = _get(detail, "message") or getattr(exc, "message", None)
    response = getattr(exc, "response", None)
    return FailureInfo(
        status=status,
        code=code if isinstance(code, str) else None,
        message=message if isinstance(message, str) else None,
        retry_after=_retry_after(getattr(response, "headers", None)),
    )


# --- circuit breaker -----------------------------------------------------------


@dataclass(frozen=True)
class BreakerConfig:
    failure_threshold: int = 5
    open_seconds: float = 30.0
    half_open_max_probes: int = 1

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if self.open_seconds < 0:
            raise ValueError("open_seconds must not be negative")
        if self.half_open_max_probes < 1:
            raise ValueError("half_open_max_probes must be at least 1")


class CircuitBreaker:
    """One provider's breaker. Not thread-safe on its own: use it through a
    ``BreakerRegistry``, which serialises access."""

    def __init__(
        self,
        config: BreakerConfig,
        clock: Callable[[], float],
        on_transition: Callable[[str, str], None] | None = None,
    ) -> None:
        self.config = config
        self._clock = clock
        self._notify = on_transition
        self.state = CLOSED
        self.failures = 0
        self.opened_at = 0.0
        self.probes = 0

    def _move(self, to: str) -> None:
        if to == self.state:
            return
        before, self.state = self.state, to
        if to == OPEN:
            self.opened_at = self._clock()
        self.probes = 0
        if to == CLOSED:
            self.failures = 0
        if self._notify is not None:
            self._notify(before, to)

    def current_state(self) -> str:
        """The state, moving ``open`` to ``half_open`` once the wait is over."""
        if (
            self.state == OPEN
            and self._clock() - self.opened_at >= self.config.open_seconds
        ):
            self._move(HALF_OPEN)
        return self.state

    def allow(self) -> bool:
        """True when a call may go to the provider now (takes a probe slot
        when half-open; free it with a success, failure or ``release``)."""
        state = self.current_state()
        if state == CLOSED:
            return True
        if state == HALF_OPEN and self.probes < self.config.half_open_max_probes:
            self.probes += 1
            return True
        return False

    def record_success(self) -> None:
        if self.state == HALF_OPEN:
            self._move(CLOSED)
        elif self.state == CLOSED:
            self.failures = 0

    def record_failure(self) -> None:
        if self.state == HALF_OPEN:
            self._move(OPEN)
        elif self.state == CLOSED:
            self.failures += 1
            if self.failures >= self.config.failure_threshold:
                self._move(OPEN)

    def release(self) -> None:
        """Ends a call that says nothing about the provider's health."""
        if self.state == HALF_OPEN and self.probes > 0:
            self.probes -= 1

    def force_open(self) -> None:
        """Opens the breaker now, e.g. to drain a provider by hand."""
        if self.state == OPEN:
            self.opened_at = self._clock()
        else:
            self._move(OPEN)


class BreakerRegistry:
    """Per-provider breakers for this process, bounded to ``max_entries``.

    Thread-safe. ``on_transition(provider, from_state, to_state)`` is called
    for every state change, with the registry lock held: keep it cheap and
    non-blocking. ``metrics()`` returns the transition counters.
    """

    def __init__(
        self,
        config: BreakerConfig | None = None,
        *,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
        on_transition: Callable[[str, str, str], None] | None = None,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self.config = config or BreakerConfig()
        self.max_entries = max_entries
        self._clock = clock
        self._on_transition = on_transition
        self._breakers: OrderedDict[str, CircuitBreaker] = OrderedDict()
        self._lock = threading.RLock()
        self._transitions = {OPEN: 0, HALF_OPEN: 0, CLOSED: 0}
        self._evictions = 0

    def _get(self, provider: str) -> CircuitBreaker:
        breaker = self._breakers.get(provider)
        if breaker is None:
            breaker = CircuitBreaker(
                self.config, self._clock, lambda a, b, p=provider: self._moved(p, a, b)
            )
            self._breakers[provider] = breaker
            while len(self._breakers) > self.max_entries:
                self._breakers.popitem(last=False)
                self._evictions += 1
        else:
            self._breakers.move_to_end(provider)
        return breaker

    def _moved(self, provider: str, before: str, after: str) -> None:
        self._transitions[after] += 1
        if self._on_transition is not None:
            # Metrics must never break a call.
            with contextlib.suppress(Exception):
                self._on_transition(provider, before, after)

    def allow(self, provider: str) -> bool:
        with self._lock:
            return self._get(provider).allow()

    def record_success(self, provider: str) -> None:
        with self._lock:
            self._get(provider).record_success()

    def record_failure(self, provider: str) -> None:
        with self._lock:
            self._get(provider).record_failure()

    def release(self, provider: str) -> None:
        with self._lock:
            self._get(provider).release()

    def force_open(self, provider: str) -> None:
        with self._lock:
            self._get(provider).force_open()

    def state(self, provider: str) -> str:
        """The provider's state; ``closed`` for a provider never seen."""
        with self._lock:
            breaker = self._breakers.get(provider)
            return breaker.current_state() if breaker else CLOSED

    def snapshot(self) -> dict[str, str]:
        """Every tracked provider's state."""
        with self._lock:
            return {p: b.current_state() for p, b in self._breakers.items()}

    def metrics(self) -> dict[str, int]:
        """Transition counters since start: ``opened``, ``half_opened``,
        ``closed``, plus ``tracked`` providers and ``evicted`` breakers."""
        with self._lock:
            return {
                "opened": self._transitions[OPEN],
                "half_opened": self._transitions[HALF_OPEN],
                "closed": self._transitions[CLOSED],
                "tracked": len(self._breakers),
                "evicted": self._evictions,
            }

    def reset(self) -> None:
        with self._lock:
            self._breakers.clear()


_default_registry: BreakerRegistry | None = None
_default_lock = threading.Lock()


def default_registry() -> BreakerRegistry:
    """The process-wide registry engines use when given none."""
    global _default_registry
    with _default_lock:
        if _default_registry is None:
            _default_registry = BreakerRegistry()
        return _default_registry


# --- engine --------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One compliant way to serve the call. ``data`` is the caller's own
    (a policy tier, a client); the engine passes it through untouched."""

    provider: str
    model: str
    context_window: int | None = None
    data: Any = None


@dataclass(frozen=True)
class FailoverAttempt:
    """One call to a provider. ``error_class`` only, never a message."""

    provider: str
    model: str
    ok: bool
    latency_ms: int
    error_class: str | None = None
    status: int | None = None


@dataclass(frozen=True)
class Skipped:
    provider: str
    model: str
    reason: str


@dataclass(frozen=True)
class FailoverResult(Generic[T]):
    response: T
    candidate: Candidate
    attempts: tuple[FailoverAttempt, ...]
    skipped: tuple[Skipped, ...] = ()

    @property
    def fallback_chain(self) -> list[str]:
        """The error class of each failed attempt, in order (PRD §7.12)."""
        return chain_of(self.attempts)


def chain_of(attempts: Sequence[FailoverAttempt]) -> list[str]:
    return [a.error_class for a in attempts if not a.ok and a.error_class]


class NoCompliantCandidate(PolicyViolation):
    """Every compliant candidate failed or was skipped: fail closed.

    The call is never sent to a provider outside the constraint set. The last
    provider error, if any, is the ``__cause__``.
    """

    def __init__(
        self,
        message: str,
        *,
        attempts: Sequence[FailoverAttempt] = (),
        skipped: Sequence[Skipped] = (),
    ) -> None:
        super().__init__(message)
        self.attempts = tuple(attempts)
        self.skipped = tuple(skipped)
        self.fallback_chain = chain_of(self.attempts)


def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


class _Run:
    """The state of one failover run, shared by the sync and async loops."""

    def __init__(
        self,
        engine: FailoverEngine,
        candidates: Sequence[Candidate],
        allow: Callable[[Candidate], bool] | None,
    ) -> None:
        self.engine = engine
        self.queue = list(candidates)
        self.allow = allow
        self.attempts: list[FailoverAttempt] = []
        self.skipped: list[Skipped] = []
        self.min_window: int | None = None  # set after a context-length failure
        self.rate_retries = 0
        self.current: Candidate | None = None

    def _skip(self, c: Candidate, reason: str) -> None:
        self.skipped.append(Skipped(c.provider, c.model, reason))

    def next_candidate(self) -> Candidate | None:
        breakers = self.engine.breakers
        while self.queue:
            c = self.queue.pop(0)
            if self.allow is not None and not self.allow(c):
                self._skip(c, SKIP_NON_COMPLIANT)
                continue
            if self.min_window is not None and (
                c.context_window is None or c.context_window <= self.min_window
            ):
                self._skip(c, SKIP_CONTEXT_WINDOW)
                continue
            if not breakers.allow(c.provider):
                self._skip(c, SKIP_CIRCUIT_OPEN)
                continue
            self.current = c
            self.rate_retries = 0
            return c
        self.current = None
        return None

    def succeeded(self, start: float) -> None:
        c = self.current
        assert c is not None
        self.engine.breakers.record_success(c.provider)
        self.attempts.append(FailoverAttempt(c.provider, c.model, True, _ms(start)))

    def failed(self, exc: BaseException, start: float) -> tuple[str, float]:
        """Records a failed attempt. Returns ``(action, wait)``: ``raise``,
        ``retry`` (same candidate after ``wait`` seconds) or ``next``."""
        c = self.current
        assert c is not None
        info = self.engine.failure_of(exc)
        cls = classify_failure(info)
        breakers = self.engine.breakers
        if cls.trips_breaker:
            breakers.record_failure(c.provider)
        else:
            breakers.release(c.provider)
        self.attempts.append(
            FailoverAttempt(
                c.provider, c.model, False, _ms(start), cls.error_class, info.status
            )
        )
        if cls.action == RAISE:
            return RAISE, 0.0
        if cls.action == RETRY_AFTER:
            wait = info.retry_after
            if (
                wait is not None
                and wait <= self.engine.max_wait
                and self.rate_retries < self.engine.rate_limit_retries
                and breakers.allow(c.provider)
            ):
                self.rate_retries += 1
                return "retry", wait
        if cls.action == NEXT_LARGER_CONTEXT:
            window = c.context_window or 0
            self.min_window = max(self.min_window or 0, window)
        return NEXT, 0.0

    def annotate(self, exc: BaseException) -> None:
        with contextlib.suppress(AttributeError, TypeError):
            exc.ps_attempts = tuple(self.attempts)  # type: ignore[attr-defined]
            exc.ps_fallback_chain = chain_of(self.attempts)  # type: ignore[attr-defined]

    def exhausted(self, last: BaseException | None) -> NoCompliantCandidate:
        if not self.attempts and not self.skipped:
            msg = "no compliant candidate to serve the call; failing closed"
        else:
            parts = [
                f"{a.provider}/{a.model}: {a.error_class}"
                for a in self.attempts
                if not a.ok
            ]
            parts += [
                f"{s.provider}/{s.model}: skipped ({s.reason})" for s in self.skipped
            ]
            msg = (
                "no compliant candidate left after "
                f"{len(self.attempts)} attempt(s); failing closed ("
                + "; ".join(parts)
                + ")"
            )
        err = NoCompliantCandidate(msg, attempts=self.attempts, skipped=self.skipped)
        err.__cause__ = last
        return err

    def result(self, response: Any) -> FailoverResult:
        assert self.current is not None
        return FailoverResult(
            response, self.current, tuple(self.attempts), tuple(self.skipped)
        )


class FailoverStream(Generic[T]):
    """A started stream: iterate it for the chunks. The attempts and the
    serving candidate are known before the first chunk is handed over. An
    error after that is raised as is: a stream that emitted is never retried."""

    def __init__(
        self, result: FailoverResult, first: list, rest: Any, breakers: BreakerRegistry
    ) -> None:
        self.result = result
        self.candidate = result.candidate
        self.attempts = result.attempts
        self.skipped = result.skipped
        self.fallback_chain = result.fallback_chain
        self._first = first
        self._rest = rest
        self._breakers = breakers
        self.interrupted = False

    def _interrupted(self, exc: BaseException) -> None:
        self.interrupted = True
        self._breakers.record_failure(self.candidate.provider)
        with contextlib.suppress(AttributeError, TypeError):
            exc.ps_error_class = "stream_interrupted"  # type: ignore[attr-defined]

    def __iter__(self) -> Iterator[T]:
        yield from self._first
        try:
            yield from self._rest
        except Exception as exc:
            self._interrupted(exc)
            raise

    async def __aiter__(self) -> AsyncIterator[T]:
        for chunk in self._first:
            yield chunk
        try:
            async for chunk in self._rest:
                yield chunk
        except Exception as exc:
            self._interrupted(exc)
            raise


class FailoverEngine:
    """Runs a call across ordered, already-compliant candidates (§7.5).

    ``breakers`` defaults to the process-wide registry. ``max_wait`` caps how
    long a 429's ``Retry-After`` is honoured (seconds); ``rate_limit_retries``
    is how many times one candidate is retried after a 429. ``failure_of``
    maps an exception to a ``FailureInfo`` (``failure_from_exception`` by
    default). ``sleep`` / ``async_sleep`` are injectable for tests.
    """

    def __init__(
        self,
        *,
        breakers: BreakerRegistry | None = None,
        max_wait: float = 10.0,
        rate_limit_retries: int = 1,
        failure_of: Callable[[BaseException], FailureInfo] = failure_from_exception,
        sleep: Callable[[float], None] = time.sleep,
        async_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.breakers = breakers if breakers is not None else default_registry()
        self.max_wait = max_wait
        self.rate_limit_retries = rate_limit_retries
        self.failure_of = failure_of
        self._sleep = sleep
        self._async_sleep = async_sleep

    def run(
        self,
        candidates: Sequence[Candidate],
        call: Callable[[Candidate], T],
        *,
        allow: Callable[[Candidate], bool] | None = None,
    ) -> FailoverResult[T]:
        run = _Run(self, candidates, allow)
        last: BaseException | None = None
        c = run.next_candidate()
        while c is not None:
            start = time.monotonic()
            try:
                response = call(c)
            except Exception as exc:
                last = exc
                action, wait = run.failed(exc, start)
                if action == RAISE:
                    run.annotate(exc)
                    raise
                if action == "retry":
                    self._sleep(wait)
                    continue
                c = run.next_candidate()
                continue
            run.succeeded(start)
            return run.result(response)
        raise run.exhausted(last)

    async def arun(
        self,
        candidates: Sequence[Candidate],
        call: Callable[[Candidate], Awaitable[T]],
        *,
        allow: Callable[[Candidate], bool] | None = None,
    ) -> FailoverResult[T]:
        run = _Run(self, candidates, allow)
        last: BaseException | None = None
        c = run.next_candidate()
        while c is not None:
            start = time.monotonic()
            try:
                response = await call(c)
            except Exception as exc:
                last = exc
                action, wait = run.failed(exc, start)
                if action == RAISE:
                    run.annotate(exc)
                    raise
                if action == "retry":
                    await self._async_sleep(wait)
                    continue
                c = run.next_candidate()
                continue
            run.succeeded(start)
            return run.result(response)
        raise run.exhausted(last)

    def run_stream(
        self,
        candidates: Sequence[Candidate],
        call: Callable[[Candidate], Any],
        *,
        allow: Callable[[Candidate], bool] | None = None,
    ) -> FailoverStream:
        """Like ``run`` for a call that returns an iterable of chunks.

        Opening the stream and reading its first chunk is the attempt: a
        failure there fails over like any other. Once a chunk exists the
        stream is handed back and never retried.
        """

        def opened(c: Candidate) -> tuple[list, Any]:
            it = iter(call(c))
            for chunk in it:
                return [chunk], it
            return [], iter(())

        result = self.run(candidates, opened, allow=allow)
        first, rest = result.response
        return FailoverStream(result, first, rest, self.breakers)

    async def arun_stream(
        self,
        candidates: Sequence[Candidate],
        call: Callable[[Candidate], Awaitable[Any]],
        *,
        allow: Callable[[Candidate], bool] | None = None,
    ) -> FailoverStream:
        """``run_stream`` for an awaitable returning an async iterable."""

        async def opened(c: Candidate) -> tuple[list, Any]:
            it = (await call(c)).__aiter__()
            try:
                first = await it.__anext__()
            except StopAsyncIteration:
                return [], _empty()
            return [first], it

        result = await self.arun(candidates, opened, allow=allow)
        first, rest = result.response
        return FailoverStream(result, first, rest, self.breakers)


async def _empty() -> AsyncIterator[Any]:
    return
    yield  # pragma: no cover


__all__ = [
    "BreakerConfig",
    "BreakerRegistry",
    "Candidate",
    "CircuitBreaker",
    "Classification",
    "FailoverAttempt",
    "FailoverEngine",
    "FailoverResult",
    "FailoverStream",
    "FailureInfo",
    "NoCompliantCandidate",
    "Skipped",
    "chain_of",
    "classify_failure",
    "default_registry",
    "failure_from_exception",
]
