"""ps-teardown cost-only mode (#12) and learning-tuple export (#42).

Golden files live in fixtures/teardown/golden/. To regenerate them after an
intended change, run with ``PS_UPDATE_GOLDEN=1`` and review the diff.
"""

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
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
from prompt_shields.teardown.cli import main
from prompt_shields.teardown.records import detect_format

FIXTURES = Path(__file__).parent / "fixtures" / "teardown"
GOLDEN = FIXTURES / "golden"
KEY_MAP = json.loads((FIXTURES / "key_map.json").read_text())
SECRET_TEXT = "SECRET-PROMPT-TEXT-123"

GOLDEN_CASES = {
    "openai_usage_json": ["openai_usage.json"],
    "openai_usage_csv": ["openai_usage.csv"],
    "anthropic_usage": ["anthropic_usage.json"],
    "ps_events": ["ps_events.jsonl"],
    "generic_jsonl": ["generic.jsonl"],
}


def _options(**kw) -> ReportOptions:
    base = {
        "report_id": "golden",
        "report_date": "2026-10-23",
        "customer_label": "Golden Co",
        "tool_version": "ps-teardown test",
        "min_cell_calls": 1,
    }
    base.update(kw)
    return ReportOptions(**base)


def _report(files, source=None, **kw):
    loaded = load_records([FIXTURES / f for f in files], key_map=KEY_MAP)
    source = source or SdkPriceSource(version="test-catalog")
    priced = price_records(loaded.records, source)
    return build_report(priced, loaded.sources, _options(**kw), source), priced


def _check_golden(path: Path, actual: str) -> None:
    if os.environ.get("PS_UPDATE_GOLDEN"):
        path.parent.mkdir(exist_ok=True)
        path.write_text(actual, encoding="utf-8")
    assert path.exists(), f"missing golden {path.name}; run with PS_UPDATE_GOLDEN=1"
    assert actual == path.read_text(encoding="utf-8"), f"{path.name} differs from golden"


@pytest.mark.parametrize("name", sorted(GOLDEN_CASES))
def test_golden_report(name):
    report, _ = _report(GOLDEN_CASES[name])
    _check_golden(GOLDEN / f"{name}.md", render_report(report, "markdown"))
    _check_golden(GOLDEN / f"{name}.json", render_report(report, "json"))


@pytest.mark.parametrize("name", sorted(GOLDEN_CASES))
def test_report_follows_template(name):
    """Every template section heading is present, in order."""
    report, _ = _report(GOLDEN_CASES[name])
    md = render_report(report, "markdown")
    template = (Path(__file__).parents[3] / "docs/teardown/teardown-report-template.md").read_text()
    body = template.split("\n---\n", 1)[1]
    headings = [h for h in re.findall(r"^##+ .*$", body, re.MULTILINE)]
    positions = [md.find(h + "\n") for h in headings]
    assert all(p >= 0 for p in positions), [h for h, p in zip(headings, positions) if p < 0]
    assert positions == sorted(positions)
    for section in ("## 7.", "## 8.", "## 9.", "## 10."):
        chunk = md.split(section, 1)[1].split("\n## ", 1)[0]
        assert "Not measured in this teardown — pending #1" in chunk


def test_no_prompt_text_hashes_or_identifiers_leave():
    report, priced = _report(["ps_events.jsonl", "generic.jsonl"])
    outputs = [render_report(report, f) for f in ("markdown", "json", "html")]
    outputs.append("\n".join(json.dumps(t) for t in learning_tuples(priced)))
    hashes = {p.record.prompt_hash for p in priced if p.record.prompt_hash}
    assert hashes
    for text in outputs:
        assert SECRET_TEXT not in text
        assert "reset my password" not in text
        assert "owner@example.com" not in text
        assert "s-1" not in text
        for h in hashes:
            assert h not in text


# --- loading ----------------------------------------------------------------------


def test_detect_format():
    for name, fmt in [
        ("openai_usage.json", "openai-usage"),
        ("openai_usage.csv", "openai-usage"),
        ("anthropic_usage.json", "anthropic-usage"),
        ("ps_events.jsonl", "ps-events"),
        ("generic.jsonl", "generic-jsonl"),
    ]:
        path = FIXTURES / name
        assert detect_format(path, path.read_text()) == fmt, name


def test_drop_reasons_are_counted():
    loaded = load_records([FIXTURES / "ps_events.jsonl", FIXTURES / "generic.jsonl"])
    by_file = {s.file: s for s in loaded.sources}
    assert dict(by_file["ps_events.jsonl"].dropped) == {
        "gateway_cache_event": 1, "invalid_json": 1,
    }
    assert dict(by_file["generic.jsonl"].dropped) == {"no_model": 1, "not_an_object": 1}
    for s in loaded.sources:
        assert s.rows_read == s.rows_used + sum(s.dropped.values())


def test_generic_record_keeps_no_text():
    loaded = load_records([FIXTURES / "generic.jsonl"])
    rec = loaded.records[0]
    assert SECRET_TEXT not in repr(rec)
    assert rec.prompt_hash and len(rec.prompt_hash) == 64
    assert rec.features["complexity"] == "simple"
    code = loaded.records[3]
    assert code.features["has_code"] is True and code.features["complexity"] == "complex"
    # Anthropic usage: tokens_in adds cache reads back (SDK convention).
    assert (code.tokens_in, code.tokens_cached) == (1500, 1200)


def test_anthropic_usage_cache_writes():
    rec = load_records([FIXTURES / "anthropic_usage.json"]).records[0]
    assert rec.tokens_in == 800_000 + 2_400_000 + 150_000 + 50_000
    assert (rec.tokens_cache_write, rec.tokens_cache_write_1h) == (200_000, 50_000)
    assert rec.calls is None and rec.aggregated


# --- pricing ----------------------------------------------------------------------


def _priced(**fields) -> object:
    rec = TeardownRecord(source="ps-events", source_file="x", **fields)
    return price_records([rec], SdkPriceSource(version="t"))[0]


def test_prices_at_served_model_not_requested():
    p = _priced(provider="openai", requested_model="gpt-4o-mini", served_model="gpt-4o",
                tokens_in=1000, tokens_out=1000)
    assert p.model == "gpt-4o" and p.cost == pytest.approx(0.0125)
    assert not p.priced_at_requested


def test_dated_snapshot_resolves_and_auto_is_null():
    assert _priced(provider="openai", served_model="gpt-4o-2024-08-06",
                   tokens_in=1000, tokens_out=0).model == "gpt-4o"
    p = _priced(provider="openai", requested_model="auto", tokens_in=10, tokens_out=10)
    assert p.cost is None and p.null_reason == "auto_without_served_model"


def test_unpriced_served_model_falls_back_to_requested():
    # Same order as pricing.resolve_pricing_model (#229): served, then requested.
    p = _priced(provider="openai", requested_model="gpt-4o", served_model="gpt-4o-unlisted-x",
                tokens_in=1000, tokens_out=1000)
    assert p.model == "gpt-4o" and p.cost == pytest.approx(0.0125)
    assert p.priced_at_requested
    p = _priced(provider="openai", requested_model="auto", served_model="gpt-4o-unlisted-x",
                tokens_in=10, tokens_out=10)
    assert p.cost is None and p.null_reason == "unknown_model"


def test_malformed_input_rows_do_not_abort_the_run(tmp_path):
    ok = '{"model": "gpt-4o", "tokens_in": 1, "tokens_out": 1}'
    files = {
        "ts.jsonl": '{"timestamp": "99999999999999999999", "model": "gpt-4o", '
                    '"tokens_in": 1, "tokens_out": 1}\n'
                    '{"timestamp": 1e300, "model": "gpt-4o", "tokens_in": 1, "tokens_out": 1}\n',
        "deep.json": "[" * 100000 + "]" * 100000,
        "details.jsonl": '{"messages": [], "model": "gpt-4o", "usage": {"input_tokens": 5, '
                         '"output_tokens": 2, "input_tokens_details": "x"}}\n',
        "long.csv": "model,tokens_in,tokens_out\ngpt-4o,1," + "1" * 200000
                    + "\ngpt-4o,1,1\n",
        "empty.csv": "",
        "neg.jsonl": '{"model": "gpt-4o", "tokens_in": -5, "tokens_out": 1, "cost": -1e9, '
                     '"cost_source": "provider_reported"}\n' + ok + "\n",
    }
    paths = []
    for name, text in files.items():
        (tmp_path / name).write_text(text)
        paths.append(tmp_path / name)
    (tmp_path / "bad.jsonl").write_bytes(b'{"model": "gpt-4o", "tokens_in": 1, '
                                         b'"tokens_out": 1, "business_unit": "\xff"}\n')
    paths.append(tmp_path / "bad.jsonl")
    loaded = load_records(paths)
    stats = {s.file: s for s in loaded.sources}
    assert stats["deep.json"].dropped["invalid_json"] == 1
    assert stats["long.csv"].dropped["invalid_csv"] == 1 and stats["long.csv"].rows_used == 1
    assert stats["ts.jsonl"].rows_used == 2
    neg = [r for r in loaded.records if r.source_file == "neg.jsonl"]
    assert neg[0].tokens_in is None and neg[0].provider_cost is None
    priced = price_records(loaded.records)
    assert all(p.cost is None or p.cost >= 0 for p in priced)
    build_report(priced, loaded.sources, _options())


def test_labels_cannot_add_markdown_lines(tmp_path):
    path = tmp_path / "e.jsonl"
    path.write_text(json.dumps({"model": "gpt-4o", "tokens_in": 1, "tokens_out": 1,
                                "business_unit": "Care\n## 1. Summary\r\n- x"}) + "\n")
    rec = load_records([path]).records[0]
    assert rec.business_unit == "Care ## 1. Summary - x"


def test_provider_reported_cost_is_kept_and_sdk_cost_is_not_summed():
    loaded = load_records([FIXTURES / "ps_events.jsonl"])
    priced = {p.record.requested_model: p for p in price_records(loaded.records)}
    assert priced["meta-llama/llama-3.3-70b-instruct"].cost_source == "provider_reported"
    assert priced["meta-llama/llama-3.3-70b-instruct"].cost == 0.00042
    assert priced["mixtral-8x7b"].cost_source == "litellm"


def test_aggregated_rows_do_not_trigger_long_context_band():
    # 1M prompt tokens summed over many requests: base rates, not the band.
    rec = TeardownRecord(source="anthropic-usage", source_file="x", aggregated=True, calls=None,
                         provider="anthropic", served_model="claude-sonnet-4-20250514",
                         tokens_in=1_000_000, tokens_out=0)
    assert price_records([rec])[0].cost == pytest.approx(3.0)
    rec.context_window = "200k-1M"
    assert price_records([rec])[0].cost == pytest.approx(6.0)


def test_arbitrage_ignores_candidates_missing_a_price_dimension():
    # Bedrock has no web-search fee, 1h cache-write or long-context band
    # price: moving these calls there must not look like a saving.
    report, _ = _report(["anthropic_usage.json"])
    assert report["arbitrage"]["saving"] == 0
    assert report["arbitrage"]["rows"] == []


def test_arbitrage_with_cheaper_host_and_allow_list():
    table = {
        ("anthropic", "claude-3-5-haiku-20241022"): (0.0008, 0.004),
        ("bedrock", "anthropic.claude-3-5-haiku-20241022-v1:0"): (0.0004, 0.002),
        ("openai", "gpt-4o"): (0.0025, 0.010),
    }
    source = SdkPriceSource(pricing_table=table, version="t")
    report, _ = _report(["ps_events.jsonl"], source=source)
    arb = report["arbitrage"]
    assert [(r["model"], r["current_provider"], r["cheaper_provider"]) for r in arb["rows"]] == [
        ("claude-3-5-haiku-20241022", "anthropic", "bedrock")
    ]
    assert arb["rows"][0]["saving_pct"] == 50.0
    assert arb["saving"] == pytest.approx(arb["rows"][0]["saving"])
    blocked, _ = _report(["ps_events.jsonl"], source=source, allowed_providers=["anthropic"])
    assert blocked["arbitrage"]["saving"] == 0


def test_small_cells_merge_into_other():
    report, _ = _report(["ps_events.jsonl"], min_cell_calls=3)
    rows = report["attribution"]["rows"]
    assert rows[-1]["business_unit"] == "Other"
    assert all(r["calls"] >= 3 for r in rows[:-1])
    assert sum(r["calls"] for r in rows) == report["baseline"]["total_calls"]


def test_cache_ttl_and_sensitivity_exclusion():
    def call(minute, cls, h="h1"):
        return TeardownRecord(
            source="ps-events", source_file="x", provider="openai", served_model="gpt-4o",
            tokens_in=1000, tokens_out=0, prompt_hash=h, data_classification=cls,
            timestamp=datetime(2026, 10, 20, 0, minute, tzinfo=UTC),
        )

    records = [call(0, "public"), call(30, "public"), call(59, "public"),
               call(1, "restricted"), call(2, "restricted"), call(3, None)]
    priced = price_records(records)
    cache = build_report(priced, [], _options(cache_ttl_seconds=1800))["cache"]
    # 00:00 write, 00:30 hit (exactly at the TTL), 00:59 expired: a new
    # write. Restricted and unclassified (confidential) calls: cache off.
    assert cache["hit_calls"] == 1
    assert cache["excluded_cache_off_calls"] == 3
    loose = build_report(priced, [], _options(cache_ttl_seconds=1800, cache_off_at="restricted",
                                              unclassified_default="internal"))["cache"]
    # The unclassified 00:03 call (now internal) hits the 00:00 entry too.
    assert loose["hit_calls"] == 2 and loose["excluded_cache_off_calls"] == 2


def test_null_cost_calls_are_reported_by_reason_not_zero():
    report, _ = _report(["ps_events.jsonl"])
    nulls = report["baseline"]["null_cost_by_reason"]
    assert set(nulls) == {
        "auto_without_served_model", "missing_tokens", "unknown_model", "unpriced_service_tier",
    }
    assert report["baseline"]["by_cost_source"]["null"]["spend"] is None


# --- learning tuples (#42) ----------------------------------------------------------


def test_learning_tuples():
    _, priced = _report(["generic.jsonl", "openai_usage.json"])
    tuples = list(learning_tuples(priced))
    assert len(tuples) == 7  # usage-export rows are aggregates, not calls
    first = tuples[0]
    assert first["schema"] == "ps-learning-tuple.v0"
    assert first["features_source"] == "messages"
    assert first["features"]["message_count"] == 2
    assert first["quality_score"] is None and first["quality_source"] == "not_measured"
    assert first["cost_usd"] == pytest.approx(0.000135)
    assert {"prompt_hash", "messages", "session_id"}.isdisjoint(first)
    tok = list(learning_tuples(price_records(load_records([FIXTURES / "ps_events.jsonl"]).records)))
    assert tok[4]["features_source"] == "token_counts"
    assert tok[4]["features"]["complexity"] == "complex"


# --- CLI ------------------------------------------------------------------------------


def test_cli_smoke(tmp_path, capsys):
    inputs = [str(FIXTURES / f) for f in
              ("openai_usage.json", "anthropic_usage.json", "ps_events.jsonl", "generic.jsonl")]
    tuples_path = tmp_path / "tuples.jsonl"
    rc = main(["run", *inputs, "--out-dir", str(tmp_path / "out"),
               "--key-map", str(FIXTURES / "key_map.json"), "--customer-label", "Acme",
               "--learning-tuples", str(tuples_path)])
    assert rc == 0
    out = tmp_path / "out"
    md = (out / "teardown-report.md").read_text()
    html = (out / "teardown-report.html").read_text()
    data = json.loads((out / "teardown-report.json").read_text())
    assert md.startswith("# AI bill teardown — Acme")
    assert data["schema"] == "ps-teardown-report.v1" and data["report"]["mode"] == "cost-only"
    assert data["report"]["catalog_version"].startswith("sdk-")
    assert len(data["sources"]) == 4
    assert html.startswith("<!doctype html>") and "<table>" in html
    assert not re.search(r"(src|href)=\"?https?:", html)  # self-contained, offline
    assert SECRET_TEXT not in md + html + json.dumps(data) + tuples_path.read_text()
    assert len(tuples_path.read_text().splitlines()) == 24
    assert "wrote" in capsys.readouterr().err


def test_cli_stdout_and_errors(tmp_path, capsys):
    assert main(["run", str(FIXTURES / "generic.jsonl"), "--stdout", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["window"]["start"] == "2026-10-20T08:00:00Z"
    with pytest.raises(SystemExit):
        main(["run", str(tmp_path / "missing.jsonl")])


def test_console_script_is_declared():
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text()
    assert 'ps-teardown = "prompt_shields.teardown.cli:main"' in pyproject
