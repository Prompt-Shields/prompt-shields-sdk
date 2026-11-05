"""Frozen baseline + counterfactual cost (#24, PRD 0001 §7.10).

The vectors in ``schemas/baseline/vectors/`` are shared with the gateway
(``gateway/src/middlewares/router/baseline.test.ts``).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from prompt_shields import RouteHint, RouterError
from prompt_shields import catalog as catalog_mod
from prompt_shields.baseline import (
    Baseline,
    BaselineError,
    freeze_baseline,
    load_baseline,
)

from tests.test_router import HI, _router, _wire

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "schemas" / "baseline"
EXAMPLE = SCHEMA_DIR / "examples" / "onboarding.json"
VECTOR_FILES = sorted((SCHEMA_DIR / "vectors").glob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _vectors():
    for path in VECTOR_FILES:
        doc = _load(path)
        baseline = _load((path.parent / doc["baseline"]).resolve())
        for vector in doc["vectors"]:
            yield pytest.param(baseline, vector, id=f"{path.stem}/{vector['id']}")


def _invalid():
    for path in VECTOR_FILES:
        doc = _load(path)
        baseline = _load((path.parent / doc["baseline"]).resolve())
        for case in doc.get("invalid_baselines", []):
            yield pytest.param(
                {**baseline, **case["patch"]}, id=f"{path.stem}/{case['id']}"
            )


@pytest.mark.parametrize("doc,vector", list(_vectors()))
def test_counterfactual_vector(doc, vector):
    baseline = Baseline.from_dict(doc)
    event = dict(vector["event"])
    baseline.apply(event)
    expected = vector["expected"]
    assert event["baseline_id"] == doc["baseline_id"]
    assert baseline.model_for(vector["event"].get("use_case_name")) == (
        expected["baseline_model"]
    )
    if expected["counterfactual_cost"] is None:
        assert "counterfactual_cost" not in event
    else:
        assert abs(event["counterfactual_cost"] - expected["counterfactual_cost"]) <= 1e-12


@pytest.mark.parametrize("doc", list(_invalid()))
def test_invalid_baseline_rejected(doc):
    with pytest.raises(BaselineError):
        Baseline.from_dict(doc)


def test_schemas_validate_example_and_vectors():
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(_load(EXAMPLE), _load(SCHEMA_DIR / "baseline.v1.schema.json"))
    vectors_schema = _load(SCHEMA_DIR / "vectors.schema.json")
    for path in VECTOR_FILES:
        jsonschema.validate(_load(path), vectors_schema)


def test_round_trip_and_file_load(tmp_path):
    baseline = load_baseline(EXAMPLE)
    assert Baseline.from_dict(baseline.to_dict()) == baseline
    assert load_baseline(baseline) is baseline
    assert load_baseline(baseline.to_dict()) == baseline
    with pytest.raises(BaselineError):
        load_baseline(tmp_path / "missing.json")


# --- freezing ---------------------------------------------------------------------


def test_freeze_pins_catalog_version_and_copies_rates():
    cat = catalog_mod.load_catalog()
    baseline = freeze_baseline(
        "acme-2026-11",
        "openai/gpt-4o",
        {"contract-review": "anthropic/claude-sonnet-4-20250514"},
        frozen_at="2026-11-05",
    )
    assert baseline.catalog_version == cat["catalog_version"]
    assert baseline.frozen_at == "2026-11-05"
    gpt = cat["models"]["openai/gpt-4o"]
    rates = baseline.models["openai/gpt-4o"]
    assert rates["input_cost_per_token"] == gpt["input_cost_per_token"]
    assert rates["cache_read_input_token_cost"] == gpt["cache_read_input_token_cost"]
    # No cache-write price in the catalog: the vendor multiplier (1.0 for OpenAI).
    assert rates["cache_creation_input_token_cost"] == gpt["input_cost_per_token"]
    sonnet = baseline.models["anthropic/claude-sonnet-4-20250514"]
    rate_in = cat["models"]["anthropic/claude-sonnet-4-20250514"]["input_cost_per_token"]
    assert sonnet["cache_read_input_token_cost"] == pytest.approx(rate_in * 0.1)
    assert sonnet["cache_creation_input_token_cost"] == pytest.approx(rate_in * 1.25)


def test_later_catalog_change_does_not_move_counterfactual():
    cat = copy.deepcopy(catalog_mod.load_catalog())
    baseline = freeze_baseline("acme", "openai/gpt-4o", catalog=cat, frozen_at="2026-11-05")
    event = {"tokens_in": 1000, "tokens_out": 500}
    before = baseline.counterfactual_cost(event)
    # A later catalog release halves the price; the frozen baseline keeps its rates.
    cat["models"]["openai/gpt-4o"]["input_cost_per_token"] /= 2
    cat["catalog_version"] = "2099-01-01"
    assert baseline.counterfactual_cost(event) == before
    reloaded = load_baseline(json.loads(json.dumps(baseline.to_dict())))
    assert reloaded.counterfactual_cost(event) == before


@pytest.mark.parametrize("model", ["openai/no-such-model", "gpt-4o", ""])
def test_freeze_rejects_unknown_models(model):
    with pytest.raises(BaselineError):
        freeze_baseline("acme", model)


def test_freeze_rejects_non_usd_models():
    eur = next(
        (k for k, e in catalog_mod.load_catalog()["models"].items()
         if e.get("currency") == "EUR"),
        None,
    )
    if eur is None:
        pytest.skip("no EUR model in the catalog")
    with pytest.raises(BaselineError):
        freeze_baseline("acme", eur)


# --- router -------------------------------------------------------------------------


def test_router_emits_baseline_and_counterfactual():
    router = _router(baseline=EXAMPLE)
    events, _ = _wire(router)
    resp = router.chat.completions.create(
        messages=HI, route=RouteHint(quality="draft"), max_tokens=100,
        data_classification="confidential",
    )
    (event,) = events
    # The fake upstream reports 10 prompt and 5 completion tokens, priced at
    # the frozen gpt-4o default ($5/M in, $15/M out).
    expected = 10 * 5e-6 + 5 * 1.5e-5
    assert event["baseline_id"] == "acme-onboarding-2026-09"
    assert event["counterfactual_cost"] == pytest.approx(expected, rel=1e-12)
    assert resp.ps.cost.baseline_id == "acme-onboarding-2026-09"
    assert resp.ps.cost.counterfactual_usd == pytest.approx(expected, rel=1e-12)


def test_router_uses_use_case_baseline_model():
    router = _router(baseline=_load(EXAMPLE), use_case="contract-review")
    events, _ = _wire(router)
    router.chat.completions.create(messages=HI, route=RouteHint(quality="draft"))
    (event,) = events
    assert event["counterfactual_cost"] == pytest.approx(10 * 3e-6 + 5 * 1.5e-5)


def test_router_without_baseline_sends_no_savings_fields():
    router = _router()
    events, _ = _wire(router)
    resp = router.chat.completions.create(messages=HI, route=RouteHint(quality="draft"))
    assert "baseline_id" not in events[0]
    assert "counterfactual_cost" not in events[0]
    assert resp.ps.cost.counterfactual_usd is None


def test_router_rejects_invalid_baseline():
    with pytest.raises(RouterError, match="invalid baseline"):
        _router(baseline={"schema_version": "baseline.v1"})


def test_gateway_mode_rejects_baseline():
    with pytest.raises(RouterError, match="PS_BASELINE_FILE"):
        _router(mode="gateway", gateway_url="http://gw", baseline=EXAMPLE)
