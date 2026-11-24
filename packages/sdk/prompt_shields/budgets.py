"""Spend budgets (budget.v1, PRD 0001 §7.8): a ``BudgetStore`` and enforcement.

A budget caps the USD spend, or the tokens, of one attribution subject (a
business unit, use case, owner, team, project, user, calling service,
requested model, or everything) per UTC day, ISO week or calendar month,
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
  the same Redis and namespace, and with the gateway
  (``pip install 'prompt-shields[optimizer-redis]'``). Every operation is
  one Lua script, so increments are atomic and a counter never exists
  without its TTL.

Enforcement, as the gateway does it:

1. ``check``: read every matching budget's counter (plus in-flight
   reservations) and decide; an exhausted ``block`` budget refuses the call
   (``BudgetExceeded``) before anything is sent.
2. ``reserve``: hold the call's pre-call estimate (USD and tokens) on each
   counter. For block budgets this is an atomic check-and-reserve, so calls
   running at the same time, in any process or gateway replica sharing the
   Redis, see each other.
3. ``commit``: after the call, drop the reservation and add the actual cost
   and tokens (the estimate when the call was not priced); ``release`` on a
   failed call adds nothing. An unsettled reservation lapses after
   ``reservation_ttl_seconds``.

When a counter crosses a budget's ``warn_at`` share of its limit, or the
limit itself, ``on_threshold`` is called once with a ``BudgetThreshold``
(and a warning is logged). The crossing is seen by the write that crosses
it, which is atomic, so with a shared Redis exactly one process reports it.
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
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("prompt_shields.budgets")

SCHEMA_VERSION = "budget.v1"
#: Every counter key starts with this; ``v1`` is the key format's version.
KEY_PREFIX = "ps:budget:v1"
#: ``global`` plus the attribution attributes a budget can be keyed on.
SCOPES = ("global", "business_unit", "use_case", "owner", "team", "project",
          "user", "service", "model")
ATTRIBUTE_SCOPES = SCOPES[1:]
WINDOWS = ("day", "week", "month")
MODES = ("warn", "downgrade", "block")
#: What a budget limits: ``usd`` (``limit_usd``) or ``tokens`` (``limit_tokens``).
UNITS = ("usd", "tokens")
#: Suffix of a token counter's key (a USD counter has none).
TOKENS_SUFFIX = ":tokens"
#: A counter outlives its window by this long, then expires.
GRACE_SECONDS = 86_400
#: Key segment used as the subject of a ``global`` budget.
GLOBAL_SUBJECT = "_"
#: How long an unsettled reservation holds budget (the gateway's default).
DEFAULT_RESERVATION_TTL_SECONDS = 15 * 60

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
    ``on_store_error="closed"``): the call was refused and nothing was sent.

    ``unit`` is ``usd`` or ``tokens``; ``limit`` and ``spent`` are in that
    unit (``limit_usd`` / ``spent_usd`` are kept for USD budgets and are
    None for a token budget)."""

    def __init__(self, message: str, *, budget_id: str, state: str,
                 spent_usd: float | None, limit_usd: float | None,
                 unit: str = "usd", limit: float | None = None,
                 spent: float | None = None) -> None:
        super().__init__(message)
        self.budget_id = budget_id
        self.state = state
        self.spent_usd = spent_usd
        self.limit_usd = limit_usd
        self.unit = unit
        self.limit = limit if limit is not None else limit_usd
        self.spent = spent if spent is not None else spent_usd


# --- document ---------------------------------------------------------------------


@dataclass(frozen=True)
class Budget:
    id: str
    scope: str
    window: str
    limit_usd: float | None
    mode: str
    subject: str | None = None
    warn_at: float = _DEFAULT_WARN_AT
    downgrade_model: str | None = None
    limit_tokens: int | None = None

    @property
    def unit(self) -> str:
        return "tokens" if self.limit_tokens is not None else "usd"

    @property
    def limit(self) -> float:
        """The limit in the budget's ``unit``."""
        if self.limit_tokens is not None:
            return float(self.limit_tokens)
        return float(self.limit_usd or 0.0)

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


BUDGET_FIELDS = frozenset({"id", "scope", "subject", "window", "limit_usd",
                           "limit_tokens", "mode", "warn_at", "downgrade_model"})


def parse_budget(raw: Any) -> Budget:
    """One budget object of a budget.v1 document. Raises ``BudgetError``."""
    if not isinstance(raw, Mapping):
        raise BudgetError("each budget must be an object")
    extra = sorted(set(raw) - BUDGET_FIELDS)
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
    has_usd, has_tokens = "limit_usd" in raw, "limit_tokens" in raw
    if has_usd == has_tokens:
        raise BudgetError(f"budget {bid!r}: needs exactly one of limit_usd, limit_tokens")
    limit_usd: float | None = None
    limit_tokens: int | None = None
    if has_usd:
        limit = raw["limit_usd"]
        if not _number(limit) or limit < 0:
            raise BudgetError(f"budget {bid!r}: limit_usd must be a number >= 0")
        limit_usd = float(limit)
    else:
        limit = raw["limit_tokens"]
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise BudgetError(f"budget {bid!r}: limit_tokens must be an integer >= 0")
        limit_tokens = limit
    mode = raw.get("mode")
    if mode not in MODES:
        raise BudgetError(f"budget {bid!r}: mode must be one of {MODES}")
    if mode == "downgrade" and limit_tokens is not None:
        raise BudgetError(f"budget {bid!r}: a token budget's mode must be 'warn' or 'block'")
    warn_at = raw.get("warn_at", _DEFAULT_WARN_AT)
    if not _number(warn_at) or not 0 < warn_at <= 1:
        raise BudgetError(f"budget {bid!r}: warn_at must be in (0, 1]")
    downgrade_model = raw.get("downgrade_model")
    if downgrade_model is not None:
        if mode != "downgrade":
            raise BudgetError(f"budget {bid!r}: downgrade_model needs mode 'downgrade'")
        if not isinstance(downgrade_model, str) or not downgrade_model:
            raise BudgetError(f"budget {bid!r}: downgrade_model must be a model id")
    return Budget(bid, scope, window, limit_usd, mode, subject, float(warn_at),
                  downgrade_model, limit_tokens)


def parse_budgets(raw: Any) -> tuple[Budget, ...]:
    """A ``budgets`` list: every budget valid, ids unique."""
    if not isinstance(raw, list):
        raise BudgetError("budget document needs a 'budgets' list")
    budgets = tuple(parse_budget(b) for b in raw)
    ids = [b.id for b in budgets]
    if len(set(ids)) != len(ids):
        raise BudgetError("budget ids must be unique")
    return budgets


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
    budgets = parse_budgets(doc.get("budgets"))
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
                period: str, unit: str = "usd") -> str:
    """``ps:budget:v1:{namespace}:{scope}:{subject}:{window}:{period}``, with
    ``:tokens`` appended for a token counter."""
    key = ":".join((KEY_PREFIX, encode_segment(namespace), scope,
                    encode_segment(subject), window, period))
    return key + TOKENS_SUFFIX if unit == "tokens" else key


def reservations_key(key: str) -> str:
    """The sorted set of in-flight reservations on counter ``key`` (shared
    with the gateway; the braces keep it in the counter's Cluster slot)."""
    return "{" + key + "}:res"


def counter_ttl(window: str, now: datetime) -> int:
    """Seconds a counter written at ``now`` lives: to its window's end plus
    ``GRACE_SECONDS``."""
    _, end = window_period(window, now)
    return math.ceil((end - now.astimezone(UTC)).total_seconds()) + GRACE_SECONDS


# --- stores ---------------------------------------------------------------------------


@runtime_checkable
class BudgetStore(Protocol):
    """Where spend counters live. Values are USD (or token) floats; a missing
    or expired counter reads as 0. ``blocking`` is True when calls do network
    I/O (async callers then run them in a thread).

    A store may also implement ``ReservingBudgetStore`` (both built-in stores
    do); without it, calls are checked against settled spend only and calls
    in flight at the same time do not see each other.
    """

    blocking: bool

    def get_many(self, keys: Sequence[str]) -> list[float]: ...

    def add(self, key: str, amount: float, ttl_seconds: int) -> float:
        """Atomically add ``amount`` and return the new value. The TTL is set
        only when the counter has none (its first write in a window)."""
        ...


@runtime_checkable
class ReservingBudgetStore(BudgetStore, Protocol):
    """A store with in-flight reservations, in the gateway's format: a
    reservation is a member ``{id}|{amount}`` of the sorted set
    ``reservations_key(key)``, scored by its expiry (epoch ms)."""

    def read(self, keys: Sequence[str]) -> list[tuple[float, float]]:
        """``(settled, reserved)`` per key; expired reservations dropped."""
        ...

    def reserve(self, key: str, member: str, amount: float, ttl_ms: int,
                limit: float | None) -> tuple[bool, float, float]:
        """Hold ``amount`` on ``key`` as ``member`` unless ``limit`` is given
        and settled + reserved >= ``limit`` (then hold nothing). Returns
        ``(ok, settled, reserved)`` as seen before the reservation. Atomic."""
        ...

    def settle(self, key: str, member: str, actual: float, ttl_seconds: int) -> float:
        """Drop ``member`` and add ``actual`` (when > 0) to the counter with
        the shared TTL rule; returns the counter's new value. Atomic."""
        ...


def _reservation_member(rid: str, amount: float) -> str:
    # The gateway writes `${id}|${amount}` and sums the part after the last
    # `|`; repr() of a float is a number Lua's tonumber reads.
    return f"{rid}|{float(amount)!r}"


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
        self._reservations: dict[str, dict[str, tuple[float, float]]] = {}
        self._lock = threading.Lock()
        self._writes = 0

    def _live(self, key: str, now: float) -> tuple[float, float] | None:
        entry = self._values.get(key)
        if entry is not None and entry[1] <= now:
            del self._values[key]
            return None
        return entry

    def _reserved(self, key: str, now: float) -> float:
        res = self._reservations.get(key)
        if not res:
            return 0.0
        for member in [m for m, (_, exp) in res.items() if exp <= now]:
            del res[member]
        if not res:
            del self._reservations[key]
            return 0.0
        return sum(amount for amount, _ in res.values())

    def get_many(self, keys: Sequence[str]) -> list[float]:
        now = self._clock()
        with self._lock:
            return [(e[0] if (e := self._live(k, now)) else 0.0) for k in keys]

    def _add(self, key: str, amount: float, ttl_seconds: int, now: float) -> float:
        entry = self._live(key, now)
        value = (entry[0] if entry else 0.0) + amount
        self._values[key] = (value, entry[1] if entry else now + ttl_seconds)
        self._writes += 1
        if self._writes % self._PRUNE_EVERY == 0:
            for k in [k for k, (_, exp) in self._values.items() if exp <= now]:
                del self._values[k]
        return value

    def add(self, key: str, amount: float, ttl_seconds: int) -> float:
        now = self._clock()
        with self._lock:
            return self._add(key, amount, ttl_seconds, now)

    def read(self, keys: Sequence[str]) -> list[tuple[float, float]]:
        now = self._clock()
        with self._lock:
            return [((e[0] if (e := self._live(k, now)) else 0.0),
                     self._reserved(k, now)) for k in keys]

    def reserve(self, key: str, member: str, amount: float, ttl_ms: int,
                limit: float | None) -> tuple[bool, float, float]:
        now = self._clock()
        with self._lock:
            entry = self._live(key, now)
            settled = entry[0] if entry else 0.0
            reserved = self._reserved(key, now)
            if limit is not None and settled + reserved >= limit:
                return False, settled, reserved
            self._reservations.setdefault(key, {})[member] = (
                float(amount), now + ttl_ms / 1000.0)
            return True, settled, reserved

    def settle(self, key: str, member: str, actual: float, ttl_seconds: int) -> float:
        now = self._clock()
        with self._lock:
            res = self._reservations.get(key)
            if res is not None:
                res.pop(member, None)
                if not res:
                    del self._reservations[key]
            if actual > 0:
                return self._add(key, actual, ttl_seconds, now)
            entry = self._live(key, now)
            return entry[0] if entry else 0.0


# KEYS[1] counter, ARGV[1] increment, ARGV[2] TTL seconds.
_INCR_LUA = """
local v = redis.call('INCRBYFLOAT', KEYS[1], ARGV[1])
if redis.call('TTL', KEYS[1]) < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return v
"""

# The gateway's scripts (gateway/src/middlewares/psBudget/store.ts), so the
# SDK and the gateway read, reserve and settle the same keys the same way.
_SUM_RESERVED = """
local reserved = 0
for _, m in ipairs(redis.call('ZRANGE', KEYS[2], 0, -1)) do
  reserved = reserved + (tonumber(string.match(m, '|([^|]*)$')) or 0)
end
"""

# KEYS: counter, reservations. ARGV: now ms. -> {settled, reserved}
_READ_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', ARGV[1])
local spent = tonumber(redis.call('GET', KEYS[1]) or '0') or 0
""" + _SUM_RESERVED + """
return {tostring(spent), tostring(reserved)}
"""

# KEYS: counter, reservations. ARGV: now ms, member, reservation ttl ms,
# limit ('' for none). -> {ok, settled, reserved}
_RESERVE_LUA = """
local now = tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
local spent = tonumber(redis.call('GET', KEYS[1]) or '0') or 0
""" + _SUM_RESERVED + """
if ARGV[4] ~= '' and spent + reserved >= tonumber(ARGV[4]) then
  return {0, tostring(spent), tostring(reserved)}
end
redis.call('ZADD', KEYS[2], now + tonumber(ARGV[3]), ARGV[2])
redis.call('PEXPIRE', KEYS[2], ARGV[3])
return {1, tostring(spent), tostring(reserved)}
"""

# KEYS: counter, reservations. ARGV: member, actual, ttl seconds.
# -> the counter's new value.
_SETTLE_LUA = """
redis.call('ZREM', KEYS[2], ARGV[1])
local v = redis.call('GET', KEYS[1]) or '0'
if tonumber(ARGV[2]) > 0 then
  v = redis.call('INCRBYFLOAT', KEYS[1], ARGV[2])
  if redis.call('TTL', KEYS[1]) < 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[3])
  end
end
return v
"""


class RedisBudgetStore:
    """Counters in Redis, shared by every process using the same Redis and
    namespace, and with the gateway. Needs the ``optimizer-redis`` extra
    (``redis``).

    Pass a ``redis.Redis`` client, or ``url`` (default
    ``PS_BUDGET_REDIS_URL``, else ``redis://localhost:6379/0``). Without
    explicit ``socket_timeout`` / ``socket_connect_timeout`` a URL-built
    client gets 0.5 s for each, so an unreachable Redis costs a call at most
    about a second before ``on_store_error`` applies.
    """

    blocking = True

    def __init__(self, client: Any = None, *, url: str | None = None,
                 clock: Callable[[], float] = time.time,
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
        self._clock = clock
        self._incr = client.register_script(_INCR_LUA)
        self._read = client.register_script(_READ_LUA)
        self._reserve = client.register_script(_RESERVE_LUA)
        self._settle = client.register_script(_SETTLE_LUA)

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def get_many(self, keys: Sequence[str]) -> list[float]:
        if not keys:
            return []
        return [float(v) if v is not None else 0.0 for v in self._client.mget(list(keys))]

    def add(self, key: str, amount: float, ttl_seconds: int) -> float:
        return float(self._incr(keys=[key], args=[repr(float(amount)), int(ttl_seconds)]))

    def read(self, keys: Sequence[str]) -> list[tuple[float, float]]:
        now = self._now_ms()
        out = []
        for key in keys:
            spent, reserved = self._read(keys=[key, reservations_key(key)], args=[now])
            out.append((float(spent), float(reserved)))
        return out

    def reserve(self, key: str, member: str, amount: float, ttl_ms: int,
                limit: float | None) -> tuple[bool, float, float]:
        ok, spent, reserved = self._reserve(
            keys=[key, reservations_key(key)],
            args=[self._now_ms(), member, max(1, int(ttl_ms)),
                  "" if limit is None else repr(float(limit))],
        )
        return int(ok) == 1, float(spent), float(reserved)

    def settle(self, key: str, member: str, actual: float, ttl_seconds: int) -> float:
        return float(self._settle(
            keys=[key, reservations_key(key)],
            args=[member, repr(float(max(actual, 0.0))), int(ttl_seconds)],
        ))


def _reserving(store: Any) -> bool:
    return all(callable(getattr(store, m, None)) for m in ("read", "reserve", "settle"))


# --- decisions ----------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetStatus:
    """One matching budget at check time. ``spent_usd`` is the counter (plus
    in-flight reservations) in the budget's unit (USD, or tokens for a token
    budget; the name is kept for compatibility); None when the store could
    not be read."""

    budget: Budget
    key: str
    ttl_seconds: int
    spent_usd: float | None
    state: str
    subject: str | None = None
    period: str | None = None

    @property
    def spent(self) -> float | None:
        return self.spent_usd


@dataclass(frozen=True)
class BudgetThreshold:
    """A counter crossed a budget's ``warn_at`` share of its limit
    (``threshold="warn"``) or the limit itself (``"exceeded"``) with this
    write. ``spent`` is the counter after the write, in ``unit``."""

    budget_id: str
    threshold: str
    scope: str
    subject: str | None
    window: str
    period: str | None
    unit: str
    limit: float
    spent: float
    mode: str

    def as_dict(self) -> dict:
        return {
            "type": "budget.threshold_crossed", "budget_id": self.budget_id,
            "threshold": self.threshold, "scope": self.scope,
            "subject": self.subject, "window": self.window, "period": self.period,
            "unit": self.unit, "limit": self.limit, "spent": self.spent,
            "mode": self.mode,
        }


@dataclass(frozen=True)
class BudgetCheck:
    """The budgets that apply to one call, before it is sent.

    ``state`` / ``budget_id``: the worst state among matching budgets and the
    first budget in that state (both None when no budget matches).
    ``action``: ``block`` (refuse), ``downgrade`` (an exhausted downgrade
    budget) or ``none``. ``ceiling_usd``: the least remaining amount of the
    matching downgrade budgets, which routers use as a cost ceiling (None
    without one). ``downgrade_model``: for plain clients, from the first
    exhausted downgrade budget that names one. ``reservation_id`` /
    ``reservations``: the call's held estimate per counter, set by
    ``BudgetEnforcer.reserve``.
    """

    statuses: tuple[BudgetStatus, ...] = ()
    state: str | None = None
    budget_id: str | None = None
    action: str = "none"
    ceiling_usd: float | None = None
    downgrade_model: str | None = None
    blocked_by: BudgetStatus | None = field(default=None, repr=False)
    reservation_id: str | None = None
    reservations: tuple[tuple[str, str], ...] = ()

    def counter_keys(self) -> list[tuple[str, int]]:
        """Distinct ``(key, ttl)`` pairs the call's cost or tokens are added to."""
        return [(k, ttl) for k, ttl, _ in self.counter_units()]

    def counter_units(self) -> list[tuple[str, int, str]]:
        """Distinct ``(key, ttl, unit)`` triples."""
        seen: dict[str, tuple[int, str]] = {}
        for s in self.statuses:
            seen.setdefault(s.key, (s.ttl_seconds, s.budget.unit))
        return [(k, ttl, unit) for k, (ttl, unit) in seen.items()]

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
    if spent >= budget.limit:
        return "exceeded"
    if spent >= budget.warn_at * budget.limit:
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
                remaining = max(b.limit - s.spent_usd, 0.0)
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


def crossings(statuses: Sequence[BudgetStatus], key: str, before: float,
              after: float) -> list[BudgetThreshold]:
    """The thresholds a write on ``key`` from ``before`` to ``after``
    crossed, for the budgets on that counter (``before < t <= after``)."""
    out: list[BudgetThreshold] = []
    if not after > before:
        return out
    seen: set[str] = set()
    for s in statuses:
        b = s.budget
        if s.key != key or b.id in seen:
            continue
        seen.add(b.id)
        marks = [("exceeded", b.limit)]
        if b.warn_at < 1:
            marks.insert(0, ("warn", b.warn_at * b.limit))
        for name, mark in marks:
            if before < mark <= after:
                out.append(BudgetThreshold(b.id, name, b.scope, s.subject, b.window,
                                           s.period, b.unit, b.limit, after, b.mode))
    return out


def estimate_call(vendor: str | None, model: str | None, messages: Any,
                  max_tokens: int | None) -> tuple[float | None, int]:
    """A call's pre-call estimate: ``(USD or None when unpriced, tokens)``,
    from about 4 characters per input token plus ``max_tokens`` (else the
    route policy's default output) output tokens."""
    from prompt_shields.pricing import estimate_cost
    from prompt_shields.route_policy import DEFAULT_OUTPUT_TOKENS, estimate_input_tokens

    tokens_in = estimate_input_tokens(messages)
    tokens_out = (max_tokens if isinstance(max_tokens, int) and max_tokens > 0
                  else DEFAULT_OUTPUT_TOKENS)
    cost = None
    if vendor and model:
        try:
            cost = estimate_cost(vendor, model, tokens_in, tokens_out)
        except Exception:  # noqa: BLE001 - an estimate never fails the call
            cost = None
    return cost, tokens_in + tokens_out


def event_tokens(event: Mapping[str, Any]) -> int | None:
    """Total tokens of a telemetry event (input + output), None when absent."""
    tin, tout = event.get("tokens_in"), event.get("tokens_out")
    if not _number(tin) and not _number(tout):
        return None
    return int((tin if _number(tin) else 0) + (tout if _number(tout) else 0))


def budget_attributes(metadata: Mapping[str, Any] | None,
                      ps_metadata: Mapping[str, Any] | None = None,
                      model: str | None = None) -> dict:
    """A call's budget attributes from the client's metadata (``business_unit``,
    ``use_case``, ``owner``, ``calling_service``, and ``team`` / ``project``
    when set there), the call's ``ps_metadata`` (``team``, ``project``,
    ``user_id``) and the requested ``model``. ``user`` is the hashed user id
    (``hash_user_id``), never the raw one."""
    from prompt_shields.feedback import hash_user_id

    md = metadata or {}
    pm = ps_metadata or {}
    attrs: dict[str, Any] = {
        "business_unit": md.get("business_unit"),
        "use_case": md.get("use_case"),
        "owner": md.get("owner"),
        "service": md.get("calling_service") or md.get("service"),
        "team": pm.get("team") or md.get("team"),
        "project": pm.get("project") or md.get("project"),
        "model": model,
    }
    user = pm.get("user_id")
    attrs["user"] = hash_user_id(user) if isinstance(user, str) and user else None
    return attrs


def _now() -> datetime:
    return datetime.now(UTC)


class BudgetEnforcer:
    """Checks a call's budgets before it is sent and records its cost after.

    ``budgets`` is a budget.v1 document (mapping, path or ``BudgetPolicy``);
    ``store`` defaults to a new ``InProcessBudgetStore`` (per-process).
    ``on_threshold`` is called with a ``BudgetThreshold`` when a write
    crosses a budget's warn share or limit (errors it raises are logged and
    ignored). Thread-safe: the stores serialise their own writes, and the
    enforcer keeps no per-call state.
    """

    def __init__(self, budgets: BudgetPolicy | Mapping[str, Any] | str | os.PathLike,
                 store: BudgetStore | None = None, *,
                 clock: Callable[[], datetime] = _now,
                 on_threshold: Callable[[BudgetThreshold], None] | None = None,
                 reservation_ttl_seconds: float = DEFAULT_RESERVATION_TTL_SECONDS,
                 ) -> None:
        self.policy = load_budget_policy(budgets)
        self.store: BudgetStore = store if store is not None else InProcessBudgetStore()
        self._clock = clock
        self.on_threshold = on_threshold
        if not (_number(reservation_ttl_seconds) and reservation_ttl_seconds > 0):
            raise BudgetError("reservation_ttl_seconds must be a positive number")
        self.reservation_ttl_ms = int(reservation_ttl_seconds * 1000)

    @property
    def reserves(self) -> bool:
        """True when the store holds in-flight reservations."""
        return _reserving(self.store)

    def _match(self, attributes: Mapping[str, Any]
               ) -> list[tuple[Budget, str, int, str, str]]:
        now = self._clock()
        out = []
        for b in self.policy.budgets:
            subject = b.subject_for(attributes)
            if subject is None:
                continue
            period, _ = window_period(b.window, now)
            key = counter_key(self.policy.namespace, b.scope, subject, b.window,
                              period, b.unit)
            out.append((b, key, counter_ttl(b.window, now), subject, period))
        return out

    def _read(self, keys: list[str]) -> list[float]:
        if _reserving(self.store):
            return [s + r for s, r in self.store.read(keys)]  # type: ignore[attr-defined]
        return list(self.store.get_many(keys))

    def check(self, attributes: Mapping[str, Any]) -> BudgetCheck:
        """The ``BudgetCheck`` for a call with these attribution
        ``attributes`` (see ``ATTRIBUTE_SCOPES``; ``budget_attributes``
        builds them)."""
        matched = self._match(attributes)
        if not matched:
            return BudgetCheck()
        keys = list(dict.fromkeys(k for _, k, _, _, _ in matched))
        spent: dict[str, float | None]
        try:
            spent = dict(zip(keys, self._read(keys), strict=True))
        except Exception as exc:  # noqa: BLE001 - any store failure
            logger.warning("budget store read failed (%s); on_store_error=%s",
                           type(exc).__name__, self.policy.on_store_error)
            spent = dict.fromkeys(keys)
        statuses = [
            BudgetStatus(b, key, ttl, spent[key], budget_state(b, spent[key]),
                         subject, period)
            for b, key, ttl, subject, period in matched
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

    def reserve(self, check: BudgetCheck, cost: float | None = None,
                tokens: int | None = None) -> BudgetCheck:
        """Hold the call's estimate (``cost`` USD on USD counters, ``tokens``
        on token counters) and return the check with its reservations.

        For a counter with block budgets the reservation is refused when
        settled + reserved spend already reaches the tightest block limit
        (another call got there first); the returned check then has
        ``action="block"`` and ``raise_if_blocked`` raises. A store without
        reservations, a blocked or unreadable check, or no matching budget:
        the check is returned unchanged.
        """
        if (not check.statuses or check.action == "block"
                or check.reservation_id is not None or not _reserving(self.store)
                or any(s.spent_usd is None for s in check.statuses)):
            return check
        store: Any = self.store
        rid = uuid.uuid4().hex
        held: list[tuple[str, str, int]] = []

        def undo() -> None:
            for key, member, ttl in held:
                try:
                    store.settle(key, member, 0.0, ttl)
                except Exception as exc:  # noqa: BLE001 - it lapses on its own
                    logger.debug("budget reservation not released (%s)",
                                 type(exc).__name__)

        for key, ttl, unit in check.counter_units():
            amount = cost if unit == "usd" else tokens
            amount = float(amount) if _number(amount) and amount > 0 else 0.0
            blocking = [s for s in check.statuses
                        if s.key == key and s.budget.mode == "block"]
            tightest = min(blocking, key=lambda s: s.budget.limit) if blocking else None
            member = _reservation_member(rid, amount)
            try:
                ok, settled, reserved = store.reserve(
                    key, member, amount, self.reservation_ttl_ms,
                    tightest.budget.limit if tightest else None)
            except Exception as exc:  # noqa: BLE001 - any store failure
                logger.warning("budget store reserve failed (%s); on_store_error=%s",
                               type(exc).__name__, self.policy.on_store_error)
                if self.policy.on_store_error == "closed" and tightest is not None:
                    undo()
                    status = replace(tightest, spent_usd=None, state="unknown")
                    return replace(check, action="block", blocked_by=status,
                                   state="unknown", budget_id=tightest.budget.id)
                continue
            if not ok:
                undo()
                assert tightest is not None
                status = replace(tightest, spent_usd=settled + reserved,
                                 state="exceeded")
                logger.warning("budget %r is exhausted", tightest.budget.id)
                return replace(check, action="block", blocked_by=status,
                               state="exceeded", budget_id=tightest.budget.id)
            held.append((key, member, ttl))
        return replace(check, reservation_id=rid,
                       reservations=tuple((k, m) for k, m, _ in held))

    async def areserve(self, check: BudgetCheck, cost: float | None = None,
                       tokens: int | None = None) -> BudgetCheck:
        if getattr(self.store, "blocking", True) and check.statuses:
            return await asyncio.to_thread(self.reserve, check, cost, tokens)
        return self.reserve(check, cost, tokens)

    def _notify(self, events: list[BudgetThreshold]) -> None:
        for ev in events:
            logger.warning(
                "budget %r crossed %s: %s of %s %s (%s %s, per %s)", ev.budget_id,
                ev.threshold, round(ev.spent, 6), ev.limit, ev.unit, ev.scope,
                ev.subject, ev.window,
            )
            if self.on_threshold is not None:
                try:
                    self.on_threshold(ev)
                except Exception:
                    logger.exception("budget on_threshold callback failed")

    def commit(self, check: BudgetCheck, cost: float | None,
               tokens: int | None = None) -> None:
        """Add a call's actual ``cost`` (USD) and ``tokens`` to its counters,
        settling its reservations (a missing actual settles at the reserved
        estimate). Never raises: the call has already happened."""
        held = dict(check.reservations)
        events: list[BudgetThreshold] = []
        for key, ttl, unit in check.counter_units():
            actual = cost if unit == "usd" else tokens
            valid = _number(actual) and actual >= 0
            member = held.get(key)
            try:
                if member is not None:
                    if not valid:
                        actual = float(member.rsplit("|", 1)[1])
                    after = self.store.settle(  # type: ignore[attr-defined]
                        key, member, float(actual), ttl)
                elif valid and actual > 0:
                    after = self.store.add(key, float(actual), ttl)
                else:
                    continue
            except Exception as exc:  # noqa: BLE001 - recording is best-effort
                logger.warning("budget store write failed (%s); spend not recorded",
                               type(exc).__name__)
                continue
            events.extend(crossings(check.statuses, key, after - float(actual), after))
        self._notify(events)

    async def acommit(self, check: BudgetCheck, cost: float | None,
                      tokens: int | None = None) -> None:
        if getattr(self.store, "blocking", True):
            await asyncio.to_thread(self.commit, check, cost, tokens)
        else:
            self.commit(check, cost, tokens)

    def release(self, check: BudgetCheck) -> None:
        """Drop a failed call's reservations without adding anything."""
        if not check.reservations:
            return
        ttls = {k: ttl for k, ttl, _ in check.counter_units()}
        for key, member in check.reservations:
            try:
                self.store.settle(key, member, 0.0, ttls.get(key, 1))  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 - it lapses on its own
                logger.warning("budget store release failed (%s); the reservation "
                               "lapses on its own", type(exc).__name__)

    async def arelease(self, check: BudgetCheck) -> None:
        if getattr(self.store, "blocking", True) and check.reservations:
            await asyncio.to_thread(self.release, check)
        else:
            self.release(check)

    def commit_soon(self, check: BudgetCheck, cost: float | None,
                    tokens: int | None = None) -> None:
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
                loop.run_in_executor(None, self.commit, check, cost, tokens)
                return
        self.commit(check, cost, tokens)

    def raise_if_blocked(self, check: BudgetCheck) -> None:
        if check.action != "block" or check.blocked_by is None:
            return
        s = check.blocked_by
        b = s.budget
        why = ("is exhausted" if s.state == "exceeded"
               else "could not be read (on_store_error='closed')")
        usd = b.unit == "usd"
        raise BudgetExceeded(
            f"budget {b.id!r} {why}; the call was not sent",
            budget_id=b.id, state=s.state,
            spent_usd=s.spent_usd if usd else None,
            limit_usd=b.limit_usd, unit=b.unit, limit=b.limit, spent=s.spent_usd,
        )


def budget_event_hook(
    inner: Callable[[dict], None] | None,
    enforcer: BudgetEnforcer,
    check: BudgetCheck,
    action: str = "none",
    requested_model: str | None = None,
) -> Callable[[dict], None]:
    """A telemetry event hook for a plain client's call: runs ``inner``, adds
    the budget fields, and records the event's cost and tokens against the
    budgets (settling the call's reservations)."""

    def hook(event: dict) -> None:
        if inner is not None:
            inner(event)
        event.update(check.telemetry(action))
        if requested_model is not None:
            event["requested_model"] = requested_model
        enforcer.commit_soon(check, event.get("cost"), event_tokens(event))

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
