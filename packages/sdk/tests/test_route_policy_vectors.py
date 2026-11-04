"""Conformance tests for the shared route-policy.v1 contract.

The schema, the example policies and the golden decision vectors live in
``schemas/route-policy/`` and are shared with the gateway router (see
``gateway/src/middlewares/router/vectors.test.ts``). Only data is shared;
each implementation has its own runner.

The SDK client does not route: it only encodes intent as ``X-PS-*`` headers
(``RouteHint``). Decision vectors run through the Python reference router
(``prompt_shields.route_policy``, used by policy replay, #13) along the same
path as the gateway runner: hints -> ``X-PS-*`` headers ->
``parse_route_headers`` -> ``build_route_request`` -> ``resolve_route``.
The sensitivity, data-policy and quality-bar vectors need the whole policy:
``test_full_policy_vector`` runs them through ``resolve_policy_route``, the
port of the gateway's ``resolvePolicyRoute`` (the gateway applies those
stages whenever a policy has them). Vectors with status ``pending`` are
skipped with their pending_reason; nothing is counted as passing that was
not run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

from prompt_shields import RouteHint
from prompt_shields.route_policy import (
    PolicyViolation,
    build_route_request,
    parse_route_headers,
    resolve_policy_route,
    resolve_route,
)

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "schemas" / "route-policy"
POLICY_SCHEMA = json.loads((SCHEMA_DIR / "route-policy.v1.schema.json").read_text())
VECTORS_SCHEMA = json.loads((SCHEMA_DIR / "vectors.schema.json").read_text())
EXAMPLES = sorted((SCHEMA_DIR / "examples").glob("*.json"))
VECTOR_FILES = sorted((SCHEMA_DIR / "vectors").glob("*.json"))

# RouteHint field -> wire header. Mirrors gateway parseRouteHeaders.
HINT_HEADERS = {
    "quality": "X-PS-Quality",
    "max_cost": "X-PS-Max-Cost",
    "model_group": "X-PS-Route",
}

# Expected fields the reference router decides today. An active vector that
# expects anything else must be marked pending, not silently ignored.
DECIDED_FIELDS = {"model", "group", "reason", "est_cost"}


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _policy_validator():
    return jsonschema.Draft202012Validator(POLICY_SCHEMA)


# Vector files whose stages only resolve_policy_route implements
# (resolve_route ignores sensitivity, data_policy and use_cases).
FULL_POLICY_FILES = ("sensitivity", "data-policy", "quality-bar")


def _all_vectors(exclude=()):
    for path in VECTOR_FILES:
        if path.stem in exclude:
            continue
        doc = _load(path)
        for vector in doc["vectors"]:
            yield pytest.param(doc["policy"], vector, id=f"{path.stem}/{vector['id']}")


# --- schemas ---------------------------------------------------------------


@pytest.mark.parametrize(
    "schema", [POLICY_SCHEMA, VECTORS_SCHEMA], ids=["policy", "vectors"]
)
def test_schema_is_valid_2020_12(schema):
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_there_are_examples_and_vectors():
    assert EXAMPLES, "no example policies found"
    assert VECTOR_FILES, "no vector files found"


# --- example policies ------------------------------------------------------


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_example_validates_against_schema(path):
    _policy_validator().validate(_load(path))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_example_cross_field_rules(path):
    """Rules the schema description states but JSON Schema cannot express."""
    policy = _load(path)
    groups = set(policy["groups"])
    assert policy["default_group"] in groups
    assert set(policy["quality_to_group"].values()) <= groups
    assert set(policy.get("group_order", [])) <= groups
    for use_case in policy.get("use_cases", {}).values():
        if "min_group" in use_case:
            assert use_case["min_group"] in policy.get("group_order", [])

    sensitivity = policy.get("sensitivity")
    if sensitivity:
        levels = set(sensitivity["levels"])
        assert sensitivity["unclassified_default"] in levels
        assert set(sensitivity.get("pii_escalation", {}).values()) <= levels
        assert set(policy.get("data_policy", {})) <= levels


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("groups"),
        lambda p: p.update(schema_version="route-policy.v2"),
        lambda p: p.update(unexpected=True),
        lambda p: p["quality_to_group"].pop("critical"),
        lambda p: p["groups"]["cheap"][0].pop("input_cost_per_1k"),
        lambda p: p["groups"]["cheap"][0].update(output_cost_per_1k=-1),
        lambda p: p["fallback"].update(no_compliant_candidate="downgrade"),
        lambda p: p["fallback"].update(content_filter_refusal="retry"),
        lambda p: p["data_policy"]["public"].update(jurisdictions=["europe"]),
        lambda p: p["sensitivity"].pop("unclassified_default"),
        lambda p: p["use_cases"]["claim-summary"].update(min_parity=1.5),
    ],
    ids=[
        "missing-groups",
        "wrong-version",
        "unknown-key",
        "missing-quality",
        "missing-price",
        "negative-price",
        "fail-open",
        "content-filter-retry",
        "bad-jurisdiction",
        "missing-default-level",
        "parity-above-1",
    ],
)
def test_schema_rejects_invalid_policy(mutate):
    policy = _load(SCHEMA_DIR / "examples" / "nordic-sovereign.json")
    mutate(policy)
    assert not _policy_validator().is_valid(policy)


# --- vector files ----------------------------------------------------------


@pytest.mark.parametrize("path", VECTOR_FILES, ids=lambda p: p.stem)
def test_vector_file_is_well_formed(path):
    doc = _load(path)
    jsonschema.Draft202012Validator(VECTORS_SCHEMA).validate(doc)
    assert (SCHEMA_DIR / "examples" / f"{doc['policy']}.json").is_file()
    ids = [v["id"] for v in doc["vectors"]]
    assert len(ids) == len(set(ids)), "duplicate vector ids"
    for vector in doc["vectors"]:
        if vector.get("status") == "pending":
            assert vector.get("pending_reason"), (
                f"{vector['id']}: pending without a reason"
            )


def test_vector_ids_are_unique_across_files():
    ids = [v["id"] for p in VECTOR_FILES for v in _load(p)["vectors"]]
    assert len(ids) == len(set(ids))


# --- SDK: hint encoding ----------------------------------------------------


@pytest.mark.parametrize("policy_name,vector", list(_all_vectors()))
def test_route_hint_encodes_vector_hints(policy_name, vector):
    """The SDK's only routing job: put the vector's hints on the wire."""
    hints = vector["request"].get("hints", {})
    headers = RouteHint(**hints).to_headers()
    assert headers == {HINT_HEADERS[k]: str(v) for k, v in hints.items()}


# --- decisions -------------------------------------------------------------


@pytest.mark.parametrize(
    "policy_name,vector", list(_all_vectors(exclude=FULL_POLICY_FILES))
)
def test_decision_vector(policy_name, vector):
    if vector.get("status") == "pending":
        pytest.skip(f"pending: {vector['pending_reason']}")

    expected = dict(vector["expected"])
    assert set(expected) <= DECIDED_FIELDS, (
        f"active vector expects undecided fields {set(expected) - DECIDED_FIELDS}"
    )

    policy = _load(SCHEMA_DIR / "examples" / f"{policy_name}.json")
    request = vector["request"]
    headers = RouteHint(**request.get("hints", {})).to_headers()
    req = build_route_request(
        {
            "model": request.get("model", "auto"),
            "messages": request["messages"],
            "max_tokens": request.get("max_tokens"),
        },
        parse_route_headers(headers),
    )
    if request.get("approx_input_tokens") is not None:
        req.approx_input_tokens = request["approx_input_tokens"]

    decision = resolve_route(req, policy)

    est_cost = expected.pop("est_cost", None)
    assert {k: getattr(decision, k) for k in expected} == expected
    if est_cost is not None:
        assert abs(decision.est_cost - est_cost) <= 1e-9 * abs(est_cost) + 1e-15


# --- decisions under the whole policy (policy replay, #13) -------------------

def _full_policy_vectors():
    for stem in FULL_POLICY_FILES:
        doc = _load(SCHEMA_DIR / "vectors" / f"{stem}.json")
        for vector in doc["vectors"]:
            yield pytest.param(doc["policy"], vector, id=f"{stem}/{vector['id']}")


@pytest.mark.parametrize("policy_name,vector", list(_full_policy_vectors()))
def test_full_policy_vector(policy_name, vector):
    policy = _load(SCHEMA_DIR / "examples" / f"{policy_name}.json")
    request = vector["request"]
    req = build_route_request(
        {
            "model": request.get("model", "auto"),
            "messages": request["messages"],
            "max_tokens": request.get("max_tokens"),
        },
        parse_route_headers(RouteHint(**request.get("hints", {})).to_headers()),
    )
    if request.get("approx_input_tokens") is not None:
        req.approx_input_tokens = request["approx_input_tokens"]
    req.data_classification = request.get("data_classification")
    req.pii_detected = list(request.get("pii_detected", []))
    req.use_case = request.get("use_case")

    expected = dict(vector["expected"])
    if expected.pop("error", None) == "PolicyViolation":
        with pytest.raises(PolicyViolation):
            resolve_policy_route(req, policy)
        return

    decision = resolve_policy_route(req, policy)
    est_cost = expected.pop("est_cost", None)
    assert {k: getattr(decision, k) for k in expected} == expected
    if est_cost is not None:
        assert abs(decision.est_cost - est_cost) <= 1e-9 * abs(est_cost) + 1e-15


def test_full_policy_router_agrees_on_active_vectors_without_constraints():
    """Without sensitivity/data_policy/use_cases, both entry points agree."""
    policy = _load(SCHEMA_DIR / "examples" / "deterministic.json")
    for path in VECTOR_FILES:
        doc = _load(path)
        if doc["policy"] != "deterministic":
            continue
        for vector in doc["vectors"]:
            if vector.get("status") == "pending":
                continue
            request = vector["request"]
            req = build_route_request(
                {
                    "model": request.get("model", "auto"),
                    "messages": request["messages"],
                    "max_tokens": request.get("max_tokens"),
                },
                parse_route_headers(RouteHint(**request.get("hints", {})).to_headers()),
            )
            if request.get("approx_input_tokens") is not None:
                req.approx_input_tokens = request["approx_input_tokens"]
            plain = resolve_route(req, policy)
            full = resolve_policy_route(req, policy)
            assert (full.model, full.group, full.reason) == (
                plain.model,
                plain.group,
                plain.reason,
            ), vector["id"]
            assert full.est_cost == pytest.approx(plain.est_cost, rel=1e-12)
