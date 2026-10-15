"""Golden cost vectors shared with the gateway (#71).

`schemas/pricing/cost-vectors.v1.json` holds normalised usage and the cost
computed by hand from vendor list prices, one or more vectors per pricing
dimension. Only data is shared; the gateway will get its own runner once it
prices calls.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from prompt_shields.pricing import add_usage_detail_fields, usage_cost

PRICING_DIR = Path(__file__).resolve().parents[3] / "schemas" / "pricing"
DOC = json.loads((PRICING_DIR / "cost-vectors.v1.json").read_text())
VECTORS = DOC["vectors"]


def test_vectors_match_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((PRICING_DIR / "cost-vectors.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(DOC)


def test_every_dimension_has_a_vector():
    assert {v["dimension"] for v in VECTORS} == {
        "base", "cache_read", "cache_write", "cache_write_1h", "service_tier",
        "long_context", "audio_tokens", "tool_fee", "provider_reported",
    }


@pytest.mark.parametrize("vector", VECTORS, ids=[v["id"] for v in VECTORS])
def test_cost_vector(vector):
    cost = usage_cost(vector["vendor"], vector["model"], vector["usage"])
    expected = vector["expected_cost"]
    if expected is None:
        assert cost is None
    else:
        assert cost == pytest.approx(expected, abs=1e-9)

    if "expected_cost_source" in vector:
        event = {"cost": cost}
        add_usage_detail_fields(event, vector["usage"])
        assert event["cost_source"] == vector["expected_cost_source"]
