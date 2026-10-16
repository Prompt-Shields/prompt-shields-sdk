"""Python reference router for ``route-policy.v1`` (``schemas/route-policy/``).

Policy replay (``ps-teardown``, #13) runs every logged call through this
router. It is a port of the gateway router
(``gateway/src/middlewares/router/``: ``index.ts``, ``policy.ts``,
``heuristic.ts``) and must reach the same decision for the same input; both
implementations run the golden vectors in ``schemas/route-policy/vectors/``.
Where JavaScript and Python differ (string length, number parsing, object key
order, case-insensitive regex) this module follows the JavaScript behaviour.

Pure and deterministic: no network, no clock, no randomness. The policy is the
route-policy.v1 document as parsed JSON (a ``dict``); it is not modified.

What it decides today, as the gateway does: explicit group (``X-PS-Route``) >
quality hint (``X-PS-Quality``) > ``heuristic`` strategy, with the
``max_cost`` budget clamp and the unknown-group fallback. Not implemented yet
(P1, the vectors for them are ``pending``): sensitivity classification and PII
escalation, the data-policy constraint and its fail-closed ``PolicyViolation``,
and the per-use-case quality bar. ``resolve_route`` ignores the
``sensitivity``, ``data_policy`` and ``use_cases`` sections of a policy, so a
replay with a policy that sets them must not report its results as
policy-compliant.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

Quality = Literal["draft", "balanced", "critical"]

QUALITIES: tuple[str, ...] = ("draft", "balanced", "critical")

#: Assumed completion length when the caller gives no bound.
DEFAULT_OUTPUT_TOKENS = 512

#: Input-token count above which the heuristic treats a request as complex.
COMPLEX_TOKEN_THRESHOLD = 2000

#: Strategies this router implements. The schema also allows ``cheapest``,
#: ``latency`` and ``learned``; those raise instead of silently using another.
SUPPORTED_STRATEGIES = frozenset({"heuristic"})


@dataclass(frozen=True)
class ParsedRouteHints:
    """Hints parsed off the inbound ``X-PS-*`` headers."""

    quality: Quality | None = None
    max_cost: float | None = None
    explicit_group: str | None = None
    cache_off: bool = False


@dataclass
class RouteRequest:
    """What the router is asked to decide for a single call."""

    messages: list[Any]
    requested_model: str = "auto"
    approx_input_tokens: int = 0
    max_output_tokens: int | None = None
    quality: Quality | None = None
    max_cost: float | None = None
    explicit_group: str | None = None


@dataclass(frozen=True)
class RouteDecision:
    """The router's answer: a concrete model, its group, why, and its cost."""

    model: str
    group: str
    reason: str
    est_cost: float


@dataclass(frozen=True)
class _Pick:
    model: str
    group: str
    est_cost: float
    under_budget: bool = field(default=True, compare=False)


# --- JavaScript-compatible helpers ------------------------------------------

_JS_DECIMAL = re.compile(
    r"[+-]?(?:Infinity|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)", re.ASCII
)
_JS_RADIX = re.compile(r"0([xX][0-9a-fA-F]+|[oO][0-7]+|[bB][01]+)")
# Characters JS String.prototype.trim() strips (WhiteSpace + LineTerminator).
_JS_WS = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def _js_number(text: str) -> float:
    """``Number(text)`` for a string: NaN when JavaScript would give NaN."""
    s = text.strip(_JS_WS)
    if s == "":
        return 0.0
    m = _JS_RADIX.fullmatch(s)
    if m:
        value = int(m.group(1)[1:], {"x": 16, "o": 8, "b": 2}[m.group(1)[0].lower()])
        try:
            return float(value)
        except OverflowError:  # JS gives Infinity past the double range
            return math.inf
    if _JS_DECIMAL.fullmatch(s):
        return float(s.replace("Infinity", "inf"))
    return math.nan


def _js_length(text: str) -> int:
    """``text.length`` in JavaScript: UTF-16 code units, not code points."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*")


def _js_keys(obj: Mapping[str, Any]) -> list[str]:
    """``Object.keys(obj)`` order: array-index keys ascending, then insertion order."""
    index_keys = []
    other = []
    for key in obj:
        if _ARRAY_INDEX.fullmatch(key) and int(key) < 2**32 - 1:
            index_keys.append(key)
        else:
            other.append(key)
    return sorted(index_keys, key=int) + other


# --- request building --------------------------------------------------------


def _header(headers: Mapping[str, Any], name: str) -> str | None:
    target = name.lower()
    for key in headers or {}:
        if key.lower() == target:
            value = headers[key]
            if value is None:
                return None
            if isinstance(value, bool):
                return "true" if value else "false"
            return str(value)
    return None


def parse_route_headers(headers: Mapping[str, Any]) -> ParsedRouteHints:
    """Parse ``X-PS-Quality``, ``X-PS-Max-Cost``, ``X-PS-Route`` and ``X-PS-Cache``.

    Header names are case-insensitive. An unknown quality and a max cost that
    is not a number are ignored, as in the gateway.
    """
    quality = _header(headers, "x-ps-quality")
    max_cost_raw = _header(headers, "x-ps-max-cost")
    max_cost = None
    if max_cost_raw is not None:
        parsed = _js_number(max_cost_raw)
        if not math.isnan(parsed):
            max_cost = parsed
    return ParsedRouteHints(
        quality=quality if quality in QUALITIES else None,  # type: ignore[arg-type]
        max_cost=max_cost,
        explicit_group=_header(headers, "x-ps-route") or None,
        cache_off=(_header(headers, "x-ps-cache") or "").lower() == "off",
    )


def estimate_input_tokens(messages: Any) -> int:
    """About 4 characters per token over all message text: ``ceil(chars / 4)``."""
    chars = 0
    for message in messages or []:
        content = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(content, str):
            chars += _js_length(content)
        elif isinstance(content, list):
            for part in content:
                text = part.get("text") if isinstance(part, Mapping) else None
                if isinstance(text, str):
                    chars += _js_length(text)
    return math.ceil(chars / 4)


def build_route_request(
    body: Mapping[str, Any] | None, hints: ParsedRouteHints
) -> RouteRequest:
    """Build a :class:`RouteRequest` from an OpenAI-style request body and hints."""
    body = body or {}
    messages = body.get("messages") or []
    max_tokens = body.get("max_tokens")
    if max_tokens is None:
        max_tokens = body.get("max_completion_tokens")
    return RouteRequest(
        messages=messages,
        requested_model=body.get("model") or "auto",
        approx_input_tokens=estimate_input_tokens(messages),
        max_output_tokens=max_tokens,
        quality=hints.quality,
        max_cost=hints.max_cost,
        explicit_group=hints.explicit_group,
    )


# --- pricing and candidate selection -----------------------------------------


def estimate_tier_cost(
    tier: Mapping[str, Any], input_tokens: float, output_tokens: float
) -> float:
    """Estimated USD cost of one route-policy.v1 ``modelTier`` for these token counts."""
    return (input_tokens / 1000) * tier["input_cost_per_1k"] + (
        output_tokens / 1000
    ) * tier["output_cost_per_1k"]


def _out_tokens(req: RouteRequest) -> float:
    return (
        DEFAULT_OUTPUT_TOKENS
        if req.max_output_tokens is None
        else req.max_output_tokens
    )


def _tiers(policy: Mapping[str, Any], group: str) -> list[Mapping[str, Any]]:
    tiers = policy["groups"].get(group)
    return tiers if isinstance(tiers, list) else []


def _pick_cheapest_in(
    tiers: list[Mapping[str, Any]], group: str, req: RouteRequest
) -> _Pick | None:
    """Cheapest tier in one group. On a tie the first listed tier wins."""
    out = _out_tokens(req)
    best: _Pick | None = None
    best_cost = math.inf
    for tier in tiers:
        cost = estimate_tier_cost(tier, req.approx_input_tokens, out)
        if cost < best_cost:
            best = _Pick(tier["model"], group, cost)
            best_cost = cost
    return best


def _cheapest_under_budget(
    policy: Mapping[str, Any], max_cost: float, req: RouteRequest
) -> _Pick | None:
    """Most capable (highest-cost) tier across all groups with cost <= max_cost.

    When nothing fits, the globally cheapest tier with ``under_budget=False``.
    """
    out = _out_tokens(req)
    under: _Pick | None = None
    cheapest: _Pick | None = None
    for group in _js_keys(policy["groups"]):
        for tier in _tiers(policy, group):
            cost = estimate_tier_cost(tier, req.approx_input_tokens, out)
            if cheapest is None or cost < cheapest.est_cost:
                cheapest = _Pick(tier["model"], group, cost)
            if cost <= max_cost and (under is None or cost > under.est_cost):
                under = _Pick(tier["model"], group, cost)
    if under is not None:
        return under
    if cheapest is not None:
        return _Pick(
            cheapest.model, cheapest.group, cheapest.est_cost, under_budget=False
        )
    return None


def _default_fallback(
    policy: Mapping[str, Any], req: RouteRequest, reason: str
) -> RouteDecision:
    default_group = policy["default_group"]
    fb = _pick_cheapest_in(_tiers(policy, default_group), default_group, req)
    return RouteDecision(
        model=fb.model if fb else req.requested_model,
        group=default_group,
        reason=reason,
        est_cost=fb.est_cost if fb else 0,
    )


def _resolve_in_group(
    policy: Mapping[str, Any], group: str, req: RouteRequest, prefix: str
) -> RouteDecision:
    tiers = _tiers(policy, group)
    if not tiers:
        return _default_fallback(policy, req, f"{prefix}:unknown-group-default")

    picked = _pick_cheapest_in(tiers, group, req)
    assert picked is not None
    if req.max_cost is not None and picked.est_cost > req.max_cost:
        clamped = _cheapest_under_budget(policy, req.max_cost, req)
        if clamped is not None:
            suffix = (
                "+budget-clamp" if clamped.under_budget else "+budget-unsatisfiable"
            )
            return RouteDecision(
                clamped.model, clamped.group, prefix + suffix, clamped.est_cost
            )
    return RouteDecision(picked.model, group, prefix, picked.est_cost)


# --- heuristic strategy ------------------------------------------------------

# JS /.../i without the u flag folds case for ASCII only, hence re.ASCII.
_JSON_SCHEMA = re.compile(
    r"json schema|response_format|strict json", re.IGNORECASE | re.ASCII
)


def _message_strings(messages: list[Any]) -> list[str]:
    out = []
    for m in messages:
        content = m.get("content") if isinstance(m, Mapping) else None
        if isinstance(content, str):
            out.append(content)
    return out


def _heuristic(req: RouteRequest, policy: Mapping[str, Any]) -> RouteDecision:
    texts = _message_strings(req.messages)
    complex_ = (
        req.approx_input_tokens > COMPLEX_TOKEN_THRESHOLD
        or any("```" in t for t in texts)
        or any(_JSON_SCHEMA.search(t) for t in texts)
    )
    label = "complex" if complex_ else "simple"
    group = "frontier" if complex_ else "cheap"
    picked = _pick_cheapest_in(_tiers(policy, group), group, req)

    if picked is None:
        return _default_fallback(policy, req, "heuristic:empty-group-fallback")

    if req.max_cost is not None and picked.est_cost > req.max_cost:
        clamped = _cheapest_under_budget(policy, req.max_cost, req)
        if clamped is not None:
            reason = (
                f"heuristic:{label}+budget-clamp"
                if clamped.under_budget
                else "heuristic:budget-unsatisfiable-cheapest"
            )
            return RouteDecision(clamped.model, clamped.group, reason, clamped.est_cost)

    return RouteDecision(picked.model, group, f"heuristic:{label}", picked.est_cost)


# --- entry point -------------------------------------------------------------


def resolve_route(req: RouteRequest, policy: Mapping[str, Any]) -> RouteDecision:
    """Decide the model for one call under a route-policy.v1 ``policy``.

    Precedence: explicit group > quality hint > the policy's strategy. The
    strategy is only consulted on the transparent path; only ``heuristic`` is
    implemented, and any other raises ``NotImplementedError`` there.
    ``sensitivity``, ``data_policy`` and ``use_cases`` are not applied yet.
    """
    if req.explicit_group:
        return _resolve_in_group(policy, req.explicit_group, req, "override")

    if req.quality:
        group = policy["quality_to_group"].get(req.quality)
        if group is None:
            group = policy["default_group"]
        return _resolve_in_group(policy, group, req, f"quality:{req.quality}")

    strategy = policy.get("strategy", "heuristic")
    if strategy not in SUPPORTED_STRATEGIES:
        raise NotImplementedError(
            f"route-policy strategy {strategy!r} is not implemented"
        )
    return _heuristic(req, policy)
