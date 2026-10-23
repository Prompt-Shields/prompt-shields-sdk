"""``ps-teardown``: the AI bill teardown CLI, cost-only mode (#12).

    ps-teardown run usage.json events.jsonl --out-dir report/ --customer-label "Acme"

Reads local files only and makes no network calls. Writes the report as
Markdown, HTML and JSON (``teardown-report.{md,html,json}``), and with
``--learning-tuples`` the learning-tuple export (#42).
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
from prompt_shields.teardown.records import FORMATS, load_records
from prompt_shields.teardown.render import render_report
from prompt_shields.teardown.tuples import write_learning_tuples

REPORT_BASENAME = "teardown-report"
REPORT_FORMATS = ("markdown", "html", "json")
_EXT = {"markdown": "md", "html": "html", "json": "json"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ps-teardown",
        description="AI bill teardown from usage logs, run locally (cost-only mode).",
    )
    parser.add_argument("--version", action="version", version=f"ps-teardown {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="build a cost-only teardown report from input files")
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

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

    loaded = load_records(args.inputs, fmt=args.input_format, key_map=key_map)
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
