"""The SDK's list price for each shared negotiated-price case (#348).

schemas/pricing/negotiated-vectors.v1.json is shared with the collector
(packages/collector/tests/test_prices.py) and the gateway
(gateway/src/middlewares/policyPull/prices.test.ts): the collector and the
gateway must turn the same list price into the same negotiated cost. This
checks the SDK's side: the list price it reports for each call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from prompt_shields import pricing

FIXTURE = Path(__file__).resolve().parents[3] / "schemas/pricing/negotiated-vectors.v1.json"
CASES = json.loads(FIXTURE.read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_sdk_list_price_matches_the_shared_fixture(case):
    candidates = [m for m in (case["served_model"], case["model"]) if m]
    model = pricing.resolve_pricing_model(case["vendor"], candidates)
    assert pricing.usage_cost(case["vendor"], model, case["usage"]) == case["list_cost"]
