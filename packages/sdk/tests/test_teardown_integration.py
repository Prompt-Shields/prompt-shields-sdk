"""The full teardown pipeline: ps-teardown + policy replay (#13) + ps-eval results (#14).

Golden files (``fixtures/teardown/golden/policy*.{md,json}``) cover the whole
report with a route policy and eval results. ``fixtures/teardown/
eval_results.json`` is the output of a real (offline, fake-executor) ``ps-eval``
run built by :func:`build_eval_results`; it is checked in so the teardown
golden does not depend on the eval code, and a test keeps it in sync. To
regenerate after an intended change, run with ``PS_UPDATE_GOLDEN=1`` and
review the diff.
"""

from __future__ import annotations

import json
import os
import re
import socket
from pathlib import Path

import pytest
from prompt_shields.evals import (
    Candidate,
    EvalConfig,
    EvalRecord,
    ExactMatch,
    FakeExecutor,
    PairwiseJudge,
    run_eval,
    stratified_sample,
)
from prompt_shields.evals import (
    build_report as build_eval_report,
)
from prompt_shields.evals.cli import main as eval_main
from prompt_shields.evals.harness import EvalPolicy
from prompt_shields.teardown import (
    ReportOptions,
    SdkPriceSource,
    TeardownRecord,
    build_report,
    learning_tuples,
    load_records,
    price_records,
    render_report,
)
from prompt_shields.teardown.cli import main as teardown_main
from prompt_shields.teardown.policy import read_eval_results, replay_priced
from prompt_shields.teardown_replay import UsageRecord, replay, replay_call

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).parent / "fixtures" / "teardown"
GOLDEN = FIXTURES / "golden"
POLICY_PATH = ROOT / "schemas" / "route-policy" / "examples" / "nordic-sovereign.json"
POLICY = json.loads(POLICY_PATH.read_text())
KEY_MAP = json.loads((FIXTURES / "key_map.json").read_text())
EVAL_RESULTS_PATH = FIXTURES / "eval_results.json"
SECRET_TEXT = "SECRET-PROMPT-TEXT-123"
INPUTS = ["ps_events.jsonl", "generic.jsonl"]


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("ps-teardown must not open a network connection")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


# --- the eval run the golden report reads ---------------------------------------

MINI = Candidate("openai", "gpt-4o-mini")
SMALL_EU = Candidate("mistral-eu", "mistral-small-eu")
JUDGE = Candidate("mistral-eu", "mistral-large-eu")


def _eval_records() -> list[EvalRecord]:
    """Synthetic logged calls with gold answers, per use case."""
    plan = [("support-bot", "internal", 40), ("faq", "public", 80),
            ("contract-review", "confidential", 40)]
    out = []
    for use_case, level, n in plan:
        for i in range(n):
            out.append(EvalRecord.from_dict({
                "id": f"{use_case}-{i}", "vendor": "openai", "served_model": "gpt-4o",
                "messages": [{"role": "user", "content": f"{use_case} question {i}"}],
                "expected": f"answer {i}", "use_case": use_case,
                "data_classification": level, "tokens_in": 400, "tokens_out": 100,
            }))
    return out


def _answer(candidate: Candidate, messages: list[dict]) -> str:
    """mini misses 1 in 40 support-bot answers; small-eu misses 1 in 4."""
    if candidate == JUDGE:
        return "VERDICT: TIE"
    text = messages[-1]["content"]
    use_case, _, i = text.rpartition(" question ")
    i = int(i)
    wrong = (
        (candidate == MINI and use_case == "support-bot" and i == 0)
        or (candidate == SMALL_EU and use_case == "support-bot" and i % 4 == 0)
    )
    return "wrong" if wrong else f"answer {i}"


def build_eval_results() -> dict:
    records = _eval_records()
    policy = EvalPolicy.from_route_policy(POLICY)
    sample = stratified_sample(records, per_stratum=100, seed=7,
                               unclassified_default=policy.unclassified_default,
                               level_of=policy.level)
    config = EvalConfig(
        candidates=[MINI, SMALL_EU],
        scorers={"*": ExactMatch(), "contract-review": PairwiseJudge(JUDGE)},
        policy=policy,
    )
    run = run_eval(sample, FakeExecutor(_answer), config)
    report = build_eval_report(records, run, bootstrap_resamples=200, bootstrap_seed=3)
    return json.loads(json.dumps(report.to_dict()))


def _check_golden(path: Path, actual: str) -> None:
    if os.environ.get("PS_UPDATE_GOLDEN"):
        path.write_text(actual, encoding="utf-8")
    assert path.exists(), f"missing golden {path.name}; run with PS_UPDATE_GOLDEN=1"
    assert actual == path.read_text(encoding="utf-8"), f"{path.name} differs from golden"


def test_eval_results_fixture_is_a_real_ps_eval_report():
    _check_golden(EVAL_RESULTS_PATH, json.dumps(build_eval_results(), indent=2) + "\n")


EVAL_RESULTS = (
    json.loads(EVAL_RESULTS_PATH.read_text()) if EVAL_RESULTS_PATH.exists()
    else build_eval_results()  # first PS_UPDATE_GOLDEN run
)


# --- full report ----------------------------------------------------------------------


def _options(**kw) -> ReportOptions:
    base = {
        "report_id": "golden", "report_date": "2026-10-23", "customer_label": "Golden Co",
        "tool_version": "ps-teardown test", "min_cell_calls": 1,
        "policy": POLICY, "policy_name": "nordic-sovereign.json",
    }
    base.update(kw)
    return ReportOptions(**base)


def _report(files=INPUTS, scan_prompts=False, **kw):
    loaded = load_records([FIXTURES / f for f in files], key_map=KEY_MAP,
                          keep_messages=scan_prompts)
    source = SdkPriceSource(version="test-catalog")
    priced = price_records(loaded.records, source)
    opts = _options(scan_prompts=scan_prompts, **kw)
    return build_report(priced, loaded.sources, opts, source)


GOLDEN_CASES = {
    "policy_cost_only": {},
    "policy_eval_full": {"scan_prompts": True, "eval_results": EVAL_RESULTS},
}


@pytest.mark.parametrize("name", sorted(GOLDEN_CASES))
def test_golden_full_report(name, no_network):
    report = _report(**GOLDEN_CASES[name])
    _check_golden(GOLDEN / f"{name}.md", render_report(report, "markdown"))
    _check_golden(GOLDEN / f"{name}.json", render_report(report, "json"))


@pytest.mark.parametrize("name", sorted(GOLDEN_CASES))
def test_full_report_follows_template(name):
    """Every template section heading is present, in order; §7–8 are filled."""
    md = render_report(_report(**GOLDEN_CASES[name]), "markdown")
    template = (ROOT / "docs/teardown/teardown-report-template.md").read_text()
    body = template.split("\n---\n", 1)[1]
    headings = re.findall(r"^##+ .*$", body, re.MULTILINE)
    positions = [md.find(h + "\n") for h in headings]
    assert all(p >= 0 for p in positions), [h for h, p in zip(headings, positions) if p < 0]
    assert positions == sorted(positions)
    for section in ("## 7.", "## 8."):
        chunk = md.split(section, 1)[1].split("\n## ", 1)[0]
        assert "Not measured" not in chunk


def test_sections_fill_in_and_leave_pending():
    policy_only = _report()
    assert set(policy_only["pending"]) == {"quality_parity", "savings_at_parity"}
    assert "policy_replay" in policy_only and "quality_parity" not in policy_only
    md = render_report(policy_only, "markdown")
    assert md.count("Not measured in this teardown") == 2
    assert "## 7. Sensitivity mix\n\nPolicy: `nordic-sovereign.json`" in md

    full = _report(scan_prompts=True, eval_results=EVAL_RESULTS)
    assert full["pending"] == {}
    assert full["report"]["mode"] == "full"
    md = render_report(full, "markdown")
    assert "Not measured in this teardown" not in md
    assert "pending #13" not in md and "pending #14" not in md


def test_replay_uses_the_report_baseline():
    report = _report()
    pr = report["policy_replay"]
    assert pr["policy_cheapest"]["baseline_spend_compared"] <= report["baseline"]["spend"]
    mix_spend = sum(level["spend"] for level in pr["sensitivity_mix"]["levels"])
    assert mix_spend == pytest.approx(report["baseline"]["spend"])
    assert pr["calls"] == report["baseline"]["total_calls"]
    sov = report["summary"]["sovereign"]
    assert sov["spend"] == pr["sensitivity_mix"]["sovereign"]["spend"]


def test_pii_escalation_and_full_mode():
    cost_only = _report()["policy_replay"]["sensitivity_mix"]
    # A recorded national_id escalates contract-review to restricted without prompt text.
    assert cost_only["pii_categories"]["national_id"] == 1
    full = _report(scan_prompts=True)
    assert full["report"]["prompt_text"]["scanned_locally"] is True
    assert "local PII scan" in full["report"]["sensitivity_input"]


def test_cache_uses_effective_levels_under_a_policy():
    plain = _report(policy=None, policy_name="")
    with_policy = _report()
    assert plain["cache"]["levels_from"] == "declared"
    assert with_policy["cache"]["levels_from"] == "route_policy"
    assert (with_policy["cache"]["excluded_cache_off_calls"]
            >= plain["cache"]["excluded_cache_off_calls"])


def test_arbitrage_checks_the_cheaper_host_against_the_data_policy():
    table = {
        ("anthropic", "claude-3-5-haiku-20241022"): (0.0008, 0.004),
        ("bedrock", "anthropic.claude-3-5-haiku-20241022-v1:0"): (0.0004, 0.002),
        ("openai", "gpt-4o"): (0.0025, 0.010),
    }
    source = SdkPriceSource(pricing_table=table, version="t")
    loaded = load_records([FIXTURES / "ps_events.jsonl"], key_map=KEY_MAP)
    priced = price_records(loaded.records, source)
    plain = build_report(priced, loaded.sources, ReportOptions(), source)["arbitrage"]
    checked = build_report(priced, loaded.sources, ReportOptions(policy=POLICY), source)
    arb = checked["arbitrage"]
    assert plain["compliance_checked"] is False and plain["rows"][0]["calls"] == 2
    # Bedrock (US, CLOUD Act) is fine for the internal call, not the confidential one.
    assert arb["compliance_checked"] is True
    assert arb["rows"][0]["calls"] == 1 and arb["calls_blocked_by_data_policy"] == 1
    assert arb["saving"] < plain["saving"]
    md = render_report(checked, "markdown")
    assert "does not check the cheaper host" not in md


def test_arbitrage_host_with_unverified_facts_fails_closed():
    from prompt_shields.teardown.policy import arbitrage_check, host_facts

    assert host_facts(POLICY, "nohost", "m") == {"provider": "nohost", "model": "m"}
    for level, ok in (("public", True), ("internal", False), ("confidential", False)):
        assert arbitrage_check(POLICY, lambda _p, lv=level: lv)(None, "nohost", "m") is ok


def test_policy_at_parity_only_routes_to_models_at_parity():
    report = _report(eval_results=EVAL_RESULTS)
    sap = report["savings_at_parity"]
    rows = {u["use_case"]: u for u in sap["use_cases"]}
    # support-bot: only gpt-4o-mini reached parity (small-eu failed 1 in 4).
    assert rows["support-bot"]["models_at_parity"] == ["gpt-4o-mini"]
    assert rows["support-bot"]["to_model"] in ("gpt-4o-mini", rows["support-bot"]["from_model"])
    # No eval set: no saving, never assumed at parity (M§7.4).
    assert rows["ticket-triage"]["eval"] == "no_eval"
    assert rows["ticket-triage"]["at_parity"] == rows["ticket-triage"]["baseline"]
    # contract-review is confidential: gpt-4o-mini was never tried (policy), small-eu passed.
    assert rows["contract-review"]["models_at_parity"] == ["mistral-small-eu"]
    cells = {(c["use_case"], c["candidate"]): c for c in report["quality_parity"]["cells"]}
    assert cells[("contract-review", "openai:gpt-4o-mini")]["n"] == 0
    assert report["quality_parity"]["eval_policy_is_route_policy"] is True
    judges = report["quality_parity"]["judges"]
    assert judges and "jurisdiction EEA per the route policy" in judges[0]
    assert sap["policy_at_parity"]["savings_pct"] is not None
    ci = sap["saving_ci"]
    assert ci is not None and ci[0] <= ci[1]
    assert report["summary"]["gate1"]["reading"] in ("met", "not met")
    # Conservative uses only Wilson-lower-bound cells: never more saving.
    assert (sap["conservative"]["savings"] <= sap["policy_at_parity"]["savings"] + 1e-9)


def test_eval_from_another_policy_is_flagged():
    other = json.loads(json.dumps(EVAL_RESULTS))
    other["methodology"]["policy"] = {"allow_all": True}
    report = _report(eval_results=other)
    assert report["quality_parity"]["eval_policy_is_route_policy"] is False
    assert any("not constrained by this route policy" in c for c in report["trust"]["caveats"])


def test_eval_results_need_a_policy():
    with pytest.raises(ValueError):
        _report(policy=None, eval_results=EVAL_RESULTS)


def test_no_prompt_text_or_hashes_in_full_report(no_network):
    report = _report(scan_prompts=True, eval_results=EVAL_RESULTS)
    for fmt in ("markdown", "html", "json"):
        text = render_report(report, fmt)
        assert SECRET_TEXT not in text
        assert "aaaa0000000000" not in text  # prompt_hash values
        assert "owner@example.com" not in text


def test_aggregated_rows_are_weighted_by_calls():
    report = _report(files=["openai_usage.json", "anthropic_usage.json"])
    pr = report["policy_replay"]
    assert pr["calls"] == report["baseline"]["total_calls"]
    assert pr["calls_unknown_rows"] == report["baseline"]["total_calls_unknown_rows"] > 0
    mix_spend = sum(level["spend"] for level in pr["sensitivity_mix"]["levels"])
    assert mix_spend == pytest.approx(report["baseline"]["spend"])


# --- CLI --------------------------------------------------------------------------------


def test_cli_full_pipeline(tmp_path, no_network, capsys):
    eval_path = tmp_path / "eval.json"
    eval_path.write_text(json.dumps(EVAL_RESULTS))
    code = teardown_main([
        "run", *(str(FIXTURES / f) for f in INPUTS), "--key-map", str(FIXTURES / "key_map.json"),
        "--policy", str(POLICY_PATH), "--scan-prompts", "--eval-results", str(eval_path),
        "--out-dir", str(tmp_path / "out"), "--report-date", "2026-10-23",
    ])
    assert code == 0
    report = json.loads((tmp_path / "out" / "teardown-report.json").read_text())
    assert report["pending"] == {}
    md = (tmp_path / "out" / "teardown-report.md").read_text()
    assert "## 10. Savings at parity" in md and SECRET_TEXT not in md


def test_cli_eval_results_text_is_never_echoed(tmp_path, no_network, capsys):
    # The eval file is untrusted: free text in a numeric field must not reach
    # the report, and a bad parameter must fail without quoting the file.
    doc = json.loads(json.dumps(EVAL_RESULTS))
    meth = doc["methodology"]
    for key in ("per_stratum", "spend_usd", "calls", "sample_seed"):
        meth[key] = SECRET_TEXT
    meth["strata"][0]["population"] = SECRET_TEXT
    meth["scoring"] = [SECRET_TEXT]
    doc["cells"][0]["parity"] = SECRET_TEXT
    doc["saving_pct"] = SECRET_TEXT
    doc["notes"] = SECRET_TEXT
    eval_path = tmp_path / "eval.json"
    eval_path.write_text(json.dumps(doc))
    args = ["run", str(FIXTURES / "ps_events.jsonl"), "--policy", str(POLICY_PATH),
            "--eval-results", str(eval_path), "--out-dir", str(tmp_path / "out")]
    assert teardown_main(args) == 0
    for out in (tmp_path / "out").iterdir():
        assert SECRET_TEXT not in out.read_text()
    doc["params"]["min_parity"] = SECRET_TEXT
    eval_path.write_text(json.dumps(doc))
    with pytest.raises(SystemExit):
        teardown_main(args)
    err = capsys.readouterr().err
    assert "params.min_parity" in err and SECRET_TEXT not in err


@pytest.mark.parametrize("extra,message", [
    (["--eval-results", "x.json"], "need --policy"),
    (["--scan-prompts"], "need --policy"),
])
def test_cli_flags_need_a_policy(extra, message, capsys):
    with pytest.raises(SystemExit):
        teardown_main(["run", str(FIXTURES / "ps_events.jsonl"), "--stdout", "json", *extra])
    assert message in capsys.readouterr().err


def test_cli_rejects_bad_policy_and_eval_files(tmp_path, capsys):
    bad_policy = tmp_path / "p.json"
    bad_policy.write_text(json.dumps({**POLICY, "strategy": "cost"}))
    with pytest.raises(SystemExit):
        teardown_main(["run", str(FIXTURES / "ps_events.jsonl"), "--policy", str(bad_policy)])
    assert "heuristic" in capsys.readouterr().err
    bad_eval = tmp_path / "e.json"
    bad_eval.write_text(json.dumps({"cells": "nope"}))
    with pytest.raises(SystemExit):
        teardown_main(["run", str(FIXTURES / "ps_events.jsonl"), "--policy", str(POLICY_PATH),
                       "--eval-results", str(bad_eval)])
    assert "--eval-results" in capsys.readouterr().err


# --- one record type ----------------------------------------------------------------------


def test_usage_record_is_a_teardown_record():
    record = UsageRecord(vendor="openai", served_model="gpt-4o", tokens_in=10, tokens_out=5)
    assert isinstance(record, TeardownRecord)
    assert record.provider == record.vendor == "openai"
    from_map = TeardownRecord.from_mapping({"vendor": "openai", "model": "gpt-4o",
                                            "detected_pii_types": "IBAN, email"})
    assert (from_map.provider, from_map.requested_model) == ("openai", "gpt-4o")
    assert from_map.detected_pii_types == ("iban", "email")
    assert UsageRecord.from_mapping({"vendor": "openai"}).provider == "openai"


def test_usage_fields_cover_every_pricing_dimension():
    from prompt_shields.pricing import PRICING_DIMENSIONS
    from prompt_shields.teardown.records import _USAGE_FIELDS

    assert set(PRICING_DIMENSIONS) <= set(_USAGE_FIELDS)
    assert set(_USAGE_FIELDS) <= set(TeardownRecord.__dataclass_fields__)


def test_loaded_records_replay_directly():
    loaded = load_records([FIXTURES / "ps_events.jsonl"], key_map=KEY_MAP)
    report = replay(loaded.records, POLICY)
    assert report["calls"] == sum(r.calls or 0 for r in loaded.records)
    call = replay_call(loaded.records[0], POLICY)
    assert call.use_case == "support-bot"
    # A dated snapshot finds its policy tier (gpt-4o, US): internal allows the US.
    assert call.baseline_jurisdiction == "US" and call.baseline_compliant is True


# --- ps-eval under a route policy (#14 gaps that needed #13) -------------------------------


def test_eval_policy_from_route_policy():
    policy = EvalPolicy.from_route_policy(POLICY)
    assert policy.allows(MINI, "internal")
    assert not policy.allows(MINI, "confidential")  # US, CLOUD Act exposure
    assert policy.allows(SMALL_EU, "confidential")
    assert policy.allows(SMALL_EU, "restricted")
    assert not policy.allows(Candidate("scaleway", "scaleway-small"), "restricted")  # providers
    assert not policy.allows(Candidate("openai", "o3"), "public")  # not a policy tier
    assert policy.describe()["route_policy"]["policy_version"] == "nordic-sovereign-1"


def test_eval_level_escalates_on_pii_in_the_prompt():
    policy = EvalPolicy.from_route_policy(POLICY)
    record = EvalRecord.from_dict({
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "fødselsnummer 01818510078, please summarise"}]}],
        "data_classification": "internal", "use_case": "claim-summary",
    })
    assert record.sensitivity() == "internal"
    assert policy.level(record) == "restricted"
    recorded = EvalRecord.from_dict({"data_classification": "Public",
                                     "metadata": {"detected_pii_types": ["IBAN"]}})
    assert policy.level(recorded) == "confidential"
    sample = stratified_sample([record], per_stratum=5, seed=0, level_of=policy.level)
    assert sample.stratum(record)[1] == "restricted"


def test_judge_is_constrained_by_the_route_policy():
    """A judge the data policy does not allow at a call's level is never called."""
    policy = EvalPolicy.from_route_policy(POLICY)
    record = EvalRecord.from_dict({
        "id": "r1", "vendor": "openai", "served_model": "gpt-4o", "expected": "x",
        "messages": [{"role": "user", "content": "q"}], "data_classification": "confidential",
    })
    sample = stratified_sample([record], per_stratum=5, seed=0, level_of=policy.level)
    us_judge = Candidate("openai", "gpt-4o")
    executor = FakeExecutor(lambda c, m: "VERDICT: A")
    config = EvalConfig(candidates=[SMALL_EU], scorers={"*": PairwiseJudge(us_judge)},
                        policy=policy)
    run = run_eval(sample, executor, config)
    assert [c.label for c, _ in executor.calls] == [SMALL_EU.label]
    assert run.judge_calls == 0 and run.trials[0].status == "unscored"


def test_ps_eval_cli_route_policy(tmp_path, capsys):
    records = tmp_path / "records.jsonl"
    records.write_text("\n".join(json.dumps({
        "id": f"r{i}", "vendor": "openai", "served_model": "gpt-4o",
        "prompt_text": "my email is jens.hansen@example.dk" if i == 0 else f"hello {i}",
        "response_text": "ok", "use_case": "faq", "data_classification": "public",
    }) for i in range(3)))
    code = eval_main(["--records", str(records), "--candidate", MINI.label,
                      "--route-policy", str(POLICY_PATH)])
    assert code == 0
    plan = json.loads(capsys.readouterr().out)
    levels = {s["sensitivity"]: s["population"] for s in plan["strata"]}
    assert levels == {"public": 2, "internal": 1}  # email escalates to internal


# --- learning tuples with replay and eval quality (#42) ---------------------------------


def _tuples(eval_results=EVAL_RESULTS, with_replay=True):
    loaded = load_records([FIXTURES / f for f in INPUTS], key_map=KEY_MAP)
    source = SdkPriceSource(version="test-catalog")
    priced = price_records(loaded.records, source)
    replays = replay_priced(priced, POLICY, source) if with_replay else None
    ev = read_eval_results(eval_results) if eval_results is not None else None
    return list(learning_tuples(priced, eval_results=ev, policy=POLICY, replays=replays))


def test_learning_tuples_carry_eval_quality_and_the_replayed_route():
    tuples = _tuples()
    # The logged model was evaluated for its use case: its measured parity.
    faq = next(t for t in tuples if t["use_case"] == "faq")
    assert faq["model"] == "gpt-4o-mini"
    assert (faq["quality_score"], faq["quality_source"], faq["quality_samples"]) == (
        1.0, "eval_parity", 80)
    assert faq["replay"]["outcome"] == "unchanged"
    # Not evaluated: never estimated...
    bot = next(t for t in tuples if t["use_case"] == "support-bot" and t["model"] == "gpt-4o")
    assert bot["quality_score"] is None and bot["quality_source"] == "not_measured"
    # ...but the replayed arm was: the policy routes the call to mini, at 0.975 parity.
    rp = bot["replay"]
    assert (rp["provider"], rp["model"], rp["group"], rp["outcome"]) == (
        "openai", "gpt-4o-mini", "cheap", "routed")
    assert rp["quality_score"] == 0.975 and rp["quality_source"] == "eval_parity"
    assert rp["cost_usd"] is not None and rp["cost_usd"] < bot["cost_usd"]
    assert bot["sensitivity_level"] == "internal"
    # One tuple per logged call: the replayed arm is not a second row.
    assert len(tuples) == 24
    assert SECRET_TEXT not in json.dumps(tuples)


def test_learning_tuples_without_eval_or_policy_stay_unscored():
    for t in _tuples(eval_results=None):
        assert t["quality_score"] is None and t["replay"]["quality_score"] is None
    assert all("replay" not in t for t in _tuples(eval_results=None, with_replay=False))


def test_eval_cell_for_another_vendor_does_not_score_the_call():
    doc = json.loads(json.dumps(EVAL_RESULTS))
    for c in doc["cells"]:
        c["candidate"] = c["candidate"].replace("openai:", "azure:")
    faq = next(t for t in _tuples(eval_results=doc) if t["use_case"] == "faq")
    assert faq["quality_score"] is None


def test_eval_candidate_label_splits_at_the_first_colon():
    from prompt_shields.teardown.tuples import _Quality

    ev = read_eval_results({"cells": [{
        "use_case": "faq", "candidate": "bedrock:anthropic.claude-3-haiku-20240307-v1:0",
        "n": 10, "passed": 10, "parity": 1.0}]})
    q = _Quality(ev)
    assert q.score("faq", "bedrock", "anthropic.claude-3-haiku-20240307-v1:0")[
        "quality_score"] == 1.0
    assert q.score("faq", "openai", "anthropic.claude-3-haiku-20240307-v1:0")[
        "quality_score"] is None


def test_learning_tuples_need_matching_replays():
    with pytest.raises(ValueError):
        list(learning_tuples([], replays=[object()], policy=POLICY))


def test_cli_learning_tuples_with_policy_and_eval(tmp_path, no_network, capsys):
    path = tmp_path / "tuples.jsonl"
    code = teardown_main([
        "run", *(str(FIXTURES / f) for f in INPUTS), "--key-map", str(FIXTURES / "key_map.json"),
        "--policy", str(POLICY_PATH), "--scan-prompts", "--eval-results", str(EVAL_RESULTS_PATH),
        "--stdout", "json", "--learning-tuples", str(path),
    ])
    assert code == 0
    capsys.readouterr()
    text = path.read_text()
    tuples = [json.loads(line) for line in text.splitlines()]
    assert len(tuples) == 24 and all("replay" in t for t in tuples)
    assert any(t["quality_source"] == "eval_parity" for t in tuples)
    assert SECRET_TEXT not in text and "owner@example.com" not in text
    assert {"messages", "prompt_hash", "session_id", "user_id"}.isdisjoint(
        k for t in tuples for k in t)
