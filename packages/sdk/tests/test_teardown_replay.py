"""Policy replay and sensitivity mix (``prompt_shields.teardown_replay``, #13).

Golden tests: synthetic records (``fixtures/teardown_replay/records.jsonl``)
with PII and checksum-valid synthetic Nordic national IDs, replayed under the
``nordic-sovereign`` example policy, in cost-only and full mode. The expected
reports are checked in; the headline numbers are also asserted by hand below,
so a regenerated golden cannot silently change them.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from pathlib import Path

import pytest
from prompt_shields.teardown_replay import (
    OUTCOME_NO_COMPLIANT,
    OUTCOME_ROUTED,
    OUTCOME_UNCHANGED,
    UsageRecord,
    level_is_sovereign,
    main,
    replay,
    replay_call,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).parent / "fixtures" / "teardown_replay"
POLICY_PATH = ROOT / "schemas" / "route-policy" / "examples" / "nordic-sovereign.json"
POLICY = json.loads(POLICY_PATH.read_text())
RECORDS = [
    json.loads(line)
    for line in (FIXTURES / "records.jsonl").read_text().splitlines()
    if line.strip()
]


@pytest.fixture
def no_network(monkeypatch):
    """Fail the test if anything tries to open a socket."""

    def refuse(*args, **kwargs):
        raise AssertionError("replay must not open a network connection")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


# --- golden reports ----------------------------------------------------------


@pytest.mark.parametrize(
    "scan_prompts,golden",
    [(False, "expected-cost-only.json"), (True, "expected-full.json")],
    ids=["cost-only", "full"],
)
def test_golden_report(scan_prompts, golden, no_network):
    report = replay(RECORDS, POLICY, scan_prompts=scan_prompts)
    assert report == json.loads((FIXTURES / golden).read_text())


def test_headline_numbers_by_hand():
    # Baseline: 0.0075 + 0.0075 + 0.024 + 0.00021 (gpt-4o-mini batch, 0.5x)
    # + 0.00225 + 0.018 (priced at requested) + 0.000075; llama-local unpriced.
    cost_only = replay(RECORDS, POLICY)
    full = replay(RECORDS, POLICY, scan_prompts=True)
    for report in (cost_only, full):
        assert report["baseline"]["spend"] == pytest.approx(0.059535)
        assert report["baseline"]["calls_null_cost"] == 1
        assert report["baseline"]["priced_at_requested"] == 1
        policy = report["strategies"]["policy_cheapest"]
        assert policy["outcomes"][OUTCOME_NO_COMPLIANT] == 1
        assert report["strategies"]["policy_at_parity"]["status"] == "not_measured"

    # Cost-only: the Norwegian ID in record 2 is unseen, so it is
    # confidential by default; the Danish CPR in record 8 is unseen, so public.
    levels = {row["level"]: row for row in cost_only["sensitivity_mix"]["levels"]}
    assert levels["confidential"]["calls"] == 2
    assert levels["confidential"]["by_source"]["default"] == 1
    assert levels["public"]["calls"] == 1
    assert cost_only["sensitivity_mix"]["sovereign"]["calls"] == 4
    assert "national_id" not in cost_only["sensitivity_mix"]["pii_categories"]

    # Full: local detection escalates records 2, 7 and 8 to restricted.
    levels = {row["level"]: row for row in full["sensitivity_mix"]["levels"]}
    assert levels["restricted"]["calls"] == 4
    assert levels["restricted"]["by_source"]["pii_escalated"] == 3
    assert levels["public"]["calls"] == 0
    assert full["sensitivity_mix"]["pii_categories"]["national_id"] == 3
    sovereign = full["sensitivity_mix"]["sovereign"]
    assert sovereign["calls"] == 5
    assert sovereign["spend"] == pytest.approx(
        0.0075 + 0.00021 + 0.00225 + 0.018 + 0.000075
    )
    # Projected: 0.0075 + 0.009 + 0.024 + 0.00052 + 0.00225 + 0.024 + 0.00008.
    policy = full["strategies"]["policy_cheapest"]
    assert policy["projected_spend"] == pytest.approx(0.06735)
    assert policy["savings"] == pytest.approx(0.059535 - 0.06735)


# --- per call ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "personnummer 19121212-1212",  # SE
        "fødselsnummer 01818510078",  # NO
        "CPR 010190-1234",  # DK
        "henkilötunnus 131052-308T",  # FI
    ],
    ids=["se", "no", "dk", "fi"],
)
def test_national_id_escalates_to_restricted_in_full_mode(text):
    record = {
        "vendor": "openai",
        "served_model": "gpt-4o",
        "tokens_in": 100,
        "tokens_out": 100,
        "data_classification": "public",
        "quality": "balanced",
        "messages": [{"role": "user", "content": text}],
    }
    cost_only = replay_call(record, POLICY)
    assert (cost_only.sensitivity_level, cost_only.sensitivity_source) == (
        "public",
        "declared",
    )
    assert cost_only.routed_model == "gpt-4o"

    full = replay_call(record, POLICY, scan_prompts=True)
    assert "national_id" in full.pii_categories
    assert (full.sensitivity_level, full.sensitivity_source) == (
        "restricted",
        "pii_escalated",
    )
    assert full.sovereign is True
    assert full.routed_model == "berget-llama-70b"
    assert full.routed_jurisdiction == "EEA"
    assert full.baseline_compliant is False


def test_recorded_pii_types_escalate_without_prompt_text():
    record = {
        "vendor": "openai",
        "served_model": "gpt-4o-mini",
        "tokens_in": 100,
        "tokens_out": 100,
        "data_classification": "internal",
        "detected_pii_types": ["national_id"],
        "quality": "draft",
    }
    call = replay_call(record, POLICY)
    assert call.sensitivity_level == "restricted"
    assert call.routed_model == "mistral-small-eu"


def test_no_compliant_candidate_keeps_baseline_cost():
    call = replay_call(RECORDS[4], POLICY)
    assert call.outcome == OUTCOME_NO_COMPLIANT
    assert call.routed_model is None
    assert call.projected_cost == call.baseline_cost == pytest.approx(0.00225)


def test_unchanged_and_routed_outcomes():
    assert replay_call(RECORDS[0], POLICY).outcome == OUTCOME_UNCHANGED
    routed = replay_call(RECORDS[1], POLICY)
    assert routed.outcome == OUTCOME_ROUTED
    assert routed.projected_pricing == "policy_rates"


def test_candidate_without_batch_is_priced_at_standard():
    policy = {
        "schema_version": "route-policy.v1",
        "default_group": "cheap",
        "quality_to_group": {
            "draft": "cheap",
            "balanced": "cheap",
            "critical": "cheap",
        },
        "groups": {
            "cheap": [
                {
                    "model": "mistral-small-latest",
                    "provider": "mistral",
                    "input_cost_per_1k": 0.0001,
                    "output_cost_per_1k": 0.0003,
                }
            ]
        },
    }
    record = {
        "vendor": "openai",
        "served_model": "gpt-4o",
        "service_tier": "batch",
        "tokens_in": 1000,
        "tokens_out": 1000,
    }
    call = replay_call(record, policy)
    assert call.baseline_cost == pytest.approx((0.0025 + 0.01) * 0.5)
    assert call.projected_pricing == "catalog"
    assert call.projected_cost == pytest.approx(0.0001 + 0.0003)


def test_missing_tokens_give_null_costs_not_zero():
    call = replay_call(
        {"vendor": "openai", "served_model": "gpt-4o", "quality": "draft"}, POLICY
    )
    assert call.baseline_cost is None
    assert call.baseline_null_reason == "missing_tokens"
    assert call.projected_cost is None
    report = replay(
        [{"vendor": "openai", "served_model": "gpt-4o", "quality": "draft"}], POLICY
    )
    assert report["baseline"]["calls_null_cost"] == 1
    assert report["strategies"]["policy_cheapest"]["calls_compared"] == 0
    assert report["strategies"]["policy_cheapest"]["savings_pct"] is None


def test_level_is_sovereign():
    assert [
        level_is_sovereign(POLICY, level)
        for level in ("public", "internal", "confidential", "restricted", None)
    ] == [False, False, True, True, False]


# --- records -----------------------------------------------------------------


def test_record_from_mapping_aliases_and_ignores_unknown_keys():
    record = UsageRecord.from_mapping(
        {"model": "gpt-4o", "cost": 99.0, "prompt_hash": "abc", "tokens_in": 1}
    )
    assert record.requested_model == "gpt-4o"
    assert record.baseline_cost is None  # an event's `cost` is never summed
    assert record.baseline_model() == "gpt-4o"
    assert UsageRecord(requested_model="auto").baseline_model() is None


def test_replay_accepts_records_and_other_dataclasses():
    @dataclass
    class IngestRow:
        vendor: str
        served_model: str
        tokens_in: int
        tokens_out: int
        data_classification: str

    row = IngestRow("openai", "gpt-4o", 1000, 500, "internal")
    from_row = replay_call(row, POLICY)
    from_record = replay_call(UsageRecord.from_mapping(vars(row)), POLICY)
    assert from_row == from_record
    with pytest.raises(TypeError):
        replay_call("not a record", POLICY)  # type: ignore[arg-type]


def test_explicit_baseline_cost_wins():
    call = replay_call(
        {"vendor": "openai", "served_model": "gpt-4o", "baseline_cost": 1.25}, POLICY
    )
    assert call.baseline_cost == 1.25


# --- privacy -----------------------------------------------------------------

SECRETS = (
    "01818510078",
    "19121212-1212",
    "010190-1234",
    "jens.hansen@example.dk",
    "press@example.com",
    "fødselsnummer",
    "diagnosis",
    "refund policy",
)


def test_report_holds_no_prompt_text_or_identifiers(no_network):
    for scan in (False, True):
        text = json.dumps(
            replay(RECORDS, POLICY, scan_prompts=scan), ensure_ascii=False
        )
        for secret in SECRETS:
            assert secret not in text


def test_call_results_hold_no_prompt_text():
    for record in RECORDS:
        call = replay_call(record, POLICY, scan_prompts=True)
        text = repr(call)
        for secret in SECRETS:
            assert secret not in text


def test_cost_only_mode_does_not_read_messages():
    class Exploding(list):
        def __iter__(self):
            raise AssertionError("cost-only replay read prompt content")

    record = UsageRecord(
        vendor="openai",
        served_model="gpt-4o",
        tokens_in=10,
        tokens_out=10,
        messages=Exploding([{"role": "user", "content": "x"}]),
    )
    replay_call(record, POLICY)


# --- standalone entry point --------------------------------------------------


def test_main_writes_report(tmp_path, no_network, capsys):
    out = tmp_path / "report.json"
    code = main(
        [
            str(FIXTURES / "records.jsonl"),
            "--policy",
            str(POLICY_PATH),
            "--scan-prompts",
            "--output",
            str(out),
        ]
    )
    assert code == 0
    assert json.loads(out.read_text()) == json.loads(
        (FIXTURES / "expected-full.json").read_text()
    )

    assert main([str(FIXTURES / "records.jsonl"), "--policy", str(POLICY_PATH)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["mode"] == "cost_only"
