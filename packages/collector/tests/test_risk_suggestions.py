"""Risk classification rules engine (#97): golden EU AI Act Annex III cases,
the data-protection ruleset, evidence, confidence and ruleset validation.
No database."""

from __future__ import annotations

import json
import uuid

import pytest
from collector.risk_suggestions import (
    RULES_DIR,
    AssetSignals,
    collect_signals,
    evaluate,
    load_rulesets,
    parse_ruleset,
)
from db.models import AIAsset, DataFlow


def _annex(signals: AssetSignals) -> list[str]:
    return [s.rule.id for s in evaluate(signals) if s.rule.framework == "EU_AI_ACT"]


def _by_id(signals: AssetSignals) -> dict:
    return {s.rule.id: s for s in evaluate(signals)}


# (case, signals, expected EU AI Act rule ids) — one or more per Annex III point.
GOLDEN_ANNEX_III = [
    (
        "1a remote biometric identification",
        AssetSignals(use_case="store-facial-recognition", pii_types=["face_image"]),
        ["eu_ai_act.annex_iii.1a.remote_biometric_identification"],
    ),
    (
        "1b biometric categorisation",
        AssetSignals(use_case="Biometric categorisation of visitors"),
        ["eu_ai_act.annex_iii.1b.biometric_categorisation"],
    ),
    (
        "1c emotion recognition",
        AssetSignals(calling_service="call-center-emotion-detection"),
        ["eu_ai_act.annex_iii.1c.emotion_recognition"],
    ),
    (
        "2 critical infrastructure",
        AssetSignals(
            use_case="power grid load balancing", business_unit="Grid Operations"
        ),
        ["eu_ai_act.annex_iii.2.critical_infrastructure"],
    ),
    (
        "3a education admission",
        AssetSignals(
            use_case="university-admissions-review", business_unit="Admissions"
        ),
        ["eu_ai_act.annex_iii.3a.education_access"],
    ),
    (
        "3b learning outcomes",
        AssetSignals(use_case="essay scoring assistant", business_unit="School of Law"),
        ["eu_ai_act.annex_iii.3b.learning_outcomes"],
    ),
    (
        "3c education level",
        AssetSignals(use_case="language course placement"),
        ["eu_ai_act.annex_iii.3c.education_level"],
    ),
    (
        "3d proctoring",
        AssetSignals(use_case="online exam proctoring"),
        ["eu_ai_act.annex_iii.3d.test_proctoring"],
    ),
    (
        "4a recruitment",
        AssetSignals(
            use_case="resume-screening",
            business_unit="HR",
            calling_service="hr-screener",
            data_sources=["ats_api", "candidates_db"],
            pii_types=["email", "national_id"],
        ),
        ["eu_ai_act.annex_iii.4a.recruitment"],
    ),
    (
        "4b workforce management",
        AssetSignals(use_case="EmployeePerformanceReview summariser"),
        ["eu_ai_act.annex_iii.4b.workforce_management"],
    ),
    (
        "5a public benefits",
        AssetSignals(use_case="housing benefit eligibility assessment"),
        ["eu_ai_act.annex_iii.5a.public_benefits"],
    ),
    (
        "5b credit scoring",
        AssetSignals(use_case="consumer credit scoring", pii_types=["income"]),
        ["eu_ai_act.annex_iii.5b.creditworthiness"],
    ),
    (
        "5c life and health insurance",
        AssetSignals(use_case="life insurance underwriting copilot"),
        ["eu_ai_act.annex_iii.5c.life_health_insurance"],
    ),
    (
        "5d emergency triage",
        AssetSignals(
            use_case="emergency call triage", output_destinations=["ambulance_dispatch"]
        ),
        ["eu_ai_act.annex_iii.5d.emergency_response"],
    ),
    (
        "6 law enforcement",
        AssetSignals(use_case="recidivism risk assessment"),
        ["eu_ai_act.annex_iii.6.law_enforcement"],
    ),
    (
        "7 migration and border control",
        AssetSignals(use_case="visa application triage", pii_types=["passport"]),
        ["eu_ai_act.annex_iii.7.migration_border"],
    ),
    (
        "8a administration of justice",
        AssetSignals(use_case="judicial case-law research for court rulings"),
        ["eu_ai_act.annex_iii.8a.administration_of_justice"],
    ),
    (
        "8b elections",
        AssetSignals(use_case="voter outreach message targeting"),
        ["eu_ai_act.annex_iii.8b.elections"],
    ),
]

# Ordinary assets that must not be suggested as Annex III.
GOLDEN_NOT_ANNEX_III = [
    (
        "support chatbot",
        AssetSignals(use_case="customer support chatbot", vendor="anthropic"),
    ),
    (
        "code assistant",
        AssetSignals(use_case="code-review-bot", business_unit="Engineering"),
    ),
    (
        "fraud detection",
        AssetSignals(use_case="card fraud detection and credit risk alerts"),
    ),
    ("llm judge", AssetSignals(use_case="llm-as-judge eval harness")),
    ("bug triage", AssetSignals(use_case="github issue triage")),
    ("ensemble voting", AssetSignals(use_case="marketing copy generator")),
    ("hr in vendor name", AssetSignals(vendor="anthropic", model="claude-sonnet")),
]


@pytest.mark.parametrize(
    "signals,expected",
    [c[1:] for c in GOLDEN_ANNEX_III],
    ids=[c[0] for c in GOLDEN_ANNEX_III],
)
def test_golden_annex_iii(signals, expected):
    assert _annex(signals) == expected


@pytest.mark.parametrize(
    "signals",
    [c[1] for c in GOLDEN_NOT_ANNEX_III],
    ids=[c[0] for c in GOLDEN_NOT_ANNEX_III],
)
def test_golden_not_annex_iii(signals):
    assert _annex(signals) == []


def test_golden_cases_cover_every_annex_iii_rule():
    (annex,) = [rs for rs in load_rulesets() if rs.name == "eu_ai_act_annex_iii"]
    covered = {rid for _, _, ids in GOLDEN_ANNEX_III for rid in ids}
    assert covered == {r.id for r in annex.rules}
    points = {r.reference.split("point ")[1][0] for r in annex.rules}
    assert points == set("12345678")


def test_suggestion_carries_rule_and_evidence():
    s = _by_id(GOLDEN_ANNEX_III[8][1])["eu_ai_act.annex_iii.4a.recruitment"]
    assert (s.rule.framework, s.rule.category, s.rule.level) == (
        "EU_AI_ACT",
        "employment",
        "high",
    )
    assert s.rule.reference == "Annex III, point 4(a)"
    assert s.rule.ruleset_version == "1.1.0"
    assert s.confidence == "high"
    signals = s.evidence["signals"]
    assert {
        "signal": "use_case",
        "value": "resume-screening",
        "matched": ["resume*"],
        "role": "trigger",
    } in signals
    assert {
        "signal": "data_sources",
        "value": "ats_api",
        "matched": ["ats"],
        "role": "trigger",
    } in signals
    assert {
        "signal": "business_unit",
        "value": "HR",
        "matched": ["hr"],
        "role": "supporting",
    } in signals
    assert {
        "signal": "pii_types",
        "value": ["email", "national_id"],
        "matched": ["national_id"],
        "role": "supporting",
    } in signals
    assert s.evidence["summary"].startswith("Annex III, point 4(a), Recruitment")
    assert "use_case 'resume-screening' matched resume*" in s.evidence["summary"]
    json.dumps(s.evidence)  # stored as JSONB


def test_confidence_levels():
    medium = _by_id(AssetSignals(use_case="resume screening"))[
        "eu_ai_act.annex_iii.4a.recruitment"
    ]
    assert medium.confidence == "medium"
    # Only a weak trigger (a recruiting business unit) fired.
    low = _by_id(
        AssetSignals(use_case="policy q&a bot", business_unit="Talent Acquisition")
    )
    assert low["eu_ai_act.annex_iii.4a.recruitment"].confidence == "low"
    # A business unit alone never suggests workforce management.
    assert (
        _annex(AssetSignals(use_case="policy q&a bot", business_unit="Human Resources"))
        == []
    )


def test_unless_excludes_fraud_detection_from_credit_scoring():
    assert _annex(AssetSignals(use_case="credit scoring")) == [
        "eu_ai_act.annex_iii.5b.creditworthiness"
    ]
    assert (
        _annex(AssetSignals(use_case="credit scoring", calling_service="fraud-svc"))
        == []
    )
    # The exclusion is about the system's purpose: a credit scoring system
    # that merely reads fraud signals is still Annex III point 5(b).
    assert _annex(
        AssetSignals(use_case="credit scoring", data_sources=["fraud_signals_db"])
    ) == ["eu_ai_act.annex_iii.5b.creditworthiness"]


def test_unless_excludes_biometric_verification_from_remote_identification():
    rid = "eu_ai_act.annex_iii.1a.remote_biometric_identification"
    assert rid in _annex(AssetSignals(use_case="facial recognition in store cameras"))
    # Annex III point 1(a) excludes verification whose sole purpose is to
    # confirm a person is who they claim to be.
    assert rid not in _annex(
        AssetSignals(use_case="face matching", calling_service="kyc-identity-verification")
    )
    assert rid not in _annex(AssetSignals(use_case="face recognition liveness check"))


def test_keywords_match_whole_words_unless_starred():
    assert _annex(AssetSignals(use_case="hiring")) == [
        "eu_ai_act.annex_iii.4a.recruitment"
    ]
    assert (
        _annex(AssetSignals(use_case="rehiring")) == []
    )  # "hiring" is not a prefix match
    assert _annex(AssetSignals(use_case="Recruiters")) == [
        "eu_ai_act.annex_iii.4a.recruitment"
    ]
    assert (
        _annex(AssetSignals(data_sources=["stats_api"])) == []
    )  # "ats" is a whole word


def test_data_protection_rules():
    sensitive = _by_id(AssetSignals(pii_types=["email", "health"]))
    assert "nist_ai_rmf.privacy.sensitive_pii" in sensitive
    assert "nist_ai_rmf.privacy.personal_data" not in sensitive  # superseded via unless
    assert "iso_42001.a5.impact_assessment" in sensitive
    assert sensitive["iso_42001.a7.personal_data"].rule.level == "medium"

    plain = _by_id(AssetSignals(pii_types=["email"]))
    assert set(plain) == {
        "nist_ai_rmf.privacy.personal_data",
        "iso_42001.a7.personal_data",
    }
    assert plain["nist_ai_rmf.privacy.personal_data"].rule.framework == "NIST_AI_RMF"

    restricted = _by_id(AssetSignals(data_classification="restricted"))
    assert set(restricted) == {
        "nist_ai_rmf.security.restricted_data",
        "iso_42001.a7.personal_data",
        "iso_42001.a5.impact_assessment",
    }
    assert _by_id(AssetSignals(data_classification="internal")) == {}
    assert evaluate(AssetSignals()) == []


def test_collect_signals_from_asset_and_flows():
    tid, aid = uuid.uuid4(), uuid.uuid4()
    asset = AIAsset(
        id=aid,
        tenant_id=tid,
        vendor="openai",
        model="gpt-4o",
        use_case_name="resume-screening",
        business_unit="HR",
        calling_service="hr-screener",
        data_classification="internal",
    )
    flows = [
        DataFlow(
            asset_id=aid,
            source_system="candidates_db",
            destination_system="openai/gpt-4o",
            direction="input",
            data_classification="confidential",
            detected_pii_types=["national_id", "email"],
        ),
        DataFlow(
            asset_id=aid,
            source_system="ats_api",
            destination_system="openai/gpt-4o",
            direction="input",
            detected_pii_types=None,
        ),
        DataFlow(
            asset_id=aid,
            source_system="openai/gpt-4o",
            destination_system="hiring_dashboard",
            direction="output",
        ),
    ]
    s = collect_signals(asset, flows)
    assert s.data_sources == ["ats_api", "candidates_db"]
    assert s.output_destinations == ["hiring_dashboard"]
    assert s.pii_types == ["email", "national_id"]
    assert s.data_classification == "confidential"
    assert s.use_case == "resume-screening"


def test_shipped_rulesets_are_data():
    files = sorted(p.name for p in RULES_DIR.glob("*.json"))
    assert files == ["data_protection.json", "eu_ai_act_annex_iii.json"]
    rulesets = load_rulesets()
    assert {rs.name for rs in rulesets} == {"eu_ai_act_annex_iii", "data_protection"}
    frameworks = {r.framework for rs in rulesets for r in rs.rules}
    assert frameworks == {"EU_AI_ACT", "NIST_AI_RMF", "ISO_42001"}


def test_custom_ruleset_directory(tmp_path):
    (tmp_path / "custom.json").write_text(
        json.dumps(
            {
                "ruleset": "acme",
                "version": "2",
                "framework": "custom",
                "rules": [
                    {
                        "id": "acme.chatbot",
                        "category": "operational",
                        "level": "low",
                        "reference": "ACME-1",
                        "title": "Chatbots",
                        "when": {
                            "all": [{"signal": "vendor", "keywords": ["openai"]}],
                            "any": [{"signal": "text", "keywords": ["chat*"]}],
                        },
                    }
                ],
            }
        )
    )
    rulesets = load_rulesets(tmp_path)
    hit = evaluate(AssetSignals(vendor="openai", use_case="chatbot"), rulesets)
    assert [(s.rule.id, s.rule.ruleset_version, s.confidence) for s in hit] == [
        ("acme.chatbot", "2", "medium")
    ]
    assert (
        evaluate(AssetSignals(vendor="anthropic", use_case="chatbot"), rulesets) == []
    )


@pytest.mark.parametrize(
    "rule,error",
    [
        ({"framework": "GDPR"}, "framework"),
        ({"level": "severe"}, "level"),
        ({"when": {}}, "at least one condition"),
        (
            {"when": {"any": [{"signal": "colour", "keywords": ["x"]}]}},
            "unknown signal",
        ),
        (
            {"when": {"any": [{"signal": "text", "keywords": "@missing"}]}},
            "unknown list",
        ),
        ({"when": {"any": [{"signal": "text", "keywords": []}]}}, "non-empty list"),
        (
            {
                "when": {
                    "any": [{"signal": "data_classification", "at_least": "secret"}]
                }
            },
            "at_least",
        ),
        (
            {
                "when": {
                    "any": [{"signal": "text", "keywords": ["x"], "strength": "meh"}]
                }
            },
            "strength",
        ),
        ({"reference": ""}, "reference"),
    ],
)
def test_invalid_rules_are_rejected(rule, error):
    base = {
        "id": "r1",
        "framework": "custom",
        "category": "c",
        "level": "low",
        "reference": "ref",
        "title": "t",
        "when": {"any": [{"signal": "text", "keywords": ["x"]}]},
    }
    with pytest.raises(ValueError, match=error):
        parse_ruleset({"ruleset": "x", "version": "1", "rules": [{**base, **rule}]})


def test_duplicate_rule_ids_are_rejected(tmp_path):
    rule = {
        "id": "r1",
        "framework": "custom",
        "category": "c",
        "level": "low",
        "reference": "ref",
        "title": "t",
        "when": {"any": [{"signal": "text", "keywords": ["x"]}]},
    }
    with pytest.raises(ValueError, match="duplicate"):
        parse_ruleset({"ruleset": "x", "version": "1", "rules": [rule, rule]})
    for name in ("a", "b"):
        (tmp_path / f"{name}.json").write_text(
            json.dumps({"ruleset": name, "version": "1", "rules": [rule]})
        )
    with pytest.raises(ValueError, match="more than one ruleset"):
        load_rulesets(tmp_path)
