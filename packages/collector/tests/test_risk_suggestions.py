"""Risk classification rules engine (#97): golden EU AI Act Annex III,
Article 5 and Article 50 cases (match and must-not-match), the
data-protection ruleset, evidence, confidence and ruleset validation.
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


def _ruleset(signals: AssetSignals, name: str) -> list[str]:
    return [s.rule.id for s in evaluate(signals) if s.rule.ruleset == name]


def _annex(signals: AssetSignals) -> list[str]:
    return _ruleset(signals, "eu_ai_act_annex_iii")


def _art5(signals: AssetSignals) -> list[str]:
    return _ruleset(signals, "eu_ai_act_article_5")


def _art50(signals: AssetSignals) -> list[str]:
    return _ruleset(signals, "eu_ai_act_article_50")


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

# Reviewer-reported false positives (#97 follow-up): (case, signals, the Annex
# III rule that must not fire, the Annex III rule ids that fire instead).
A = "eu_ai_act.annex_iii."
GOLDEN_FALSE_POSITIVES = [
    (
        "loan applicant",
        AssetSignals(use_case="loan applicant scoring"),
        A + "4a.recruitment",
        [A + "5b.creditworthiness"],
    ),
    (
        "mortgage applicants",
        AssetSignals(
            use_case="mortgage-applicant-document-check",
            calling_service="applicant-intake",
        ),
        A + "4a.recruitment",
        [A + "5b.creditworthiness"],
    ),
    (
        "loan applicant employment verification",
        AssetSignals(use_case="loan applicant employment verification"),
        A + "4a.recruitment",
        [A + "5b.creditworthiness"],
    ),
    (
        "visa applicants",
        AssetSignals(use_case="visa applicant document summariser"),
        A + "4a.recruitment",
        [A + "7.migration_border"],
    ),
    (
        "recsys candidate generation",
        AssetSignals(use_case="candidate generation for product recommendations"),
        A + "4a.recruitment",
        [],
    ),
    (
        "rag candidate passages",
        AssetSignals(calling_service="reranker", use_case="rerank candidate passages"),
        A + "4a.recruitment",
        [],
    ),
    (
        "election candidates",
        AssetSignals(use_case="election candidate fact sheets"),
        A + "4a.recruitment",
        [A + "8b.elections"],
    ),
    (
        "hospital admissions",
        AssetSignals(use_case="hospital admissions forecasting"),
        A + "3a.education_access",
        [],
    ),
    (
        "patient admission notes",
        AssetSignals(
            use_case="admission note summariser", business_unit="Inpatient Care"
        ),
        A + "3a.education_access",
        [],
    ),
    (
        "university hospital admissions",
        AssetSignals(use_case="university hospital patient admission triage"),
        A + "3a.education_access",
        [],
    ),
    (
        "admission without education context",
        AssetSignals(use_case="theme park admission ticketing"),
        A + "3a.education_access",
        [],
    ),
    (
        "animal welfare",
        AssetSignals(use_case="animal welfare inspection reports"),
        A + "5a.public_benefits",
        [],
    ),
    (
        "employee welfare",
        AssetSignals(use_case="employee welfare benefits faq"),
        A + "5a.public_benefits",
        [],
    ),
    (
        "welfare business unit",
        AssetSignals(use_case="policy q&a bot", business_unit="Animal Welfare"),
        A + "5a.public_benefits",
        [],
    ),
    (
        "social security number",
        AssetSignals(use_case="social security number redaction"),
        A + "5a.public_benefits",
        [],
    ),
    (
        "raft leader election",
        AssetSignals(
            use_case="leader election incident summariser", calling_service="raft-ops"
        ),
        A + "8b.elections",
        [],
    ),
    (
        "ensemble voting",
        AssetSignals(use_case="ensemble voting classifier"),
        A + "8b.elections",
        [],
    ),
    (
        "campaign volunteer scheduling",
        AssetSignals(use_case="political campaign volunteer scheduling"),
        A + "8b.elections",
        [],
    ),
    (
        "campaign logistics service",
        AssetSignals(
            use_case="election day logistics", calling_service="campaign-logistics"
        ),
        A + "8b.elections",
        [],
    ),
    # Non-employment applicant phrases still win over an HR context (#97
    # follow-up 2): the exclusion is a phrase around the applicant term.
    (
        "applicants for loans with HR unit",
        AssetSignals(use_case="applicants for loans: scoring", business_unit="Talent"),
        A + "4a.recruitment",
        [],
    ),
    (
        "university applicant with HR unit",
        AssetSignals(use_case="university applicant ranking", business_unit="HR"),
        A + "4a.recruitment",
        [],
    ),
    (
        "campaign event scheduling",
        AssetSignals(use_case="political campaign event scheduling"),
        A + "8b.elections",
        [],
    ),
    (
        "campaign office administration",
        AssetSignals(
            use_case="campaign office administration assistant",
            calling_service="election-hq",
        ),
        A + "8b.elections",
        [],
    ),
]

# The scoped keywords still fire with the right context.
GOLDEN_SCOPED_MATCHES = [
    (
        "applicant with HR unit",
        AssetSignals(use_case="applicant summaries", business_unit="Talent"),
        A + "4a.recruitment",
    ),
    (
        "candidate with job context",
        AssetSignals(use_case="candidate ranking for open roles"),
        A + "4a.recruitment",
    ),
    (
        "candidate from the ATS",
        AssetSignals(use_case="candidate summary", data_sources=["ats"]),
        A + "4a.recruitment",
    ),
    (
        "admissions with education BU",
        AssetSignals(
            use_case="admissions essay review",
            business_unit="Undergraduate Admissions Office",
        ),
        A + "3a.education_access",
    ),
    (
        "college admissions",
        AssetSignals(use_case="college admission ranking"),
        A + "3a.education_access",
    ),
    (
        "welfare claims",
        AssetSignals(use_case="welfare claims triage"),
        A + "5a.public_benefits",
    ),
    (
        "social welfare",
        AssetSignals(use_case="social welfare caseworker assistant"),
        A + "5a.public_benefits",
    ),
    (
        "social security benefits",
        AssetSignals(use_case="social security pension adviser"),
        A + "5a.public_benefits",
    ),
    (
        "election persuasion",
        AssetSignals(use_case="election ad generator"),
        A + "8b.elections",
    ),
    (
        "campaign logistics with voter targeting",
        AssetSignals(use_case="campaign logistics and voter microtargeting"),
        A + "8b.elections",
    ),
    # #97 follow-up 2: an education, credit or benefits word elsewhere on the
    # asset no longer hides a job applicant or candidate.
    (
        "job applicants for university staff roles",
        AssetSignals(
            use_case="HR screening of job applicants for university staff roles"
        ),
        A + "4a.recruitment",
    ),
    (
        "applicants for university staff roles",
        AssetSignals(
            use_case="screening of applicants for university staff roles",
            business_unit="Human Resources",
        ),
        A + "4a.recruitment",
    ),
    (
        "applicant shortlisting for university vacancies",
        AssetSignals(
            use_case="applicant shortlisting for university lecturer vacancies"
        ),
        A + "4a.recruitment",
    ),
    (
        "applicant ranking with a benefits service",
        AssetSignals(
            use_case="applicant ranking",
            business_unit="Talent",
            calling_service="benefits-and-credit-check",
        ),
        A + "4a.recruitment",
    ),
    (
        "candidate ranking mentioning benefits",
        AssetSignals(
            use_case="candidate ranking for open roles incl. benefits package summary"
        ),
        A + "4a.recruitment",
    ),
    (
        "applicants to student-support jobs",
        AssetSignals(use_case="interview notes for applicants to student-support jobs"),
        A + "4a.recruitment",
    ),
    # Scheduling or administrative wording is no longer campaign logistics.
    (
        "election ad scheduling",
        AssetSignals(use_case="election ad scheduling"),
        A + "8b.elections",
    ),
    (
        "referendum message scheduling",
        AssetSignals(use_case="referendum message scheduling"),
        A + "8b.elections",
    ),
    (
        "voter eligibility administrative review",
        AssetSignals(use_case="voter eligibility administrative review"),
        A + "8b.elections",
    ),
    (
        "event scheduling with voter microtargeting",
        AssetSignals(use_case="campaign event scheduling with voter microtargeting"),
        A + "8b.elections",
    ),
]

P = "eu_ai_act.article_5."
# (case, signals, the Article 5 rule ids expected): every point (a)-(h).
GOLDEN_ARTICLE_5 = [
    (
        "1a subliminal",
        AssetSignals(use_case="subliminal ad messaging"),
        [P + "1a.manipulative_techniques"],
    ),
    (
        "1a dark patterns",
        AssetSignals(calling_service="checkout-dark-patterns"),
        [P + "1a.manipulative_techniques"],
    ),
    (
        "1b vulnerable upsell",
        AssetSignals(use_case="upsell payday loans to users in financial distress"),
        [P + "1b.exploiting_vulnerabilities"],
    ),
    (
        "1b children persuasion",
        AssetSignals(use_case="in-game purchase nudges for kids"),
        [P + "1b.exploiting_vulnerabilities"],
    ),
    (
        "1c social scoring",
        AssetSignals(use_case="citizen social scoring"),
        [P + "1c.social_scoring"],
    ),
    (
        "1d individual crime prediction",
        AssetSignals(use_case="crime prediction from personality profiles"),
        [P + "1d.individual_crime_prediction"],
    ),
    (
        "1e face scraping",
        AssetSignals(
            use_case="scrape faces from social media",
            output_destinations=["face_index"],
        ),
        [P + "1e.facial_image_scraping"],
    ),
    (
        "1f emotion at work",
        AssetSignals(use_case="employee emotion detection on video calls"),
        [P + "1f.emotion_recognition_workplace_education"],
    ),
    (
        "1f emotion in class",
        AssetSignals(use_case="emotion recognition", business_unit="School of Law"),
        [P + "1f.emotion_recognition_workplace_education"],
    ),
    # "safety" alone is not the medical-or-safety exception (#97 follow-up 2).
    (
        "1f emotion at work for workplace safety",
        AssetSignals(
            use_case="employee emotion recognition for workplace safety compliance"
        ),
        [P + "1f.emotion_recognition_workplace_education"],
    ),
    (
        "1f emotion in class for child safety",
        AssetSignals(
            use_case="student emotion detection for child safety in classrooms"
        ),
        [P + "1f.emotion_recognition_workplace_education"],
    ),
    (
        "1f safety-named service",
        AssetSignals(
            use_case="emotion analytics on staff video calls",
            calling_service="safety-insights",
        ),
        [P + "1f.emotion_recognition_workplace_education"],
    ),
    (
        "1f health and safety unit",
        AssetSignals(
            use_case="emotion recognition",
            business_unit="Health and Safety",
            calling_service="worker-wellbeing",
        ),
        [P + "1f.emotion_recognition_workplace_education"],
    ),
    (
        # A patient-facing metric is not a medical reason for reading the
        # employees' emotions.
        "1f staff emotion for patient satisfaction",
        AssetSignals(
            use_case="employee emotion recognition to improve patient satisfaction"
        ),
        [P + "1f.emotion_recognition_workplace_education"],
    ),
    (
        "1g sensitive traits",
        AssetSignals(use_case="infer sexual orientation from face images"),
        [P + "1g.biometric_categorisation_sensitive_traits"],
    ),
    (
        "1g via biometric pii",
        AssetSignals(use_case="ethnicity classifier", pii_types=["face_image"]),
        [P + "1g.biometric_categorisation_sensitive_traits"],
    ),
    (
        "1h live rbi for police",
        AssetSignals(
            use_case="live facial recognition on street cameras",
            output_destinations=["police_watchlist_alerts"],
        ),
        [P + "1h.realtime_remote_biometric_identification"],
    ),
    (
        # Matching faces against a watchlist is identification, even when the
        # use case also mentions identity verification.
        "1h watchlist search described as verification",
        AssetSignals(
            use_case="live facial recognition CCTV identity verification",
            calling_service="police-watchlist",
        ),
        [P + "1h.realtime_remote_biometric_identification"],
    ),
]

GOLDEN_NOT_ARTICLE_5 = [
    (
        "1a dark pattern audit",
        AssetSignals(use_case="dark pattern detection for UX audits"),
    ),
    ("1a data manipulation", AssetSignals(use_case="dataframe manipulation helper")),
    (
        "1b child safety",
        AssetSignals(use_case="child safety classifier for exploitation reports"),
    ),
    ("1b kids content", AssetSignals(use_case="bedtime stories for kids")),
    (
        "1b minor version",
        AssetSignals(use_case="persuasive copy for minor version release"),
    ),
    ("1c credit scoring", AssetSignals(use_case="consumer credit scoring")),
    (
        "1c social media scoring",
        AssetSignals(use_case="social media sentiment scoring"),
    ),
    ("1c lead scoring", AssetSignals(use_case="sales lead scoring")),
    (
        "1d place-based hotspots",
        AssetSignals(use_case="crime hotspot prediction for patrol planning"),
    ),
    (
        "1d recidivism without profiling",
        AssetSignals(use_case="recidivism risk assessment"),
    ),
    ("1e price scraping", AssetSignals(use_case="web scraping for price monitoring")),
    ("1e face blur", AssetSignals(use_case="face blur for uploaded images")),
    (
        "1f customer emotion",
        AssetSignals(calling_service="call-center-emotion-detection"),
    ),
    (
        "1f driver safety",
        AssetSignals(use_case="driver drowsiness emotion detection for workers"),
    ),
    (
        "1f driver fatigue",
        AssetSignals(
            use_case="driver fatigue and emotion detection for delivery workers"
        ),
    ),
    (
        "1f pilot alertness",
        AssetSignals(
            use_case="pilot alertness emotion recognition for flight crew staff"
        ),
    ),
    (
        "1f drowsiness service",
        AssetSignals(
            use_case="emotion detection for employees",
            calling_service="driver-drowsiness-monitoring",
        ),
    ),
    (
        "1f clinical",
        AssetSignals(
            use_case="clinical emotion recognition for patients", business_unit="HR"
        ),
    ),
    ("1g religious texts", AssetSignals(use_case="religious text translation")),
    ("1g facial attributes", AssetSignals(use_case="facial attribute age estimation")),
    (
        "1h retail facial recognition",
        AssetSignals(use_case="real-time facial recognition for store loss prevention"),
    ),
    ("1h police mugshot search", AssetSignals(use_case="police mugshot face search")),
    (
        "1h verification",
        AssetSignals(
            use_case="live face verification", calling_service="police-badge-login"
        ),
    ),
]

E = "eu_ai_act.article_50."
GOLDEN_ARTICLE_50 = [
    (
        "50(1) chatbot",
        AssetSignals(use_case="customer support chatbot"),
        [E + "1.ai_interaction_disclosure"],
    ),
    (
        "50(1) voice assistant",
        AssetSignals(calling_service="voice-assistant"),
        [E + "1.ai_interaction_disclosure"],
    ),
    (
        "50(2) image generation",
        AssetSignals(use_case="marketing image generation"),
        [E + "2.synthetic_content_marking"],
    ),
    (
        "50(2) tts",
        AssetSignals(calling_service="tts-service"),
        [E + "2.synthetic_content_marking"],
    ),
    (
        "50(3) emotion recognition",
        AssetSignals(calling_service="call-center-emotion-detection"),
        [E + "3.emotion_biometric_disclosure"],
    ),
    (
        "50(2)+(4) deepfake",
        AssetSignals(use_case="voice cloning for dubbing"),
        [E + "2.synthetic_content_marking", E + "4.deep_fake_disclosure"],
    ),
    (
        "50(4) public-interest text",
        AssetSignals(use_case="automated journalism"),
        [E + "4.deep_fake_disclosure"],
    ),
]

GOLDEN_NOT_ARTICLE_50 = [
    ("code assistant", AssetSignals(use_case="code-review-bot")),
    ("batch summariser", AssetSignals(use_case="nightly invoice summariser")),
    ("image classification", AssetSignals(use_case="product image classification")),
    ("transcript tagging", AssetSignals(use_case="sales call transcript tagging")),
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


@pytest.mark.parametrize(
    "signals,rule_id,expected",
    [c[1:] for c in GOLDEN_FALSE_POSITIVES],
    ids=[c[0] for c in GOLDEN_FALSE_POSITIVES],
)
def test_golden_false_positives_do_not_fire(signals, rule_id, expected):
    fired = _annex(signals)
    assert rule_id not in fired
    assert fired == expected


@pytest.mark.parametrize(
    "signals,rule_id",
    [c[1:] for c in GOLDEN_SCOPED_MATCHES],
    ids=[c[0] for c in GOLDEN_SCOPED_MATCHES],
)
def test_golden_scoped_keywords_still_match(signals, rule_id):
    assert rule_id in _annex(signals)


def test_context_matches_are_evidence():
    s = _by_id(AssetSignals(use_case="applicant summaries", business_unit="Talent"))[
        A + "4a.recruitment"
    ]
    assert {
        "signal": "business_unit",
        "value": "Talent",
        "matched": ["talent"],
        "role": "context",
    } in s.evidence["signals"]
    assert "(context)" in s.evidence["summary"]


@pytest.mark.parametrize(
    "signals,expected",
    [c[1:] for c in GOLDEN_ARTICLE_5],
    ids=[c[0] for c in GOLDEN_ARTICLE_5],
)
def test_golden_article_5(signals, expected):
    assert _art5(signals) == expected


@pytest.mark.parametrize(
    "signals",
    [c[1] for c in GOLDEN_NOT_ARTICLE_5 + GOLDEN_NOT_ANNEX_III],
    ids=[c[0] for c in GOLDEN_NOT_ARTICLE_5 + GOLDEN_NOT_ANNEX_III],
)
def test_golden_not_article_5(signals):
    assert _art5(signals) == []


def test_annex_iii_golden_cases_are_not_prohibited():
    for _, signals, _ in GOLDEN_ANNEX_III + GOLDEN_SCOPED_MATCHES:
        assert _art5(signals) == []


@pytest.mark.parametrize(
    "signals,expected",
    [c[1:] for c in GOLDEN_ARTICLE_50],
    ids=[c[0] for c in GOLDEN_ARTICLE_50],
)
def test_golden_article_50(signals, expected):
    assert _art50(signals) == expected


@pytest.mark.parametrize(
    "signals",
    [c[1] for c in GOLDEN_NOT_ARTICLE_50],
    ids=[c[0] for c in GOLDEN_NOT_ARTICLE_50],
)
def test_golden_not_article_50(signals):
    assert _art50(signals) == []


def test_article_5_rules_are_critical_and_cover_every_point():
    (art5,) = [rs for rs in load_rulesets() if rs.name == "eu_ai_act_article_5"]
    assert art5.version == "1.1.0"
    assert {r.level for r in art5.rules} == {"critical"}
    assert {r.category for r in art5.rules} == {"prohibited_practice"}
    assert all("human review required" in r.title for r in art5.rules)
    points = sorted(r.reference.split(":")[0] for r in art5.rules)
    assert points == [f"Article 5(1)({p})" for p in "abcdefgh"]
    covered = {rid for _, _, ids in GOLDEN_ARTICLE_5 for rid in ids}
    assert covered == {r.id for r in art5.rules}


def test_golden_cases_cover_every_article_50_rule():
    (art50,) = [rs for rs in load_rulesets() if rs.name == "eu_ai_act_article_50"]
    covered = {rid for _, _, ids in GOLDEN_ARTICLE_50 for rid in ids}
    assert covered == {r.id for r in art50.rules}
    assert {r.category for r in art50.rules} == {"transparency"}


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
    assert s.rule.ruleset_version == "1.3.0"
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
        AssetSignals(
            use_case="face matching", calling_service="kyc-identity-verification"
        )
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


def test_starred_words_inside_phrases():
    # Every word of a phrase can be a prefix ("loan* applicant*").
    rs = parse_ruleset(
        {
            "ruleset": "x",
            "version": "1",
            "rules": [
                {
                    "id": "r1",
                    "framework": "custom",
                    "category": "c",
                    "level": "low",
                    "reference": "ref",
                    "title": "t",
                    "when": {
                        "any": [{"signal": "text", "keywords": ["loan* applicant*"]}]
                    },
                }
            ],
        }
    )

    def fires(text):
        return [s.rule.id for s in evaluate(AssetSignals(use_case=text), [rs])]

    assert fires("loans applicants triage") == ["r1"]
    assert fires("loan-applicant") == ["r1"]
    assert fires("loan officer applicant") == []
    assert fires("applicants for loans") == []
    assert fires("payloan applicant") == []


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
    assert files == [
        "data_protection.json",
        "eu_ai_act_annex_iii.json",
        "eu_ai_act_article_5.json",
        "eu_ai_act_article_50.json",
    ]
    rulesets = load_rulesets()
    assert {rs.name for rs in rulesets} == {
        "eu_ai_act_annex_iii",
        "eu_ai_act_article_5",
        "eu_ai_act_article_50",
        "data_protection",
    }
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
        (
            {"when": {"any": [{"signal": "text", "keywords": ["x"], "context": []}]}},
            "context must be a non-empty list",
        ),
        (
            {
                "when": {
                    "any": [
                        {
                            "signal": "text",
                            "keywords": ["x"],
                            "exclude": [{"signal": "colour", "keywords": ["y"]}],
                        }
                    ]
                }
            },
            "unknown signal",
        ),
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
