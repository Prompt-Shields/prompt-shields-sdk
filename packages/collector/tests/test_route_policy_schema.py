"""The collector validates central policy sections as the gateway does (#383).

The route section's route-policy.v1 document goes through
collector/route_policy_schema.py (a port of the gateway's validator over a
bundled copy of the shared schema) and the access section through
policy_doc's mirror of the gateway's PS_OIDC_POLICY parser. These tests keep
both from drifting: the bundled schema equals schemas/route-policy/, it uses
no keyword the port lacks, the shared examples pass, and every case in
schemas/central-policy/*.vectors.json (which the gateway's
sectionVectors.test.ts also runs) gets the same verdict here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from collector import policy_doc, route_policy_schema

ROOT = Path(__file__).resolve().parents[3]
SHARED_SCHEMA = ROOT / "schemas" / "route-policy" / "route-policy.v1.schema.json"
EXAMPLES = sorted((ROOT / "schemas" / "route-policy" / "examples").glob("*.json"))
VECTORS = ROOT / "schemas" / "central-policy"
GATEWAY_SCHEMA = ROOT / "gateway" / "src" / "middlewares" / "router" / "route-policy.v1.schema.json"


def _cases(name: str) -> list[dict]:
    return json.loads((VECTORS / name).read_text(encoding="utf-8"))["cases"]


def _doc(**sections) -> dict:
    return {"schema": "ps-policy.v1", "sections": sections}


def _accepts(doc: dict) -> bool:
    try:
        policy_doc.validate_document(doc)
        return True
    except policy_doc.PolicyDocumentError:
        return False


def test_bundled_schema_is_the_shared_one():
    shared = json.loads(SHARED_SCHEMA.read_text(encoding="utf-8"))
    assert route_policy_schema.schema() == shared
    # The gateway bundles its own copy (policyFile.test.ts keeps it equal).
    assert json.loads(GATEWAY_SCHEMA.read_text(encoding="utf-8")) == shared


def test_schema_uses_only_supported_keywords():
    used = route_policy_schema.keywords_in(route_policy_schema.schema())
    supported = route_policy_schema.ANNOTATIONS | route_policy_schema.ASSERTIONS
    assert used - supported == set()


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_shared_examples_pass(path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert route_policy_schema.route_policy_problems(doc) == []
    assert _accepts(_doc(route={"version": 1, "policy": doc}))


@pytest.mark.parametrize("case", _cases("route-v1.vectors.json"), ids=lambda c: c["name"])
def test_route_vectors_agree_with_the_gateway(case):
    assert _accepts(_doc(route={"version": 1, "policy": case["policy"]})) is case["valid"]


@pytest.mark.parametrize("case", _cases("access-v1.vectors.json"), ids=lambda c: c["name"])
def test_access_vectors_agree_with_the_gateway(case):
    assert _accepts(_doc(access={"version": 1, **case["access"]})) is case["valid"]


def test_route_problems_name_the_section_path():
    policy = {
        "schema_version": "route-policy.v1",
        "default_group": "frontier",
        "quality_to_group": {"draft": "cheap", "balanced": "cheap", "critical": "cheap"},
        "groups": {
            "cheap": [{"model": "m", "input_cost_per_1k": "x", "output_cost_per_1k": 0}]
        },
    }
    with pytest.raises(policy_doc.PolicyDocumentError) as e:
        policy_doc.validate_document(_doc(route={"version": 1, "policy": policy}))
    assert e.value.problems == [
        "$.sections.route.policy.groups.cheap[0].input_cost_per_1k: expected number, got string"
    ]
    policy["groups"]["cheap"][0]["input_cost_per_1k"] = 0
    with pytest.raises(policy_doc.PolicyDocumentError) as e:
        policy_doc.validate_document(_doc(route={"version": 1, "policy": policy}))
    assert e.value.problems == [
        '$.sections.route.policy.default_group: "frontier" is not a group'
    ]


def test_javascript_semantics():
    v = route_policy_schema.validate
    assert v({}, {"type": "integer"}, 3.0) == []
    assert v({}, {"type": "integer"}, True) == ["$: expected integer, got boolean"]
    assert v({}, {"pattern": "^a$"}, "a\n") == ["$: does not match ^a$"]
    assert v({}, {"enum": [1]}, 1.0) == []
    # One astral character is two UTF-16 code units, as JavaScript counts.
    assert v({}, {"minLength": 2}, "\U0001f600") == []
