"""Unit tests for the Python reference router (``prompt_shields.route_policy``).

The golden vectors (test_route_policy_vectors.py) are the contract. These
tests pin the places where Python and JavaScript differ and the router must
follow the gateway. Expected values were produced by running the same inputs
through gateway/src/middlewares/router (resolveRoute + HeuristicStrategy).
"""

from __future__ import annotations

import copy
import math

import pytest
from prompt_shields.route_policy import (
    RouteDecision,
    build_route_request,
    estimate_input_tokens,
    parse_route_headers,
    resolve_route,
)


def _tier(model, inp, out):
    return {"model": model, "input_cost_per_1k": inp, "output_cost_per_1k": out}


POLICY = {
    "schema_version": "route-policy.v1",
    "default_group": "balanced",
    "quality_to_group": {
        "draft": "cheap",
        "balanced": "balanced",
        "critical": "frontier",
    },
    "groups": {
        "zeta": [_tier("z", 0.001, 0.001)],
        "2": [_tier("two", 0.001, 0.001)],
        "cheap": [_tier("mini", 0.0001, 0.0004)],
        "balanced": [_tier("mid", 0.0025, 0.01)],
        "frontier": [_tier("big", 0.015, 0.06)],
    },
}


def _route(body, headers, policy=POLICY):
    req = build_route_request(body, parse_route_headers(headers))
    return req, resolve_route(req, policy)


def _hi(**extra):
    return {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 100, **extra}


def test_json_schema_match_is_case_insensitive():
    req, d = _route(
        {"messages": [{"role": "user", "content": "Strict JSON please"}]}, {}
    )
    assert req.approx_input_tokens == 5
    assert (d.model, d.group, d.reason) == ("big", "frontier", "heuristic:complex")
    assert d.est_cost == pytest.approx(0.030795, rel=1e-12)


def test_case_folding_is_ascii_only_like_js():
    # U+017F folds to "s" under Python's Unicode IGNORECASE, not in JS /i.
    _, d = _route({"messages": [{"role": "user", "content": "\u017ftrict json"}]}, {})
    assert d.reason == "heuristic:simple"


def test_token_estimate_counts_utf16_code_units():
    assert estimate_input_tokens([{"content": "\U0001f600\U0001f600"}]) == 1
    assert estimate_input_tokens([{"content": [{"text": "abcd"}, {"text": "e"}]}]) == 2
    assert estimate_input_tokens([None, "x", {"content": 5}]) == 0


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0.002", 0.002),
        (" 1e-9 ", 1e-9),
        ("0x1", 1.0),
        ("", 0.0),
        ("Infinity", math.inf),
        ("1_0", None),
        ("inf", None),
        ("nan", None),
        ("abc", None),
        ("-0x1", None),
        ("١", None),  # Arabic-Indic digit one: float() accepts it, JS does not
    ],
)
def test_max_cost_header_parses_like_js_number(raw, expected):
    assert parse_route_headers({"X-PS-Max-Cost": raw}).max_cost == expected


def test_header_names_are_case_insensitive_and_unknown_quality_ignored():
    hints = parse_route_headers(
        {"x-ps-QUALITY": "draft", "X-Ps-Route": "", "x-ps-cache": "OFF"}
    )
    assert (hints.quality, hints.explicit_group, hints.cache_off) == (
        "draft",
        None,
        True,
    )
    assert parse_route_headers({"X-PS-Quality": "best"}).quality is None


def test_empty_max_cost_is_zero_budget():
    _, d = _route(_hi(), {"X-PS-Quality": "critical", "X-PS-Max-Cost": ""})
    assert (d.model, d.reason) == ("mini", "quality:critical+budget-unsatisfiable")


def test_budget_clamp_tie_follows_js_object_key_order():
    # "2" and "zeta" cost the same; JS lists array-index keys first.
    _, d = _route(_hi(), {"X-PS-Quality": "critical", "X-PS-Max-Cost": "0.0006"})
    assert (d.model, d.group, d.reason) == ("two", "2", "quality:critical+budget-clamp")


def test_override_budget_unsatisfiable():
    _, d = _route(_hi(), {"X-PS-Route": "cheap", "X-PS-Max-Cost": " 1e-9 "})
    assert (d.model, d.group, d.reason) == (
        "mini",
        "cheap",
        "override+budget-unsatisfiable",
    )


def test_cheapest_in_group_tie_keeps_first_listed():
    policy = copy.deepcopy(POLICY)
    policy["groups"]["cheap"] = [
        _tier("first", 0.0001, 0.0004),
        _tier("second", 0.0001, 0.0004),
    ]
    _, d = _route(_hi(), {"X-PS-Quality": "draft"}, policy)
    assert d.model == "first"


def test_prototype_named_group_is_unknown():
    # The gateway throws here (groups["constructor"] is Object); the
    # reference router treats it as any other unknown group.
    _, d = _route(_hi(), {"X-PS-Route": "constructor"})
    assert (d.group, d.reason) == ("balanced", "override:unknown-group-default")


def test_heuristic_group_missing_falls_back_to_default():
    policy = copy.deepcopy(POLICY)
    del policy["groups"]["cheap"]
    _, d = _route(_hi(), {}, policy)
    assert d == RouteDecision(
        "mid", "balanced", "heuristic:empty-group-fallback", d.est_cost
    )


def test_unknown_group_with_empty_default_keeps_requested_model():
    policy = copy.deepcopy(POLICY)
    policy["groups"]["balanced"] = []
    _, d = _route(_hi(model="gpt-x"), {"X-PS-Route": "nope"}, policy)
    assert d == RouteDecision("gpt-x", "balanced", "override:unknown-group-default", 0)


def test_unsupported_strategy_raises_only_on_transparent_path():
    policy = {**POLICY, "strategy": "learned"}
    with pytest.raises(NotImplementedError):
        _route(_hi(), {}, policy)
    assert _route(_hi(), {"X-PS-Quality": "draft"}, policy)[1].model == "mini"


def test_max_completion_tokens_and_default_output():
    req = build_route_request(
        {"messages": [], "max_completion_tokens": 7}, parse_route_headers({})
    )
    assert req.max_output_tokens == 7
    assert build_route_request(None, parse_route_headers({})).requested_model == "auto"


def test_policy_is_not_mutated():
    before = copy.deepcopy(POLICY)
    _route(_hi(), {"X-PS-Quality": "critical", "X-PS-Max-Cost": "0.0006"})
    assert POLICY == before
