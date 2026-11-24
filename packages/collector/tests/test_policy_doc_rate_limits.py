"""The ``rate_limits`` v1 policy section (#344): collector-side validation,
mirroring the gateway's parseRateLimitSection (psRateLimit/config.ts)."""

from __future__ import annotations

import pytest
from collector import policy_doc


def _doc(section: dict) -> dict:
    return {"schema": policy_doc.SCHEMA, "sections": {"rate_limits": section}}


def test_valid_section_is_accepted():
    doc = _doc(
        {
            "version": 1,
            "on_store_error": "closed",
            "rules": [
                {"id": "all", "scope": "global", "rpm": 600, "tpm": 200000},
                {
                    "id": "team-a.chat",
                    "scope": "team",
                    "subjects": ["team-a"],
                    "match": {"model": "gpt-4o*", "provider": ["openai"], "route": "/v1/chat/*"},
                    "tpd": 5_000_000,
                    "concurrency": 20,
                },
            ],
        }
    )
    assert policy_doc.validate_document(doc) is doc
    assert "rate_limits" in policy_doc.SECTIONS
    assert "rate_limits" not in policy_doc.RESERVED_SECTIONS


@pytest.mark.parametrize(
    ("section", "problem"),
    [
        ({"version": 2, "rules": []}, "rate_limits.version must be one of [1]"),
        ({"version": 1}, "rules must be an array"),
        ({"version": 1, "rules": [], "x": 1}, "unknown keys ['x']"),
        ({"version": 1, "rules": [], "on_store_error": "maybe"}, "on_store_error"),
        ({"version": 1, "rules": [{"id": "a", "scope": "global"}]}, "needs at least one"),
        ({"version": 1, "rules": [{"id": "a", "scope": "org", "rpm": 1}]}, "scope must be"),
        ({"version": 1, "rules": [{"id": "a b", "scope": "global", "rpm": 1}]}, "id must be"),
        (
            {
                "version": 1,
                "rules": [
                    {"id": "a", "scope": "global", "rpm": 1},
                    {"id": "a", "scope": "global", "rpm": 2},
                ],
            },
            "used twice",
        ),
        ({"version": 1, "rules": [{"id": "a", "scope": "global", "rpm": 0}]}, "positive integer"),
        ({"version": 1, "rules": [{"id": "a", "scope": "global", "rpm": True}]}, "positive integer"),
        ({"version": 1, "rules": [{"id": "a", "scope": "global", "rpm": 1.5}]}, "positive integer"),
        (
            {"version": 1, "rules": [{"id": "a", "scope": "global", "subjects": ["x"], "rpm": 1}]},
            "subjects is not allowed",
        ),
        (
            {"version": 1, "rules": [{"id": "a", "scope": "user", "match": {"tool": "x"}, "rpm": 1}]},
            "match: unknown keys",
        ),
        (
            {"version": 1, "rules": [{"id": "a", "scope": "user", "match": {"model": []}, "rpm": 1}]},
            "non-empty",
        ),
        (
            {"version": 1, "rules": [{"id": f"r{i}", "scope": "global", "rpm": 1} for i in range(201)]},
            "at most 200 rules",
        ),
    ],
)
def test_invalid_sections_are_refused(section, problem):
    with pytest.raises(policy_doc.PolicyDocumentError) as err:
        policy_doc.validate_document(_doc(section))
    assert any(problem in p for p in err.value.problems), err.value.problems
