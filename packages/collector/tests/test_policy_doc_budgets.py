"""The central policy's ``budgets`` section v1 (#29): validation only, no
database. The budgets are budget.v1 budgets (schemas/budget/), checked as
the SDK and the gateway check them; the shared decision vectors' document
must pass as a section.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from collector import policy_doc

VECTORS = Path(__file__).resolve().parents[3] / "schemas" / "budget" / "vectors"


def _doc(section: dict) -> dict:
    return {"schema": policy_doc.SCHEMA, "sections": {"budgets": section}}


def _budget(**kw) -> dict:
    out = {"id": "b", "scope": "team", "subject": "data", "window": "month",
           "limit_usd": 100, "mode": "warn"}
    out.update(kw)
    return {k: v for k, v in out.items() if v is not None}


def test_budgets_is_a_section_not_reserved():
    assert "budgets" in policy_doc.SECTIONS
    assert "budgets" not in policy_doc.RESERVED_SECTIONS


def test_valid_section_with_every_scope_and_token_budgets():
    budgets = [
        _budget(id="tenant-monthly", scope="global", subject=None, limit_usd=5000),
        _budget(id="team", scope="team"),
        _budget(id="proj-tokens", scope="project", subject="*", limit_usd=None,
                limit_tokens=10_000_000, mode="block"),
        _budget(id="user", scope="user", subject="*", window="day", limit_usd=5,
                mode="block", warn_at=0.5),
        _budget(id="svc", scope="service", subject="billing", mode="downgrade",
                downgrade_model="gpt-4o-mini"),
        _budget(id="model", scope="model", subject="gpt-4o", window="week"),
    ]
    doc = _doc({"version": 1, "on_store_error": "closed", "budgets": budgets})
    assert policy_doc.validate_document(doc) is doc
    assert policy_doc.section_summary(doc) == {"budgets": 1}


def test_decision_vector_budgets_are_a_valid_section():
    budgets = json.loads((VECTORS / "decisions.json").read_text())["budgets"]["budgets"]
    policy_doc.validate_document(_doc({"version": 1, "budgets": budgets}))


@pytest.mark.parametrize(
    "section,problem",
    [
        ({"version": 2, "budgets": []}, "version must be one of [1]"),
        ({"version": 1}, "budgets must be an array"),
        ({"version": 1, "budgets": [], "namespace": "x"}, "unknown keys ['namespace']"),
        ({"version": 1, "budgets": [], "on_store_error": "maybe"}, "on_store_error"),
        ({"version": 1, "budgets": [_budget(), _budget()]}, 'id "b" is used twice'),
        ({"version": 1, "budgets": [_budget(id="Bad")]}, ".id must match"),
        ({"version": 1, "budgets": [_budget(scope="tenant")]}, ".scope must be one of"),
        ({"version": 1, "budgets": [_budget(scope="global")]}, "global budget has no subject"),
        ({"version": 1, "budgets": [_budget(subject=None)]}, "subject is required"),
        ({"version": 1, "budgets": [_budget(window="year")]}, ".window must be one of"),
        ({"version": 1, "budgets": [_budget(limit_tokens=5)]}, "exactly one of"),
        ({"version": 1, "budgets": [_budget(limit_usd=None)]}, "exactly one of"),
        ({"version": 1, "budgets": [_budget(limit_usd=-1)]}, "limit_usd must be"),
        ({"version": 1, "budgets": [_budget(limit_usd=True)]}, "limit_usd must be"),
        ({"version": 1, "budgets": [_budget(limit_usd=float("nan"))]}, "limit_usd must be"),
        ({"version": 1, "budgets": [_budget(limit_usd=None, limit_tokens=1.5)]},
         "limit_tokens must be"),
        ({"version": 1, "budgets": [_budget(limit_usd=None, limit_tokens=5,
                                            mode="downgrade")]}, "token budget"),
        ({"version": 1, "budgets": [_budget(mode="throttle")]}, ".mode must be one of"),
        ({"version": 1, "budgets": [_budget(warn_at=0)]}, "warn_at"),
        ({"version": 1, "budgets": [_budget(downgrade_model="m")]}, "needs mode"),
        ({"version": 1, "budgets": [_budget(extra=1)]}, "unknown keys ['extra']"),
        ({"version": 1, "budgets": ["x"]}, "must be an object"),
    ],
)
def test_invalid_sections_are_refused(section, problem):
    with pytest.raises(policy_doc.PolicyDocumentError) as err:
        policy_doc.validate_document(_doc(section))
    assert any(problem in p for p in err.value.problems), err.value.problems


def test_every_problem_is_listed():
    with pytest.raises(policy_doc.PolicyDocumentError) as err:
        policy_doc.validate_document(_doc({"version": 1, "budgets": [
            _budget(id="a", window="year"), _budget(id="b", mode="nope")]}))
    assert len(err.value.problems) == 2


def test_sections_are_inherited_whole():
    tenant = policy_doc.ActivePolicy("default", 1, {"budgets": {"version": 1, "budgets": [
        _budget(id="t")]}})
    team = policy_doc.ActivePolicy("eu", 2, {"budgets": {"version": 1, "budgets": []}})
    eff = policy_doc.compile_effective([tenant, team])
    assert eff["sections"]["budgets"]["budgets"] == []
    assert eff["version"] == "default@1+eu@2"
