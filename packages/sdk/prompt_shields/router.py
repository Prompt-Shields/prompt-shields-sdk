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

``pseudonymise=True`` (local mode only, off by default) replaces the PII the
SDK detects with placeholders before the call and restores the answer
(``prompt_shields.pseudonymise``). The call keeps the original prompt's
sensitivity unless the policy's ``sensitivity.anonymise_then_route`` lists
that level; then it is routed at the level of what is left.

Only candidates whose provider is configured are considered: a policy tier
whose provider has no entry in ``providers`` is removed before routing, the
same as a tier the data policy does not allow.

Every response comes back in the OpenAI chat-completions shape
(``RoutedChatCompletion``), whichever provider served it. The provider's own
response object is kept as ``resp.raw``, and ``resp.ps`` carries the route,
the cost, the attempts and the ``fallback_chain``.

Failover (local mode, #21): the router builds an ordered list of candidates
that all passed the same data-policy check as the chosen one (and the use
case's eligible models and the budget; for a pinned model, only the same
model on another provider), and hands it to
``prompt_shields.failover.FailoverEngine`` (PRD §7.5). A candidate that does
not comply is never added, so a fallback never leaves the constraint set.
When every candidate failed or was skipped, ``NoCompliantCandidate`` (a
``PolicyViolation``) is raised. Per-provider circuit breakers are shared by
every router in the process unless ``breakers=`` / ``breaker_config=`` say
otherwise.

Routing learner, auto mode (``learner=``, #34; opt-in, local mode only): an
``prompt_shields.learner_auto.AutoLearner`` may replace the heuristic's pick
on ``model="auto"`` calls without an explicit group or quality hint, with a
model from ``route_policy.policy_safe_tiers`` (data policy, eligible models,
min group) within the budget clamp. If the learner raises, the call goes
on with the policy's pick (the error type is logged and counted in
``AutoLearner.fallbacks``). Without it nothing changes.

Spend budgets (``budgets=``, a budget.v1 document, #29) are checked before
each call in local mode: an exhausted ``block`` budget raises
``BudgetExceeded`` before any attempt; a ``downgrade`` budget's remaining
amount becomes the ``max_cost`` clamp, so it narrows both the chosen model
and the failover candidates, within the policy's constraints. The cost of
the attempt that served the call is recorded once afterwards in
``budget_store`` (default: in-process, per-process counters; use
``RedisBudgetStore`` to share them); failed attempts record nothing. See
``prompt_shields.budgets``.

Streaming (``stream=True``, local mode) returns a ``RoutedStream``
(``AsyncRoutedStream``): the route, data-policy gate, budget check and
learner choice happen first, as for any call. An attempt counts as served
once its first chunk is in, so failover only happens before that (the
request raising, or the stream failing before its first chunk); an error
after it goes to the caller and is never retried, as on the gateway. One
telemetry event is built when the stream ends, fails or is closed early,
with the provider's usage from the final chunk where it sends one, else
estimated tokens (``tokens_estimated``), and the spend is recorded once from
it. With ``pseudonymise=True`` the chunks go through ``restore_stream`` and
the mapping is dropped when the stream is done. Chunks keep the serving
provider's shape (chat-completion chunks, or Anthropic Messages events).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
import weakref
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from prompt_shields.baseline import Baseline, BaselineError, load_baseline
from prompt_shields.budgets import (
    BudgetCheck,
    BudgetError,
    BudgetStore,
    make_enforcer,
)
from prompt_shields.failover import (
    BreakerConfig,
    BreakerRegistry,
    Candidate,
    FailoverAttempt,
    FailoverEngine,
    NoCompliantCandidate,
    chain_of,
    classify_failure,
    default_registry,
)
from prompt_shields.learner import context_vector
from prompt_shields.learner_auto import CALL_FAILED, AutoLearner, LearnerChoice
from prompt_shields.pii import detect_pii_categories
from prompt_shields.pricing import (
    COST_SOURCE_PRICING_TABLE,
    COST_SOURCE_PRICING_TABLE_ESTIMATED_TOKENS,
    estimate_cost,
)
from prompt_shields.prompt_features import extract_prompt_features
from prompt_shields.pseudonymise import Pseudonymisation, pseudonymise
from prompt_shields.route_policy import (
    DEFAULT_OUTPUT_TOKENS,
    PolicyViolation,
    RouteDecision,
    RouteRequest,
    anonymise_then_route_allowed,
    build_route_request,
    classify_sensitivity,
    data_policy_rule,
    estimate_tier_cost,
    parse_route_headers,
    policy_safe_tiers,
    resolve_policy_route,
    tier_complies,
)
from prompt_shields.sovereign import (
    SovereignProviderError,
    get_openai_compatible,
    resolve_base_url,
)
from prompt_shields.streaming import _StreamBase
from prompt_shields.types import PSMetadata, RouteHint

logger = logging.getLogger("prompt_shields.router")

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
    #: Set when the routing learner (auto mode) decided this call.
    learner: LearnerChoice | None = None


@dataclass(frozen=True)
class CostInfo:
    """``usd`` is the event's cost (``None`` when unknown), ``source`` how it
    was produced (the event's ``cost_source``), ``estimated_usd`` the router's
    estimate before the call. ``counterfactual_usd`` is what the call would
    have cost on its baseline model (``baseline_id``), when the router has a
    baseline and the call could be priced."""

    usd: float | None
    source: str | None
    estimated_usd: float | None = None
    counterfactual_usd: float | None = None
    baseline_id: str | None = None


@dataclass(frozen=True)
class Attempt:
    """One upstream attempt. ``error`` is the exception class name and
    ``error_class`` the failover class (``server_error``, ``rate_limited``,
    ...); never an error message or content."""

    provider: str | None
    model: str | None
    ok: bool
    latency_ms: int
    error: str | None = None
    error_class: str | None = None
    status: int | None = None


@dataclass(frozen=True)
class BudgetInfo:
    """The call's spend budgets (#29): the worst ``state`` among matching
    budgets (``ok`` / ``warn`` / ``unknown`` / ``exceeded``), the budget in
    that state, and ``action`` (``none`` or ``downgraded``)."""

    state: str
    budget_id: str | None
    action: str


@dataclass(frozen=True)
class PseudonymisationInfo:
    """What pseudonymisation did on a call (``pseudonymise=True``). Counts
    and levels only, never values.

    ``entities`` is distinct values replaced per PII category; ``restored``
    placeholders put back into the answer; ``unknown_placeholders``
    placeholder-shaped tokens in the answer that were left as they are.
    ``detected_level`` is the sensitivity of the original prompt;
    ``anonymise_then_route`` is True when the policy let the call be routed
    at the lower level of what was left after pseudonymisation
    (``sensitivity.anonymise_then_route``); the route's ``sensitivity_level``
    is then that lower level.
    """

    entities: Mapping[str, int]
    restored: int
    unknown_placeholders: int
    detected_level: str | None
    anonymise_then_route: bool


@dataclass(frozen=True)
class PSResult:
    """``fallback_chain`` is the error class of each failed attempt, in
    order: empty when the first attempt succeeded."""

    route: RouteInfo
    cost: CostInfo
    attempts: tuple[Attempt, ...]
    budget: BudgetInfo | None = None
    fallback_chain: tuple[str, ...] = ()
    pseudonymisation: PseudonymisationInfo | None = None


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


def _scan_pii(messages: Any) -> list[str]:
    """PII categories in everything the router may send: dict or object
    messages (an SDK message object from an earlier response), text content
    parts, and assistant tool-call arguments. Unlike ``scan_messages`` it
    reads attribute objects too, so no message text bypasses escalation."""
    texts: list[str] = []
    for message in messages or ():
        texts.extend(_part_texts(_field(message, "content")))
        for tc in _field(message, "tool_calls") or ():
            fn = _field(tc, "function")
            for value in (_field(fn, "name"), _field(fn, "arguments")):
                if isinstance(value, str):
                    texts.append(value)
    return detect_pii_categories(" ".join(texts))


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
    #: Catalog provider id behind an OpenAI-compatible entry, if known.
    catalog: str | None = None

    @property
    def openai_compatible(self) -> bool:
        """Reached through the OpenAI SDK at another provider's base URL, so
        the OpenAI price table does not apply to its model ids."""
        return (
            self.vendor == "openai"
            and self.name != "openai"
            and bool(self.options.get("base_url"))
        )


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
        # A named OpenAI-compatible provider (catalog id, #20) gets its base
        # URL from the catalog unless the entry sets one.
        catalog_provider = config.pop("catalog_provider", None)
        region = config.pop("region", None)
        catalog_id = catalog_provider or (
            name if get_openai_compatible(name) is not None else None
        )
        if vendor == "openai" and not config.get("base_url"):
            known = catalog_provider or name
            if known != "openai":
                if catalog_provider is None and get_openai_compatible(name) is None:
                    # Never send this provider's key to api.openai.com.
                    raise RouterError(
                        f"provider {name!r} has no base_url and is not a known "
                        "OpenAI-compatible provider; set base_url or "
                        "catalog_provider"
                    )
                try:
                    config["base_url"] = resolve_base_url(known, region)
                except SovereignProviderError as exc:
                    raise RouterError(f"provider {name!r}: {exc}") from exc
            elif region is not None:
                raise RouterError(f"provider {name!r}: region needs a catalog provider")
        elif region is not None:
            raise RouterError(
                f"provider {name!r}: region selects a catalog base URL; "
                "it cannot be combined with base_url"
            )
        out[name] = _Provider(name, vendor, api_key, config, catalog_id)
    return out


@dataclass(frozen=True)
class _Plan:
    """What one call will do: the decision, the tier and its provider.

    ``messages`` is what is sent: the caller's messages, or their
    pseudonymised copy when ``pseudo`` is set (its mapping never leaves this
    process and is cleared when the call is done).
    """

    decision: RouteDecision
    tier: Mapping[str, Any]
    provider: _Provider
    requested_model: str
    data_classification: str | None
    use_case: str | None
    budget: BudgetCheck | None = None
    budget_action: str = "none"
    request: RouteRequest | None = None
    messages: list = field(default_factory=list)
    pseudo: Pseudonymisation | None = None
    original_pii: tuple[str, ...] = ()
    detected_level: str | None = None
    anonymised_route: bool = False
    learner: LearnerChoice | None = None
    #: Auto mode moved the pick: the policy's own (decision, tier, provider),
    #: used when the learner's model cannot take the request.
    policy_pick: tuple | None = None


class _AttemptLimit(Exception):
    """Internal: the call's ``max_attempts`` is used up."""


@dataclass
class _AttemptTracker:
    """The failed attempts of one routed call, as they happen."""

    failures: list[FailoverAttempt] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # exception class names
    last: BaseException | None = None


#: ``fallback.max_attempts`` when the policy does not set it (schema default).
DEFAULT_MAX_ATTEMPTS = 3

#: Longest 429 ``Retry-After`` honoured (seconds) when the policy sets no
#: ``fallback.retry_after_max_wait_ms``.
DEFAULT_RETRY_AFTER_MAX_WAIT = 10.0


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
        baseline: Baseline | Mapping[str, Any] | str | os.PathLike | None = None,
        budgets: Any = None,
        budget_store: BudgetStore | None = None,
        failover: bool = True,
        breakers: BreakerRegistry | None = None,
        breaker_config: BreakerConfig | None = None,
        max_attempts: int | None = None,
        retry_after_max_wait: float | None = None,
        rate_limit_retries: int = 1,
        pseudonymise: bool = False,
        learner: AutoLearner | None = None,
    ):
        if mode not in ("local", "gateway"):
            raise RouterError(f"mode must be 'local' or 'gateway', got {mode!r}")
        if pseudonymise and mode != "local":
            # The gateway classifies what it receives: it would see only the
            # placeholders and route at a lower level than the policy allows.
            raise RouterError(
                "pseudonymise=True needs mode='local'; the gateway would "
                "classify the placeholders, not the original prompt"
            )
        if learner is not None and not isinstance(learner, AutoLearner):
            raise RouterError("learner must be a prompt_shields.learner_auto.AutoLearner")
        if learner is not None and mode != "local":
            # Auto mode runs in the SDK router only; the gateway decides by
            # its own policy and does not run the learner yet.
            raise RouterError("learner= (auto mode) needs mode='local'")
        self.mode: RouteMode = mode
        #: The routing learner in auto mode (opt-in; None routes as before).
        self.learner = learner
        self.pseudonymise = bool(pseudonymise)
        self._scan_pii_events = scan_pii
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
        self.baseline: Baseline | None = None
        self.budgets = None
        self._budget_attrs = {"business_unit": business_unit, "owner": owner}

        if mode == "gateway":
            if budgets is not None or budget_store is not None:
                raise RouterError(
                    "mode='gateway': the gateway enforces spend budgets with "
                    "its shared store"
                )
            if baseline is not None:
                raise RouterError(
                    "mode='gateway': the gateway adds the counterfactual cost; "
                    "set its baseline with PS_BASELINE_FILE"
                )
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
        if baseline is not None:
            try:
                self.baseline = load_baseline(baseline)
            except BaselineError as exc:
                raise RouterError(f"invalid baseline: {exc}") from exc
        try:
            self.budgets = make_enforcer(budgets, budget_store)
        except BudgetError as exc:
            raise RouterError(f"invalid budgets: {exc}") from exc
        self._reachable = self._reachable_policy(self.policy)
        if not any(self._reachable["groups"].values()):
            raise RouterError(
                "no model in the route policy is served by a configured provider"
            )
        # Each provider's instrumented client is built on first use.
        self._client_kwargs = client_kwargs
        self._init_failover(
            failover, breakers, breaker_config, max_attempts,
            retry_after_max_wait, rate_limit_retries,
        )

    def _init_failover(
        self,
        enabled: bool,
        breakers: BreakerRegistry | None,
        breaker_config: BreakerConfig | None,
        max_attempts: int | None,
        retry_after_max_wait: float | None,
        rate_limit_retries: int,
    ) -> None:
        """Failover settings: a kwarg wins over the policy's ``fallback``
        section, which wins over the defaults."""
        if breakers is not None and breaker_config is not None:
            raise RouterError("pass breakers or breaker_config, not both")
        fallback = (self.policy or {}).get("fallback") or {}
        if max_attempts is None:
            max_attempts = fallback.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
        if (
            not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or max_attempts < 1
        ):
            raise RouterError("max_attempts must be an integer of at least 1")
        if retry_after_max_wait is None:
            wait_ms = fallback.get("retry_after_max_wait_ms")
            retry_after_max_wait = (
                DEFAULT_RETRY_AFTER_MAX_WAIT if wait_ms is None else wait_ms / 1000
            )
        if retry_after_max_wait < 0:
            raise RouterError("retry_after_max_wait must not be negative")
        if rate_limit_retries < 0:
            raise RouterError("rate_limit_retries must not be negative")
        self.failover = enabled
        self.max_attempts = max_attempts
        if breaker_config is not None:
            breakers = BreakerRegistry(breaker_config)
        #: Per-provider circuit breakers (``.snapshot()``, ``.metrics()``).
        self.breakers = breakers if breakers is not None else default_registry()
        self._engine = FailoverEngine(
            breakers=self.breakers,
            max_wait=retry_after_max_wait,
            rate_limit_retries=rate_limit_retries,
        )

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
        pii_categories: set[str] | None = None,
    ) -> RouteRequest:
        hints = parse_route_headers(route.to_headers() if route else {})
        req = build_route_request(
            {"model": model, "messages": messages, "max_tokens": max_tokens}, hints
        )
        req.data_classification = data_classification
        req.use_case = use_case
        if pii_categories is not None:
            req.pii_detected = sorted(pii_categories)
            return req
        # Always scanned, also with scan_pii=False: escalation is a safety
        # control and nothing leaves the process here.
        found = set(_scan_pii(messages))
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
        would fail closed, ``BudgetExceeded`` when a block budget is
        exhausted. Spend budgets are read but nothing is recorded. With
        ``pseudonymise=True`` the decision is the one the pseudonymised call
        would get.
        """
        plan = self._plan(
            messages, model, route, max_tokens, data_classification, use_case,
            pii_detected, budget=self._budget_check(use_case),
        )
        if plan.pseudo is not None:
            plan.pseudo.clear()
        return plan.decision

    # --- budgets --------------------------------------------------------------

    def _budget_attributes(self, use_case: str | None) -> dict:
        return {**self._budget_attrs, "use_case": use_case or self._use_case}

    def _budget_check(self, use_case: str | None) -> BudgetCheck | None:
        if self.budgets is None:
            return None
        return self.budgets.check(self._budget_attributes(use_case))

    async def _abudget_check(self, use_case: str | None) -> BudgetCheck | None:
        if self.budgets is None:
            return None
        return await self.budgets.acheck(self._budget_attributes(use_case))

    def _apply_budget(
        self, decision: RouteDecision, req: RouteRequest, check: BudgetCheck
    ) -> tuple[RouteDecision, str, RouteRequest]:
        """Block, or downgrade ``decision`` to fit the remaining budget; also
        returns the request with the budget's ``max_cost`` clamp.

        A downgrade budget's remaining amount is a cost ceiling, applied as
        the ``max_cost`` clamp over the allowed candidates (the data policy
        still applies, so a budget never downgrades to a non-compliant
        model). Nothing fits: the cheapest allowed candidate.
        """
        assert self.budgets is not None
        self.budgets.raise_if_blocked(check)
        ceiling = check.ceiling_usd
        if ceiling is None:
            return decision, "none", req
        # The clamped request also bounds the failover candidates
        # (``_fallback_plans`` reads ``plan.request.max_cost``).
        req = replace(
            req,
            max_cost=ceiling if req.max_cost is None else min(req.max_cost, ceiling),
        )
        if decision.est_cost <= ceiling:
            return decision, "none", req
        cheaper = resolve_policy_route(req, self._reachable)
        if cheaper.est_cost >= decision.est_cost:
            return decision, "none", req
        return (replace(cheaper, reason=cheaper.reason + "+budget-downgrade"),
                "downgraded", req)

    def _plan(
        self,
        messages: list,
        model: str,
        route: RouteHint | None,
        max_tokens: int | None,
        data_classification: str | None,
        use_case: str | None,
        pii_detected: list[str] | None = None,
        budget: BudgetCheck | None = None,
    ) -> _Plan:
        if self.mode != "local":
            raise RouterError("route() decides locally; this router delegates to the gateway")
        declared = data_classification or self._data_classification
        use_case = use_case or self._use_case
        pseudo = pseudonymise(messages) if self.pseudonymise else None
        try:
            extra: dict[str, Any] = {"messages": messages}
            if pseudo is None:
                req = self._route_request(
                    messages, model, route, max_tokens, declared, use_case,
                    pii_detected,
                )
            else:
                # Categories the caller supplied (a DLP scan) cannot be
                # shown to be gone, so they always count.
                supplied = set(pii_detected or ())
                local = set(_scan_pii(messages))
                original = local | supplied
                # What is still detected in what is sent: keyword topics
                # (health_data, ...) and anything that was not replaced.
                residual = set(_scan_pii(pseudo.messages)) | supplied
                detected_level, _ = classify_sensitivity(
                    self._reachable, declared, original
                )
                allowed = anonymise_then_route_allowed(self._reachable, detected_level)
                req = self._route_request(
                    pseudo.messages, model, route, max_tokens, declared, use_case,
                    None, pii_categories=residual if allowed else original,
                )
                extra = {
                    "messages": pseudo.messages, "pseudo": pseudo,
                    "original_pii": tuple(sorted(local)),
                    "detected_level": detected_level,
                }
            decision = self._decide(req)
            if pseudo is not None and decision.sensitivity_level != extra["detected_level"]:
                # Only reachable when the policy allowed the lower level.
                decision = replace(
                    decision, reason=decision.reason + "+anonymise-then-route"
                )
                extra["anonymised_route"] = True
            budget_action = "none"
            if budget is not None:
                decision, budget_action, req = self._apply_budget(decision, req, budget)
            tier = self._find_tier(decision, req)
            provider = self._providers[self._tier_provider(tier)]  # type: ignore[index]
            if self.learner is not None:
                try:
                    learned = self._learner_pick(
                        req, decision, tier, provider, extra["messages"]
                    )
                except Exception as exc:  # noqa: BLE001 - the learner never fails the call
                    # The learner never fails the call: the policy's pick
                    # stands (counted in the learner's guard state).
                    self._learner_error("choose", exc)
                    learned = None
                if learned is not None:
                    if learned[1] is not tier:
                        extra["policy_pick"] = (decision, tier, provider)
                    decision, tier, provider, extra["learner"] = learned
        except BaseException:
            if pseudo is not None:
                pseudo.clear()
            raise
        return _Plan(decision, tier, provider, model, declared, use_case,
                     budget, budget_action, req, **extra)

    def _learner_pick(
        self, req: RouteRequest, decision: RouteDecision,
        tier: Mapping[str, Any], provider: _Provider, messages: list,
    ) -> tuple[RouteDecision, Mapping[str, Any], _Provider, LearnerChoice] | None:
        """Auto mode: let the learner pick within the policy-safe set.

        Only on the strategy path (``model="auto"``, no explicit group, no
        quality hint): those are the caller's choices. The options are the
        policy's own pick plus ``policy_safe_tiers`` for the call's
        sensitivity and use case (data policy, eligible models, min group),
        served by a configured provider and within the ``max_cost`` /
        budget clamp, the same bound failover uses.
        """
        learner = self.learner
        assert learner is not None
        if (
            decision.reason == "pinned"
            or req.requested_model != "auto"
            or req.explicit_group is not None
            or req.quality is not None
            or not learner.enabled_for(req.use_case)
        ):
            return None
        out = (
            DEFAULT_OUTPUT_TOKENS
            if req.max_output_tokens is None
            else req.max_output_tokens
        )
        budget = None if req.max_cost is None else max(req.max_cost, decision.est_cost)
        policy_label = f"{provider.name}:{tier['model']}"
        options: dict[str, tuple[str, Mapping[str, Any], str, float]] = {
            policy_label: (decision.group, tier, provider.name, decision.est_cost),
        }
        for group, t in policy_safe_tiers(
            self._reachable, decision.sensitivity_level, req.use_case
        ):
            name = self._tier_provider(t)
            if name is None:
                continue
            label = f"{name}:{t['model']}"
            if label in options:
                continue
            cost = estimate_tier_cost(t, req.approx_input_tokens, out)
            if budget is not None and cost > budget:
                continue
            options[label] = (group, t, name, cost)
        features = extract_prompt_features(messages, use_case=req.use_case)
        choice = learner.choose(
            req.use_case, decision.sensitivity_level, list(options), policy_label,
            self._reachable,
            context=context_vector({
                "approx_input_tokens": features.approx_input_tokens,
                "message_count": features.message_count,
                "has_code": features.has_code,
                "has_json_schema": features.has_json_schema,
                "complexity": features.complexity,
                "language": features.language,
            }),
        )
        if choice is None:
            return None
        group, t, name, cost = options[choice.model]
        if choice.model != policy_label:
            decision = replace(
                decision, model=t["model"], group=group, est_cost=cost,
                reason=f"{decision.reason}+learner-{choice.mode}",
            )
        elif choice.mode == "explore":
            decision = replace(decision, reason=f"{decision.reason}+learner-explore")
        return decision, t, self._providers[name], choice

    # --- failover -------------------------------------------------------------

    def _fallback_plans(self, plan: _Plan) -> list[_Plan]:
        """``plan`` first, then the other candidates failover may use,
        cheapest first (ties in policy order).

        Every candidate passes the same data-policy rule as ``plan`` (the
        rule for the call's effective sensitivity) and is served by a
        configured provider. The pool is ``plan``'s group; for a pinned
        model it is the same model on any other provider. A use case's
        ``eligible_models`` and the ``max_cost`` budget (or ``plan``'s own
        cost, when that is above the budget) narrow it further. A candidate
        that does not comply is never added.
        """
        d, req = plan.decision, plan.request
        if req is None:
            return [plan]
        rule = data_policy_rule(self._reachable, d.sensitivity_level)
        out = (
            DEFAULT_OUTPUT_TOKENS
            if req.max_output_tokens is None
            else req.max_output_tokens
        )
        eligible: set[str] | None = None
        if req.explicit_group is None and req.use_case is not None:
            bar = (self._reachable.get("use_cases") or {}).get(req.use_case) or {}
            if bar.get("eval_set"):
                eligible = set(bar.get("eligible_models") or ())
        budget = None if req.max_cost is None else max(req.max_cost, d.est_cost)
        if d.reason == "pinned":
            pool = [
                (group, tier)
                for group, tiers in self._reachable["groups"].items()
                for tier in tiers
                if tier["model"] == d.model
            ]
        else:
            pool = [(d.group, t) for t in self._reachable["groups"].get(d.group) or ()]
        seen = {(plan.provider.name, plan.tier["model"])}
        scored = []
        for index, (group, tier) in enumerate(pool):
            name = self._tier_provider(tier)
            if name is None or (name, tier["model"]) in seen:
                continue
            if not tier_complies(tier, rule):
                continue
            if eligible is not None and tier["model"] not in eligible:
                continue
            cost = estimate_tier_cost(tier, req.approx_input_tokens, out)
            if budget is not None and cost > budget:
                continue
            seen.add((name, tier["model"]))
            scored.append((cost, index, group, tier, name))
        scored.sort(key=lambda item: (item[0], item[1]))
        plans = [plan]
        for cost, _, group, tier, name in scored:
            plans.append(replace(
                plan,
                decision=replace(d, model=tier["model"], group=group, est_cost=cost),
                tier=tier,
                provider=self._providers[name],
            ))
        return plans

    def _complies(self, plan: _Plan) -> bool:
        """Defence in depth: the engine re-checks each candidate before it
        calls it (``allow=``)."""
        rule = data_policy_rule(self._reachable, plan.decision.sensitivity_level)
        return plan.provider.name in self._providers and tier_complies(
            plan.tier, rule
        )

    def _candidates(
        self, plan: _Plan, messages: list, max_tokens: int | None, kwargs: dict
    ) -> list[Candidate]:
        """The candidates to try, in order. Each carries ``(plan, call args)``
        as its ``data``.

        A fallback whose provider cannot take this request (OpenAI-only
        parameters on an Anthropic model) is left out; the chosen candidate
        raises ``RouterError`` as before.
        """
        try:
            first = self._call_args(plan, messages, max_tokens, kwargs)
        except RouterError:
            if plan.policy_pick is None:
                raise
            # The learner's model cannot take this request (an Anthropic
            # model and OpenAI-only parameters): route as the policy would.
            d, t, p = plan.policy_pick
            plan = replace(plan, decision=d, tier=t, provider=p,
                           learner=None, policy_pick=None)
            first = self._call_args(plan, messages, max_tokens, kwargs)
        plans = [(plan, first)]
        if self.failover:
            for fallback in self._fallback_plans(plan)[1:]:
                try:
                    args = self._call_args(fallback, messages, max_tokens, kwargs)
                except RouterError:
                    continue
                plans.append((fallback, args))
        return [
            Candidate(p.provider.name, p.decision.model, None, (p, args))
            for p, args in plans
        ]

    def _failed(self, c: Candidate, exc: BaseException, start: float,
                tracker: _AttemptTracker) -> None:
        """Notes a failed attempt for the next attempt's telemetry event,
        classified as the engine classifies it."""
        info = self._engine.failure_of(exc)
        tracker.failures.append(FailoverAttempt(
            c.provider, c.model, False, int((time.monotonic() - start) * 1000),
            classify_failure(info).error_class, info.status,
        ))
        tracker.errors.append(type(exc).__name__)
        tracker.last = exc

    def _attempt_limit(self, tracker: _AttemptTracker) -> NoCompliantCandidate:
        """``max_attempts`` used up: fail closed like an exhausted list."""
        attempts = tracker.failures
        err = NoCompliantCandidate(
            f"no compliant candidate served the call within max_attempts="
            f"{self.max_attempts}; failing closed ("
            + "; ".join(f"{a.provider}/{a.model}: {a.error_class}" for a in attempts)
            + ")",
            attempts=attempts,
        )
        err.__cause__ = tracker.last
        return err

    @staticmethod
    def _attempts(
        engine_attempts: tuple[FailoverAttempt, ...], tracker: _AttemptTracker
    ) -> tuple[Attempt, ...]:
        errors = iter(tracker.errors)
        return tuple(
            Attempt(
                a.provider, a.model, a.ok, a.latency_ms,
                None if a.ok else next(errors, None), a.error_class, a.status,
            )
            for a in engine_attempts
        )

    # --- telemetry and response ----------------------------------------------

    def _event_hook(self, plan: _Plan, captured: dict,
                    tracker: _AttemptTracker | None = None):
        decision, tier = plan.decision, plan.tier
        policy_version = (self.policy or {}).get("policy_version")
        baseline = self.baseline
        scan_pii = self._scan_pii_events

        def hook(event: dict) -> None:
            # Failover (#21): the error class of each failed attempt before
            # this one, never a message (PRD §7.12). The collector stores
            # fallback_chain; retry_count and attempts match the gateway's
            # event and are only sent after a failed attempt.
            prior = list(tracker.failures) if tracker is not None else []
            event["fallback_chain"] = chain_of(prior)
            if prior:
                event["retry_count"] = len(prior)
                event["attempts"] = [
                    {"attempt": i + 1, "provider": a.provider, "model": a.model,
                     "status": a.status, "latency_ms": a.latency_ms,
                     "error_class": a.error_class}
                    for i, a in enumerate(prior)
                ] + [{"attempt": len(prior) + 1, "provider": plan.provider.name,
                      "model": decision.model,
                      "latency_ms": event.get("latency_ms")}]
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
            if plan.learner is not None:
                event["route_learner_mode"] = plan.learner.mode
                event["route_propensity"] = plan.learner.propensity
                event["route_learner_frozen"] = plan.learner.frozen
                if plan.learner.learner_version is not None:
                    event["route_learner_version"] = plan.learner.learner_version
            # Pseudonymised: the client scanned the placeholders. Report the
            # categories of the original prompt (labels only, never values).
            if plan.pseudo is not None and scan_pii:
                if plan.original_pii:
                    event["detected_pii_types"] = list(plan.original_pii)
                else:
                    event.pop("detected_pii_types", None)
            # An OpenAI-compatible provider is dispatched as vendor "openai";
            # a price from OpenAI's table would be the wrong vendor's rate
            # for its model id. Use the catalog's USD rate for the provider
            # instead, else the tier's rates below. A provider-reported cost
            # is kept.
            if plan.provider.openai_compatible and event.get("cost_source") in (
                COST_SOURCE_PRICING_TABLE, COST_SOURCE_PRICING_TABLE_ESTIMATED_TOKENS,
            ):
                event["cost"] = None
                event.pop("cost_source", None)
            if (
                plan.provider.openai_compatible
                and plan.provider.catalog is not None
                and event.get("cost") is None
            ):
                cost = estimate_cost(
                    plan.provider.catalog,
                    event.get("served_model") or decision.model,
                    event.get("tokens_in"),
                    event.get("tokens_out"),
                    tokens_cached=event.get("tokens_cached"),
                )
                if cost is not None:
                    event["cost"] = cost
                    event["cost_source"] = COST_SOURCE_PRICING_TABLE
            if plan.budget is not None:
                event.update(plan.budget.telemetry(plan.budget_action))
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
            # Verified savings (#24): the same tokens on the frozen baseline.
            if baseline is not None:
                baseline.apply(event)
            captured.update(event)
            # The live event: outcome signals are added to it after this
            # hook, and auto mode reads them once the call returns.
            captured["_event"] = event

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
                      attempts: tuple[Attempt, ...],
                      policy_version: str | None) -> RoutedChatCompletion:
        out = (
            from_anthropic(response) if plan.provider.vendor == "anthropic"
            else from_openai(response)
        )
        pseudo_info = _restore_completion(out, plan) if plan.pseudo is not None else None
        out.ps = _RouterCore._ps_result(plan, event, attempts, policy_version, pseudo_info)
        return out

    @staticmethod
    def _ps_result(plan: _Plan, event: dict, attempts: tuple[Attempt, ...],
                   policy_version: str | None,
                   pseudo_info: PseudonymisationInfo | None) -> PSResult:
        d = plan.decision
        return PSResult(
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
                learner=plan.learner,
            ),
            cost=CostInfo(
                event.get("cost"),
                event.get("cost_source"),
                d.est_cost,
                counterfactual_usd=event.get("counterfactual_cost"),
                baseline_id=event.get("baseline_id"),
            ),
            attempts=attempts,
            fallback_chain=tuple(
                a.error_class for a in attempts if not a.ok and a.error_class
            ),
            budget=(
                BudgetInfo(plan.budget.state, plan.budget.budget_id, plan.budget_action)
                if plan.budget is not None and plan.budget.state is not None
                else None
            ),
            pseudonymisation=pseudo_info,
        )

    def _learner_error(self, stage: str, exc: Exception) -> None:
        """Auto mode: the learner raised in ``stage`` (``choose`` or
        ``observe``). The call goes on without it; only the error type is
        logged (the message could carry request data)."""
        logger.warning(
            "routing learner %s raised %s; the call proceeds on the policy's pick",
            stage, type(exc).__name__,
        )
        learner = self.learner
        if learner is not None:
            try:
                learner.note_fallback(stage, exc)
            except Exception:  # noqa: BLE001, S110 - counting is best effort
                pass

    def _learner_failed(self, plan: _Plan) -> None:
        """Auto mode: the call failed outright (no candidate served it)."""
        if plan.learner is not None and self.learner is not None:
            try:
                self.learner.observe(None, plan.learner, [CALL_FAILED])
            except Exception as exc:  # noqa: BLE001 - the learner never fails the call
                self._learner_error("observe", exc)

    def _learner_observe(self, plan: _Plan, captured: dict,
                         attempts: tuple[Attempt, ...]) -> None:
        """Auto mode: report the served call's outcome to the learner."""
        if plan.learner is not None and self.learner is not None:
            live = captured.get("_event") or captured
            signals = live.get("outcome_signals")
            if signals is None and len(attempts) > 1:
                signals = ["fallback"]  # outcome signals are turned off
            try:
                self.learner.observe(captured.get("call_id"), plan.learner, signals)
            except Exception as exc:  # noqa: BLE001 - the learner never fails the call
                self._learner_error("observe", exc)

    def _routed(self, candidate: Candidate, response: Any, captured: dict,
                attempts: tuple[Attempt, ...]) -> RoutedChatCompletion:
        self._learner_observe(candidate.data[0], captured, attempts)
        return self._local_result(
            candidate.data[0], response, captured, attempts,
            (self.policy or {}).get("policy_version"),
        )

    @staticmethod
    def _budget_cost(plan: _Plan, event: dict) -> float | None:
        """The cost recorded against the budgets: the event's, else the
        router's estimate."""
        cost = event.get("cost")
        return cost if cost is not None else plan.decision.est_cost

    def _record_spend(self, plan: _Plan, captured: dict) -> None:
        """Records the cost of the attempt that served the call (``plan`` is
        that candidate's), once; failed attempts are never recorded."""
        if plan.budget is not None and self.budgets is not None:
            self.budgets.commit(plan.budget, self._budget_cost(plan, captured))

    async def _arecord_spend(self, plan: _Plan, captured: dict) -> None:
        if plan.budget is not None and self.budgets is not None:
            await self.budgets.acommit(plan.budget, self._budget_cost(plan, captured))

    # --- streaming (local mode) ----------------------------------------------

    def _stream_event_hook(self, plan: _Plan, captured: dict,
                           tracker: _AttemptTracker):
        """``_event_hook`` for a streamed attempt that also records the spend.

        The instrumented client builds one event per stream, when it ends
        (exhausted, failed, closed early or garbage-collected), with the
        provider's usage from the final chunk, else estimated tokens
        (``tokens_estimated``). An attempt that failed before its first
        chunk builds none (``_stream_lazy_start``). So the cost is recorded
        here, once, however the stream ends; ``commit_soon`` writes a network
        store from a thread when an event loop is running.

        ``AsyncShieldsRouter`` streams use ``commit_soon`` too, not
        ``acommit``, as the instrumented async clients do
        (``budget_event_hook``): the hook is synchronous and may run inside
        the garbage collector (an abandoned stream), where nothing can be
        awaited; inside a running loop ``commit_soon`` already hands a
        blocking store to the default executor, so the loop is never
        blocked, and a task scheduled from here could be lost at shutdown.
        """
        inner = self._event_hook(plan, captured, tracker)
        recorded = False

        def hook(event: dict) -> None:
            nonlocal recorded
            inner(event)
            if recorded:
                return
            recorded = True
            if plan.budget is not None and self.budgets is not None:
                self.budgets.commit_soon(plan.budget, self._budget_cost(plan, event))

        return hook

    def _stream_finished(self, plan: _Plan, captured: dict,
                         attempts: tuple[Attempt, ...]) -> PSResult:
        """A routed stream ended: tell the learner, drop the mapping and
        return the final ``ps``, with the event's cost."""
        try:
            self._learner_observe(plan, captured, attempts)
            pseudo_info = None
            if plan.pseudo is not None:
                stats = plan.pseudo.stats()
                pseudo_info = PseudonymisationInfo(
                    entities=dict(stats.entities),
                    restored=stats.restored,
                    unknown_placeholders=stats.unknown,
                    detected_level=plan.detected_level,
                    anonymise_then_route=plan.anonymised_route,
                )
            return self._ps_result(
                plan, captured, attempts,
                (self.policy or {}).get("policy_version"), pseudo_info,
            )
        finally:
            if plan.pseudo is not None:
                plan.pseudo.clear()

    def _stream_result(self, candidate: Candidate, stream: Any, captured: dict,
                       attempts: tuple[Attempt, ...], is_async: bool) -> Any:
        """The serving attempt's stream (first chunk already read) for the
        caller: placeholders restored across chunks, ``ps`` set now and
        completed when the stream ends."""
        plan = candidate.data[0]
        # The instrumented client's stream (or the provider's, if wrapping it
        # failed): what finalising an abandoned RoutedStream reports and closes.
        upstream = getattr(stream, "_stream", stream)
        if plan.pseudo is not None:
            stream = plan.pseudo.restore_stream(stream, clear_when_done=True)
        ps = self._ps_result(
            plan, {}, attempts, (self.policy or {}).get("policy_version"), None
        )
        cls = AsyncRoutedStream if is_async else RoutedStream
        return cls(stream, ps, lambda: self._stream_finished(plan, captured, attempts),
                   upstream)

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


def _restore_completion(out: RoutedChatCompletion, plan: _Plan) -> PseudonymisationInfo:
    """Put the call's values back into ``out``: content, refusal and tool-call
    arguments. ``out.raw`` stays as the provider returned it."""
    pseudo = plan.pseudo
    assert pseudo is not None
    for choice in out.choices:
        m = choice.message
        m.content = pseudo.restore(m.content)
        m.refusal = pseudo.restore(m.refusal)
        for tc in m.tool_calls or ():
            tc.function.arguments = pseudo.restore(
                tc.function.arguments, json_escape=True
            ) or ""
    stats = pseudo.stats()
    return PseudonymisationInfo(
        entities=dict(stats.entities),
        restored=stats.restored,
        unknown_placeholders=stats.unknown,
        detected_level=plan.detected_level,
        anonymise_then_route=plan.anonymised_route,
    )


_NO_CHUNK = object()


class _Prefetched:
    """A stream whose first chunk the router already read (so a failure
    before it could fail over); that chunk is handed out first."""

    def __init__(self, stream: Any, iterator: Any, first: Any) -> None:
        self._stream = stream
        self._iterator = iterator
        self._first = first

    def __iter__(self):
        return self

    def __next__(self):
        if self._first is not _NO_CHUNK:
            chunk, self._first = self._first, _NO_CHUNK
            return chunk
        if self._iterator is None:
            raise StopIteration
        return next(self._iterator)

    def close(self) -> None:
        self._first = _NO_CHUNK
        close = getattr(self._stream, "close", None)
        if close is not None:
            close()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._stream, name)


class _AsyncPrefetched:
    # No __iter__: restore_stream picks its async wrapper by that.

    def __init__(self, stream: Any, iterator: Any, first: Any) -> None:
        self._stream = stream
        self._iterator = iterator
        self._first = first

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._stream, name)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._first is not _NO_CHUNK:
            chunk, self._first = self._first, _NO_CHUNK
            return chunk
        if self._iterator is None:
            raise StopAsyncIteration
        return await self._iterator.__anext__()

    async def close(self) -> None:
        self._first = _NO_CHUNK
        await _aclose(self._stream)

    async def aclose(self) -> None:
        await self.close()


def _prefetch(stream: Any) -> _Prefetched:
    """Read the first chunk. An error here is still before the stream
    started, so failover may try the next candidate."""
    iterator = iter(stream)
    try:
        first = next(iterator)
    except StopIteration:
        return _Prefetched(stream, None, _NO_CHUNK)
    return _Prefetched(stream, iterator, first)


async def _aprefetch(stream: Any) -> _AsyncPrefetched:
    iterator = stream.__aiter__()
    try:
        first = await iterator.__anext__()
    except StopAsyncIteration:
        return _AsyncPrefetched(stream, None, _NO_CHUNK)
    return _AsyncPrefetched(stream, iterator, first)


async def _aclose(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if close is not None:
        result = close()
        if hasattr(result, "__await__"):
            await result


#: Upstream closes scheduled by finalised async streams, kept until done so
#: the event loop does not drop the tasks.
_closing: set = set()


def _finalize_stream(upstream: Any, on_end: Any, is_async: bool) -> None:
    """A RoutedStream was garbage-collected before it ended (never read, or
    left mid-way without ``close()``). Runs from the garbage collector, at
    most once, never after the stream ended, never at interpreter shutdown.

    In order: the telemetry event and spend are recorded (enqueued, no I/O),
    the upstream is closed, then ``on_end`` tells the learner and drops the
    pseudonymisation mapping. Each step is fail-open and never raises; only
    exception class names are logged, never content.
    """
    if sys.is_finalizing():
        return
    try:
        if isinstance(upstream, _StreamBase):
            # The event that the instrumented stream would build when it is
            # collected, built now so the learner sees its call id.
            upstream.abandon()
        if is_async:
            # An async close cannot be awaited here: scheduled on the running
            # loop when there is one (else the connection is released when
            # the stream is collected, as before).
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                task = loop.create_task(_aclose(upstream))
                _closing.add(task)
                task.add_done_callback(_closing.discard)
        else:
            close = getattr(upstream, "close", None)
            if close is not None:
                close()  # the event is built already: closing sends nothing
    except Exception as exc:  # noqa: BLE001 - never raise from a finalizer
        with contextlib.suppress(Exception):
            logger.debug("routed stream finalise error: %s", type(exc).__name__)
    try:
        on_end()
    except Exception as exc:  # noqa: BLE001 - never raise from a finalizer
        with contextlib.suppress(Exception):
            logger.debug("routed stream finalise error: %s", type(exc).__name__)


class RoutedStream:
    """A local-mode ``stream=True`` call.

    Iterate it, or use it as a context manager, for the serving provider's
    chunks in that provider's shape: chat-completion chunks from OpenAI and
    OpenAI-compatible providers, Messages stream events from Anthropic
    (``ps.route.provider`` says which). With ``pseudonymise=True`` the
    values are restored, also when a placeholder is split across chunks.

    ``ps`` is set from the start (route, attempts, ``fallback_chain``,
    budget, estimated cost) and completed when the stream ends, fails or is
    closed: ``ps.cost.usd`` and ``ps.pseudonymisation`` are filled in then.
    Failover happens only before the first chunk; an error after it is
    raised to the caller and never retried.

    A stream dropped without ``close()`` (never read, or left mid-way) is
    finalised when it is garbage-collected: its telemetry event and spend
    are recorded, the upstream is closed (scheduled on the running loop for
    an async stream), the learner is told and the mapping is dropped, each
    once. Prefer ``with`` / ``close()``: finalisation waits for the garbage
    collector when the stream is part of a reference cycle.
    """

    def __init__(self, stream: Any, ps: PSResult, on_end: Any,
                 upstream: Any = None) -> None:
        self._stream = stream
        self._on_end = on_end
        self._ended = False
        self.ps = ps
        # Holds no reference to self; detached when the stream ends, so an
        # ended stream is never reported again.
        self._finalizer = weakref.finalize(
            self, _finalize_stream, upstream if upstream is not None else stream,
            on_end, isinstance(self, AsyncRoutedStream),
        )
        self._finalizer.atexit = False

    def _end(self) -> None:
        if self._ended:
            return
        self._ended = True
        self._finalizer.detach()
        try:
            self.ps = self._on_end()
        except Exception as exc:  # noqa: BLE001 - fail-open: the stream is over
            logger.debug("routed stream finish error: %s", type(exc).__name__)

    def __iter__(self):
        return self

    def __next__(self):
        if self._ended:
            raise StopIteration
        try:
            return next(self._stream)
        except BaseException:
            # Exhausted, or failed after the first chunk: the error is the
            # caller's, never retried mid-stream.
            self._end()
            raise

    def close(self) -> None:
        try:
            close = getattr(self._stream, "close", None)
            if close is not None:
                close()
        finally:
            self._end()

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._stream, name)


class AsyncRoutedStream(RoutedStream):
    """``RoutedStream`` for ``AsyncShieldsRouter``: ``async for`` and
    ``async with``."""

    def __iter__(self):
        raise TypeError("use 'async for' with an AsyncShieldsRouter stream")

    def __next__(self):
        raise TypeError("use 'async for' with an AsyncShieldsRouter stream")

    def __enter__(self):
        raise TypeError("use 'async with' with an AsyncShieldsRouter stream")

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._ended:
            raise StopAsyncIteration
        try:
            return await self._stream.__anext__()
        except BaseException:
            self._end()
            raise

    async def close(self) -> None:  # type: ignore[override]
        try:
            await _aclose(self._stream)
        finally:
            self._end()

    async def aclose(self) -> None:
        await self.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


class ShieldsRouter(_RouterCore):
    """Picks the model per call under a route policy, then sends it with your
    own provider keys (``mode="local"``), or leaves the choice to the gateway
    (``mode="gateway"``).

    ``providers`` maps a provider name, as policy tiers name it in
    ``provider``, to ``{"api_key": ..., "vendor": "openai" | "anthropic",
    **client options}``. ``vendor`` defaults to ``anthropic`` for the
    ``anthropic`` provider and to ``openai`` (OpenAI-compatible) for every
    other. A provider named after a catalog OpenAI-compatible provider
    (``mistral``, ``berget``, ``infercom``, ``regolo``, ``ionos``,
    ``stackit``, ``scaleway``, ``ovhcloud``; see ``prompt_shields.sovereign``)
    gets its base URL from the catalog; any other name needs ``base_url`` or
    ``catalog_provider``. ``region`` picks a catalogued region. Tiers without ``provider`` go to
    ``default_provider`` (the only provider when there is one).

    ``client.chat.completions.create(model="auto", messages=[...],
    route=RouteHint(...))`` returns a ``RoutedChatCompletion``.

    Failover (local mode; see ``prompt_shields.failover``):

    * ``failover`` (default ``True``): ``False`` makes one attempt per call
      and raises the provider's error, as before #21.
    * ``breakers``: a ``BreakerRegistry`` to use, e.g. one shared by several
      routers. Default: the process-wide ``default_registry()``.
    * ``breaker_config``: a ``BreakerConfig`` (``failure_threshold``,
      ``open_seconds``, ``half_open_max_probes``) for a registry of this
      router's own. Not together with ``breakers``.
    * ``max_attempts``: provider attempts per call, retries included.
      Default: the policy's ``fallback.max_attempts``, else 3.
    * ``retry_after_max_wait``: longest 429 ``Retry-After`` honoured, in
      seconds. Default: the policy's ``fallback.retry_after_max_wait_ms``,
      else 10.
    * ``rate_limit_retries``: retries of one candidate after a 429 (1).
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

        # Route, data-policy gate, budgets and the learner's choice all
        # happen before anything is sent, streamed or not.
        plan = r._plan(
            messages, model, route,
            max_tokens if max_tokens is not None
            else kwargs.get("max_completion_tokens"),
            data_classification, use_case,
            budget=r._budget_check(use_case),
        )
        streaming = kwargs.get("stream") is True
        try:
            result = self._send(plan, max_tokens, kwargs, ps_metadata, captured)
        except BaseException:
            if plan.pseudo is not None:
                plan.pseudo.clear()
            raise
        # The mapping lives for this call only; a stream drops it when it
        # ends, fails or is closed.
        if plan.pseudo is not None and not streaming:
            plan.pseudo.clear()
        return result

    def _send(self, plan: Any, max_tokens: int | None, kwargs: dict,
              ps_metadata: PSMetadata | None, captured: dict) -> Any:
        r = self._router
        candidates = r._candidates(plan, plan.messages, max_tokens, kwargs)
        try:
            if kwargs.get("stream") is True:
                return self._dispatch_stream(candidates, ps_metadata, captured)
            return self._dispatch(candidates, ps_metadata, captured)
        except Exception:
            # Auto mode: a call that failed outright counts against the
            # learner's pick (the rollback guard sees it).
            r._learner_failed(candidates[0].data[0])
            raise

    def _dispatch(self, candidates: list[Candidate],
                  ps_metadata: PSMetadata | None, captured: dict) -> Any:
        r = self._router
        tracker = _AttemptTracker()

        def call(c: Candidate) -> Any:
            cplan, (call_messages, call_max, call_kwargs) = c.data
            if len(tracker.failures) >= r.max_attempts:
                raise _AttemptLimit()
            captured.clear()
            start = time.monotonic()
            try:
                return r._client(cplan.provider.name).chat.completions._create(
                    model=cplan.decision.model, messages=call_messages,
                    ps_metadata=ps_metadata, max_tokens=call_max,
                    _event_hook=r._event_hook(cplan, captured, tracker),
                    **call_kwargs,
                )
            except Exception as exc:
                r._failed(c, exc, start, tracker)
                raise

        if not r.failover:
            start = time.monotonic()
            response = call(candidates[0])
            attempt = Attempt(candidates[0].provider, candidates[0].model, True,
                              int((time.monotonic() - start) * 1000))
            r._record_spend(candidates[0].data[0], captured)
            return r._routed(candidates[0], response, captured, (attempt,))
        try:
            result = r._engine.run(
                candidates, call, allow=lambda c: r._complies(c.data[0])
            )
        except _AttemptLimit:
            raise r._attempt_limit(tracker) from tracker.last
        r._record_spend(result.candidate.data[0], captured)
        return r._routed(result.candidate, result.response, captured,
                         r._attempts(result.attempts, tracker))

    def _dispatch_stream(self, candidates: list[Candidate],
                         ps_metadata: PSMetadata | None, captured: dict) -> Any:
        """``stream=True``: an attempt succeeds once its first chunk is in.

        A failure before that (the request raising, or the stream failing
        before any chunk: connection errors, 5xx, 429) is a failed attempt
        like a non-streamed one, so failover may try the next candidate.
        Once the first chunk is in, the engine is done: a later error goes
        to the caller and is never retried. The spend is recorded once,
        when the stream's telemetry event is built (``_stream_event_hook``).
        """
        r = self._router
        tracker = _AttemptTracker()

        def call(c: Candidate) -> Any:
            cplan, (call_messages, call_max, call_kwargs) = c.data
            if len(tracker.failures) >= r.max_attempts:
                raise _AttemptLimit()
            captured.clear()
            start = time.monotonic()
            stream = None
            try:
                stream = r._client(cplan.provider.name).chat.completions._create(
                    model=cplan.decision.model, messages=call_messages,
                    ps_metadata=ps_metadata, max_tokens=call_max,
                    _event_hook=r._stream_event_hook(cplan, captured, tracker),
                    _stream_lazy_start=True,
                    **call_kwargs,
                )
                return _prefetch(stream)
            except BaseException as exc:
                if stream is not None:
                    with contextlib.suppress(Exception):
                        stream.close()
                if isinstance(exc, Exception):
                    r._failed(c, exc, start, tracker)
                raise

        if not r.failover:
            start = time.monotonic()
            stream = call(candidates[0])
            attempt = Attempt(candidates[0].provider, candidates[0].model, True,
                              int((time.monotonic() - start) * 1000))
            return r._stream_result(candidates[0], stream, captured, (attempt,), False)
        try:
            result = r._engine.run(
                candidates, call, allow=lambda c: r._complies(c.data[0])
            )
        except _AttemptLimit:
            raise r._attempt_limit(tracker) from tracker.last
        return r._stream_result(result.candidate, result.response, captured,
                                r._attempts(result.attempts, tracker), False)


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

        plan = r._plan(
            messages, model, route,
            max_tokens if max_tokens is not None
            else kwargs.get("max_completion_tokens"),
            data_classification, use_case,
            budget=await r._abudget_check(use_case),
        )
        streaming = kwargs.get("stream") is True
        try:
            result = await self._send(plan, max_tokens, kwargs, ps_metadata, captured)
        except BaseException:
            if plan.pseudo is not None:
                plan.pseudo.clear()
            raise
        # The mapping lives for this call only; a stream drops it when it
        # ends, fails or is closed.
        if plan.pseudo is not None and not streaming:
            plan.pseudo.clear()
        return result

    async def _send(self, plan: Any, max_tokens: int | None, kwargs: dict,
              ps_metadata: PSMetadata | None, captured: dict) -> Any:
        r = self._router
        candidates = r._candidates(plan, plan.messages, max_tokens, kwargs)
        try:
            if kwargs.get("stream") is True:
                return await self._dispatch_stream(candidates, ps_metadata, captured)
            return await self._dispatch(candidates, ps_metadata, captured)
        except Exception:
            # Auto mode: a call that failed outright counts against the
            # learner's pick (the rollback guard sees it).
            r._learner_failed(candidates[0].data[0])
            raise

    async def _dispatch(self, candidates: list[Candidate],
                  ps_metadata: PSMetadata | None, captured: dict) -> Any:
        r = self._router
        tracker = _AttemptTracker()

        async def call(c: Candidate) -> Any:
            cplan, (call_messages, call_max, call_kwargs) = c.data
            if len(tracker.failures) >= r.max_attempts:
                raise _AttemptLimit()
            captured.clear()
            start = time.monotonic()
            try:
                return await r._client(cplan.provider.name).chat.completions._create(
                    model=cplan.decision.model, messages=call_messages,
                    ps_metadata=ps_metadata, max_tokens=call_max,
                    _event_hook=r._event_hook(cplan, captured, tracker),
                    **call_kwargs,
                )
            except Exception as exc:
                r._failed(c, exc, start, tracker)
                raise

        if not r.failover:
            start = time.monotonic()
            response = await call(candidates[0])
            attempt = Attempt(candidates[0].provider, candidates[0].model, True,
                              int((time.monotonic() - start) * 1000))
            await r._arecord_spend(candidates[0].data[0], captured)
            return r._routed(candidates[0], response, captured, (attempt,))
        try:
            result = await r._engine.arun(
                candidates, call, allow=lambda c: r._complies(c.data[0])
            )
        except _AttemptLimit:
            raise r._attempt_limit(tracker) from tracker.last
        await r._arecord_spend(result.candidate.data[0], captured)
        return r._routed(result.candidate, result.response, captured,
                         r._attempts(result.attempts, tracker))

    async def _dispatch_stream(self, candidates: list[Candidate],
                               ps_metadata: PSMetadata | None,
                               captured: dict) -> Any:
        """``stream=True``; see ``_RouterCompletions._dispatch_stream``."""
        r = self._router
        tracker = _AttemptTracker()

        async def call(c: Candidate) -> Any:
            cplan, (call_messages, call_max, call_kwargs) = c.data
            if len(tracker.failures) >= r.max_attempts:
                raise _AttemptLimit()
            captured.clear()
            start = time.monotonic()
            stream = None
            try:
                stream = await r._client(cplan.provider.name).chat.completions._create(
                    model=cplan.decision.model, messages=call_messages,
                    ps_metadata=ps_metadata, max_tokens=call_max,
                    _event_hook=r._stream_event_hook(cplan, captured, tracker),
                    _stream_lazy_start=True,
                    **call_kwargs,
                )
                return await _aprefetch(stream)
            except BaseException as exc:
                if stream is not None:
                    with contextlib.suppress(Exception):
                        await _aclose(stream)
                if isinstance(exc, Exception):
                    r._failed(c, exc, start, tracker)
                raise

        if not r.failover:
            start = time.monotonic()
            stream = await call(candidates[0])
            attempt = Attempt(candidates[0].provider, candidates[0].model, True,
                              int((time.monotonic() - start) * 1000))
            return r._stream_result(candidates[0], stream, captured, (attempt,), True)
        try:
            result = await r._engine.arun(
                candidates, call, allow=lambda c: r._complies(c.data[0])
            )
        except _AttemptLimit:
            raise r._attempt_limit(tracker) from tracker.last
        return r._stream_result(result.candidate, result.response, captured,
                                r._attempts(result.attempts, tracker), True)
