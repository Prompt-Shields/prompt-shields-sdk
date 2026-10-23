"""Eval harness (#14): sampling, re-execution, scoring, parity and savings.

No test makes a network call: executors are fakes, recordings, or
ProviderExecutor around fake SDK client objects.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from prompt_shields.evals import (
    Candidate,
    Completion,
    EvalConfig,
    EvalPolicy,
    EvalRecord,
    ExactMatch,
    FakeExecutor,
    PairwiseJudge,
    PromptReexecutionNotAllowed,
    ProviderExecutor,
    RateLimiter,
    RecordedExecutor,
    SchemaValid,
    SimilarityMatch,
    SpendCap,
    build_report,
    cli,
    load_records,
    recording,
    run_eval,
    stratified_sample,
    wilson_interval,
)
from prompt_shields.evals.budget import worst_case_input_tokens
from prompt_shields.evals.harness import (
    SCORED,
    SKIPPED_BUDGET,
    SKIPPED_POLICY,
    SKIPPED_UNPRICED,
    UNSCORED,
)
from prompt_shields.evals.scoring import judge_messages, parse_verdict

MINI = Candidate("openai", "gpt-4o-mini")
HAIKU = Candidate("anthropic", "claude-3-5-haiku-20241022")
JUDGE = Candidate("anthropic", "claude-sonnet-4-20250514")


def _record(i: int, use_case: str = "support", **overrides) -> EvalRecord:
    data = {
        "id": f"{use_case}-{i}",
        "vendor": "openai",
        "served_model": "gpt-4o",
        "prompt_text": f"question {i} about {use_case}",
        "response_text": f"answer {i}",
        "use_case": use_case,
        "data_classification": "internal",
        "tokens_in": 1000,
        "tokens_out": 200,
        "latency_ms": 900,
    }
    data.update(overrides)
    return EvalRecord.from_dict(data)


def _population(n: int = 40, use_cases=("support", "extract")) -> list[EvalRecord]:
    return [_record(i, uc) for uc in use_cases for i in range(n)]


def _answer_index(messages) -> str:
    return messages[-1]["content"].split()[1]


def _echo(candidate, messages):
    return f"answer {_answer_index(messages)}"


def _config(**overrides) -> EvalConfig:
    base = {
        "candidates": [MINI],
        "scorers": {"*": ExactMatch()},
        "policy": EvalPolicy.allow_all(),
    }
    base.update(overrides)
    return EvalConfig(**base)


# --- records -------------------------------------------------------------------


def test_record_from_sdk_event_and_generic_jsonl(tmp_path):
    path = tmp_path / "logs.jsonl"
    path.write_text("\n".join([
        json.dumps({"event_id": "e1", "vendor": "openai", "requested_model": "auto",
                    "served_model": "gpt-4o-mini", "tokens_in": 10, "tokens_out": 5,
                    "use_case_name": "triage", "data_classification": "public"}),
        "",
        json.dumps({"messages": [{"role": "user", "content": "hi"}],
                    "model": "claude-3-5-haiku-20241022", "provider": "anthropic",
                    "usage": {"input_tokens": 7, "output_tokens": 3},
                    "metadata": {"use_case": "chat", "data_classification": "restricted"},
                    "response": "hello"}),
    ]))
    first, second = load_records(path)
    assert first.record_id == "e1"
    assert first.model == "gpt-4o-mini"  # served model wins
    assert first.use_case == "triage"
    assert first.messages is None and not first.has_prompt
    assert first.sensitivity() == "public"
    assert second.record_id == "line-3"
    assert (second.vendor, second.tokens_in, second.tokens_out) == ("anthropic", 7, 3)
    assert second.use_case == "chat"
    assert second.sensitivity() == "restricted"
    assert second.reference == "hello"


def test_record_defaults_and_repricing():
    record = EvalRecord.from_dict({"prompt_text": "x", "vendor": "openai",
                                   "model": "gpt-4o", "tokens_in": 1000,
                                   "tokens_out": 1000, "cost": 99.0})
    assert record.use_case == "(unattributed)"
    assert record.sensitivity() == "confidential"  # M§5.1 default
    # Re-priced from tokens at the served model, not the logged cost (M§4.2).
    assert record.baseline_cost() == pytest.approx(0.0125)
    unknown = EvalRecord.from_dict({"model": "custom-ft", "vendor": "openai",
                                    "tokens_in": 1, "tokens_out": 1, "cost": 0.5})
    assert unknown.baseline_cost() == 0.5
    assert EvalRecord.from_dict({"expected": "gold", "response": "logged"}).reference == "gold"


# --- sampling ------------------------------------------------------------------


def test_stratified_sample_is_deterministic_and_capped():
    population = _population(50) + [_record(i, "support", prompt_text=None) for i in
                                     range(50, 55)]
    a = stratified_sample(population, per_stratum=10, seed=7)
    b = stratified_sample(list(reversed(population)), per_stratum=10, seed=7)
    assert [r.record_id for r in a.records] == [r.record_id for r in b.records]
    c = stratified_sample(population, per_stratum=10, seed=8)
    assert [r.record_id for r in a.records] != [r.record_id for r in c.records]
    support = a.strata[("support", "internal", "medium")]
    assert (support.population, support.eligible, support.sampled) == (55, 50, 10)
    assert all(r.has_prompt for r in a.records)


def test_sample_strata_by_sensitivity_and_complexity():
    population = [
        _record(1, data_classification="restricted"),
        _record(2, tokens_in=50),
        _record(3, tokens_in=20_000),
        _record(4, data_classification=None),
    ]
    sample = stratified_sample(population, per_stratum=5, seed=0)
    assert set(sample.strata) == {
        ("support", "restricted", "medium"),
        ("support", "internal", "short"),
        ("support", "internal", "long"),
        ("support", "confidential", "medium"),
    }
    only = stratified_sample(_population(5), per_stratum=5, seed=0,
                             use_cases=["extract"])
    assert {r.use_case for r in only.records} == {"extract"}
    assert only.strata[("support", "internal", "medium")].population == 5


# --- statistics ------------------------------------------------------------------


def test_wilson_matches_the_methodology_thresholds():
    assert wilson_interval(73, 73)[0] == pytest.approx(0.9500, abs=1e-4)
    assert wilson_interval(72, 72)[0] == pytest.approx(0.9493, abs=1e-4)
    low, high = wilson_interval(8, 10)
    assert low == pytest.approx(0.4902, abs=1e-4)
    assert high == pytest.approx(0.9433, abs=1e-4)
    assert wilson_interval(0, 0) is None


# --- opt-in, spend cap, rate limit ------------------------------------------------


class _FakeOpenAI:
    """Stands in for openai.OpenAI; records calls, never touches the network."""

    def __init__(self, text="answer 1", prompt_tokens=1000, completion_tokens=200):
        self.calls = []
        self._text = text
        self._usage = (prompt_tokens, completion_tokens)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        text = self._text(kwargs) if callable(self._text) else self._text
        return SimpleNamespace(
            model=kwargs["model"] + "-2024-07-18",
            choices=[SimpleNamespace(message=SimpleNamespace(content=text,
                                                             tool_calls=None))],
            usage=SimpleNamespace(prompt_tokens=self._usage[0],
                                  completion_tokens=self._usage[1],
                                  prompt_tokens_details=None,
                                  completion_tokens_details=None),
            service_tier=None,
        )


class _FakeAnthropic:
    def __init__(self, text="VERDICT: TIE"):
        self.calls = []
        self._text = text
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        text = self._text(kwargs) if callable(self._text) else self._text
        return SimpleNamespace(
            model=kwargs["model"],
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=500, output_tokens=5,
                                  cache_read_input_tokens=None,
                                  cache_creation_input_tokens=None,
                                  cache_creation=None, server_tool_use=None,
                                  service_tier=None),
        )


def test_live_executor_needs_explicit_opt_in_and_a_cap():
    client = _FakeOpenAI()
    executor = ProviderExecutor({"openai": client})
    sample = stratified_sample(_population(3), per_stratum=3, seed=0)
    with pytest.raises(PromptReexecutionNotAllowed):
        run_eval(sample, executor, _config(max_spend_usd=10))
    with pytest.raises(ValueError, match="max_spend_usd"):
        run_eval(sample, executor, _config(allow_prompt_reexecution=True))
    # A live judge alone also needs the opt-in.
    with pytest.raises(PromptReexecutionNotAllowed):
        run_eval(sample, FakeExecutor(_echo), _config(max_spend_usd=1),
                 judge_executor=executor)
    assert client.calls == []


def test_provider_executor_reads_usage_text_and_cost():
    client = _FakeOpenAI(text=lambda kw: _echo(None, kw["messages"]))
    executor = ProviderExecutor({"openai": client})
    sample = stratified_sample(_population(2, ("support",)), per_stratum=2, seed=0)
    run = run_eval(sample, executor, _config(allow_prompt_reexecution=True,
                                             max_spend_usd=1.0, max_tokens=300))
    assert [t.status for t in run.trials] == [SCORED, SCORED]
    assert all(t.passed for t in run.trials)
    # 1000 in + 200 out at gpt-4o-mini list price.
    assert run.trials[0].candidate_cost == pytest.approx(0.00027)
    assert run.spend_usd == pytest.approx(0.00054)
    assert client.calls[0]["max_tokens"] == 300
    assert client.calls[0]["temperature"] == 0.0


def test_provider_executor_anthropic_moves_system_prompt():
    client = _FakeAnthropic(text="hello")
    executor = ProviderExecutor({"anthropic": client})
    completion = executor.complete(
        HAIKU, [{"role": "system", "content": "be brief"},
                {"role": "user", "content": "hi"}],
        max_tokens=50, temperature=None,
    )
    assert completion.text == "hello"
    assert completion.tokens_in == 500 and completion.tokens_out == 5
    assert completion.cost == pytest.approx(0.00042)
    assert client.calls[0]["system"] == "be brief"
    assert client.calls[0]["messages"] == [{"role": "user", "content": "hi"}]
    assert "temperature" not in client.calls[0]
    missing = executor.complete(MINI, [{"role": "user", "content": "x"}],
                                max_tokens=5, temperature=0)
    assert missing.error and missing.text is None


def test_spend_cap_stops_calls_before_they_overspend():
    client = _FakeOpenAI(text=lambda kw: _echo(None, kw["messages"]))
    executor = ProviderExecutor({"openai": client})
    sample = stratified_sample(_population(10, ("support",)), per_stratum=10, seed=0)
    # Worst case per call: 8 input tokens + 1024 output at gpt-4o-mini ≈ USD 0.000616;
    # actual cost booked: USD 0.00027. A USD 0.002 cap fits 6 calls.
    run = run_eval(sample, executor, _config(allow_prompt_reexecution=True,
                                             max_spend_usd=0.002))
    statuses = [t.status for t in run.trials]
    assert statuses.count(SCORED) == len(client.calls) == 6
    assert statuses.count(SKIPPED_BUDGET) == 4
    assert run.spend_usd <= 0.002


def test_unpriced_candidate_is_never_called_live():
    client = _FakeOpenAI()
    executor = ProviderExecutor({"openai": client})
    sample = stratified_sample(_population(2, ("support",)), per_stratum=2, seed=0)
    run = run_eval(sample, executor, _config(
        candidates=[Candidate("openai", "some-unlisted-model")],
        allow_prompt_reexecution=True, max_spend_usd=100,
    ))
    assert {t.status for t in run.trials} == {SKIPPED_UNPRICED}
    assert client.calls == []


def test_rate_limiter_spaces_calls_without_real_sleeping():
    now = [0.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(120, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        limiter.acquire()
    assert slept == [0.5, 0.5]
    RateLimiter(None).acquire()  # no limit: returns at once
    with pytest.raises(ValueError):
        RateLimiter(0)


def test_rate_limit_applies_to_runs():
    now = [0.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    sample = stratified_sample(_population(3, ("support",)), per_stratum=3, seed=0)
    run_eval(sample, FakeExecutor(_echo), _config(requests_per_minute=60),
             clock=lambda: now[0], sleep=sleep)
    assert slept == [1.0, 1.0]


def test_spend_cap_accounting():
    cap = SpendCap(1.0)
    assert cap.allows(0.6)
    cap.book(None, 0.6)  # unknown actual cost books the worst case
    assert not cap.allows(0.5)
    assert cap.refused == 1
    assert cap.remaining == pytest.approx(0.4)
    with pytest.raises(ValueError):
        SpendCap(-1)
    # NaN compares False with everything and would allow every call.
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            SpendCap(bad)
        with pytest.raises(ValueError):
            RateLimiter(bad)


def test_worst_case_input_tokens_is_an_upper_bound():
    # ~4 characters per token undercounts CJK text by about 4x.
    cjk = [{"role": "user", "content": "漢" * 1000}]
    assert worst_case_input_tokens(cjk) >= 3000
    image = [{"role": "user", "content": [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
    ]}]
    assert worst_case_input_tokens(image) is None


def test_prompt_with_unbounded_parts_is_never_called_live():
    client = _FakeOpenAI()
    executor = ProviderExecutor({"openai": client})
    record = _record(1, prompt_text=None, messages=[{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
    ]}])
    sample = stratified_sample([record], per_stratum=1, seed=0)
    run = run_eval(sample, executor, _config(allow_prompt_reexecution=True,
                                             max_spend_usd=100))
    assert [t.status for t in run.trials] == [SKIPPED_UNPRICED]
    assert client.calls == []


def test_unknown_sensitivity_label_is_restricted():
    assert _record(1, data_classification="Secret").sensitivity() == "restricted"
    assert _record(1, data_classification=" Public ").sensitivity() == "public"
    assert _record(1, data_classification=None).sensitivity() == "confidential"


# --- policy ------------------------------------------------------------------------


def test_policy_blocks_candidates_and_judges_per_sensitivity():
    policy = EvalPolicy.from_dict({
        "allowed_models": {
            "internal": ["openai:gpt-4o-mini", "claude-sonnet-4-20250514"],
            "restricted": [],
        },
    })
    population = [_record(1), _record(2, data_classification="restricted"),
                  _record(3, data_classification=None)]
    sample = stratified_sample(population, per_stratum=5, seed=0)
    judge_executor = FakeExecutor(lambda c, m: "VERDICT: TIE")
    run = run_eval(sample, FakeExecutor(_echo),
                   _config(candidates=[MINI, HAIKU],
                           scorers={"*": PairwiseJudge(JUDGE)}, policy=policy),
                   judge_executor=judge_executor)
    by_key = {(t.record_id, t.candidate): t.status for t in run.trials}
    assert by_key[("support-1", MINI.label)] == SCORED
    assert by_key[("support-1", HAIKU.label)] == SKIPPED_POLICY
    assert by_key[("support-2", MINI.label)] == SKIPPED_POLICY  # restricted
    assert by_key[("support-3", MINI.label)] == SKIPPED_POLICY  # confidential default
    # The judge saw only the internal prompt.
    assert len(judge_executor.calls) == 2

    judge_blocked = EvalPolicy.from_dict({"allowed_models": {"internal": ["*"]}})
    blocked = EvalPolicy.from_dict({"allowed_models": {"internal": [MINI.label]}})
    run = run_eval(stratified_sample([_record(1)], per_stratum=1, seed=0),
                   FakeExecutor(_echo),
                   _config(scorers={"*": PairwiseJudge(JUDGE)}, policy=blocked))
    assert run.trials[0].status == UNSCORED
    assert run.trials[0].detail == "judge refused"
    assert judge_blocked.allows(HAIKU, "internal")
    assert not judge_blocked.allows(HAIKU, "public")


def test_policy_from_dict_validation():
    assert EvalPolicy.from_dict({"allow_all": True}).allows(MINI, "restricted")
    with pytest.raises(TypeError):
        EvalPolicy.from_dict({})
    with pytest.raises(ValueError):
        EvalPolicy.from_dict({"allowed_models": {"secret": []}})
    with pytest.raises(ValueError):
        EvalPolicy.from_dict({"allow_all": True, "unclassified_default": "x"})


# --- scoring -----------------------------------------------------------------------


def _no_judge(messages):
    raise AssertionError("no judge expected")


def test_exact_schema_and_similarity_scorers():
    record = _record(1)
    assert ExactMatch().score(record, "a  b", " a b ", _no_judge).passed
    assert not ExactMatch().score(record, "A b", "a b", _no_judge).passed
    assert ExactMatch(casefold=True).score(record, "A b", "a b", _no_judge).passed
    assert ExactMatch().score(record, None, "x", _no_judge).passed is None

    schema = {"type": "object", "required": ["id"],
              "properties": {"id": {"type": "integer"}}}
    scorer = SchemaValid(schema)
    assert scorer.score(record, None, '{"id": 3}', _no_judge).passed
    assert scorer.score(record, None, '```json\n{"id": 3}\n```', _no_judge).passed
    bad = scorer.score(record, None, '{"id": "3"}', _no_judge)
    assert not bad.passed and bad.detail.startswith("schema:")
    assert not SchemaValid().score(record, None, "not json", _no_judge).passed

    similar = SimilarityMatch(0.8).score(record, "The answer is 42.",
                                         "the answer is 42", _no_judge)
    assert similar.passed and similar.value > 0.9
    assert not SimilarityMatch(0.8).score(record, "yes", "no way", _no_judge).passed


def _judge_answering(*verdicts):
    replies = iter(verdicts)
    seen = []

    def call(messages):
        seen.append(messages)
        return Completion(text=f"VERDICT: {next(replies)}")

    return call, seen


def test_pairwise_judge_runs_both_orders_and_ties_disagreements():
    record = _record(1)
    judge = PairwiseJudge(JUDGE)

    def candidate_first(messages):
        body = messages[1]["content"]
        return body.index("CAND") < body.index("REF")

    # The judge prefers the candidate in both orders: win.
    def prefers_candidate(messages):
        return Completion(text="VERDICT: A" if candidate_first(messages) else "VERDICT: B")

    assert judge.score(record, "REF", "CAND", prefers_candidate).outcome == "win"

    # Always picks position A: the orders disagree, which counts as a tie.
    call, seen = _judge_answering("A", "A")
    score = judge.score(record, "REF", "CAND", call)
    assert score.outcome == "tie" and score.passed
    assert len(seen) == 2
    assert {candidate_first(m) for m in seen} == {True, False}

    def prefers_reference(messages):
        return Completion(text="VERDICT: B" if candidate_first(messages) else "VERDICT: A")

    loss = judge.score(record, "REF", "CAND", prefers_reference)
    assert loss.outcome == "loss" and loss.passed is False

    assert judge.score(record, "REF", "CAND", lambda m: None).passed is None
    garbled = judge.score(record, "REF", "CAND", lambda m: Completion(text="dunno"))
    assert garbled.passed is None


def test_judge_prompt_and_verdict_parsing():
    messages = judge_messages([{"role": "user", "content": "Q"}], "one", "two")
    assert messages[0]["role"] == "system"
    assert "<answer_a>\none\n</answer_a>" in messages[1]["content"]
    assert parse_verdict("thinking... VERDICT: b") == "B"
    assert parse_verdict("VERDICT: A\nVERDICT: TIE") == "TIE"
    assert parse_verdict(None) is None


def test_same_family_judge_is_noted():
    sample = stratified_sample([_record(1)], per_stratum=1, seed=0)
    run = run_eval(sample, FakeExecutor(_echo),
                   _config(candidates=[HAIKU], scorers={"*": PairwiseJudge(JUDGE)}),
                   judge_executor=FakeExecutor(lambda c, m: "VERDICT: TIE"))
    assert any("same family" in note for note in run.notes)


def test_rerun_baseline_when_no_logged_answer():
    population = [_record(i, response_text=None) for i in range(3)]
    sample = stratified_sample(population, per_stratum=3, seed=0)
    executor = FakeExecutor(_echo)
    no_rerun = run_eval(sample, executor, _config())
    assert {t.status for t in no_rerun.trials} == {UNSCORED}
    run = run_eval(sample, executor, _config(rerun_baseline=True))
    assert run.baseline_reruns == 3
    assert {t.status for t in run.trials} == {SCORED}


# --- report ------------------------------------------------------------------------


def _mixed_responder(fail_every: dict[str, int]):
    """Echo the right answer, except every Nth prompt for some candidates."""

    def respond(candidate, messages):
        index = int(_answer_index(messages))
        every = fail_every.get(candidate.label)
        if every and index % every == 0:
            return "wrong"
        return f"answer {index}"

    return respond


def test_report_parity_cells_and_savings_at_parity():
    population = _population(40) + [_record(i, "rare") for i in range(5)]
    # 'rare' gets no eval set: only support and extract are sampled.
    sample = stratified_sample(population, per_stratum=40, seed=1,
                               use_cases=["support", "extract"])
    # MINI always right; HAIKU wrong on every 4th prompt (75% parity).
    executor = FakeExecutor(_mixed_responder({HAIKU.label: 4}))
    run = run_eval(sample, executor, _config(candidates=[MINI, HAIKU]))
    report = build_report(population, run, bootstrap_resamples=200, bootstrap_seed=3)

    cells = {(c.use_case, c.candidate): c for c in report.cells}
    mini = cells[("support", MINI.label)]
    assert (mini.n, mini.passed, mini.status) == (40, 40, "at_parity")
    assert mini.ci_low == pytest.approx(wilson_interval(40, 40)[0], abs=1e-4)
    assert not mini.conservative_at_parity  # Wilson low bound 0.912 < 0.95
    haiku = cells[("support", HAIKU.label)]
    assert haiku.parity == 0.75 and haiku.status == "below_parity"
    assert mini.median_latency_delta_ms == 100 - 900
    assert mini.cost_ratio is not None and mini.cost_ratio < 1

    rows = {r.use_case: r for r in report.use_cases}
    # gpt-4o -> gpt-4o-mini at the same tokens: 0.0045 -> 0.00027 per call.
    assert rows["support"].to_model == MINI.label
    assert rows["support"].from_model == "openai:gpt-4o"
    assert rows["support"].baseline_usd == pytest.approx(40 * 0.0045)
    assert rows["support"].at_parity_usd == pytest.approx(40 * 0.00027)
    assert rows["rare"].to_model is None and rows["rare"].eval_cells == 0
    expected = (80 * (0.0045 - 0.00027)) / (85 * 0.0045)
    assert report.saving_pct == pytest.approx(expected, abs=1e-4)
    # Nothing passes the Wilson lower bound with n = 40.
    assert report.conservative_saving_pct == 0
    low, high = report.saving_ci
    assert low <= report.saving_pct <= high

    again = build_report(population, run, bootstrap_resamples=200, bootstrap_seed=3)
    assert again.saving_ci == report.saving_ci

    md = report.to_markdown()
    assert "## 9. Quality parity" in md and "## 10. Savings at parity" in md
    assert "| support | openai:gpt-4o-mini | 40 | 100.0% |" in md
    assert "Other (use cases under 10 calls)" in md  # 'rare' merged (M§2.3)
    assert "question" not in md and "answer 1" not in md  # no prompt/response text
    assert "support-1" not in md  # no record ids
    payload = json.dumps(report.to_dict())
    assert "question" not in payload and "support-1" not in payload


def test_insufficient_sample_is_never_at_parity():
    population = _population(10, ("support",))
    sample = stratified_sample(population, per_stratum=10, seed=0)
    run = run_eval(sample, FakeExecutor(_echo), _config())
    report = build_report(population, run, bootstrap_resamples=50)
    assert report.cells[0].status == "insufficient_sample"
    assert report.saving_pct == 0
    assert "insufficient sample" in report.to_markdown()


def test_policy_limits_projected_savings_per_call():
    population = _population(40, ("support",)) + [
        _record(i, "support", data_classification="restricted") for i in range(40, 60)
    ]
    policy = EvalPolicy.from_dict({"allowed_models": {"internal": ["*"],
                                                      "restricted": []}})
    sample = stratified_sample(population, per_stratum=40, seed=0)
    run = run_eval(sample, FakeExecutor(_echo), _config(policy=policy))
    report = build_report(population, run, bootstrap_resamples=0)
    row = report.use_cases[0]
    # Restricted calls keep their baseline cost.
    assert row.at_parity_usd == pytest.approx(40 * 0.00027 + 20 * 0.0045)
    assert report.saving_ci is None


# --- recordings and CLI ----------------------------------------------------------


def test_recorded_executor_replays_by_prompt():
    record = _record(1)
    completion = Completion(text="answer 1", tokens_in=8, tokens_out=2, cost=0.001,
                            latency_ms=50)
    replay = RecordedExecutor([recording(MINI, record.messages, completion)])
    assert replay.complete(MINI, record.messages, max_tokens=10,
                           temperature=0) == completion
    assert replay.complete(HAIKU, record.messages, max_tokens=10,
                           temperature=0).error
    assert replay.sends_prompts is False


def _write_records(path, records):
    lines = []
    for r in records:
        lines.append(json.dumps({
            "id": r.record_id, "vendor": r.vendor, "served_model": r.model,
            "messages": r.messages, "response_text": r.response_text,
            "use_case": r.use_case, "data_classification": r.data_classification,
            "tokens_in": r.tokens_in, "tokens_out": r.tokens_out,
            "latency_ms": r.latency_ms,
        }))
    path.write_text("\n".join(lines))


def test_cli_dry_run_prints_plan_without_calls(tmp_path, capsys):
    records = tmp_path / "r.jsonl"
    _write_records(records, _population(5))
    assert cli.main(["--records", str(records), "--candidate", MINI.label,
                     "--allow-all-models", "--per-stratum", "3"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["sampled_records"] == 6
    assert plan["candidates"][MINI.label]["calls"] == 6
    assert plan["worst_case_total_usd"] > 0


def test_cli_live_requires_opt_in_and_cap(tmp_path, capsys):
    records = tmp_path / "r.jsonl"
    _write_records(records, _population(2))
    args = ["--records", str(records), "--candidate", MINI.label,
            "--allow-all-models", "--mode", "live"]
    assert cli.main(args) == 2
    assert "--allow-prompt-reexecution" in capsys.readouterr().err
    assert cli.main([*args, "--allow-prompt-reexecution"]) == 2
    assert "--max-spend-usd" in capsys.readouterr().err
    for bad in ("nan", "inf", "-1"):
        assert cli.main([*args, "--allow-prompt-reexecution",
                         "--max-spend-usd", bad]) == 2
        assert "--max-spend-usd" in capsys.readouterr().err


def test_cli_mock_and_replay_write_reports(tmp_path):
    records = tmp_path / "r.jsonl"
    population = _population(35, ("support",))
    _write_records(records, population)
    md, js = tmp_path / "report.md", tmp_path / "report.json"
    assert cli.main(["--records", str(records), "--candidate", MINI.label,
                     "--allow-all-models", "--mode", "mock", "--per-stratum", "35",
                     "--bootstrap-resamples", "20",
                     "--out-md", str(md), "--out-json", str(js)]) == 0
    assert "MOCK RUN" in md.read_text()
    assert json.loads(js.read_text())["cells"][0]["n"] == 35

    recordings = tmp_path / "rec.jsonl"
    recordings.write_text("\n".join(
        json.dumps(recording(MINI, r.messages, Completion(text="wrong")))
        for r in population
    ))
    assert cli.main(["--records", str(records), "--candidate", MINI.label,
                     "--allow-all-models", "--mode", "replay", "--recordings",
                     str(recordings), "--per-stratum", "35",
                     "--bootstrap-resamples", "0", "--out-json", str(js)]) == 0
    cell = json.loads(js.read_text())["cells"][0]
    assert cell["parity"] == 0.0 and cell["status"] == "below_parity"


def test_cli_rejects_bad_input(tmp_path, capsys):
    records = tmp_path / "r.jsonl"
    records.write_text("[1, 2]\n")
    assert cli.main(["--records", str(records), "--candidate", MINI.label,
                     "--allow-all-models"]) == 2
    assert cli.main(["--records", str(records), "--candidate", "nocolon",
                     "--allow-all-models"]) == 2
    assert cli.main(["--records", str(records), "--candidate", MINI.label,
                     "--allow-all-models", "--scorer", "judge"]) == 2


def test_cli_live_rejects_vendor_without_endpoint(tmp_path, capsys):
    records = tmp_path / "r.jsonl"
    _write_records(records, _population(2))
    assert cli.main(["--records", str(records), "--candidate", "mistral:mistral-small",
                     "--allow-all-models", "--mode", "live",
                     "--allow-prompt-reexecution", "--max-spend-usd", "1"]) == 2
    assert "base_url" in capsys.readouterr().err
