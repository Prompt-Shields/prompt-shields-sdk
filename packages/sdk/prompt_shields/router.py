"""ShieldsRouter: the SDK picks the model for each call (opt-in, PRD 0001 §7.11).

The other SDK clients never pick a model; they only send ``RouteHint`` as
``X-PS-*`` headers for the gateway. ``ShieldsRouter`` and
``AsyncShieldsRouter`` are the opt-in exception, for teams without the
gateway:

* ``mode="local"`` (default): every call is routed in this process under a
  route-policy.v1 document (``schemas/route-policy/``) by
  ``route_policy.resolve_policy_route``: sensitivity classification with
  escalate-only PII escalation, the fail-closed data policy, the use case's
  quality bar, then the hints and the budget clamp. The call is then sent
  with the caller's own key (BYOK) through the instrumented client for the
  chosen provider (``ShieldsClient`` / ``AsyncShieldsClient``), which emits
  the usual telemetry event plus the route decision.
* ``mode="gateway"``: the decision is left to the gateway. The call goes to
  the gateway with the hints as ``X-PS-*`` headers, as ``RouteHint`` does.

When no allowed candidate is left, ``PolicyViolation`` is raised and nothing
is sent: a call is never served by a non-compliant model.

Only candidates whose provider is configured are considered: a policy tier
whose provider has no entry in ``providers`` is removed before routing, the
same as a tier the data policy does not allow.

Every response comes back in the OpenAI chat-completions shape
(``RoutedChatCompletion``), whichever provider served it. The provider's own
response object is kept as ``resp.raw``, and ``resp.ps`` carries the route,
the cost and the attempts. Failover between providers is not done here yet
(#21): each call makes one attempt, recorded in ``resp.ps.attempts``.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from prompt_shields.pii import scan_messages
from prompt_shields.route_policy import (
    DEFAULT_OUTPUT_TOKENS,
    PolicyViolation,
    RouteDecision,
    RouteRequest,
    build_route_request,
    classify_sensitivity,
    data_policy_rule,
    estimate_tier_cost,
    parse_route_headers,
    resolve_policy_route,
    tier_complies,
)
from prompt_shields.types import PSMetadata, RouteHint

RouteMode = Literal["local", "gateway"]

#: Vendors a provider entry can dispatch through. Any other provider is
#: reached through the OpenAI SDK with its own ``base_url`` (OpenAI-compatible).
DISPATCH_VENDORS = ("openai", "anthropic")

#: ``cost_source`` when the provider and the pricing table gave no cost and
#: the event is priced at the policy tier's rates instead.
COST_SOURCE_ROUTE_POLICY = "route_policy"

# OpenAI request parameters that have no Anthropic equivalent here. Sending
# them to Anthropic would fail upstream, or change the call's meaning if
# dropped, so a call that uses them and is routed to Anthropic is refused.
_OPENAI_ONLY_PARAMS = frozenset({
    "tools", "tool_choice", "parallel_tool_calls", "functions", "function_call",
    "response_format", "n", "logprobs", "top_logprobs", "logit_bias", "seed",
    "presence_penalty", "frequency_penalty", "stream_options", "audio",
    "modalities", "prediction", "reasoning_effort", "store",
})

_ANTHROPIC_FINISH = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
    "pause_turn": "stop",
}


class RouterError(Exception):
    """The router cannot send this call: misconfiguration, an unknown pinned
    model, or a request it cannot translate for the chosen provider."""


# --- response shape -----------------------------------------------------------


@dataclass(frozen=True)
class RouteInfo:
    """Where a call was routed, and why."""

    mode: RouteMode
    requested_model: str
    model: str | None
    provider: str | None = None
    group: str | None = None
    reason: str | None = None
    jurisdiction: str | None = None
    policy_version: str | None = None
    sensitivity_level: str | None = None
    sensitivity_source: str | None = None
    est_cost: float | None = None


@dataclass(frozen=True)
class CostInfo:
    """``usd`` is the event's cost (``None`` when unknown), ``source`` how it
    was produced (the event's ``cost_source``), ``estimated_usd`` the router's
    estimate before the call."""

    usd: float | None
    source: str | None
    estimated_usd: float | None = None


@dataclass(frozen=True)
class Attempt:
    """One upstream attempt. ``error`` is the exception class name only."""

    provider: str | None
    model: str | None
    ok: bool
    latency_ms: int
    error: str | None = None


@dataclass(frozen=True)
class PSResult:
    route: RouteInfo
    cost: CostInfo
    attempts: tuple[Attempt, ...]


@dataclass
class FunctionCall:
    name: str
    arguments: str


@dataclass
class ToolCall:
    id: str
    function: FunctionCall
    type: str = "function"


@dataclass
class ChatMessage:
    role: str
    content: str | None
    tool_calls: list[ToolCall] | None = None
    refusal: str | None = None


@dataclass
class Choice:
    index: int
    message: ChatMessage
    finish_reason: str | None


@dataclass
class CompletionUsage:
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


@dataclass
class RoutedChatCompletion:
    """An OpenAI chat-completions response, whichever provider served it.

    ``raw`` is the provider's own response; ``ps`` the route, cost and
    attempts. ``to_dict()`` gives the OpenAI JSON shape (without ``raw`` and
    ``ps``).
    """

    id: str | None
    model: str | None
    created: int | None
    choices: list[Choice]
    usage: CompletionUsage | None
    raw: Any = field(repr=False, default=None)
    ps: PSResult | None = None
    object: str = "chat.completion"

    def to_dict(self) -> dict:
        def message(m: ChatMessage) -> dict:
            out: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.tool_calls:
                out["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in m.tool_calls
                ]
            if m.refusal is not None:
                out["refusal"] = m.refusal
            return out

        out: dict[str, Any] = {
            "id": self.id,
            "object": self.object,
            "created": self.created,
            "model": self.model,
            "choices": [
                {
                    "index": c.index,
                    "message": message(c.message),
                    "finish_reason": c.finish_reason,
                }
                for c in self.choices
            ],
        }
        if self.usage is not None:
            out["usage"] = {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
                "total_tokens": self.usage.total_tokens,
            }
        return out


def _field(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _usage(prompt: int | None, completion: int | None) -> CompletionUsage | None:
    if prompt is None and completion is None:
        return None
    total = (prompt or 0) + (completion or 0)
    return CompletionUsage(prompt, completion, total)


def from_openai(response: Any) -> RoutedChatCompletion:
    """Normalise a chat-completions response (SDK object or JSON dict)."""
    choices = []
    for i, choice in enumerate(_field(response, "choices") or ()):
        msg = _field(choice, "message")
        tool_calls = []
        for tc in _field(msg, "tool_calls") or ():
            fn = _field(tc, "function")
            tool_calls.append(ToolCall(
                id=_str(_field(tc, "id")) or "",
                function=FunctionCall(
                    name=_str(_field(fn, "name")) or "",
                    arguments=_str(_field(fn, "arguments")) or "",
                ),
                type=_str(_field(tc, "type")) or "function",
            ))
        index = _int(_field(choice, "index"))
        choices.append(Choice(
            index=index if index is not None else i,
            message=ChatMessage(
                role=_str(_field(msg, "role")) or "assistant",
                content=_str(_field(msg, "content")),
                tool_calls=tool_calls or None,
                refusal=_str(_field(msg, "refusal")),
            ),
            finish_reason=_str(_field(choice, "finish_reason")),
        ))
    usage = _field(response, "usage")
    prompt = _int(_field(usage, "prompt_tokens"))
    completion = _int(_field(usage, "completion_tokens"))
    normalised = _usage(prompt, completion)
    total = _int(_field(usage, "total_tokens"))
    if normalised is not None and total is not None:
        normalised.total_tokens = total
    return RoutedChatCompletion(
        id=_str(_field(response, "id")),
        model=_str(_field(response, "model")),
        created=_int(_field(response, "created")),
        choices=choices,
        usage=normalised,
        raw=response,
    )


def from_anthropic(response: Any) -> RoutedChatCompletion:
    """Translate an Anthropic Messages response into the OpenAI shape.

    Text blocks are joined into ``content``; ``tool_use`` blocks become
    ``tool_calls`` with JSON ``arguments``. ``prompt_tokens`` includes cache
    reads and writes, as on OpenAI.
    """
    texts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in _field(response, "content") or ():
        kind = _field(block, "type")
        if kind == "text":
            texts.append(_str(_field(block, "text")) or "")
        elif kind == "tool_use":
            tool_calls.append(ToolCall(
                id=_str(_field(block, "id")) or "",
                function=FunctionCall(
                    name=_str(_field(block, "name")) or "",
                    arguments=json.dumps(_field(block, "input") or {}),
                ),
            ))
    stop = _str(_field(response, "stop_reason"))
    usage = _field(response, "usage")
    input_tokens = _int(_field(usage, "input_tokens"))
    prompt = None
    if input_tokens is not None:
        prompt = (
            input_tokens
            + (_int(_field(usage, "cache_read_input_tokens")) or 0)
            + (_int(_field(usage, "cache_creation_input_tokens")) or 0)
        )
    return RoutedChatCompletion(
        id=_str(_field(response, "id")),
        model=_str(_field(response, "model")),
        created=int(time.time()),
        choices=[Choice(
            index=0,
            message=ChatMessage(
                role="assistant",
                content="".join(texts) if texts or not tool_calls else None,
                tool_calls=tool_calls or None,
            ),
            finish_reason=_ANTHROPIC_FINISH.get(stop, stop) if stop else None,
        )],
        usage=_usage(prompt, _int(_field(usage, "output_tokens"))),
        raw=response,
    )


# --- request translation -------------------------------------------------------


def _part_texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    out = []
    if isinstance(content, list):
        for part in content:
            text = _field(part, "text")
            if isinstance(text, str):
                out.append(text)
    return out


def to_anthropic_request(messages: list, kwargs: dict) -> tuple[list, dict]:
    """Translate an OpenAI chat request into Anthropic Messages arguments.

    ``system`` / ``developer`` messages become ``system``; ``stop`` becomes
    ``stop_sequences`` and ``max_completion_tokens`` ``max_tokens``. Tool
    messages and OpenAI-only parameters raise ``RouterError``.
    """
    unsupported = sorted(_OPENAI_ONLY_PARAMS & set(kwargs))
    if unsupported:
        raise RouterError(
            "routed to an Anthropic model, which this router cannot send "
            f"these OpenAI parameters to: {', '.join(unsupported)}"
        )
    system: list[str] = []
    out: list[dict] = []
    for message in messages:
        role = _field(message, "role")
        content = _field(message, "content")
        if role in ("system", "developer"):
            system.extend(_part_texts(content))
            continue
        if role not in ("user", "assistant") or _field(message, "tool_calls"):
            raise RouterError(
                "routed to an Anthropic model; tool messages are not "
                "translated yet"
            )
        if isinstance(content, list):
            content = [
                {"type": "text", "text": part["text"]}
                if isinstance(part, Mapping) and part.get("type") == "text"
                else part
                for part in content
            ]
        out.append({"role": role, "content": content})
    kwargs = dict(kwargs)
    if system:
        kwargs["system"] = "\n\n".join(system)
    stop = kwargs.pop("stop", None)
    if stop is not None:
        kwargs["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
    return out, kwargs


# --- policy and providers ---------------------------------------------------------


def load_policy(policy: Mapping[str, Any] | str | os.PathLike) -> dict:
    """A route-policy.v1 document from a mapping or a ``.json`` / ``.yaml`` file."""
    if isinstance(policy, Mapping):
        doc = dict(policy)
    else:
        path = Path(policy)
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() in (".yaml", ".yml"):
            try:
                import yaml
            except ImportError as exc:  # pragma: no cover - depends on env
                raise RouterError(
                    "reading a YAML route policy needs PyYAML (pip install pyyaml)"
                ) from exc
            doc = yaml.safe_load(text)
        else:
            doc = json.loads(text)
    if not isinstance(doc, Mapping):
        raise RouterError("route policy must be a JSON/YAML object")
    if doc.get("schema_version") != "route-policy.v1":
        raise RouterError("route policy must have schema_version 'route-policy.v1'")
    for key in ("groups", "default_group", "quality_to_group"):
        if key not in doc:
            raise RouterError(f"route policy is missing {key!r}")
    return dict(doc)


@dataclass(frozen=True)
class _Provider:
    name: str
    vendor: str
    api_key: str
    options: dict


def _parse_providers(providers: Mapping[str, Mapping[str, Any]]) -> dict[str, _Provider]:
    out = {}
    for name, config in (providers or {}).items():
        config = dict(config)
        api_key = config.pop("api_key", None)
        if not api_key:
            raise RouterError(f"provider {name!r} has no api_key")
        vendor = config.pop("vendor", None) or (
            "anthropic" if name == "anthropic" else "openai"
        )
        if vendor not in DISPATCH_VENDORS:
            raise RouterError(
                f"provider {name!r}: vendor must be one of {DISPATCH_VENDORS}, "
                f"got {vendor!r} (use 'openai' with a base_url for "
                "OpenAI-compatible APIs)"
            )
        out[name] = _Provider(name, vendor, api_key, config)
    return out


@dataclass(frozen=True)
class _Plan:
    """What one call will do: the decision, the tier and its provider."""

    decision: RouteDecision
    tier: Mapping[str, Any]
    provider: _Provider
    requested_model: str
    data_classification: str | None
    use_case: str | None


class _RouterCore:
    """Routing and bookkeeping shared by the sync and async routers."""

    _client_cls: type
    _client_label = ""

    def __init__(
        self,
        *,
        ps_api_key: str,
        providers: Mapping[str, Mapping[str, Any]] | None = None,
        policy: Mapping[str, Any] | str | os.PathLike | None = None,
        mode: RouteMode = "local",
        default_provider: str | None = None,
        gateway_url: str | None = None,
        gateway_api_key: str | None = None,
        gateway_headers: Mapping[str, str] | None = None,
        ps_collector_url: str = "http://localhost:8000",
        business_unit: str | None = None,
        use_case: str | None = None,
        owner: str | None = None,
        data_classification: str | None = None,
        environment: str | None = None,
        calling_service: str | None = None,
        scan_pii: bool = True,
        send_prompt_text: bool = False,
        pricing_table: dict | None = None,
        atlas_url: str | None = None,
        atlas_api_key: str | None = None,
    ):
        if mode not in ("local", "gateway"):
            raise RouterError(f"mode must be 'local' or 'gateway', got {mode!r}")
        self.mode: RouteMode = mode
        self._use_case = use_case
        self._data_classification = data_classification
        client_kwargs = {
            "ps_api_key": ps_api_key,
            "ps_collector_url": ps_collector_url,
            "business_unit": business_unit,
            "use_case": use_case,
            "owner": owner,
            "data_classification": data_classification,
            "environment": environment,
            "calling_service": calling_service,
            "scan_pii": scan_pii,
            "send_prompt_text": send_prompt_text,
            "pricing_table": pricing_table,
            "atlas_url": atlas_url,
            "atlas_api_key": atlas_api_key,
        }
        self._clients: dict[str, Any] = {}
        self.policy: dict | None = None
        self._providers: dict[str, _Provider] = {}

        if mode == "gateway":
            if not gateway_url:
                raise RouterError("mode='gateway' needs gateway_url")
            headers = {
                header: value
                for header, value in {
                    "X-PS-Business-Unit": business_unit,
                    "X-PS-Use-Case": use_case,
                    "X-PS-Owner": owner,
                    "X-PS-Data-Classification": data_classification,
                    "X-PS-Environment": environment,
                    "X-PS-Calling-Service": calling_service,
                }.items()
                if value is not None
            }
            headers.update(gateway_headers or {})
            # The gateway forwards the bearer key to the provider.
            self._clients["gateway"] = self._client_cls(
                vendor="openai",
                api_key=gateway_api_key or "unused",
                base_url=gateway_url,
                default_headers=headers,
                **client_kwargs,
            )
            if policy is not None:
                self.policy = load_policy(policy)
            return

        if policy is None:
            raise RouterError("mode='local' needs a route policy")
        self._providers = _parse_providers(providers or {})
        if not self._providers:
            raise RouterError("mode='local' needs at least one provider")
        if default_provider is None and len(self._providers) == 1:
            default_provider = next(iter(self._providers))
        if default_provider is not None and default_provider not in self._providers:
            raise RouterError(f"default_provider {default_provider!r} is not configured")
        self._default_provider = default_provider
        self.policy = load_policy(policy)
        self._reachable = self._reachable_policy(self.policy)
        if not any(self._reachable["groups"].values()):
            raise RouterError(
                "no model in the route policy is served by a configured provider"
            )
        # Each provider's instrumented client is built on first use.
        self._client_kwargs = client_kwargs

    def _client(self, name: str) -> Any:
        """The instrumented client for provider ``name`` (built on first use)."""
        client = self._clients.get(name)
        if client is None:
            p = self._providers[name]
            client = self._client_cls(
                vendor=p.vendor, api_key=p.api_key, **self._client_kwargs, **p.options
            )
            self._clients[name] = client
        return client

    # --- routing --------------------------------------------------------------

    def _tier_provider(self, tier: Mapping[str, Any]) -> str | None:
        name = tier.get("provider") or self._default_provider
        return name if name in self._providers else None

    def _reachable_policy(self, policy: Mapping[str, Any]) -> dict:
        """``policy`` without the tiers no configured provider can serve."""
        groups = {
            group: [t for t in tiers if self._tier_provider(t) is not None]
            for group, tiers in policy["groups"].items()
        }
        return {**policy, "groups": groups}

    def _route_request(
        self,
        messages: list,
        model: str,
        route: RouteHint | None,
        max_tokens: int | None,
        data_classification: str | None,
        use_case: str | None,
        pii_detected: list[str] | None,
    ) -> RouteRequest:
        hints = parse_route_headers(route.to_headers() if route else {})
        req = build_route_request(
            {"model": model, "messages": messages, "max_tokens": max_tokens}, hints
        )
        req.data_classification = data_classification
        req.use_case = use_case
        # Always scanned, also with scan_pii=False: escalation is a safety
        # control and nothing leaves the process here.
        found = set(scan_messages(messages))
        found.update(pii_detected or ())
        req.pii_detected = sorted(found)
        return req

    def _decide(self, req: RouteRequest) -> RouteDecision:
        """The decision for ``req`` over the configured providers' tiers.

        ``model="auto"`` or any hint: ``resolve_policy_route``. A concrete
        model with no hint is pinned, but still has to be in the policy and
        allowed at the call's sensitivity.
        """
        hinted = (
            req.quality is not None
            or req.max_cost is not None
            or req.explicit_group is not None
        )
        if req.requested_model == "auto" or hinted:
            return resolve_policy_route(req, self._reachable)
        return self._pinned(req)

    def _pinned(self, req: RouteRequest) -> RouteDecision:
        model = req.requested_model
        level, source = classify_sensitivity(
            self._reachable, req.data_classification, req.pii_detected
        )
        rule = data_policy_rule(self._reachable, level)
        out = req.max_output_tokens
        listed = False
        best: RouteDecision | None = None
        for group, tiers in self._reachable["groups"].items():
            for tier in tiers:
                if tier["model"] != model:
                    continue
                listed = True
                if not tier_complies(tier, rule):
                    continue
                cost = estimate_tier_cost(
                    tier,
                    req.approx_input_tokens,
                    DEFAULT_OUTPUT_TOKENS if out is None else out,
                )
                if best is None or cost < best.est_cost:
                    best = RouteDecision(model, group, "pinned", cost, level, source)
        if best is not None:
            return best
        if listed:
            raise PolicyViolation(
                f"model {model!r} is not allowed at sensitivity {level!r}",
                sensitivity_level=level,
                sensitivity_source=source,
            )
        raise RouterError(
            f"model {model!r} is not in the route policy for any configured "
            "provider; add it to a group or use model='auto'"
        )

    def _find_tier(self, decision: RouteDecision, req: RouteRequest) -> Mapping[str, Any]:
        """The policy tier behind ``decision``, checked against the data policy."""
        rule = data_policy_rule(self._reachable, decision.sensitivity_level)
        out = (
            DEFAULT_OUTPUT_TOKENS
            if req.max_output_tokens is None
            else req.max_output_tokens
        )
        matches = [
            t for t in self._reachable["groups"].get(decision.group) or ()
            if t["model"] == decision.model and tier_complies(t, rule)
        ]
        for tier in matches:
            cost = estimate_tier_cost(tier, req.approx_input_tokens, out)
            if abs(cost - decision.est_cost) <= 1e-12 + 1e-9 * abs(cost):
                return tier
        if matches:
            return matches[0]
        # Never dispatch something the data policy does not allow.
        raise PolicyViolation(
            f"no allowed tier for model {decision.model!r} in group {decision.group!r}",
            sensitivity_level=decision.sensitivity_level,
            sensitivity_source=decision.sensitivity_source,
            group=decision.group,
        )

    def route(
        self,
        messages: list,
        *,
        model: str = "auto",
        route: RouteHint | None = None,
        max_tokens: int | None = None,
        data_classification: str | None = None,
        use_case: str | None = None,
        pii_detected: list[str] | None = None,
    ) -> RouteDecision:
        """The decision a call would get, without sending it (local mode).

        ``pii_detected`` adds categories found elsewhere (a DLP scan) to the
        ones the local scanner finds. Raises ``PolicyViolation`` when the call
        would fail closed.
        """
        return self._plan(
            messages, model, route, max_tokens, data_classification, use_case,
            pii_detected,
        ).decision

    def _plan(
        self,
        messages: list,
        model: str,
        route: RouteHint | None,
        max_tokens: int | None,
        data_classification: str | None,
        use_case: str | None,
        pii_detected: list[str] | None = None,
    ) -> _Plan:
        if self.mode != "local":
            raise RouterError("route() decides locally; this router delegates to the gateway")
        declared = data_classification or self._data_classification
        use_case = use_case or self._use_case
        req = self._route_request(
            messages, model, route, max_tokens, declared, use_case, pii_detected
        )
        decision = self._decide(req)
        tier = self._find_tier(decision, req)
        provider = self._providers[self._tier_provider(tier)]  # type: ignore[index]
        return _Plan(decision, tier, provider, model, declared, use_case)

    # --- telemetry and response ----------------------------------------------

    def _event_hook(self, plan: _Plan, captured: dict):
        decision, tier = plan.decision, plan.tier
        policy_version = (self.policy or {}).get("policy_version")

        def hook(event: dict) -> None:
            event["requested_model"] = plan.requested_model
            event["route_mode"] = "local"
            event["route_group"] = decision.group
            event["route_reason"] = decision.reason
            event["route_est_cost"] = decision.est_cost
            event["served_provider"] = plan.provider.name
            if tier.get("jurisdiction") is not None:
                event["served_jurisdiction"] = tier["jurisdiction"]
            if policy_version is not None:
                event["policy_version"] = policy_version
            if decision.sensitivity_level is not None:
                event["sensitivity_level"] = decision.sensitivity_level
                event["sensitivity_source"] = decision.sensitivity_source
            if plan.data_classification is not None:
                event["data_classification"] = plan.data_classification
            if plan.use_case is not None:
                event["use_case_name"] = plan.use_case
            # No provider-reported cost and no pricing-table entry (an
            # OpenAI-compatible provider's own model id): price the actual
            # tokens at the policy tier's rates.
            if (
                event.get("cost") is None
                and event.get("tokens_in") is not None
                and event.get("tokens_out") is not None
            ):
                event["cost"] = estimate_tier_cost(
                    tier, event["tokens_in"], event["tokens_out"]
                )
                event["cost_source"] = COST_SOURCE_ROUTE_POLICY
            captured.update(event)

        return hook

    def _gateway_hook(self, model: str, captured: dict):
        def hook(event: dict) -> None:
            event["requested_model"] = model
            event["route_mode"] = "gateway"
            captured.update(event)

        return hook

    @staticmethod
    def _call_args(
        plan: _Plan, messages: list, max_tokens: int | None, kwargs: dict
    ) -> tuple[list, int | None, dict]:
        """Messages, ``max_tokens`` and options for the chosen provider."""
        if plan.provider.vendor != "anthropic":
            return messages, max_tokens, kwargs
        kwargs = dict(kwargs)
        max_completion = kwargs.pop("max_completion_tokens", None)
        messages, kwargs = to_anthropic_request(messages, kwargs)
        return messages, max_tokens if max_tokens is not None else max_completion, kwargs

    @staticmethod
    def _gateway_kwargs(
        kwargs: dict, data_classification: str | None, use_case: str | None
    ) -> dict:
        """Per-call business context as the gateway's ``X-PS-*`` headers."""
        headers = {
            header: value
            for header, value in {
                "X-PS-Data-Classification": data_classification,
                "X-PS-Use-Case": use_case,
            }.items()
            if value is not None
        }
        if not headers:
            return kwargs
        return {
            **kwargs,
            "extra_headers": {**(kwargs.get("extra_headers") or {}), **headers},
        }

    @staticmethod
    def _local_result(plan: _Plan, response: Any, event: dict,
                      attempt: Attempt, policy_version: str | None) -> RoutedChatCompletion:
        out = (
            from_anthropic(response) if plan.provider.vendor == "anthropic"
            else from_openai(response)
        )
        d = plan.decision
        out.ps = PSResult(
            route=RouteInfo(
                mode="local",
                requested_model=plan.requested_model,
                model=event.get("served_model") or d.model,
                provider=plan.provider.name,
                group=d.group,
                reason=d.reason,
                jurisdiction=plan.tier.get("jurisdiction"),
                policy_version=policy_version,
                sensitivity_level=d.sensitivity_level,
                sensitivity_source=d.sensitivity_source,
                est_cost=d.est_cost,
            ),
            cost=CostInfo(event.get("cost"), event.get("cost_source"), d.est_cost),
            attempts=(attempt,),
        )
        return out

    @staticmethod
    def _gateway_result(model: str, response: Any, event: dict,
                        attempt: Attempt) -> RoutedChatCompletion:
        out = from_openai(response)
        out.ps = PSResult(
            route=RouteInfo(
                mode="gateway",
                requested_model=model,
                model=event.get("served_model") or out.model,
            ),
            cost=CostInfo(event.get("cost"), event.get("cost_source")),
            attempts=(attempt,),
        )
        return out

    def telemetry_stats(self) -> dict:
        """Delivery counters of each provider client used so far, by name."""
        return {name: c.telemetry_stats() for name, c in self._clients.items()}


def _refuse_stream(kwargs: dict) -> None:
    if kwargs.get("stream") is True:
        raise RouterError(
            "stream=True is not supported by the router in local mode yet"
        )


class ShieldsRouter(_RouterCore):
    """Picks the model per call under a route policy, then sends it with your
    own provider keys (``mode="local"``), or leaves the choice to the gateway
    (``mode="gateway"``).

    ``providers`` maps a provider name, as policy tiers name it in
    ``provider``, to ``{"api_key": ..., "vendor": "openai" | "anthropic",
    **client options}``. ``vendor`` defaults to ``anthropic`` for the
    ``anthropic`` provider and to ``openai`` (OpenAI-compatible, set
    ``base_url``) for every other. Tiers without ``provider`` go to
    ``default_provider`` (the only provider when there is one).

    ``client.chat.completions.create(model="auto", messages=[...],
    route=RouteHint(...))`` returns a ``RoutedChatCompletion``.
    """

    def __init__(self, **kwargs: Any):
        from prompt_shields.client import ShieldsClient

        self._client_cls = ShieldsClient
        super().__init__(**kwargs)
        self.chat = _RouterChat(self)

    def close(self) -> None:
        """Final telemetry flush of every provider's client."""
        for c in self._clients.values():
            c.close()


class AsyncShieldsRouter(_RouterCore):
    """Async ``ShieldsRouter``; ``await client.chat.completions.create(...)``."""

    def __init__(self, **kwargs: Any):
        from prompt_shields.async_client import AsyncShieldsClient

        self._client_cls = AsyncShieldsClient
        super().__init__(**kwargs)
        self.chat = _AsyncRouterChat(self)

    async def aclose(self) -> None:
        for c in self._clients.values():
            await c.aclose()


class _RouterChat:
    def __init__(self, router: ShieldsRouter):
        self.completions = _RouterCompletions(router)


class _AsyncRouterChat:
    def __init__(self, router: AsyncShieldsRouter):
        self.completions = _AsyncRouterCompletions(router)


class _RouterCompletions:
    def __init__(self, router: ShieldsRouter):
        self._router = router

    def create(
        self,
        *,
        messages: list,
        model: str = "auto",
        route: RouteHint | None = None,
        ps_metadata: PSMetadata | None = None,
        max_tokens: int | None = None,
        data_classification: str | None = None,
        use_case: str | None = None,
        **kwargs: Any,
    ) -> Any:
        r = self._router
        captured: dict = {}
        if r.mode == "gateway":
            client = r._clients["gateway"]
            start = time.monotonic()
            response = client.chat.completions._create(
                model=model, messages=messages, ps_metadata=ps_metadata,
                route=route, max_tokens=max_tokens,
                _event_hook=r._gateway_hook(model, captured),
                **r._gateway_kwargs(kwargs, data_classification, use_case),
            )
            if kwargs.get("stream") is True:
                return response
            attempt = Attempt(None, captured.get("served_model"), True,
                              int((time.monotonic() - start) * 1000))
            return r._gateway_result(model, response, captured, attempt)

        _refuse_stream(kwargs)
        plan = r._plan(messages, model, route, max_tokens, data_classification, use_case)
        call_messages, call_max, call_kwargs = r._call_args(
            plan, messages, max_tokens, kwargs
        )
        client = r._client(plan.provider.name)
        start = time.monotonic()
        # The seam for failover (#21): one attempt for now.
        response = client.chat.completions._create(
            model=plan.decision.model, messages=call_messages,
            ps_metadata=ps_metadata, max_tokens=call_max,
            _event_hook=r._event_hook(plan, captured), **call_kwargs,
        )
        attempt = Attempt(plan.provider.name, plan.decision.model, True,
                          int((time.monotonic() - start) * 1000))
        return r._local_result(plan, response, captured, attempt,
                               (r.policy or {}).get("policy_version"))


class _AsyncRouterCompletions:
    def __init__(self, router: AsyncShieldsRouter):
        self._router = router

    async def create(
        self,
        *,
        messages: list,
        model: str = "auto",
        route: RouteHint | None = None,
        ps_metadata: PSMetadata | None = None,
        max_tokens: int | None = None,
        data_classification: str | None = None,
        use_case: str | None = None,
        **kwargs: Any,
    ) -> Any:
        r = self._router
        captured: dict = {}
        if r.mode == "gateway":
            client = r._clients["gateway"]
            start = time.monotonic()
            response = await client.chat.completions._create(
                model=model, messages=messages, ps_metadata=ps_metadata,
                route=route, max_tokens=max_tokens,
                _event_hook=r._gateway_hook(model, captured),
                **r._gateway_kwargs(kwargs, data_classification, use_case),
            )
            if kwargs.get("stream") is True:
                return response
            attempt = Attempt(None, captured.get("served_model"), True,
                              int((time.monotonic() - start) * 1000))
            return r._gateway_result(model, response, captured, attempt)

        _refuse_stream(kwargs)
        plan = r._plan(messages, model, route, max_tokens, data_classification, use_case)
        call_messages, call_max, call_kwargs = r._call_args(
            plan, messages, max_tokens, kwargs
        )
        client = r._client(plan.provider.name)
        start = time.monotonic()
        # The seam for failover (#21): one attempt for now.
        response = await client.chat.completions._create(
            model=plan.decision.model, messages=call_messages,
            ps_metadata=ps_metadata, max_tokens=call_max,
            _event_hook=r._event_hook(plan, captured), **call_kwargs,
        )
        attempt = Attempt(plan.provider.name, plan.decision.model, True,
                          int((time.monotonic() - start) * 1000))
        return r._local_result(plan, response, captured, attempt,
                               (r.policy or {}).get("policy_version"))
