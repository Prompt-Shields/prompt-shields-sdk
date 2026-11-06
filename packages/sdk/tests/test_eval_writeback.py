"""ps-eval parity → route policy quality bar (#23): only models that passed
parity at the use case's ``min_parity`` stay eligible, and the router honours it."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from prompt_shields.evals.writeback import apply_parity, main, policy_sha256
from prompt_shields.route_policy import RouteRequest, resolve_policy_route

ROOT = Path(__file__).resolve().parents[3]
SCHEMA_DIR = ROOT / "schemas" / "route-policy"
POLICY = json.loads((SCHEMA_DIR / "examples" / "nordic-sovereign.json").read_text())
FIXTURE = Path(__file__).parent / "fixtures" / "teardown" / "eval_results.json"


def _cell(candidate, passed, n, ci_low):
    return {"use_case": "claim-summary", "candidate": candidate, "n": n,
            "passed": passed, "parity": round(passed / n, 4), "ci_low": ci_low}


def _report(policy=POLICY, notes=()):
    return {
        "cells": [
            _cell("scaleway:scaleway-small", 36, 40, 0.77),         # below 0.95
            _cell("mistral-eu:mistral-small-eu", 39, 40, 0.87),     # 0.975
            _cell("openai:gpt-4o-mini", 10, 10, 0.72),              # too few samples
        ],
        "use_cases": [{"use_case": "claim-summary",
                       "from_model": "mistral-eu:mistral-large-eu"}],
        "params": {"min_parity": 0.95, "min_cell_samples": 30},
        "methodology": {"policy": {"route_policy": {"sha256": policy_sha256(policy)}}},
        "notes": list(notes),
    }


def _route(policy, use_case="claim-summary"):
    return resolve_policy_route(
        RouteRequest(messages=[{"role": "user", "content": "hi"}], approx_input_tokens=1, max_output_tokens=100,
                     quality="draft", data_classification="confidential",
                     use_case=use_case),
        policy)


def test_only_models_at_parity_become_eligible_and_the_router_uses_them():
    before = copy.deepcopy(POLICY)
    new, summary = apply_parity(POLICY, _report(), eval_set="claims-v4")
    assert POLICY == before  # input untouched
    bar = new["use_cases"]["claim-summary"]
    assert bar == {"eval_set": "claims-v4", "min_parity": 0.95,
                   "eligible_models": ["mistral-large-eu", "mistral-small-eu"]}
    assert new["use_cases"]["legal-review"] == POLICY["use_cases"]["legal-review"]
    row = summary["use_cases"][0]
    assert row["passed"] == ["mistral-small-eu"]
    assert {e["model"]: e["why"].split(" ")[0] for e in row["excluded"]} == {
        "gpt-4o-mini": "n=10", "scaleway-small": "parity=0.9"}
    assert summary["warnings"] == []
    # scaleway-small is compliant and cheaper but failed parity.
    assert _route(new).model == "mistral-small-eu"


def test_per_use_case_min_parity_is_the_threshold():
    strict = copy.deepcopy(POLICY)
    strict["use_cases"]["claim-summary"]["min_parity"] = 0.98
    new, _ = apply_parity(strict, _report(strict), eval_set="claims-v4")
    assert new["use_cases"]["claim-summary"]["eligible_models"] == ["mistral-large-eu"]
    d = _route(new)
    assert (d.model, d.group) == ("mistral-large-eu", "frontier")
    assert "+use-case-eligible" in d.reason


def test_conservative_uses_the_wilson_lower_bound():
    new, _ = apply_parity(POLICY, _report(), eval_set="x", conservative=True,
                          keep_models=["berget:berget-llama-70b"])
    assert new["use_cases"]["claim-summary"]["eligible_models"] == [
        "berget-llama-70b", "mistral-large-eu"]


def test_new_use_case_gets_an_explicit_bar_and_output_matches_the_schema():
    jsonschema = pytest.importorskip("jsonschema")
    report = _report()
    for c in report["cells"]:
        c["use_case"] = "triage"
    report["use_cases"][0]["use_case"] = "triage"
    new, _ = apply_parity(POLICY, report, eval_set="triage-v1")
    assert new["use_cases"]["triage"]["min_parity"] == 0.95
    jsonschema.validate(new, json.loads(
        (SCHEMA_DIR / "route-policy.v1.schema.json").read_text()))


def test_mock_reports_and_empty_eval_sets_are_refused():
    with pytest.raises(ValueError, match="mock run"):
        apply_parity(POLICY, _report(notes=["MOCK RUN: echoed"]), eval_set="x")
    with pytest.raises(ValueError, match="eval_set"):
        apply_parity(POLICY, _report(), eval_set="")


def test_warnings_for_policy_drift_unknown_models_and_missing_use_cases():
    report = json.loads(FIXTURE.read_text())  # a real ps-eval --out-json shape
    new, summary = apply_parity(POLICY, report, eval_set="t",
                                use_cases=["faq", "absent"])
    assert set(new["use_cases"]) == {"legal-review", "claim-summary", "faq"}
    assert new["use_cases"]["faq"]["eligible_models"] == ["gpt-4o", "gpt-4o-mini", "mistral-small-eu"]
    # The fixture ran under this exact policy, so no drift warning.
    assert summary["warnings"] == ["use case 'absent' has no cells in the report"]
    edited = {**POLICY, "policy_version": "nordic-sovereign-2"}
    _, summary = apply_parity(edited, report, eval_set="t", use_cases=["faq"])
    assert summary["warnings"] == [
        "the eval ran under a different version of this route policy"]
    del report["methodology"]["policy"]["route_policy"]
    report["cells"].append({**report["cells"][2], "candidate": "x:not-a-tier"})
    _, summary = apply_parity(POLICY, report, eval_set="t", use_cases=["faq"])
    assert "did not record a route policy" in summary["warnings"][0]
    assert "not in any policy group: not-a-tier" in summary["warnings"][1]


def test_cli_writes_the_policy(tmp_path, capsys):
    (tmp_path / "r.json").write_text(json.dumps(_report()))
    (tmp_path / "p.json").write_text(json.dumps(POLICY))
    out = tmp_path / "out.json"
    assert main(["--report", str(tmp_path / "r.json"), "--route-policy",
                 str(tmp_path / "p.json"), "--eval-set", "claims-v4",
                 "--out", str(out)]) == 0
    written = json.loads(out.read_text())
    assert written["use_cases"]["claim-summary"]["eval_set"] == "claims-v4"
    assert "excluded gpt-4o-mini, scaleway-small" in capsys.readouterr().err
    (tmp_path / "r.json").write_text(json.dumps(_report(notes=["MOCK RUN: x"])))
    assert main(["--report", str(tmp_path / "r.json"), "--route-policy",
                 str(tmp_path / "p.json"), "--eval-set", "x"]) == 2


SCHEMA = json.loads((SCHEMA_DIR / "route-policy.v1.schema.json").read_text())


def test_output_is_schema_valid_and_keeps_unrelated_keys_in_order():
    jsonschema = pytest.importorskip("jsonschema")
    new, _ = apply_parity(POLICY, _report(), eval_set="claims-v4",
                          keep_models=["", "berget:berget-llama-70b"])
    jsonschema.validate(new, SCHEMA)
    assert list(new) == list(POLICY)
    assert list(new["use_cases"]) == list(POLICY["use_cases"])
    for key in POLICY:
        if key != "use_cases":
            assert new[key] == POLICY[key]


def test_conservative_recomputes_the_bound_instead_of_trusting_rounding():
    # Wilson low of 169/172 is 0.94998..., which the report rounds to 0.95.
    report = _report()
    report["cells"] = [_cell("mistral-eu:mistral-small-eu", 169, 172, 0.95)]
    new, _ = apply_parity(POLICY, report, eval_set="x")
    assert "mistral-small-eu" in new["use_cases"]["claim-summary"]["eligible_models"]
    new, _ = apply_parity(POLICY, report, eval_set="x", conservative=True)
    assert new["use_cases"]["claim-summary"]["eligible_models"] == ["mistral-large-eu"]


def test_no_eligible_model_is_written_as_empty_list_and_fails_closed():
    jsonschema = pytest.importorskip("jsonschema")
    from prompt_shields.route_policy import PolicyViolation

    report = _report()
    report["use_cases"] = []  # no logged reference model
    report["cells"] = [_cell("scaleway:scaleway-small", 20, 40, 0.35)]
    new, summary = apply_parity(POLICY, report, eval_set="x")
    assert new["use_cases"]["claim-summary"]["eligible_models"] == []
    jsonschema.validate(new, SCHEMA)
    assert any("no model is eligible" in w for w in summary["warnings"])
    with pytest.raises(PolicyViolation):
        _route(new)


def test_cli_can_rewrite_the_policy_in_place(tmp_path):
    (tmp_path / "r.json").write_text(json.dumps(_report()))
    policy = tmp_path / "p.json"
    policy.write_text(json.dumps(POLICY))
    assert main(["--report", str(tmp_path / "r.json"), "--route-policy",
                 str(policy), "--eval-set", "claims-v4", "--out", str(policy)]) == 0
    assert json.loads(policy.read_text())["use_cases"]["claim-summary"][
        "eval_set"] == "claims-v4"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["p.json", "r.json"]
