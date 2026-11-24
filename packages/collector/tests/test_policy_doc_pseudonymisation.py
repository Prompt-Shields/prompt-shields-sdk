"""The ``pseudonymisation`` v1 section of ps-policy.v1 (#350).

The gateway parses the same section (gateway/src/middlewares/psPseudonymise/
config.ts); both refuse the same documents.
"""

import pytest
from collector import policy_doc


def _doc(section):
    return {"schema": "ps-policy.v1", "sections": {"pseudonymisation": section}}


@pytest.mark.parametrize(
    "section",
    [
        {"version": 1, "enabled": False},
        {"version": 1, "enabled": True},
        {
            "version": 1,
            "enabled": True,
            "categories": ["email", "national_id"],
            "on_unsupported": "passthrough",
        },
    ],
)
def test_valid_sections(section):
    assert policy_doc.validate_document(_doc(section)) == _doc(section)


@pytest.mark.parametrize(
    ("section", "problem"),
    [
        ({"version": 1}, "enabled must be true or false"),
        ({"version": 1, "enabled": "yes"}, "enabled must be true or false"),
        ({"version": 1, "enabled": True, "categories": []}, "non-empty array"),
        ({"version": 1, "enabled": True, "categories": ["person_name"]}, "unknown ['person_name']"),
        ({"version": 1, "enabled": True, "on_unsupported": "allow"}, "on_unsupported"),
        ({"version": 1, "enabled": True, "key": "x"}, "unknown keys ['key']"),
        ({"version": 2, "enabled": True}, "version must be one of [1]"),
    ],
)
def test_invalid_sections(section, problem):
    with pytest.raises(policy_doc.PolicyDocumentError) as err:
        policy_doc.validate_document(_doc(section))
    assert any(problem in p for p in err.value.problems), err.value.problems


def test_no_longer_reserved_and_categories_match_the_sdk():
    # prompt_shields.pii.SPAN_CATEGORIES (the SDK is not a collector dependency).
    sdk_span_categories = (
        "national_id", "email", "iban", "ssn", "credit_card", "ip_address", "phone",
    )
    assert "pseudonymisation" not in policy_doc.RESERVED_SECTIONS
    assert "pseudonymisation" in policy_doc.SECTIONS
    assert policy_doc.PSEUDONYMISATION_CATEGORIES == sdk_span_categories


def _active(sections, name="p", version=1):
    return policy_doc.ActivePolicy(name, version, sections)


def test_enabled_section_is_required_in_the_bundle():
    on = {"pseudonymisation": {"version": 1, "enabled": True}}
    off = {"pseudonymisation": {"version": 1, "enabled": False}}
    team_on = _active(on, "team")
    bundle = policy_doc.build_bundle(
        _active(on), {"t1": ("eng", team_on)}, {"p1": ("app", "t1", None)}
    )
    assert bundle["tenant"]["required"] == ["pseudonymisation"]
    assert bundle["teams"]["t1"]["required"] == ["pseudonymisation"]
    assert bundle["projects"]["p1"]["required"] == ["pseudonymisation"]
    # Off (or absent): nothing is required, and the bundle is as before.
    assert "required" not in policy_doc.compile_effective([_active(off)])
    assert "required" not in policy_doc.compile_effective([_active({})])
    # A more specific policy turning it off is not required either.
    assert "required" not in policy_doc.compile_effective([_active(on), _active(off)])


def test_tenant_only_in_team_and_project_policies():
    doc = _doc({"version": 1, "enabled": True})
    assert policy_doc.tenant_only_problems(doc, "tenant") == []
    for scope in ("team", "project"):
        [problem] = policy_doc.tenant_only_problems(doc, scope)
        assert "$.sections.pseudonymisation" in problem and "tenant" in problem
    assert policy_doc.tenant_only_problems({"sections": {}}, "team") == []
    assert policy_doc.tenant_only_problems(None, "team") == []
