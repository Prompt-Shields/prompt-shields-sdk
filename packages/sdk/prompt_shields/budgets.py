"""Spend budgets (budget.v1, PRD 0001 §7.8): a ``BudgetStore`` and enforcement.

A budget caps the USD spend of one attribution subject (a business unit, a
use case, an owner, or everything) per UTC day, ISO week or calendar month,
with mode ``warn``, ``downgrade`` or ``block``. The document format is
``schemas/budget/budget.v1.schema.json``; the counter key format, shared
with the gateway, is in ``schemas/budget/README.md`` and pinned by
``schemas/budget/vectors/``.

Two stores:

* ``InProcessBudgetStore`` (default): counters in this process's memory.
  **Per-process only**: each process (worker, replica, pod) has its own
  counters and they are lost on restart, so N processes can together spend
  up to N times a limit. Use it for one process, or as a soft guard.
* ``RedisBudgetStore``: counters in Redis, shared by every process that uses
  the same Redis and namespace (``pip install 'prompt-shields[optimizer-redis]'``).
  Increments are atomic (one Lua script: ``INCRBYFLOAT`` plus ``EXPIRE`` on
  the first write of a window).

Enforcement checks the spend already recorded before a call and records the
call's cost after it. Calls in flight at the same time are not counted
against each other, so a limit can be overshot by up to the cost of the
calls running concurrently when it is reached.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("prompt_shields.budgets")

SCHEMA_VERSION = "budget.v1"
#: Every counter key starts with this; ``v1`` is the key format's version.
KEY_PREFIX = "ps:budget:v1"
SCOPES = ("global", "business_unit", "use_case", "owner")
WINDOWS = ("day", "week", "month")
MODES = ("warn", "downgrade", "block")
#: A counter outlives its window by this long, then expires.
GRACE_SECONDS = 86_400
#: Key segment used as the subject of a ``global`` budget.
GLOBAL_SUBJECT = "_"

#: ``budget_state`` values from best to worst. ``unknown``: the store could
#: not be read (and ``on_store_error`` is ``open``, or the budget is ``warn``).
STATES = ("ok", "warn", "unknown", "exceeded")
_SEVERITY = {s: i for i, s in enumerate(STATES)}

_DEFAULT_WARN_AT = 0.8
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)


class BudgetError(ValueError):
    """An invalid budget document or store configuration."""


class BudgetExceeded(Exception):
    """A ``block`` budget is exhausted (or unreadable with
    ``on_store_error="closed"``): the call was refused and nothing was sent."""

    def __init__(self, message: str, *, budget_id: str, state: str,
                 spent_usd: float | None, limit_usd: float) -> None:
        super().__init__(message)
        self.budget_id = budget_id
        self.state = state
        self.spent_usd = spent_usd
        self.limit_usd = limit_usd


# --- document ---------------------------------------------------------------------


@dataclass(frozen=True)
class Budget:
    id: str
    scope: str
    window: str
    limit_usd: float
    mode: str
    subject: str | None = None
    warn_at: float = _DEFAULT_WARN_AT
    downgrade_model: str | None = None

    def subject_for(self, attributes: Mapping[str, Any]) -> str | None:
        """The counter subject for a call with ``attributes``, or ``None``
        when this budget does not apply to it."""
        if self.scope == "global":
            return GLOBAL_SUBJECT
        value = attributes.get(self.scope)
        if not isinstance(value, str) or not value:
            return None
        if self.subject == "*" or self.subject == value:
            return value
        return None


@dataclass(frozen=True)
class BudgetPolicy:
    budgets: tuple[Budget, ...]
    namespace: str = "default"
    on_store_error: str = "open"


def _token(value: Any, what: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", value):
        raise BudgetError(f"{what} must match ^[a-z0-9][a-z0-9._-]*$, got {value!r}")
    return value


def _number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _parse_budget(raw: Any) -> Budget:
    if not isinstance(raw, Mapping):
        raise BudgetError("each budget must be an object")
    known = {"id", "scope", "subject", "window", "limit_usd", "mode", "warn_at",
             "downgrade_model"}
    extra = sorted(set(raw) - known)
    if extra:
        raise BudgetError(f"budget {raw.get('id')!r}: unknown field(s) {extra}")
    bid = _token(raw.get("id"), "budget id")
    scope = raw.get("scope")
    if scope not in SCOPES:
        raise BudgetError(f"budget {bid!r}: scope must be one of {SCOPES}")
    subject = raw.get("subject")
    if scope == "global":
        if subject is not None:
            raise BudgetError(f"budget {bid!r}: a global budget has no subject")
    elif not isinstance(subject, str) or not subject:
        raise BudgetError(f"budget {bid!r}: scope {scope!r} needs a subject")
    window = raw.get("window")
    if window not in WINDOWS:
        raise BudgetError(f"budget {bid!r}: window must be one of {WINDOWS}")
    limit = raw.get("limit_usd")
    if not _number(limit) or limit < 0:
        raise BudgetError(f"budget {bid!r}: limit_usd must be a number >= 0")
    mode = raw.get("mode")
    if mode not in MODES:
        raise BudgetError(f"budget {bid!r}: mode must be one of {MODES}")
    warn_at = raw.get("warn_at", _DEFAULT_WARN_AT)
    if not _number(warn_at) or not 0 < warn_at <= 1:
        raise BudgetError(f"budget {bid!r}: warn_at must be in (0, 1]")
    downgrade_model = raw.get("downgrade_model")
    if downgrade_model is not None:
        if mode != "downgrade":
            raise BudgetError(f"budget {bid!r}: downgrade_model needs mode 'downgrade'")
        if not isinstance(downgrade_model, str) or not downgrade_model:
            raise BudgetError(f"budget {bid!r}: downgrade_model must be a model id")
    return Budget(bid, scope, window, float(limit), mode, subject, float(warn_at),
                  downgrade_model)


def load_budget_policy(doc: BudgetPolicy | Mapping[str, Any] | str | os.PathLike
                       ) -> BudgetPolicy:
    """A budget.v1 document from a mapping or a ``.json`` / ``.yaml`` file."""
    if isinstance(doc, BudgetPolicy):
        return doc
    if not isinstance(doc, Mapping):
        path = Path(doc)
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() in (".yaml", ".yml"):
            try:
                import yaml
            except ImportError as exc:  # pragma: no cover - depends on env
                raise BudgetError(
                    "reading a YAML budget document needs PyYAML (pip install pyyaml)"
                ) from exc
            doc = yaml.safe_load(text)
        else:
            doc = json.loads(text)
    if not isinstance(doc, Mapping):
        raise BudgetError("a budget document must be an object")
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise BudgetError(f"budget document must have schema_version {SCHEMA_VERSION!r}")
    extra = sorted(set(doc) - {"$schema", "schema_version", "namespace",
                               "on_store_error", "budgets"})
    if extra:
        raise BudgetError(f"budget document: unknown field(s) {extra}")
    raw = doc.get("budgets")
    if not isinstance(raw, list):
        raise BudgetError("budget document needs a 'budgets' list")
    budgets = tuple(_parse_budget(b) for b in raw)
    ids = [b.id for b in budgets]
    if len(set(ids)) != len(ids):
        raise BudgetError("budget ids must be unique")
    on_error = doc.get("on_store_error", "open")
    if on_error not in ("open", "closed"):
        raise BudgetError("on_store_error must be 'open' or 'closed'")
    namespace = _token(doc.get("namespace", "default"), "namespace")
    return BudgetPolicy(budgets, namespace, on_error)


# --- keys ---------------------------------------------------------------------------


def encode_segment(value: str) -> str:
    """Percent-encode every UTF-8 byte outside ``[A-Za-z0-9._~-]`` as ``%XX``
    (uppercase hex). ``:`` can then only be a key separator."""
    out = []
    for ch in value:
        if ch in _UNRESERVED:
            out.append(ch)
        else:
            out.extend(f"%{b:02X}" for b in ch.encode("utf-8"))
    return "".join(out)


def window_period(window: str, now: datetime) -> tuple[str, datetime]:
    """``(period label, period end)`` of the UTC ``window`` containing ``now``."""
    now = now.astimezone(UTC)
    day = datetime(now.year, now.month, now.day, tzinfo=UTC)
    if window == "day":
        return day.strftime("%Y-%m-%d"), day + timedelta(days=1)
    if window == "week":
        iso = now.isocalendar()
        start = day - timedelta(days=now.weekday())
        return f"{iso.year:04d}-W{iso.week:02d}", start + timedelta(days=7)
    if window == "month":
        end = (
            datetime(now.year + 1, 1, 1, tzinfo=UTC) if now.month == 12
            else datetime(now.year, now.month + 1, 1, tzinfo=UTC)
        )
        return f"{now.year:04d}-{now.month:02d}", end
    raise BudgetError(f"window must be one of {WINDOWS}, got {window!r}")


def counter_key(namespace: str, scope: str, subject: str, window: str,
                period: str) -> str:
    """``ps:budget:v1:{namespace}:{scope}:{subject}:{window}:{period}``."""
    return ":".join((KEY_PREFIX, encode_segment(namespace), scope,
                     encode_segment(subject), window, period))


def counter_ttl(window: str, now: datetime) -> int:
    """Seconds a counter written at ``now`` lives: to its window's end plus
    ``GRACE_SECONDS``."""
    _, end = window_period(window, now)
    return math.ceil((end - now.astimezone(UTC)).total_seconds()) + GRACE_SECONDS


# --- stores ---------------------------------------------------------------------------


@runtime_checkable
class BudgetStore(Protocol):
    """Where spend counters live. Values are USD floats; a missing or expired
    counter reads as 0. ``blocking`` is True when calls do network I/O (async
    callers then run them in a thread)."""

    blocking: bool

    def get_many(self, keys: Sequence[str]) -> list[float]: ...

    def add(self, key: str, amount: float, ttl_seconds: int) -> float:
        """Atomically add ``amount`` and return the new value. The TTL is set
        only when the counter has none (its first write in a window)."""
        ...


class InProcessBudgetStore:
    """Counters in this process's memory (thread-safe).

    Per-process only: other processes, replicas and restarts do not see
    these counters. Use ``RedisBudgetStore`` to share spend across them.
    """

    blocking = False
    _PRUNE_EVERY = 1024

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._values: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._writes = 0

    def _live(self, key: str, now: float) -> tuple[float, float] | None:
        entry = self._values.get(key)
        if entry is not None and entry[1] <= now:
            del self._values[key]
            return None
        return entry

    def get_many(self, keys: Sequence[str]) -> list[float]:
        now = self._clock()
        with self._lock:
            return [(e[0] if (e := self._live(k, now)) else 0.0) for k in keys]

    def add(self, key: str, amount: float, ttl_seconds: int) -> float:
        now = self._clock()
        with self._lock:
            entry = self._live(key, now)
            value = (entry[0] if entry else 0.0) + amount
            self._values[key] = (value, entry[1] if entry else now + ttl_seconds)
            self._writes += 1
            if self._writes % self._PRUNE_EVERY == 0:
                for k in [k for k, (_, exp) in self._values.items() if exp <= now]:
                    del self._values[k]
            return value


# KEYS[1] counter, ARGV[1] increment (USD), ARGV[2] TTL seconds.
_INCR_LUA = """
local v = redis.call('INCRBYFLOAT', KEYS[1], ARGV[1])
if redis.call('TTL', KEYS[1]) < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return v
"""


class RedisBudgetStore:
    """Counters in Redis, shared by every process using the same Redis and
    namespace. Needs the ``optimizer-redis`` extra (``redis``).

    Pass a ``redis.Redis`` client, or ``url`` (default
    ``redis://localhost:6379/0``). Without explicit ``socket_timeout`` /
    ``socket_connect_timeout`` a URL-built client gets 0.5 s for each, so an
    unreachable Redis costs a call at most about a second before
    ``on_store_error`` applies.
    """

    blocking = True

    def __init__(self, client: Any = None, *, url: str | None = None,
                 **redis_kwargs: Any) -> None:
        if client is None:
            try:
                import redis
            except ImportError as exc:
                raise BudgetError(
                    "RedisBudgetStore needs the redis package: "
                    "pip install 'prompt-shields[optimizer-redis]'"
                ) from exc
            redis_kwargs.setdefault("socket_timeout", 0.5)
            redis_kwargs.setdefault("socket_connect_timeout", 0.5)
            client = redis.Redis.from_url(
                url or os.environ.get("PS_BUDGET_REDIS_URL") or "redis://localhost:6379/0",
                **redis_kwargs,
            )
        self._client = client
        self._incr = client.register_script(_INCR_LUA)

    def get_many(self, keys: Sequence[str]) -> list[float]:
        if not keys:
            return []
        return [float(v) if v is not None else 0.0 for v in self._client.mget(list(keys))]

    def add(self, key: str, amount: float, ttl_seconds: int) -> float:
        return float(self._incr(keys=[key], args=[repr(float(amount)), int(ttl_seconds)]))


# --- decisions ----------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetStatus:
    """One matching budget at check time. ``spent_usd`` is None when the
    store could not be read."""

    budget: Budget
    key: str
    ttl_seconds: int
    spent_usd: float | None
    state: str


@dataclass(frozen=True)
class BudgetCheck:
    """The budgets that apply to one call, before it is sent.

    ``state`` / ``budget_id``: the worst state among matching budgets and the
    first budget in that state (both None when no budget matches).
    ``action``: ``block`` (refuse), ``downgrade`` (an exhausted downgrade
    budget) or ``none``. ``ceiling_usd``: the least remaining amount of the
    matching downgrade budgets, which routers use as a cost ceiling (None
    without one). ``downgrade_model``: for plain clients, from the first
    exhausted downgrade budget that names one.
    """

    statuses: tuple[BudgetStatus, ...] = ()
    state: str | None = None
    budget_id: str | None = None
    action: str = "none"
    ceiling_usd: float | None = None
    downgrade_model: str | None = None
    blocked_by: BudgetStatus | None = field(default=None, repr=False)

    def counter_keys(self) -> list[tuple[str, int]]:
        """Distinct ``(key, ttl)`` pairs the call's cost is added to."""
        seen: dict[str, int] = {}
        for s in self.statuses:
            seen.setdefault(s.key, s.ttl_seconds)
        return list(seen.items())

    def telemetry(self, action: str = "none") -> dict:
        """``budget_state`` / ``budget_id`` / ``budget_action`` event fields
        (empty when no budget matches). ``action`` is what was done:
        ``none`` or ``downgraded``."""
        if self.state is None:
            return {}
        return {"budget_state": self.state, "budget_id": self.budget_id,
                "budget_action": action}


def budget_state(budget: Budget, spent: float | None) -> str:
    """``ok`` / ``warn`` / ``exceeded`` for ``spent`` (``unknown`` when None)."""
    if spent is None:
        return "unknown"
    if spent >= budget.limit_usd:
        return "exceeded"
    if spent >= budget.warn_at * budget.limit_usd:
        return "warn"
    return "ok"


def decide(statuses: Sequence[BudgetStatus], on_store_error: str = "open") -> BudgetCheck:
    """Combine the matching budgets' statuses into one ``BudgetCheck``.

    A block budget blocks once exhausted; a downgrade budget contributes its
    remaining amount (``limit - spent``, at least 0) to ``ceiling_usd`` and
    asks for a downgrade once exhausted. An unreadable budget (``unknown``)
    does nothing with ``on_store_error="open"``; with ``"closed"`` it acts as
    exhausted (block blocks, downgrade gets ceiling 0). Warn budgets never act.
    """
    if not statuses:
        return BudgetCheck()
    worst = max(statuses, key=lambda s: _SEVERITY[s.state])  # first on ties
    action = "none"
    ceiling: float | None = None
    downgrade_model: str | None = None
    blocked_by: BudgetStatus | None = None
    for s in statuses:
        b = s.budget
        exhausted = s.state == "exceeded" or (
            s.state == "unknown" and on_store_error == "closed"
        )
        if b.mode == "block" and exhausted and blocked_by is None:
            blocked_by = s
        elif b.mode == "downgrade":
            if s.spent_usd is None:
                if on_store_error != "closed":
                    continue
                remaining = 0.0
            else:
                remaining = max(b.limit_usd - s.spent_usd, 0.0)
            ceiling = remaining if ceiling is None else min(ceiling, remaining)
            if exhausted:
                if action == "none":
                    action = "downgrade"
                if downgrade_model is None:
                    downgrade_model = b.downgrade_model
    if blocked_by is not None:
        action = "block"
    return BudgetCheck(tuple(statuses), worst.state, worst.budget.id, action,
                       ceiling, downgrade_model, blocked_by)


def _now() -> datetime:
    return datetime.now(UTC)


class BudgetEnforcer:
    """Checks a call's budgets before it is sent and records its cost after.

    ``budgets`` is a budget.v1 document (mapping, path or ``BudgetPolicy``);
    ``store`` defaults to a new ``InProcessBudgetStore`` (per-process).
    """

    def __init__(self, budgets: BudgetPolicy | Mapping[str, Any] | str | os.PathLike,
                 store: BudgetStore | None = None, *,
                 clock: Callable[[], datetime] = _now) -> None:
        self.policy = load_budget_policy(budgets)
        self.store: BudgetStore = store if store is not None else InProcessBudgetStore()
        self._clock = clock

    def _match(self, attributes: Mapping[str, Any]) -> list[tuple[Budget, str, int]]:
        now = self._clock()
        out = []
        for b in self.policy.budgets:
            subject = b.subject_for(attributes)
            if subject is None:
                continue
            period, _ = window_period(b.window, now)
            key = counter_key(self.policy.namespace, b.scope, subject, b.window, period)
            out.append((b, key, counter_ttl(b.window, now)))
        return out

    def check(self, attributes: Mapping[str, Any]) -> BudgetCheck:
        """The ``BudgetCheck`` for a call with these attribution
        ``attributes`` (``business_unit`` / ``use_case`` / ``owner``)."""
        matched = self._match(attributes)
        if not matched:
            return BudgetCheck()
        spent: list[float | None]
        try:
            spent = list(self.store.get_many([k for _, k, _ in matched]))
        except Exception as exc:  # noqa: BLE001 - any store failure
            logger.warning("budget store read failed (%s); on_store_error=%s",
                           type(exc).__name__, self.policy.on_store_error)
            spent = [None] * len(matched)
        statuses = [
            BudgetStatus(b, key, ttl, s, budget_state(b, s))
            for (b, key, ttl), s in zip(matched, spent, strict=True)
        ]
        result = decide(statuses, self.policy.on_store_error)
        if result.state == "exceeded":
            logger.warning("budget %r is exhausted", result.budget_id)
        return result

    async def acheck(self, attributes: Mapping[str, Any]) -> BudgetCheck:
        """``check`` without blocking the event loop on a network store."""
        if getattr(self.store, "blocking", True):
            return await asyncio.to_thread(self.check, attributes)
        return self.check(attributes)

    def commit(self, check: BudgetCheck, cost: float | None) -> None:
        """Add a call's ``cost`` to its counters. Never raises: the call has
        already happened."""
        if cost is None or not _number(cost) or cost <= 0:
            return
        for key, ttl in check.counter_keys():
            try:
                self.store.add(key, float(cost), ttl)
            except Exception as exc:  # noqa: BLE001 - recording is best-effort
                logger.warning("budget store write failed (%s); spend not recorded",
                               type(exc).__name__)

    async def acommit(self, check: BudgetCheck, cost: float | None) -> None:
        if getattr(self.store, "blocking", True):
            await asyncio.to_thread(self.commit, check, cost)
        else:
            self.commit(check, cost)

    def commit_soon(self, check: BudgetCheck, cost: float | None) -> None:
        """``commit`` from synchronous code that may run inside an event loop
        (a stream's final event): a network store is written from a thread."""
        if not check.statuses:
            return
        if getattr(self.store, "blocking", True):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                loop.run_in_executor(None, self.commit, check, cost)
                return
        self.commit(check, cost)

    def raise_if_blocked(self, check: BudgetCheck) -> None:
        if check.action != "block" or check.blocked_by is None:
            return
        s = check.blocked_by
        why = ("is exhausted" if s.state == "exceeded"
               else "could not be read (on_store_error='closed')")
        raise BudgetExceeded(
            f"budget {s.budget.id!r} {why}; the call was not sent",
            budget_id=s.budget.id, state=s.state, spent_usd=s.spent_usd,
            limit_usd=s.budget.limit_usd,
        )


def budget_event_hook(
    inner: Callable[[dict], None] | None,
    enforcer: BudgetEnforcer,
    check: BudgetCheck,
    action: str = "none",
    requested_model: str | None = None,
) -> Callable[[dict], None]:
    """A telemetry event hook for a plain client's call: runs ``inner``, adds
    the budget fields, and records the event's cost against the budgets."""

    def hook(event: dict) -> None:
        if inner is not None:
            inner(event)
        event.update(check.telemetry(action))
        if requested_model is not None:
            event["requested_model"] = requested_model
        enforcer.commit_soon(check, event.get("cost"))

    return hook


def plain_client_model(
    check: BudgetCheck, model: str, allow_downgrade: bool
) -> tuple[str, str | None, str]:
    """``(model to send, requested model or None, budget_action)`` for a plain
    client's call.

    A plain client runs no route policy, so a switch to ``downgrade_model``
    is not checked against any data policy or sensitivity rule, and on an
    OpenAI-compatible endpoint that serves many vendors (an aggregator, a
    proxy) the model id can select a different provider or region. The
    switch therefore happens only when the client was built with
    ``allow_budget_downgrade=True``; otherwise the call is sent unchanged
    (like a ``warn`` budget) and a warning is logged. ``ShieldsRouter``
    downgrades within its data policy and needs no opt-in.
    """
    if (
        check.action != "downgrade"
        or not check.downgrade_model
        or check.downgrade_model == model
    ):
        return model, None, "none"
    if not allow_downgrade:
        logger.warning(
            "budget %r is exhausted but its downgrade_model was not applied: "
            "a plain client switches models only with allow_budget_downgrade=True "
            "(the switch is not checked against any data policy)",
            check.budget_id,
        )
        return model, None, "none"
    return check.downgrade_model, model, "downgraded"


def warn_unapplied_downgrade(enforcer: BudgetEnforcer | None,
                             allow_downgrade: bool) -> None:
    """Log once, at client construction, when a budget names a
    ``downgrade_model`` that this plain client will not apply."""
    if enforcer is None or allow_downgrade:
        return
    named = [b.id for b in enforcer.policy.budgets if b.downgrade_model]
    if named:
        logger.warning(
            "budget(s) %s name a downgrade_model, which this plain client does "
            "not apply without allow_budget_downgrade=True; exhausted, they "
            "only warn", named,
        )


def make_enforcer(budgets: Any, store: BudgetStore | None) -> BudgetEnforcer | None:
    """``budgets`` as an enforcer: None stays None, an enforcer is kept."""
    if budgets is None:
        if store is not None:
            raise BudgetError("budget_store given without budgets")
        return None
    if isinstance(budgets, BudgetEnforcer):
        if store is not None:
            raise BudgetError("pass budget_store to the BudgetEnforcer, not both")
        return budgets
    return BudgetEnforcer(budgets, store)
