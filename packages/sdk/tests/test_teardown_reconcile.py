"""``ps-teardown reconcile``: recorded event cost vs. provider invoice or usage
export, per provider / model / day, gaps above ±2% flagged (M§3.1, #24)."""

import csv
import json
from pathlib import Path

import pytest
from prompt_shields.teardown.cli import main
from prompt_shields.teardown.engine import price_records
from prompt_shields.teardown.reconcile import (
    MISSING_IN_PROVIDER,
    MISSING_IN_PS,
    RECONCILED,
    UNRECONCILED,
    ps_side,
    read_invoice_csv,
    reconcile,
)
from prompt_shields.teardown.records import load_records


def _events(tmp_path, rows, name="events.jsonl"):
    path = tmp_path / name
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def _invoice(tmp_path, rows, name="invoice.csv"):
    path = tmp_path / name
    fields = sorted({k for r in rows for k in r})
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return path


def _ev(ts, model, cost, vendor="openai", **extra):
    return {"timestamp": ts, "vendor": vendor, "model": model, "served_model": model,
            "tokens_in": 100, "tokens_out": 10, "cost": cost, "cost_source": "pricing_table",
            **extra}


EVENTS = [
    _ev("2026-11-01T09:00:00Z", "gpt-4o", 6.0),
    _ev("2026-11-01T10:00:00Z", "gpt-4o", 4.0),
    _ev("2026-11-01T11:00:00Z", "gpt-4o-mini", 1.0),
    _ev("2026-11-02T09:00:00Z", "gpt-4o", 10.0),
    _ev("2026-11-02T09:30:00Z", "claude-sonnet-4", 3.0, vendor="anthropic"),
]


def _report(tmp_path, events, invoice_rows, tolerance=2.0):
    loaded = load_records([_events(tmp_path, events)], fmt="ps-events")
    return reconcile(ps_side(loaded.records), read_invoice_csv(_invoice(tmp_path, invoice_rows)),
                     tolerance)


def _row(report, model, day):
    return next(r for r in report["rows"] if (r["model"], r["day"]) == (model, day))


def test_matching_invoice_reconciles(tmp_path):
    report = _report(tmp_path, EVENTS, [
        {"date": "2026-11-01", "provider": "openai", "model": "gpt-4o", "amount_usd": "10.00"},
        {"date": "2026-11-01", "provider": "OpenAI", "model": "gpt-4o-mini", "amount_usd": "1.00"},
        {"date": "2026-11-02", "provider": "openai", "model": "gpt-4o", "amount_usd": "10.1"},
    ])
    assert report["status"] == RECONCILED
    assert report["flagged_rows"] == 0
    assert _row(report, "gpt-4o", "2026-11-02")["gap_pct"] == pytest.approx(-0.99)
    (openai,) = report["providers"]
    assert openai["ps_cost_usd"] == 21.0 and openai["provider_usd"] == 21.1
    assert openai["window"] == {"start": "2026-11-01", "end": "2026-11-02"}
    # No Anthropic invoice: listed, not flagged.
    assert report["not_compared"] == [{"provider": "anthropic", "ps_cost_usd": 3.0}]


def test_gap_above_two_percent_is_flagged(tmp_path):
    report = _report(tmp_path, EVENTS, [
        {"date": "2026-11-01", "provider": "openai", "model": "gpt-4o", "amount_usd": "10.30"},
        {"date": "2026-11-01", "provider": "openai", "model": "gpt-4o-mini", "amount_usd": "1.02"},
        {"date": "2026-11-02", "provider": "openai", "model": "gpt-4o", "amount_usd": "10"},
    ])
    flagged = _row(report, "gpt-4o", "2026-11-01")
    assert flagged["status"] == UNRECONCILED and flagged["flagged"]
    assert flagged["gap_pct"] == pytest.approx(-2.91)
    within = _row(report, "gpt-4o-mini", "2026-11-01")
    assert within["status"] == RECONCILED and within["gap_pct"] == pytest.approx(-1.96)
    assert report["flagged_rows"] == 1
    assert report["status"] == UNRECONCILED
    # The provider total is within tolerance (21 vs 21.32 = -1.5%) but a
    # row is not, so the report as a whole is unreconciled.
    assert report["providers"][0]["status"] == RECONCILED


def test_missing_lines_on_either_side(tmp_path):
    report = _report(tmp_path, EVENTS, [
        {"date": "2026-11-01", "provider": "openai", "model": "gpt-4o", "amount_usd": "10"},
        {"date": "2026-11-01", "provider": "openai", "model": "gpt-4.1", "amount_usd": "2"},
        {"date": "2026-11-02", "provider": "openai", "model": "gpt-4o", "amount_usd": "10"},
    ])
    assert _row(report, "gpt-4.1", "2026-11-01")["status"] == MISSING_IN_PS
    assert _row(report, "gpt-4.1", "2026-11-01")["gap_pct"] == -100.0
    assert _row(report, "gpt-4o-mini", "2026-11-01")["status"] == MISSING_IN_PROVIDER
    assert report["flagged_rows"] == 2


def test_rows_without_model_or_date_set_a_coarser_grain(tmp_path):
    per_day = _report(tmp_path, EVENTS, [
        {"date": "2026-11-01", "provider": "openai", "amount_usd": "11"},
        {"date": "2026-11-02", "provider": "openai", "amount_usd": "10"},
    ])
    assert [(r["model"], r["day"], r["status"]) for r in per_day["rows"]] == [
        ("*", "2026-11-01", RECONCILED), ("*", "2026-11-02", RECONCILED)]

    window = _report(tmp_path, EVENTS, [
        {"provider": "anthropic", "amount_usd": "3.5"},
    ])
    (row,) = window["rows"]
    assert (row["model"], row["day"], row["status"]) == ("*", "*", UNRECONCILED)
    assert row["gap_pct"] == pytest.approx(-14.29)


def test_events_without_cost_and_outside_the_window_are_counted(tmp_path):
    events = [*EVENTS, _ev("2026-11-01T12:00:00Z", "gpt-4o", None),
              _ev("2026-10-30T12:00:00Z", "gpt-4o", 99.0)]
    report = _report(tmp_path, events, [
        {"date": "2026-11-01", "provider": "openai", "model": "gpt-4o", "amount_usd": "10"},
    ])
    (openai,) = report["providers"]
    assert openai["ps_events_without_cost"] == 1
    # 2026-10-30 and 2026-11-02 are outside the invoice's one-day window.
    assert openai["ps_lines_outside_window"] == 2
    assert _row(report, "gpt-4o", "2026-11-01")["status"] == RECONCILED


def test_invoice_rows_are_validated(tmp_path):
    side = read_invoice_csv(_invoice(tmp_path, [
        {"date": "2026-11-01", "provider": "openai", "model": "gpt-4o", "amount_usd": "1",
         "currency": "usd"},
        {"date": "2026-11-01", "provider": "openai", "model": "gpt-4o", "amount_usd": "1",
         "currency": "EUR"},
        {"date": "2026-11-01", "provider": "openai", "model": "gpt-4o", "amount_usd": "-1"},
        {"date": "2026-11-01", "provider": "", "model": "claude-sonnet-4", "amount_usd": "2"},
        {"date": "2026-11-01", "provider": "", "model": "llama-x", "amount_usd": "2"},
        {"date": "yesterday", "provider": "openai", "model": "gpt-4o", "amount_usd": "1"},
    ]))
    assert [(line.provider, line.amount) for line in side.lines] == [
        ("openai", 1.0), ("anthropic", 2.0)]
    (stats,) = side.sources
    assert dict(stats.dropped) == {"not_usd": 1, "no_amount": 1, "no_provider": 1,
                                   "bad_date": 1}


def test_cli_invoice_markdown_and_fail_on_gap(tmp_path, capsys):
    events = _events(tmp_path, EVENTS)
    invoice = _invoice(tmp_path, [
        {"date": "2026-11-01", "provider": "openai", "model": "gpt-4o", "amount_usd": "12"},
        {"date": "2026-11-01", "provider": "openai", "model": "gpt-4o-mini", "amount_usd": "1"},
    ])
    assert main(["reconcile", str(events), "--invoice", str(invoice)]) == 0
    out = capsys.readouterr().out
    assert "# Invoice reconciliation" in out
    assert "**Status: unreconciled**" in out
    assert "| openai | gpt-4o | 2026-11-01 | $10 | $12 | -16.67% | unreconciled ⚠ |" in out

    assert main(["reconcile", str(events), "--invoice", str(invoice), "--fail-on-gap",
                 "--format", "json", "--out", str(tmp_path / "r.json")]) == 3
    report = json.loads((tmp_path / "r.json").read_text())
    assert report["schema_version"] == "reconcile.v1"
    assert report["flagged_rows"] == 1

    assert main(["reconcile", str(events), "--invoice", str(invoice), "--fail-on-gap",
                 "--tolerance", "20"]) == 0


def test_cli_usage_export_priced_at_list_price(tmp_path, capsys):
    usage = Path(__file__).parent / "fixtures" / "teardown" / "openai_usage.json"
    priced = price_records(load_records([usage]).records)
    # Events that cost exactly what the export prices at, per model and day.
    events = [
        {"timestamp": p.record.timestamp.isoformat(), "vendor": "openai",
         "served_model": p.record.served_model, "cost": p.cost}
        for p in priced if p.cost is not None
    ]
    path = _events(tmp_path, events)
    assert main(["reconcile", str(path), "--usage", str(usage), "--format", "json",
                 "--fail-on-gap"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == RECONCILED and report["rows"]

    # 5% less recorded than the provider's usage implies: every row flagged.
    for e in events:
        e["cost"] *= 0.95
    path = _events(tmp_path, events)
    assert main(["reconcile", str(path), "--usage", str(usage), "--format", "json",
                 "--fail-on-gap"]) == 3
    report = json.loads(capsys.readouterr().out)
    assert all(r["flagged"] and r["gap_pct"] == pytest.approx(-5.0, abs=0.02)
               for r in report["rows"])


def test_cli_argument_errors(tmp_path):
    events = _events(tmp_path, EVENTS)
    with pytest.raises(SystemExit):
        main(["reconcile", str(events)])
    with pytest.raises(SystemExit):  # an event log is not a usage export
        main(["reconcile", str(events), "--usage", str(events)])
    with pytest.raises(SystemExit):
        main(["reconcile", str(events), "--invoice", str(tmp_path / "missing.csv")])


def test_dated_snapshots_and_prefixes_match_the_base_model(tmp_path):
    events = [
        _ev("2026-11-01T09:00:00Z", "gpt-4o-2024-08-06", 6.0),
        _ev("2026-11-01T10:00:00Z", "gpt-4o", 4.0),
        _ev("2026-11-01T11:00:00Z", "us.anthropic.claude-sonnet-4-v1:0", 2.0, vendor="bedrock"),
    ]
    report = _report(tmp_path, events, [
        {"date": "2026-11-01", "provider": "openai", "model": "openai/GPT-4o-2024-11-20",
         "amount_usd": "10"},
        {"date": "2026-11-01", "provider": "bedrock", "model": "anthropic.claude-sonnet-4-v1:0",
         "amount_usd": "2"},
    ])
    assert report["status"] == RECONCILED
    assert [(r["model"], r["status"]) for r in report["rows"]] == [
        ("anthropic.claude-sonnet-4-v1:0", RECONCILED), ("gpt-4o", RECONCILED)]


def test_invoice_labels_cannot_break_the_markdown(tmp_path):
    from prompt_shields.teardown.reconcile import render_markdown

    report = _report(tmp_path, [], [
        {"date": "2026-11-01", "provider": "evil\n# injected|x", "model": "m|odel\r\nrow",
         "amount_usd": "1"},
    ])
    text = render_markdown(report)
    assert "\n# injected" not in text
    assert "evil # injected\\|x" in text and "m\\|odel row" in text


def test_oversized_invoice_is_not_read(tmp_path, monkeypatch):
    import prompt_shields.teardown.reconcile as rec

    path = _invoice(tmp_path, [
        {"date": "2026-11-01", "provider": "openai", "model": "gpt-4o", "amount_usd": "1"},
        {"date": "2026-11-02", "provider": "openai", "model": "gpt-4o", "amount_usd": "1"},
    ])
    monkeypatch.setattr(rec, "MAX_INVOICE_ROWS", 1)
    side = read_invoice_csv(path)
    assert len(side.lines) == 1 and side.sources[0].dropped["too_many_rows"] == 1
    monkeypatch.setattr(rec, "MAX_INVOICE_BYTES", 10)
    side = read_invoice_csv(path)
    assert side.lines == [] and side.sources[0].dropped["too_large"] == 1


# -- provider aliases (#24) ---------------------------------------------------

@pytest.mark.parametrize(("a", "b"), [
    # Anthropic: -latest, -0 major aliases and Vertex AI ids -> the catalog snapshot.
    ("claude-3-5-sonnet-latest", "claude-3-5-sonnet-20241022"),
    ("claude-3-opus-latest", "claude-3-opus-20240229"),
    ("claude-sonnet-4-0", "claude-sonnet-4-20250514"),
    ("claude-opus-4-0", "claude-opus-4-20250514"),
    ("claude-3-5-sonnet-v2@20241022", "claude-3-5-sonnet-20241022"),
    ("claude-sonnet-4@20250514", "claude-sonnet-4-20250514"),
    # ... and a dated snapshot -> the catalog's undated name.
    ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
    ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
    # A model the catalog does not list: the suffix rules alone.
    ("claude-3-7-sonnet-latest", "claude-3-7-sonnet-20250219"),
    # Gemini: -latest and -001 / -002 stable versions.
    ("gemini-1.5-pro-002", "gemini-1.5-pro"),
    ("gemini-1.5-flash-latest", "models/gemini-1.5-flash-001"),
    ("gemini-2.0-flash-001", "gemini-2.0-flash"),
    ("gemini-2.0-flash-lite-001", "gemini-2.0-flash-lite"),
    # Mistral: -YYMM versions and the -latest alias.
    ("mistral-large-2411", "mistral-large-latest"),
    ("codestral-2501", "codestral-latest"),
    ("ministral-8b-2410", "ministral-8b-latest"),
    # OpenAI: legacy -MMDD snapshots of a catalog model.
    ("gpt-4-0613", "gpt-4"),
    ("gpt-3.5-turbo-0125", "gpt-3.5-turbo"),
])
def test_provider_aliases_share_a_key(a, b):
    from prompt_shields.teardown.reconcile import _model_key

    assert _model_key(a) == _model_key(b)


@pytest.mark.parametrize(("a", "b"), [
    # A snapshot the catalog does not list is not the one it does.
    ("claude-3-5-sonnet-20240620", "claude-3-5-sonnet-20241022"),
    ("claude-3-5-sonnet-20240620", "claude-3-5-sonnet-latest"),
    ("claude-3-5-haiku-latest", "claude-3-5-sonnet-latest"),
    ("claude-opus-4-1", "claude-opus-4-0"),
    ("claude-opus-4-1", "claude-opus-4-20250514"),
    ("claude-opus-4-5", "claude-opus-4-20250514"),
    ("claude-sonnet-4-5", "claude-sonnet-4-20250514"),
    ("claude-sonnet-4-5-20250929", "claude-sonnet-4-0"),
    ("claude-haiku-4-5", "claude-3-5-haiku-latest"),
    # Gemini variants and previews.
    ("gemini-2.5-flash-lite", "gemini-2.5-flash-001"),
    ("gemini-2.5-flash-lite-001", "gemini-2.5-flash"),
    ("gemini-1.5-flash-8b-001", "gemini-1.5-flash"),
    ("gemini-2.5-flash-preview-05-20", "gemini-2.5-flash"),
    ("gemini-1.5-pro-002", "gemini-1.5-flash-002"),
    # OpenAI -latest ids and sizes are separate models.
    ("chatgpt-4o-latest", "gpt-4o"),
    ("gpt-4-32k-0613", "gpt-4"),
    ("gpt-4o-mini", "gpt-4o"),
    ("gpt-4.1-mini-2025-04-14", "gpt-4.1"),
    # Mistral and Cohere near misses.
    ("mistral-small-3.2-24b-instruct-2506", "mistral-small-latest"),
    ("mistral-small-2503", "mistral-medium-latest"),
    ("codestral-2501", "mistral-large-latest"),
    ("command-r-08-2024", "command-r-plus-08-2024"),
])
def test_near_miss_names_do_not_share_a_key(a, b):
    from prompt_shields.teardown.reconcile import _model_key

    assert _model_key(a) != _model_key(b)


def test_already_matched_names_keep_their_key():
    from prompt_shields.teardown.reconcile import _model_key

    assert _model_key("openai/GPT-4o-2024-11-20") == "gpt-4o"
    assert _model_key("claude-3-5-sonnet-20241022") == "claude-3-5-sonnet-20241022"
    assert _model_key("us.anthropic.claude-sonnet-4-v1:0") == "anthropic.claude-sonnet-4-v1:0"
    assert _model_key("mistral-large-latest") == "mistral-large-latest"
    assert _model_key("gemini-2.5-flash") == "gemini-2.5-flash"


def test_catalog_aliases_are_used_when_listed():
    from prompt_shields.teardown.reconcile import _build_index, _canonical_model

    index = _build_index({"models": {
        "acme/acme-large-v3": {"model": "acme-large-v3", "aliases": ["acme-large", "Acme-Big"]},
    }})
    assert _canonical_model("acme-large", index) == "acme-large-v3"
    assert _canonical_model("acme-big", index) == "acme-large-v3"
    assert _canonical_model("acme-small", index) == "acme-small"


def test_invoice_aliases_reconcile_against_logged_snapshots(tmp_path):
    events = [
        _ev("2026-11-01T09:00:00Z", "claude-3-5-sonnet-20241022", 5.0, vendor="anthropic"),
        _ev("2026-11-01T10:00:00Z", "claude-3-5-sonnet-20240620", 1.0, vendor="anthropic"),
        _ev("2026-11-01T11:00:00Z", "gemini-1.5-pro-002", 2.0, vendor="google"),
        _ev("2026-11-01T12:00:00Z", "gemini-2.5-flash-lite", 0.5, vendor="google"),
    ]
    report = _report(tmp_path, events, [
        {"date": "2026-11-01", "provider": "anthropic", "model": "claude-3-5-sonnet-latest",
         "amount_usd": "5"},
        {"date": "2026-11-01", "provider": "google", "model": "gemini-1.5-pro-latest",
         "amount_usd": "2"},
        {"date": "2026-11-01", "provider": "google", "model": "gemini-2.5-flash",
         "amount_usd": "0.5"},
    ])
    rows = {(r["provider"], r["model"]): r["status"] for r in report["rows"]}
    assert rows == {
        ("anthropic", "claude-3-5-sonnet-20241022"): RECONCILED,
        # The June snapshot is a different model: not folded into the alias.
        ("anthropic", "claude-3-5-sonnet"): MISSING_IN_PROVIDER,
        ("google", "gemini-1.5-pro"): RECONCILED,
        ("google", "gemini-2.5-flash"): MISSING_IN_PS,
        ("google", "gemini-2.5-flash-lite"): MISSING_IN_PROVIDER,
    }
