"""``ps-teardown``: the AI bill teardown CLI (#12, #13, #14).

    ps-teardown run usage.json events.jsonl --out-dir report/ --customer-label "Acme"
    ps-teardown run events.jsonl --policy policy.json --eval-results eval.json
    ps-teardown reconcile ai_usage_events.csv --invoice invoice.csv
    ps-teardown reconcile events.jsonl --usage openai_usage.json --fail-on-gap

Reads local files only and makes no network calls. Writes the report as
Markdown, HTML and JSON (``teardown-report.{md,html,json}``), and with
``--learning-tuples`` the learning-tuple export (#42). ``--policy`` adds the
sensitivity mix and policy replay; ``--eval-results`` (the JSON a prior
``ps-eval`` run wrote) adds quality parity and savings at parity. The
teardown run itself never executes a prompt.

``reconcile`` compares the cost Prompt Shields recorded (each event's own
``cost``) with a provider invoice CSV or usage export, per provider, model
and day, and flags gaps above ``--tolerance`` (2%); see ``reconcile.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from prompt_shields import __version__
from prompt_shields.teardown.catalog import SdkPriceSource
from prompt_shields.teardown.engine import (
    LEVELS,
    ReportOptions,
    build_report,
    price_records,
)
from prompt_shields.teardown.policy import EvalResultsError, read_eval_results
from prompt_shields.teardown.reconcile import (
    DEFAULT_TOLERANCE_PCT,
    RECONCILED,
    Side,
    ps_side,
    read_invoice_csv,
    reconcile,
    render,
    usage_export_side,
)
from prompt_shields.teardown.records import FORMATS, load_records
from prompt_shields.teardown.render import render_report
from prompt_shields.teardown.tuples import write_learning_tuples

REPORT_BASENAME = "teardown-report"
REPORT_FORMATS = ("markdown", "html", "json")
_EXT = {"markdown": "md", "html": "html", "json": "json"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ps-teardown",
        description="AI bill teardown from usage logs, run locally.",
    )
    parser.add_argument("--version", action="version", version=f"ps-teardown {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="build a teardown report from input files")
    run.add_argument("inputs", nargs="+", help="usage exports, event logs or JSONL files")
    run.add_argument("--input-format", choices=FORMATS,
                     help="force the input format for every file (default: detect per file)")
    run.add_argument("--out-dir", default=".", help="directory for the report files (default: .)")
    run.add_argument("--format", action="append", choices=REPORT_FORMATS, dest="formats",
                     help="report format(s) to write (repeatable; default: all three)")
    run.add_argument("--stdout", choices=REPORT_FORMATS,
                     help="print the report in this format instead of writing files")
    run.add_argument("--key-map", metavar="JSON",
                     help="JSON file mapping project / workspace / API key ids to "
                          "{business_unit, use_case, data_classification}")
    run.add_argument("--customer-label", default="{{customer_label}}")
    run.add_argument("--report-id", default=None, help="default: teardown-<date>")
    run.add_argument("--report-date", default=None, help="default: today (UTC)")
    run.add_argument("--min-cell-calls", type=int, default=10,
                     help="merge table rows with fewer calls into 'Other' (default: 10)")
    run.add_argument("--cache-ttl", type=int, default=3600, metavar="SECONDS",
                     help="exact-match cache TTL (default: 3600)")
    run.add_argument("--cache-off-at", choices=LEVELS, default="confidential",
                     help="sensitivity at and above which the cache is off (default: confidential)")
    run.add_argument("--unclassified-default", choices=LEVELS, default="confidential",
                     help="level of a call with no declared classification (default: confidential)")
    run.add_argument("--allow-provider", action="append", metavar="PROVIDER",
                     help="limit same-model arbitrage to these providers (repeatable)")
    run.add_argument("--learning-tuples", metavar="PATH",
                     help="also write learning tuples (JSONL, no prompt text) to PATH")
    run.add_argument("--policy", metavar="JSON",
                     help="route-policy.v1 file: replay every call through it and add the "
                          "sensitivity mix and projected spend per strategy (#13)")
    run.add_argument("--scan-prompts", action="store_true",
                     help="full mode (needs --policy): scan generic-jsonl message text "
                          "locally for PII and use it in the policy heuristic; only "
                          "category names reach the report")
    run.add_argument("--eval-results", metavar="JSON",
                     help="ps-eval --out-json results (needs --policy): add quality parity "
                          "and savings at parity (#14); no prompt is executed")
    rec = sub.add_parser(
        "reconcile",
        help="compare recorded event costs with a provider invoice or usage export (M§3.1)",
    )
    rec.add_argument("events", nargs="+",
                     help="Prompt Shields events: ai_usage_events rows or SDK / gateway "
                          "events (JSONL, JSON or CSV)")
    rec.add_argument("--invoice", action="append", default=[], metavar="CSV",
                     help="invoice CSV: date, provider, model, amount_usd (repeatable)")
    rec.add_argument("--usage", action="append", default=[], metavar="FILE",
                     help="OpenAI / Anthropic usage export, priced at list price (repeatable)")
    rec.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE_PCT, metavar="PCT",
                     help="flag gaps above this percentage (default: 2)")
    rec.add_argument("--format", choices=("markdown", "json"), default="markdown")
    rec.add_argument("--out", metavar="PATH", help="write the report here (default: stdout)")
    rec.add_argument("--fail-on-gap", action="store_true",
                     help="exit with status 3 when anything is flagged")
    return parser


def _reconcile(parser: argparse.ArgumentParser, args) -> int:
    if not args.invoice and not args.usage:
        parser.error("reconcile: give --invoice and/or --usage")
    if args.tolerance < 0:
        parser.error("--tolerance: must not be negative")
    for path in [*args.events, *args.invoice, *args.usage]:
        if not Path(path).is_file():
            parser.error(f"input not found: {path}")

    events = load_records(args.events, fmt="ps-events")
    ours = ps_side(events.records)
    ours.sources = events.sources

    theirs = Side()
    for path in args.invoice:
        side = read_invoice_csv(path)
        theirs.lines += side.lines
        theirs.sources += side.sources
    if args.usage:
        loaded = load_records(args.usage)
        wrong = [s.file for s in loaded.sources
                 if s.source not in ("openai-usage", "anthropic-usage")]
        if wrong:
            parser.error(f"--usage: not an OpenAI or Anthropic usage export: {', '.join(wrong)}")
        side = usage_export_side(loaded.records)
        both = {line.provider for line in side.lines} & {line.provider for line in theirs.lines}
        if both:
            parser.error(f"--invoice and --usage both cover {', '.join(sorted(both))}; "
                         "give one per provider")
        theirs.lines += side.lines
        theirs.unpriced.update(side.unpriced)
        theirs.sources += loaded.sources

    report = reconcile(ours, theirs, args.tolerance)
    text = render(report, args.format)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"ps-teardown: wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    print(f"ps-teardown: reconciliation {report['status']} "
          f"({report['flagged_rows']} flagged row(s))", file=sys.stderr)
    return 3 if args.fail_on_gap and report["status"] != RECONCILED else 0


def _read_json(parser: argparse.ArgumentParser, flag: str, path: str):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        parser.error(f"{flag}: {exc}")


def _check_policy(parser: argparse.ArgumentParser, policy) -> None:
    if not isinstance(policy, dict) or policy.get("schema_version") != "route-policy.v1":
        parser.error("--policy: expected a route-policy.v1 document")
    if not isinstance(policy.get("groups"), dict) or "default_group" not in policy:
        parser.error("--policy: missing 'groups' or 'default_group'")
    if policy.get("strategy", "heuristic") != "heuristic":
        parser.error("--policy: replay supports only the 'heuristic' strategy")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "reconcile":
        return _reconcile(parser, args)

    for path in args.inputs:
        if not Path(path).is_file():
            parser.error(f"input not found: {path}")
    key_map = None
    if args.key_map:
        try:
            key_map = json.loads(Path(args.key_map).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            parser.error(f"--key-map: {exc}")
        if not isinstance(key_map, dict):
            parser.error("--key-map: expected a JSON object")

    policy = eval_results = None
    if (args.scan_prompts or args.eval_results) and not args.policy:
        parser.error("--scan-prompts and --eval-results need --policy")
    if args.policy:
        policy = _read_json(parser, "--policy", args.policy)
        _check_policy(parser, policy)
    if args.eval_results:
        eval_results = _read_json(parser, "--eval-results", args.eval_results)
        try:
            read_eval_results(eval_results)
        except EvalResultsError as exc:
            parser.error(f"--eval-results: {exc}")

    loaded = load_records(args.inputs, fmt=args.input_format, key_map=key_map,
                          keep_messages=args.scan_prompts)
    source = SdkPriceSource()
    priced = price_records(loaded.records, source)
    date = args.report_date or datetime.now(UTC).strftime("%Y-%m-%d")
    options = ReportOptions(
        report_id=args.report_id or f"teardown-{date}",
        report_date=date,
        customer_label=args.customer_label,
        tool_version=f"ps-teardown {__version__}",
        min_cell_calls=args.min_cell_calls,
        cache_ttl_seconds=args.cache_ttl,
        cache_off_at=args.cache_off_at,
        unclassified_default=args.unclassified_default,
        allowed_providers=args.allow_provider,
        policy=policy,
        policy_name=Path(args.policy).name if args.policy else "",
        scan_prompts=args.scan_prompts,
        eval_results=eval_results,
    )
    report = build_report(priced, loaded.sources, options, source)

    if args.learning_tuples:
        with open(args.learning_tuples, "w", encoding="utf-8") as fh:
            n = write_learning_tuples(priced, fh)
        print(f"ps-teardown: wrote {n} learning tuples to {args.learning_tuples}", file=sys.stderr)

    if args.stdout:
        sys.stdout.write(render_report(report, args.stdout))
        return 0
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for fmt in args.formats or REPORT_FORMATS:
        path = out_dir / f"{REPORT_BASENAME}.{_EXT[fmt]}"
        path.write_text(render_report(report, fmt), encoding="utf-8")
        print(f"ps-teardown: wrote {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
