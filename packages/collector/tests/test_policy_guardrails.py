"""The ps-policy.v1 ``guardrails`` v1 section (#349): validation only."""

from __future__ import annotations

import pytest
from collector import policy_doc
from collector.policy_doc import PolicyDocumentError, validate_document


def doc(section: dict) -> dict:
    return {"schema": "ps-policy.v1", "sections": {"guardrails": section}}


def problems(section: dict) -> list[str]:
    with pytest.raises(PolicyDocumentError) as err:
        validate_document(doc(section))
    return err.value.problems


def rule(**kw) -> dict:
    return {"id": "a", "check": "pii", "action": "log", **kw}


def test_valid_section_is_accepted():
    section = {
        "version": 1,
        "stream_window_chars": 256,
        "rules": [
            {"id": "redact-email", "check": "pii", "categories": ["email"], "action": "redact"},
            {
                "id": "cards-out",
                "check": "pii",
                "stages": ["output"],
                "action": "redact",
                "categories": ["credit_card"],
                "models": ["gpt-4o*"],
            },
            {"id": "no-keys", "check": "secrets", "action": "deny"},
            {"id": "inj", "check": "injection", "stages": ["input"], "action": "log"},
            {"id": "health", "check": "pii", "categories": ["health_data"], "action": "deny"},
        ],
    }
    assert validate_document(doc(section)) == doc(section)
    assert validate_document(doc({"version": 1, "rules": []}))


def test_guardrails_is_no_longer_reserved():
    assert "guardrails" in policy_doc.SECTIONS
    assert "guardrails" not in policy_doc.RESERVED_SECTIONS


@pytest.mark.parametrize(
    "section, expected",
    [
        ({"version": 2, "rules": []}, "guardrails.version must be one of [1]"),
        ({"version": 1}, "rules must be an array"),
        ({"version": 1, "rules": [], "extra": 1}, "unknown keys ['extra']"),
        ({"version": 1, "rules": [], "stream_window_chars": 10}, "stream_window_chars must be"),
        ({"version": 1, "rules": [], "stream_window_chars": True}, "stream_window_chars must be"),
        ({"version": 1, "rules": ["x"]}, "rules[0] must be an object"),
        ({"version": 1, "rules": [rule(id="A")]}, "id must be"),
        ({"version": 1, "rules": [rule(), rule()]}, 'id "a" is used twice'),
        ({"version": 1, "rules": [rule(check="virus")]}, "check must be"),
        ({"version": 1, "rules": [rule(action="block")]}, "action must be"),
        (
            {"version": 1, "rules": [rule(check="injection", action="redact")]},
            "action must be one of ['deny', 'log'] for injection",
        ),
        ({"version": 1, "rules": [rule(stages=[])]}, "stages must be"),
        ({"version": 1, "rules": [rule(stages=["input", "input"])]}, "stages must be"),
        ({"version": 1, "rules": [rule(categories=["dna"])]}, "categories must be"),
        (
            {"version": 1, "rules": [rule(check="injection", categories=["x"])]},
            "injection has no categories",
        ),
        (
            {"version": 1, "rules": [rule(action="redact", categories=["email", "health_data"])]},
            "cannot be redacted",
        ),
        ({"version": 1, "rules": [rule(models=["bad\nmodel"])]}, "models must be"),
        ({"version": 1, "rules": [rule(x=1)]}, "unknown keys ['x']"),
        (
            {"version": 1, "rules": [rule(id=f"r{i}") for i in range(65)]},
            "at most 64 rules",
        ),
    ],
)
def test_invalid_sections_are_refused(section, expected):
    found = problems(section)
    assert any(expected in p for p in found), found


def test_secret_categories_are_checked():
    ok = {"version": 1, "rules": [rule(check="secrets", action="redact",
                                       categories=["openai_key", "jwt"])]}
    assert validate_document(doc(ok))
    bad = {"version": 1, "rules": [rule(check="secrets", action="redact", categories=["email"])]}
    assert any("categories must be" in p for p in problems(bad))
