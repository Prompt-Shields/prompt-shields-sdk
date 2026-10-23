"""Tests for ShieldsRouter / AsyncShieldsRouter (#22, PRD 0001 §7.11).

No network: each provider's instrumented client gets a fake upstream SDK
object, and its telemetry sender records events instead of sending them.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from anthropic.types import Message
from openai.types.chat import ChatCompletion
from prompt_shields import (
    AsyncShieldsRouter,
    PolicyViolation,
    RoutedChatCompletion,
    RouteHint,
    RouterError,
    ShieldsClient,
    ShieldsRouter,
)
from prompt_shields.route_policy import resolve_policy_route

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "schemas" / "route-policy"
VECTOR_FILES = sorted((SCHEMA_DIR / "vectors").glob("*.json"))


def _policy(name: str) -> dict:
    return json.loads((SCHEMA_DIR / "examples" / f"{name}.json").read_text())


NORDIC = _policy("nordic-sovereign")
NORDIC_PROVIDERS = {
    "openai": {"api_key": "sk-openai"},
    "anthropic": {"api_key": "sk-ant"},
    "scaleway": {"api_key": "sk-scw", "base_url": "https://scw.example/v1"},
    "mistral-eu": {"api_key": "sk-mis", "base_url": "https://mis.example/v1"},
    "berget": {"api_key": "sk-berget", "base_url": "https://api.berget.ai/v1"},
}
HI = [{"role": "user", "content": "hi"}]


def _openai_response(model: str, content: str = "ok", prompt=10, completion=5):
    return ChatCompletion.model_validate({
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1700000000,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    })


def _anthropic_response(model: str):
    return Message.model_validate({
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [
            {"type": "text", "text": "Hello"},
            {"type": "tool_use", "id": "tu_1", "name": "lookup",
             "input": {"q": "x"}},
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 7,
                  "cache_read_input_tokens": 3},
    })


class _FakeUpstream:
    """Stands in for openai.OpenAI / anthropic.Anthropic (sync or async)."""

    def __init__(self, vendor: str, is_async: bool):
        self.calls: list[dict] = []
        self._vendor = vendor

        def respond(**kwargs):
            self.calls.append(kwargs)
            if vendor == "anthropic":
                return _anthropic_response(kwargs["model"])
            return _openai_response(kwargs["model"])

        async def arespond(**kwargs):
            return respond(**kwargs)

        create = arespond if is_async else respond
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
        self.messages = SimpleNamespace(create=create)


def _wire(router, is_async=False):
    """Fake every provider's upstream and capture telemetry events."""
    events: list[dict] = []
    upstreams = {}
    names = list(router._providers) or ["gateway"]
    for name in names:
        client = router._clients["gateway"] if name == "gateway" else router._client(name)
        fake = _FakeUpstream(client._vendor, is_async)
        client._upstream = fake
        upstreams[name] = fake
        client._telemetry.enqueue = events.append
        if is_async:
            async def noop():
                return None
            client._telemetry.flush = noop
        else:
            client._telemetry.flush_sync = lambda: None
    return events, upstreams


def _router(**kwargs) -> ShieldsRouter:
    kwargs.setdefault("ps_api_key", "ps-test")
    kwargs.setdefault("providers", NORDIC_PROVIDERS)
    kwargs.setdefault("policy", NORDIC)
    return ShieldsRouter(**kwargs)


# --- golden vectors -----------------------------------------------------------

VECTOR_PROVIDERS = {
    "nordic-sovereign": NORDIC_PROVIDERS,
    # Tiers without `provider` go to the only configured provider.
    "deterministic": {"openai": {"api_key": "sk-test"}},
}


def _vectors():
    for path in VECTOR_FILES:
        doc = json.loads(path.read_text())
        for vector in doc["vectors"]:
            yield pytest.param(doc["policy"], vector, id=f"{path.stem}/{vector['id']}")


@pytest.mark.parametrize("policy_name,vector", list(_vectors()))
def test_router_passes_golden_vector(policy_name, vector):
    """Every shared decision vector, pending for the gateway or not, decides
    the same through the router (all providers configured)."""
    router = _router(
        providers=VECTOR_PROVIDERS[policy_name], policy=_policy(policy_name)
    )
    request = vector["request"]
    hint = RouteHint(**request.get("hints", {}))
    args = {
        "model": request.get("model", "auto"),
        "route": hint,
        "max_tokens": request.get("max_tokens"),
        "data_classification": request.get("data_classification"),
        "use_case": request.get("use_case"),
        "pii_detected": request.get("pii_detected"),
    }
    expected = dict(vector["expected"])

    def decide():
        if request.get("approx_input_tokens") is None:
            return router.route(request["messages"], **args)
        req = router._route_request(
            request["messages"], args["model"], hint, args["max_tokens"],
            args["data_classification"], args["use_case"], args["pii_detected"],
        )
        req.approx_input_tokens = request["approx_input_tokens"]
        return router._decide(req)

    if expected.pop("error", None) == "PolicyViolation":
        with pytest.raises(PolicyViolation):
            decide()
        return
    decision = decide()
    est_cost = expected.pop("est_cost", None)
    assert {k: getattr(decision, k) for k in expected} == expected
    if est_cost is not None:
        assert decision.est_cost == pytest.approx(est_cost, rel=1e-9, abs=1e-15)


# --- local mode: dispatch, response shape, telemetry ------------------------------


def test_routes_and_dispatches_to_chosen_provider():
    router = _router()
    events, upstreams = _wire(router)
    resp = router.chat.completions.create(
        messages=HI, route=RouteHint(quality="draft"), max_tokens=100,
        data_classification="confidential",
    )
    # confidential: EEA + ZDR + no CLOUD Act -> cheapest compliant in cheap.
    assert isinstance(resp, RoutedChatCompletion)
    assert resp.ps.route.model == "scaleway-small"
    assert resp.ps.route.provider == "scaleway"
    assert resp.ps.route.jurisdiction == "EEA"
    assert resp.ps.route.sensitivity_level == "confidential"
    assert resp.ps.route.policy_version == "nordic-sovereign-1"
    assert resp.ps.route.mode == "local"
    assert [a.provider for a in resp.ps.attempts] == ["scaleway"]
    assert resp.ps.attempts[0].ok
    # Only the chosen provider was called, with the user's max_tokens.
    assert upstreams["scaleway"].calls[0]["max_tokens"] == 100
    assert upstreams["scaleway"].calls[0]["model"] == "scaleway-small"
    assert all(not u.calls for n, u in upstreams.items() if n != "scaleway")
    # No X-PS-* headers go to a provider directly.
    assert "extra_headers" not in upstreams["scaleway"].calls[0]

    (event,) = events
    assert event["requested_model"] == "auto"
    assert event["served_model"] == "scaleway-small"
    assert event["route_mode"] == "local"
    assert event["route_group"] == "cheap"
    assert event["route_reason"] == "quality:draft"
    assert event["served_provider"] == "scaleway"
    assert event["served_jurisdiction"] == "EEA"
    assert event["sensitivity_level"] == "confidential"
    assert event["sensitivity_source"] == "declared"
    assert event["policy_version"] == "nordic-sovereign-1"
    # Not in the pricing table: priced at the policy tier's rates.
    assert event["cost_source"] == "route_policy"
    assert event["cost"] == pytest.approx(10 / 1000 * 0.0002 + 5 / 1000 * 0.0005)
    assert resp.ps.cost.usd == event["cost"]
    assert resp.ps.cost.source == "route_policy"
    assert resp.ps.cost.estimated_usd == pytest.approx(resp.ps.route.est_cost)


def test_openai_shape_with_raw():
    router = _router()
    _wire(router)
    resp = router.chat.completions.create(
        model="gpt-4o-mini", messages=HI, data_classification="public"
    )
    assert resp.ps.route.provider == "openai"
    assert resp.choices[0].message.content == "ok"
    assert resp.choices[0].finish_reason == "stop"
    assert resp.usage.total_tokens == 15
    assert isinstance(resp.raw, ChatCompletion)
    assert resp.to_dict()["choices"][0]["message"] == {
        "role": "assistant", "content": "ok",
    }
    # Priced from the built-in table for a known model.
    assert resp.ps.cost.source == "pricing_table"


def test_anthropic_route_is_translated_both_ways():
    router = _router()
    events, upstreams = _wire(router)
    resp = router.chat.completions.create(
        messages=[
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        ],
        route=RouteHint(model_group="frontier"),
        data_classification="public",
        max_completion_tokens=64,
        stop="END",
        temperature=0.2,
    )
    call = upstreams["anthropic"].calls[0]
    assert call["model"] == "claude-sonnet-4-20250514"
    assert call["system"] == "Be brief."
    assert call["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    ]
    assert call["max_tokens"] == 64
    assert call["stop_sequences"] == ["END"]
    assert call["temperature"] == 0.2

    assert resp.object == "chat.completion"
    msg = resp.choices[0].message
    assert msg.content == "Hello"
    assert msg.tool_calls[0].function.name == "lookup"
    assert json.loads(msg.tool_calls[0].function.arguments) == {"q": "x"}
    assert resp.choices[0].finish_reason == "tool_calls"
    assert resp.usage.prompt_tokens == 15  # 12 + 3 cache reads
    assert isinstance(resp.raw, Message)
    assert events[0]["vendor"] == "anthropic"
    assert events[0]["route_reason"] == "override"


def test_openai_only_params_refused_for_anthropic_route():
    router = _router()
    _, upstreams = _wire(router)
    with pytest.raises(RouterError, match="response_format"):
        router.chat.completions.create(
            messages=HI, route=RouteHint(model_group="frontier"),
            data_classification="public", response_format={"type": "json_object"},
        )
    assert not upstreams["anthropic"].calls


# --- fail closed ------------------------------------------------------------------


def test_no_compliant_candidate_fails_closed_and_sends_nothing():
    router = _router()
    events, upstreams = _wire(router)
    with pytest.raises(PolicyViolation) as err:
        router.chat.completions.create(
            messages=HI, route=RouteHint(model_group="us-hosted"),
            data_classification="restricted",
        )
    assert err.value.sensitivity_level == "restricted"
    assert not events
    assert all(not u.calls for u in upstreams.values())


def test_pii_in_prompt_escalates_sensitivity():
    router = _router()
    _wire(router)
    # A patient record: health_data escalates internal -> restricted, which
    # only berget and mistral-eu may serve.
    resp = router.chat.completions.create(
        messages=[{"role": "user", "content": "Summarise this medical record"}],
        route=RouteHint(quality="balanced"), data_classification="internal",
    )
    assert resp.ps.route.sensitivity_level == "restricted"
    assert resp.ps.route.sensitivity_source == "pii_escalated"
    assert resp.ps.route.provider in ("berget", "mistral-eu")


def test_pii_in_content_parts_is_scanned_even_with_scan_pii_off():
    router = _router(scan_pii=False)
    decision = router.route(
        [{"role": "user", "content": [{"type": "text", "text": "lab result 4"}]}],
        route=RouteHint(quality="draft"), data_classification="public",
    )
    assert decision.sensitivity_level == "restricted"


def test_unconfigured_provider_is_never_used():
    # Without berget/mistral-eu keys, nothing may serve restricted data.
    router = _router(providers={
        "openai": {"api_key": "sk"}, "anthropic": {"api_key": "sk"},
        "scaleway": {"api_key": "sk", "base_url": "https://scw.example/v1"},
    })
    with pytest.raises(PolicyViolation):
        router.route(HI, data_classification="restricted")
    # Confidential still has scaleway in `cheap`.
    assert router.route(
        HI, route=RouteHint(quality="draft"), data_classification="confidential"
    ).model == "scaleway-small"


def test_unclassified_call_uses_policy_default_level():
    router = _router()
    decision = router.route(HI, route=RouteHint(quality="draft"))
    assert (decision.sensitivity_level, decision.sensitivity_source) == (
        "confidential", "default",
    )
    assert decision.model == "scaleway-small"


def test_client_level_classification_and_per_call_override():
    router = _router(data_classification="public", use_case="legal-review")
    d = router.route(HI)
    assert d.sensitivity_level == "public"
    assert d.group == "frontier"  # legal-review: min_group frontier
    d = router.route(HI, data_classification="restricted", use_case="other")
    assert d.sensitivity_level == "restricted"


# --- pinned model -------------------------------------------------------------------


def test_pinned_model_is_checked_against_data_policy():
    router = _router()
    events, upstreams = _wire(router)
    resp = router.chat.completions.create(
        model="gpt-4o", messages=HI, data_classification="internal"
    )
    assert resp.ps.route.reason == "pinned"
    assert resp.ps.route.provider == "openai"
    assert events[0]["requested_model"] == "gpt-4o"
    with pytest.raises(PolicyViolation):
        router.chat.completions.create(
            model="gpt-4o", messages=HI, data_classification="confidential"
        )
    assert len(upstreams["openai"].calls) == 1


def test_pinned_model_outside_policy_is_refused():
    router = _router()
    with pytest.raises(RouterError, match="not in the route policy"):
        router.route(HI, model="gpt-5-unknown", data_classification="public")


def test_hint_with_concrete_model_routes_like_gateway():
    router = _router()
    d = router.route(HI, model="gpt-4o", route=RouteHint(quality="draft"),
                     data_classification="public")
    assert (d.group, d.reason) == ("cheap", "quality:draft")
    assert d.model == "scaleway-small"


def test_route_agrees_with_reference_router():
    router = _router()
    for level in ("public", "internal", "confidential", "restricted"):
        for quality in ("draft", "balanced", "critical"):
            d = router.route(HI, route=RouteHint(quality=quality),
                             data_classification=level, max_tokens=100)
            req = router._route_request(HI, "auto", RouteHint(quality=quality),
                                        100, level, None, None)
            assert d == resolve_policy_route(req, NORDIC)


# --- configuration ---------------------------------------------------------------------


def test_policy_from_json_and_yaml_files(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(NORDIC))
    assert _router(policy=str(path)).policy["policy_version"] == "nordic-sovereign-1"
    yaml = pytest.importorskip("yaml")
    ypath = tmp_path / "ps-route.yaml"
    ypath.write_text(yaml.safe_dump(NORDIC))
    assert _router(policy=ypath).policy == NORDIC


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"providers": {}}, "at least one provider"),
        ({"providers": {"openai": {}}}, "no api_key"),
        ({"providers": {"x": {"api_key": "k", "vendor": "cohere"}}}, "vendor"),
        ({"providers": {"other": {"api_key": "k", "base_url": "http://x"}}},
         "no model in the route policy"),
        ({"policy": {"groups": {}}}, "schema_version"),
        ({"mode": "edge"}, "mode"),
        ({"default_provider": "nope"}, "default_provider"),
    ],
)
def test_bad_configuration_is_rejected(kwargs, match):
    with pytest.raises(RouterError, match=match):
        _router(**kwargs)


def test_stream_is_refused_in_local_mode():
    router = _router()
    with pytest.raises(RouterError, match="stream"):
        router.chat.completions.create(messages=HI, stream=True)


def test_existing_clients_are_unchanged_by_the_router():
    """ShieldsClient still never routes: model and hints pass through as-is."""
    client = ShieldsClient(api_key="sk", ps_api_key="ps")
    fake = _FakeUpstream("openai", is_async=False)
    client._upstream = fake
    client._telemetry.enqueue = lambda e: None
    client._telemetry.flush_sync = lambda: None
    resp = client.chat.completions.create(
        model="auto", messages=HI, route=RouteHint(quality="draft")
    )
    assert isinstance(resp, ChatCompletion)
    assert fake.calls[0]["model"] == "auto"
    assert fake.calls[0]["extra_headers"] == {"X-PS-Quality": "draft"}


# --- gateway mode -------------------------------------------------------------------


def test_gateway_mode_delegates_with_headers():
    router = ShieldsRouter(
        ps_api_key="ps", mode="gateway", gateway_url="https://gw.example/v1",
        gateway_api_key="sk-provider", use_case="claims",
    )
    gw = router._clients["gateway"]
    assert str(gw._upstream.base_url).startswith("https://gw.example/v1")
    assert gw._upstream.default_headers["X-PS-Use-Case"] == "claims"
    events, upstreams = _wire(router)
    resp = router.chat.completions.create(
        messages=HI, route=RouteHint(quality="draft", max_cost=0.01),
        data_classification="confidential",
    )
    call = upstreams["gateway"].calls[0]
    assert call["model"] == "auto"
    assert call["extra_headers"] == {
        "X-PS-Quality": "draft",
        "X-PS-Max-Cost": "0.01",
        "X-PS-Data-Classification": "confidential",
    }
    assert resp.ps.route.mode == "gateway"
    assert events[0]["route_mode"] == "gateway"
    assert events[0]["requested_model"] == "auto"
    with pytest.raises(RouterError):
        router.route(HI)


def test_gateway_mode_needs_url():
    with pytest.raises(RouterError, match="gateway_url"):
        ShieldsRouter(ps_api_key="ps", mode="gateway")


# --- async -----------------------------------------------------------------------------


async def test_async_router_local_and_fail_closed():
    router = AsyncShieldsRouter(
        ps_api_key="ps", providers=NORDIC_PROVIDERS, policy=copy.deepcopy(NORDIC)
    )
    events, upstreams = _wire(router, is_async=True)
    resp = await router.chat.completions.create(
        messages=HI, route=RouteHint(quality="critical"),
        data_classification="confidential",
    )
    assert resp.ps.route.model == "mistral-large-eu"
    assert resp.ps.route.provider == "mistral-eu"
    assert events[0]["route_group"] == "frontier"
    assert upstreams["mistral-eu"].calls[0]["model"] == "mistral-large-eu"

    with pytest.raises(PolicyViolation):
        await router.chat.completions.create(
            messages=HI, route=RouteHint(model_group="us-hosted"),
            data_classification="restricted",
        )
    assert len(events) == 1


async def test_async_router_anthropic_and_gateway():
    router = AsyncShieldsRouter(
        ps_api_key="ps", providers=NORDIC_PROVIDERS, policy=NORDIC
    )
    _, upstreams = _wire(router, is_async=True)
    resp = await router.chat.completions.create(
        messages=HI, route=RouteHint(model_group="frontier"),
        data_classification="public",
    )
    assert resp.choices[0].message.content == "Hello"
    assert upstreams["anthropic"].calls[0]["max_tokens"] == 1024

    gw = AsyncShieldsRouter(ps_api_key="ps", mode="gateway",
                            gateway_url="https://gw.example/v1")
    events, ups = _wire(gw, is_async=True)
    resp = await gw.chat.completions.create(messages=HI)
    assert ups["gateway"].calls[0]["model"] == "auto"
    assert resp.ps.route.mode == "gateway"
    assert events[0]["route_mode"] == "gateway"
