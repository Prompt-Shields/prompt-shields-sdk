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

Two entry points:

* ``resolve_route`` decides what the gateway decides today: explicit group
  (``X-PS-Route``) > quality hint (``X-PS-Quality``) > ``heuristic``
  strategy, with the ``max_cost`` budget clamp and the unknown-group
  fallback. It ignores the ``sensitivity``, ``data_policy`` and ``use_cases``
  sections of a policy, as the gateway does.
* ``resolve_policy_route`` applies the whole policy, as policy replay needs
  it: sensitivity classification with escalate-only PII escalation
  (``classify_sensitivity``), the data-policy constraint with its fail-closed
  ``PolicyViolation``, and the per-use-case quality bar, then the same
  precedence and budget clamp as ``resolve_route`` over the remaining
  candidates. The gateway does not implement these stages yet, so their
  vectors (``sensitivity.json``, ``data-policy.json``, ``quality-bar.json``)
  stay ``pending`` for it; the Python runner checks them against this
  function.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping
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
    # Only read by resolve_policy_route.
    data_classification: str | None = None
    pii_detected: list[str] = field(default_factory=list)
    use_case: str | None = None


@dataclass(frozen=True)
class RouteDecision:
    """The router's answer: a concrete model, its group, why, and its cost.

    ``sensitivity_level`` and ``sensitivity_source`` are set by
    ``resolve_policy_route`` only.
    """

    model: str
    group: str
    reason: str
    est_cost: float
    sensitivity_level: str | None = None
    sensitivity_source: str | None = None


class PolicyViolation(Exception):
    """No candidate the data policy (and use-case bar) allows: fail closed.

    ``fallback.no_compliant_candidate`` is always ``fail_closed``: the call is
    never served by a non-compliant model.
    """

    def __init__(
        self,
        message: str,
        *,
        sensitivity_level: str | None = None,
        sensitivity_source: str | None = None,
        group: str | None = None,
    ) -> None:
        super().__init__(message)
        self.sensitivity_level = sensitivity_level
        self.sensitivity_source = sensitivity_source
        self.group = group


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
    policy: Mapping[str, Any],
    max_cost: float,
    req: RouteRequest,
    allow: Callable[[Mapping[str, Any]], bool] | None = None,
) -> _Pick | None:
    """Most capable (highest-cost) tier across all groups with cost <= max_cost.

    When nothing fits, the globally cheapest tier with ``under_budget=False``.
    ``allow`` limits the tiers considered (data policy, use-case bar).
    """
    out = _out_tokens(req)
    under: _Pick | None = None
    cheapest: _Pick | None = None
    for group in _js_keys(policy["groups"]):
        for tier in _tiers(policy, group):
            if allow is not None and not allow(tier):
                continue
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


def _is_complex(req: RouteRequest) -> bool:
    texts = _message_strings(req.messages)
    return (
        req.approx_input_tokens > COMPLEX_TOKEN_THRESHOLD
        or any("```" in t for t in texts)
        or any(_JSON_SCHEMA.search(t) for t in texts)
    )


def _heuristic(req: RouteRequest, policy: Mapping[str, Any]) -> RouteDecision:
    complex_ = _is_complex(req)
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


# --- full policy: sensitivity, data policy, use-case quality bar --------------

DEFAULT_LEVELS: tuple[str, ...] = ("public", "internal", "confidential", "restricted")

#: Country codes inside the European Economic Area. A rule that allows ``EEA``
#: also allows a candidate whose jurisdiction is one of these. The reverse is
#: not true: a rule that allows only ``NO`` does not allow a candidate that is
#: only known to be somewhere in the ``EEA``.
# fmt: off
EEA_COUNTRIES = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
    "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
    "SI", "ES", "SE", "IS", "LI", "NO",
})
# fmt: on


def classify_sensitivity(
    policy: Mapping[str, Any],
    declared: str | None,
    pii_detected: Iterable[str] = (),
) -> tuple[str | None, str]:
    """Effective sensitivity level of one call and where it came from.

    ``effective = max(declared, or unclassified_default if none,
    every pii_escalation level for each detected category)``. Escalate-only:
    detection never lowers a level. A declared level that is not one of the
    policy's ``levels`` counts as unclassified. The source is ``declared``,
    ``default`` or ``pii_escalated`` (only when a detection raised the level).

    A policy without a ``sensitivity`` section has no levels: the declared
    value is passed through (``None`` when there is none) and nothing escalates.
    """
    sensitivity = policy.get("sensitivity")
    if not sensitivity:
        return (declared, "declared") if declared else (None, "default")
    levels = list(sensitivity.get("levels") or DEFAULT_LEVELS)
    rank = {level: i for i, level in enumerate(levels)}
    if declared in rank:
        level, source = declared, "declared"
    else:
        level, source = sensitivity["unclassified_default"], "default"
    escalation = sensitivity.get("pii_escalation") or {}
    for category in pii_detected or ():
        escalated = escalation.get(category)
        if escalated in rank and rank[escalated] > rank.get(level, -1):
            level, source = escalated, "pii_escalated"
    return level, source


def data_policy_rule(
    policy: Mapping[str, Any], level: str | None
) -> Mapping[str, Any] | None:
    """The ``data_policy`` rule for ``level``; ``None`` means no constraint."""
    if level is None:
        return None
    return (policy.get("data_policy") or {}).get(level)


def _jurisdiction_allowed(jurisdiction: Any, allowed: Iterable[str]) -> bool:
    allowed = set(allowed)
    if "any" in allowed:
        return True
    if not isinstance(jurisdiction, str):
        return False
    return jurisdiction in allowed or (
        "EEA" in allowed and jurisdiction in EEA_COUNTRIES
    )


def tier_complies(tier: Mapping[str, Any], rule: Mapping[str, Any] | None) -> bool:
    """True when a ``modelTier`` meets a ``dataPolicyRule``.

    Fails closed on missing metadata: a candidate with no ``jurisdiction``
    only passes ``any``, one with no ``provider`` never passes a provider
    allow-list, ``require_zdr`` needs ``zdr: true`` and
    ``us_cloud_act_exposure: false`` needs the candidate to state ``false``.
    """
    if rule is None:
        return True
    if not _jurisdiction_allowed(tier.get("jurisdiction"), rule["jurisdictions"]):
        return False
    providers = rule.get("providers")
    if providers is not None and tier.get("provider") not in providers:
        return False
    if rule.get("require_zdr") and tier.get("zdr") is not True:
        return False
    return not (
        rule.get("us_cloud_act_exposure") is False
        and tier.get("us_cloud_act_exposure") is not False
    )


def _group_rank(policy: Mapping[str, Any], group: str) -> int | None:
    order = policy.get("group_order") or []
    return order.index(group) if group in order else None


def resolve_policy_route(req: RouteRequest, policy: Mapping[str, Any]) -> RouteDecision:
    """Decide the model for one call under the whole route-policy.v1 ``policy``.

    Precedence: data policy > explicit group > hints > use-case quality bar >
    strategy.

    1. ``classify_sensitivity`` gives the effective level.
    2. Only candidates that meet the level's ``data_policy`` rule are kept,
       in every group and in the budget clamp.
    3. The group comes from the explicit group, else the quality hint, else
       the ``heuristic`` strategy; an unknown or empty group (as written in
       the policy, before filtering) falls back to ``default_group``.
    4. Unless the group was given explicitly, the use case's bar applies:
       ``min_group`` raises the group (by ``group_order``) and an ``eval_set``
       limits candidates to ``eligible_models``. When no eligible candidate
       is left in the group, the next groups up ``group_order`` are tried.
    5. Cheapest remaining candidate in the group, then the ``max_cost`` clamp
       over all allowed candidates.

    Raises ``PolicyViolation`` when no allowed candidate is left. It never
    falls back to a non-compliant model.
    """
    level, source = classify_sensitivity(
        policy, req.data_classification, req.pii_detected
    )
    rule = data_policy_rule(policy, level)

    bar = None
    if req.explicit_group is None and req.use_case is not None:
        bar = (policy.get("use_cases") or {}).get(req.use_case)
    eligible: set[str] | None = None
    if bar and bar.get("eval_set"):
        eligible = set(bar.get("eligible_models") or ())

    def allow(tier: Mapping[str, Any]) -> bool:
        if eligible is not None and tier["model"] not in eligible:
            return False
        return tier_complies(tier, rule)

    def violation(msg: str, group: str | None) -> PolicyViolation:
        return PolicyViolation(
            msg, sensitivity_level=level, sensitivity_source=source, group=group
        )

    # 3. Pick the group as resolve_route would.
    if req.explicit_group:
        group, reason = req.explicit_group, "override"
        if not _tiers(policy, group):
            group, reason = policy["default_group"], "override:unknown-group-default"
    elif req.quality:
        group = policy["quality_to_group"].get(req.quality) or policy["default_group"]
        reason = f"quality:{req.quality}"
        if not _tiers(policy, group):
            group = policy["default_group"]
            reason += ":unknown-group-default"
    else:
        strategy = policy.get("strategy", "heuristic")
        if strategy not in SUPPORTED_STRATEGIES:
            raise NotImplementedError(
                f"route-policy strategy {strategy!r} is not implemented"
            )
        label = "complex" if _is_complex(req) else "simple"
        group = "frontier" if label == "complex" else "cheap"
        reason = f"heuristic:{label}"
        if not _tiers(policy, group):
            group, reason = policy["default_group"], "heuristic:empty-group-fallback"

    # 4. Use-case quality bar (not on the explicit path).
    if bar and bar.get("min_group"):
        have, need = _group_rank(policy, group), _group_rank(policy, bar["min_group"])
        if need is not None and (have is None or have < need):
            group = bar["min_group"]
            reason += "+use-case-min-group"

    candidates = [t for t in _tiers(policy, group) if allow(t)]
    if not candidates and eligible is not None:
        order = policy.get("group_order") or []
        start = _group_rank(policy, group)
        for higher in order[(start + 1 if start is not None else len(order)) :]:
            candidates = [t for t in _tiers(policy, higher) if allow(t)]
            if candidates:
                group = higher
                reason += "+use-case-eligible"
                break
    if not candidates:
        raise violation(
            f"no candidate in group {group!r} is allowed at sensitivity {level!r}",
            group,
        )

    # 5. Cheapest allowed candidate, then the budget clamp.
    picked = _pick_cheapest_in(candidates, group, req)
    assert picked is not None
    if req.max_cost is not None and picked.est_cost > req.max_cost:
        clamped = _cheapest_under_budget(policy, req.max_cost, req, allow)
        if clamped is not None:
            # Same reason strings as resolve_route for the same path.
            if clamped.under_budget:
                reason += "+budget-clamp"
            elif reason.startswith("heuristic:") and "+" not in reason:
                reason = "heuristic:budget-unsatisfiable-cheapest"
            else:
                reason += "+budget-unsatisfiable"
            picked = clamped
    return RouteDecision(
        picked.model, picked.group, reason, picked.est_cost, level, source
    )
