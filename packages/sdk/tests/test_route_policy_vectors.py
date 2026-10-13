"""Conformance tests for the shared route-policy.v1 contract.

The schema, the example policies and the golden decision vectors live in
``schemas/route-policy/`` and are shared with the gateway router (see
``gateway/src/middlewares/router/vectors.test.ts``). Only data is shared;
each implementation has its own runner.

The SDK does not make routing decisions today: it only encodes intent as
``X-PS-*`` headers (``RouteHint``). So this runner checks the schema, the
examples, the vector files and the hint encoding, and reports every decision
vector as skipped with a visible reason until the Python reference router
lands (backlog items 8 and 17). Nothing is counted as passing that was not run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

from prompt_shields import RouteHint

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

NO_PYTHON_ROUTER = (
    "No Python decision function yet: the SDK only emits route hints. "
    "The Python reference router is backlog items 8 and 17."
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _policy_validator():
    return jsonschema.Draft202012Validator(POLICY_SCHEMA)


def _all_vectors():
    for path in VECTOR_FILES:
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


@pytest.mark.parametrize("policy_name,vector", list(_all_vectors()))
def test_decision_vector(policy_name, vector):
    if vector.get("status") == "pending":
        pytest.skip(f"pending: {vector['pending_reason']}")
    pytest.skip(NO_PYTHON_ROUTER)
