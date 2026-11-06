"""Invoice reconciliation (``ps-teardown reconcile``, M§3.1, #24).

Compares what Prompt Shields computed a call cost (the event's own ``cost``:
``ai_usage_events.cost`` rows, or SDK / gateway events) with what the
provider says, per provider, model and UTC day, and flags every gap above
the tolerance (±2% by default, PRD G4).

The provider side is either

* an invoice CSV (:func:`read_invoice_csv`): billed amounts, one row per
  ``date``, ``provider``, ``model`` and ``amount_usd``. A row without a
  model is a provider total for that day and a row without a date one for
  the whole window; the comparison for that provider is then made at that
  coarser grain. Or
* a provider usage export (``openai-usage`` / ``anthropic-usage``, the
  formats ``ps-teardown run`` reads): token counts, priced here at catalog
  list price (M§4). This catches traffic missing from the telemetry and
  token mismatches, but not negotiated discounts or credits, which only an
  invoice shows.

Prompt Shields' side is never re-priced: reconciliation checks the cost the
platform recorded. Events without a cost are counted, never taken as zero
(M§4.5). On the Prompt Shields side, only days inside the provider side's
window are compared; a provider with no provider-side data is listed as not
compared.

Everything runs locally; invoice amounts are read from the file and written
only to the report.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prompt_shields.teardown.engine import price_records
from prompt_shields.teardown.records import (
    SourceStats,
    TeardownRecord,
    _float,
    _str,
    infer_provider,
    normalise_provider,
    parse_time,
)

#: Gap (percent of the provider amount) above which a row is flagged (PRD G4).
DEFAULT_TOLERANCE_PCT = 2.0

#: Status of one comparison row.
RECONCILED = "reconciled"
UNRECONCILED = "unreconciled"
MISSING_IN_PS = "missing_in_ps"
MISSING_IN_PROVIDER = "missing_in_provider"

#: A row key part meaning "all models" / "the whole window".
ANY = "*"

_DATE_COLUMNS = ("date", "day", "usage_date", "start_time", "starting_at", "period_start")
_PROVIDER_COLUMNS = ("provider", "vendor")
_MODEL_COLUMNS = ("model", "served_model", "line_item")
_AMOUNT_COLUMNS = ("amount_usd", "cost_usd", "amount", "cost")

#: An invoice CSV larger than this is not read (dropped as ``too_large``):
#: invoices are per provider / model / day, so this is far above any real one.
MAX_INVOICE_BYTES = 32 * 1024 * 1024
#: Rows past this many in one invoice CSV are not read (``too_many_rows``).
MAX_INVOICE_ROWS = 200_000
#: Provider and model labels from an invoice are cut to this many characters.
MAX_LABEL_CHARS = 128

# Same version-stripping the SDK's pricing applies (providers.py
# ``pricing_candidates``, teardown ``catalog.resolve_model``): an OpenAI-style
# date (``gpt-4o-2024-08-06`` -> ``gpt-4o``), a Google ``models/`` prefix and a
# Bedrock cross-region profile (``us.anthropic...``). Invoices and usage
# exports name snapshots and aliases inconsistently, so both sides are keyed
# by the base model; otherwise one model shows as two "missing" rows.
_DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")
_BEDROCK_PROFILE = re.compile(r"^(?:us|eu|apac|us-gov|global|jp|au|ca)\.")


@dataclass
class Line:
    """One amount on one side, keyed by provider, model and day (``None``
    model or day: the provider total for that day / window)."""

    provider: str
    model: str | None
    day: str | None
    amount: float


@dataclass
class Side:
    """Amounts from one side, plus what could not be used."""

    lines: list[Line] = field(default_factory=list)
    #: Rows or records on this side that carry no amount, per provider.
    unpriced: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    sources: list[SourceStats] = field(default_factory=list)


def _model_key(model: str | None) -> str | None:
    model = _str(model)
    if not model:
        return None
    m = model.lower()[:MAX_LABEL_CHARS]
    m = m.rsplit("/", 1)[-1] if "/" in m else m
    m = _BEDROCK_PROFILE.sub("", _DATE_SUFFIX.sub("", m))
    return m or None


def _provider_key(value: Any) -> str | None:
    label = _str(value)
    return normalise_provider(label[:MAX_LABEL_CHARS]) if label else None


def _day(record: TeardownRecord) -> str | None:
    return record.timestamp.date().isoformat() if record.timestamp else None


def ps_side(records: Iterable[TeardownRecord]) -> Side:
    """Prompt Shields' side: each event's own ``cost`` (``event_cost``)."""
    side = Side()
    for rec in records:
        provider = rec.provider or infer_provider(rec.pricing_model)
        if provider is None:
            continue
        if rec.event_cost is None:
            side.unpriced[provider] += 1
            continue
        side.lines.append(Line(provider, _model_key(rec.pricing_model), _day(rec), rec.event_cost))
    return side


def usage_export_side(records: Iterable[TeardownRecord]) -> Side:
    """The provider side from usage exports, priced at catalog list price."""
    side = Side()
    for p in price_records(records):
        provider = p.provider or "unknown"
        if p.cost is None:
            side.unpriced[provider] += 1
            continue
        side.lines.append(
            Line(provider, _model_key(p.record.pricing_model), _day(p.record), p.cost)
        )
    return side


def _pick(row: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def read_invoice_csv(path: str | Path) -> Side:
    """Read an invoice CSV (see the module docstring). Rows in a currency
    other than USD, or without a usable amount or provider, are dropped and
    counted per reason."""
    path = Path(path)
    stats = SourceStats(source="invoice-csv", file=path.name)
    side = Side(sources=[stats])
    if path.stat().st_size > MAX_INVOICE_BYTES:
        stats.drop("too_large")
        return side
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    try:
        for raw in reader:
            if len(rows) >= MAX_INVOICE_ROWS:
                stats.drop("too_many_rows")
                break
            rows.append(raw)
    except csv.Error:
        stats.drop("invalid_csv")
        return side
    for raw in rows:
        stats.rows_read += 1
        row = {(k or "").strip().lower(): (v.strip() if isinstance(v, str) else v)
               for k, v in raw.items()}
        currency = _str(row.get("currency"))
        if currency and currency.upper() != "USD":
            stats.drop("not_usd")
            continue
        amount = _float(_pick(row, _AMOUNT_COLUMNS))
        if amount is None:
            stats.drop("no_amount")
            continue
        model = _str(_pick(row, _MODEL_COLUMNS))
        provider = _provider_key(_pick(row, _PROVIDER_COLUMNS)) or infer_provider(model)
        if provider is None:
            stats.drop("no_provider")
            continue
        date_value = _pick(row, _DATE_COLUMNS)
        when = parse_time(date_value)
        if date_value is not None and when is None:
            stats.drop("bad_date")
            continue
        stats.rows_used += 1
        side.lines.append(
            Line(provider, _model_key(model), when.date().isoformat() if when else None, amount)
        )
    return side


def _round(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None


def _status(ps: float | None, provider: float | None, tolerance_pct: float):
    """``(status, gap_pct)`` for one pair of amounts (None: no line). The
    gap is relative to the provider amount, so it is None when that is 0."""
    p, t = _round(ps), _round(provider)
    if t is None:
        return (RECONCILED if p == 0 else MISSING_IN_PROVIDER), None
    if p is None:
        return (RECONCILED if t == 0 else MISSING_IN_PS), (-100.0 if t else None)
    if t == 0:
        return (RECONCILED if p == 0 else UNRECONCILED), None
    gap = round((p - t) / t * 100, 2)
    return (RECONCILED if abs(gap) <= tolerance_pct else UNRECONCILED), gap


def reconcile(ps: Side, provider: Side, tolerance_pct: float = DEFAULT_TOLERANCE_PCT) -> dict:
    """Compare the two sides per provider, model and day; flag every row
    whose gap exceeds ``tolerance_pct`` (or that one side lacks)."""
    by_provider: dict[str, list[Line]] = defaultdict(list)
    for line in provider.lines:
        by_provider[line.provider].append(line)
    ps_by_provider: dict[str, list[Line]] = defaultdict(list)
    for line in ps.lines:
        ps_by_provider[line.provider].append(line)

    rows: list[dict] = []
    totals: list[dict] = []
    for name in sorted(by_provider):
        lines = by_provider[name]
        # A provider-side row without a model / date sets the grain for that
        # provider: everything is compared per day / over the window.
        any_model = any(line.model is None for line in lines)
        any_day = any(line.day is None for line in lines)
        days = sorted({line.day for line in lines if line.day})
        first, last = (days[0], days[-1]) if days else (None, None)

        def key(line: Line, any_model=any_model, any_day=any_day) -> tuple[str, str]:
            return (ANY if any_model else (line.model or ANY), ANY if any_day else (line.day or ANY))

        theirs: dict[tuple[str, str], float] = defaultdict(float)
        for line in lines:
            theirs[key(line)] += line.amount
        ours: dict[tuple[str, str], float] = defaultdict(float)
        outside = 0
        for line in ps_by_provider.get(name, []):
            if not any_day and (line.day is None or not first <= line.day <= last):
                outside += 1
                continue
            ours[key(line)] += line.amount

        ps_total = provider_total = 0.0
        for model, day in sorted(set(theirs) | set(ours)):
            p_amt, t_amt = ours.get((model, day)), theirs.get((model, day))
            ps_total += p_amt or 0.0
            provider_total += t_amt or 0.0
            status, gap = _status(p_amt, t_amt, tolerance_pct)
            rows.append({
                "provider": name,
                "model": model,
                "day": day,
                "ps_cost_usd": _round(p_amt),
                "provider_usd": _round(t_amt),
                "gap_pct": gap,
                "status": status,
                "flagged": status != RECONCILED,
            })
        status, gap = _status(ps_total, provider_total, tolerance_pct)
        totals.append({
            "provider": name,
            "window": {"start": first, "end": last},
            "ps_cost_usd": _round(ps_total),
            "provider_usd": _round(provider_total),
            "gap_pct": gap,
            "status": status,
            "ps_events_without_cost": ps.unpriced.get(name, 0),
            "provider_rows_without_amount": provider.unpriced.get(name, 0),
            "ps_lines_outside_window": outside,
        })

    not_compared = [
        {
            "provider": name,
            "ps_cost_usd": round(sum(line.amount for line in ps_by_provider[name]), 6),
        }
        for name in sorted(set(ps_by_provider) - set(by_provider))
    ]
    flagged = [r for r in rows if r["flagged"]]
    return {
        "schema_version": "reconcile.v1",
        "tolerance_pct": tolerance_pct,
        "status": (
            UNRECONCILED
            if flagged or any(t["status"] != RECONCILED for t in totals)
            else RECONCILED
        ),
        "flagged_rows": len(flagged),
        "providers": totals,
        "rows": rows,
        "not_compared": not_compared,
        "sources": [
            {"source": s.source, "file": s.file, "rows_read": s.rows_read,
             "rows_used": s.rows_used, "dropped": dict(s.dropped)}
            for s in ps.sources + provider.sources
        ],
    }


def _fmt_usd(value: float | None) -> str:
    return "—" if value is None else f"${value:,.6f}".rstrip("0").rstrip(".")


def _fmt_gap(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}%"


def _cell(value: Any) -> str:
    """A label for a Markdown table cell: no line breaks, ``|`` escaped."""
    return _CONTROL_OR_PIPE.sub(lambda m: "\\|" if m.group() == "|" else " ", str(value))


_CONTROL_OR_PIPE = re.compile(r"[\x00-\x1f\x7f|]")


def render_markdown(report: Mapping[str, Any]) -> str:
    tol = report["tolerance_pct"]
    out = [
        "# Invoice reconciliation",
        "",
        (
            f"Prompt Shields cost (events' own `cost`) against the provider, per provider, "
            f"model and UTC day. Gaps above ±{tol:g}% are flagged (M§3.1)."
        ),
        "",
        f"**Status: {report['status']}** — {report['flagged_rows']} flagged row(s).",
        "",
        "## Per provider",
        "",
        "| Provider | Window | Prompt Shields | Provider | Gap | Status | Events without cost |",
        "|---|---|---:|---:|---:|---|---:|",
    ]
    for t in report["providers"]:
        w = t["window"]
        window = f"{w['start']} – {w['end']}" if w["start"] else "whole window"
        out.append(
            f"| {_cell(t['provider'])} | {window} | {_fmt_usd(t['ps_cost_usd'])} | "
            f"{_fmt_usd(t['provider_usd'])} | {_fmt_gap(t['gap_pct'])} | {t['status']} | "
            f"{t['ps_events_without_cost']} |"
        )
    out += [
        "",
        "## Per provider, model and day",
        "",
        "| Provider | Model | Day | Prompt Shields | Provider | Gap | Status |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for r in report["rows"]:
        mark = " ⚠" if r["flagged"] else ""
        out.append(
            f"| {_cell(r['provider'])} | {_cell(r['model'])} | {r['day']} | "
            f"{_fmt_usd(r['ps_cost_usd'])} | "
            f"{_fmt_usd(r['provider_usd'])} | {_fmt_gap(r['gap_pct'])} | {r['status']}{mark} |"
        )
    if report["not_compared"]:
        out += ["", "## Not compared (no provider data supplied)", ""]
        out += [
            f"- {_cell(n['provider'])}: {_fmt_usd(n['ps_cost_usd'])}"
            for n in report["not_compared"]
        ]
    out += [
        "",
        (
            "`*` is a provider-side total over all models or the whole window. Usual causes "
            "of a gap: negotiated discounts or credits (usage exports are priced at list "
            "price, so only an invoice shows them), traffic missing from telemetry, events "
            "without a cost, price changes inside the window."
        ),
        "",
    ]
    return "\n".join(out)


def render(report: Mapping[str, Any], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(report, indent=2, sort_keys=False) + "\n"
    return render_markdown(report)
