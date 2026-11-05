"""Pricing and cost-only analysis for ``ps-teardown`` (#12).

``price_records`` prices normalised records through a :class:`PriceSource`;
``build_report`` turns priced records into the report data (a JSON-ready
``dict``) that ``render`` turns into Markdown or HTML. Sections that need
policy replay or quality parity are present in the data as ``pending``
markers, never estimated (methodology M§1), unless the options carry a
route policy (``ReportOptions.policy``: sensitivity mix and policy replay,
#13) and ``ps-eval`` results (``ReportOptions.eval_results``: quality parity
and savings at parity, #14); see ``policy.py``.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from prompt_shields.teardown import policy as policy_mode
from prompt_shields.teardown.catalog import PriceSource, SdkPriceSource
from prompt_shields.teardown.records import SourceStats, TeardownRecord, infer_provider

REPORT_SCHEMA = "ps-teardown-report.v1"

COST_PROVIDER_REPORTED = "provider_reported"
COST_PRICING_TABLE = "pricing_table"
COST_ESTIMATED_TOKENS = "pricing_table_estimated_tokens"
#: Cost sources the template lists, in its order (M§4.3).
COST_SOURCES = (COST_PROVIDER_REPORTED, COST_PRICING_TABLE, COST_ESTIMATED_TOKENS, "litellm")

LEVELS = ("public", "internal", "confidential", "restricted")

#: Template sections that need an input the run may not have, and the issue
#: that built them: a route policy (#13) or ``ps-eval`` results (#14). A
#: section without its input stays pending; it is never estimated.
PENDING = {
    "sensitivity_mix": "#13",
    "policy_replay": "#13",
    "quality_parity": "#14",
    "savings_at_parity": "#14",
}

OTHER = "Other"


@dataclass
class PricedRecord:
    """A :class:`TeardownRecord` with its baseline cost (M§4)."""

    record: TeardownRecord
    provider: str | None
    #: Model the cost was computed at (the catalog's id), or the raw name
    #: when it is unpriced.
    model: str | None
    cost: float | None
    #: One of ``COST_SOURCES`` (or another framework name); None when null.
    cost_source: str | None
    #: Why ``cost`` is None (``catalog.NULL_*``); None when priced.
    null_reason: str | None = None
    #: No ``served_model``: priced at ``requested_model`` (M§4.2).
    priced_at_requested: bool = False

    @property
    def calls(self) -> int | None:
        return self.record.calls

    @property
    def tokens(self) -> int:
        return (self.record.tokens_in or 0) + (self.record.tokens_out or 0)


def price_records(
    records: Iterable[TeardownRecord], source: PriceSource | None = None
) -> list[PricedRecord]:
    """Price every record at its served model (M§4.2, M§4.3).

    A provider-reported amount is used as it is. Otherwise the usage is
    re-priced from token counts. A framework's own cost (LiteLLM) is used
    only when the catalog has no price. Anything else is a null cost with a
    reason; it is never zero.
    """
    source = source or SdkPriceSource()
    out: list[PricedRecord] = []
    for rec in records:
        name = rec.pricing_model
        provider = rec.provider or infer_provider(name)
        at_requested = rec.served_model is None and rec.requested_model is not None
        priced = None if name == "auto" else source.resolve_model(provider, name)
        if (
            priced is None
            and rec.served_model
            and rec.requested_model not in (None, "auto", rec.served_model)
        ):
            # A served id with no price (an unlisted snapshot) is priced at
            # the requested model, as the SDK does since #229
            # (``pricing.resolve_pricing_model``: served first, requested last).
            priced = source.resolve_model(provider, rec.requested_model)
            at_requested = priced is not None
        if rec.provider_cost is not None:
            out.append(PricedRecord(rec, provider, priced or name, round(rec.provider_cost, 6),
                                    COST_PROVIDER_REPORTED, None, at_requested))
            continue
        cost = source.cost(rec, provider, priced) if priced and provider else None
        if cost is not None:
            src = COST_ESTIMATED_TOKENS if rec.tokens_estimated else COST_PRICING_TABLE
            out.append(PricedRecord(rec, provider, priced, cost, src, None, at_requested))
        elif rec.framework_cost is not None:
            out.append(PricedRecord(rec, provider, priced or name, round(rec.framework_cost, 6),
                                    rec.framework_cost_source, None, at_requested))
        else:
            reason = source.null_reason(rec if rec.provider else replace(rec, provider=provider))
            out.append(PricedRecord(rec, provider, priced or name, None, None, reason,
                                    at_requested))
    return out


# --- report options -----------------------------------------------------------


@dataclass
class ReportOptions:
    report_id: str = "teardown"
    report_date: str = ""
    customer_label: str = "{{customer_label}}"
    tool_version: str = ""
    #: Rows covering fewer calls are merged into "Other" (M§2.3).
    min_cell_calls: int = 10
    #: Exact-match cache TTL (M§6.4).
    cache_ttl_seconds: int = 3600
    #: The cache is off for this level and above (PRD default).
    cache_off_at: str = "confidential"
    #: Level of a call with no declared classification (M§5.1).
    unclassified_default: str = "confidential"
    #: Providers arbitrage may move traffic to; None means any in the catalog.
    allowed_providers: Sequence[str] | None = None
    top_drivers: int = 3
    catalog_version: str = ""
    extra_caveats: list[str] = field(default_factory=list)
    #: A route-policy.v1 document: adds the sensitivity mix and policy
    #: replay (#13). ``policy_name`` is the file name recorded in the report.
    policy: Mapping[str, Any] | None = None
    policy_name: str = ""
    #: Full mode: records carry ``messages`` and replay scans them locally
    #: for PII and runs the heuristic on them.
    scan_prompts: bool = False
    #: A ``ps-eval --out-json`` report: adds quality parity and savings at
    #: parity (#14). Needs ``policy``.
    eval_results: Mapping[str, Any] | None = None


# --- helpers -------------------------------------------------------------------


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 6)


def _pct(part: float, whole: float) -> float | None:
    return None if not whole else round(100.0 * part / whole, 2)


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


@dataclass
class _Cell:
    calls: int = 0
    calls_unknown: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    spend: float = 0.0
    priced: int = 0
    by_model: Counter = field(default_factory=Counter)

    def add(self, p: PricedRecord) -> None:
        if p.calls is None:
            self.calls_unknown += 1
        else:
            self.calls += p.calls
        r = p.record
        self.tokens_in += r.tokens_in or 0
        self.tokens_out += r.tokens_out or 0
        self.tokens_cached += r.tokens_cached or 0
        if p.cost is not None:
            self.spend += p.cost
            self.priced += 1
            self.by_model[p.model or "(unknown)"] += p.cost

    def merge(self, other: _Cell) -> None:
        self.calls += other.calls
        self.calls_unknown += other.calls_unknown
        self.tokens_in += other.tokens_in
        self.tokens_out += other.tokens_out
        self.tokens_cached += other.tokens_cached
        self.spend += other.spend
        self.priced += other.priced
        self.by_model.update(other.by_model)

    def small(self, min_calls: int) -> bool:
        # A cell whose call count is unknown (aggregated export rows) is not
        # a small cell: it stands for a whole bucket of traffic.
        return self.calls_unknown == 0 and self.calls < min_calls

    def as_dict(self, baseline: float) -> dict:
        return {
            "calls": self.calls,
            "calls_unknown_rows": self.calls_unknown,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_cached": self.tokens_cached,
            "spend": _r(self.spend) if self.priced else None,
            "share_pct": _pct(self.spend, baseline) if self.priced else None,
            "main_model": self.by_model.most_common(1)[0][0] if self.by_model else None,
        }


def _cells(priced: Iterable[PricedRecord], key, min_calls: int) -> list[tuple[tuple, _Cell]]:
    cells: dict[tuple, _Cell] = defaultdict(_Cell)
    for p in priced:
        cells[key(p)].add(p)
    kept, other = [], _Cell()
    merged = 0
    for k, c in cells.items():
        if c.small(min_calls):
            other.merge(c)
            merged += 1
        else:
            kept.append((k, c))
    kept.sort(key=lambda kc: (-kc[1].spend, -kc[1].calls, [str(x) for x in kc[0]]))
    if merged:
        kept.append(((OTHER,), other))
    return kept


# --- sections ------------------------------------------------------------------


def _window(priced: Sequence[PricedRecord]) -> dict:
    starts = [p.record.timestamp for p in priced if p.record.timestamp]
    ends = [p.record.timestamp_end or p.record.timestamp for p in priced if p.record.timestamp]
    if not starts:
        return {"start": None, "end": None, "days": None, "records_without_timestamp": len(priced)}
    start, end = min(starts), max(ends)
    days = (end - start).total_seconds() / 86400.0
    return {
        "start": _iso(start),
        "end": _iso(end),
        "days": round(days, 2) if days > 0 else None,
        "records_without_timestamp": sum(1 for p in priced if not p.record.timestamp),
    }


def _month(amount: float | None, days: float | None) -> float | None:
    if amount is None or not days:
        return None
    return _r(amount * 30.0 / days)


def _baseline(priced: Sequence[PricedRecord], opts: ReportOptions) -> dict:
    baseline = math.fsum(p.cost for p in priced if p.cost is not None)
    rows = []
    for k, c in _cells(priced, lambda p: (p.provider or "(unknown)", p.model or "(unknown)"),
                       opts.min_cell_calls):
        d = c.as_dict(baseline)
        d.pop("main_model")
        rows.append({"provider": k[0], "model": k[1] if len(k) > 1 else "", **d})
    by_source: dict[str, dict] = {}
    for p in priced:
        name = p.cost_source or "null"
        s = by_source.setdefault(name, {"calls": 0, "calls_unknown_rows": 0, "spend": 0.0})
        if p.calls is None:
            s["calls_unknown_rows"] += 1
        else:
            s["calls"] += p.calls
        if p.cost is not None:
            s["spend"] += p.cost
    for s in by_source.values():
        s["spend"] = _r(s["spend"])
    by_source.get("null", {}).update(spend=None)
    nulls: dict[str, dict] = {}
    for p in priced:
        if p.cost is None:
            n = nulls.setdefault(p.null_reason or "unpriced", {"calls": 0, "calls_unknown_rows": 0, "tokens": 0})
            if p.calls is None:
                n["calls_unknown_rows"] += 1
            else:
                n["calls"] += p.calls
            n["tokens"] += p.tokens
    mismatch = sum(
        p.calls or 0 for p in priced
        if p.record.served_model and p.record.requested_model
        and p.record.served_model != p.record.requested_model
    )
    total_calls = sum(p.calls or 0 for p in priced)
    return {
        "spend": _r(baseline),
        "total_calls": total_calls,
        "total_calls_unknown_rows": sum(1 for p in priced if p.calls is None),
        "rows": rows,
        "by_cost_source": dict(sorted(by_source.items())),
        "null_cost_by_reason": dict(sorted(nulls.items())),
        "requested_ne_served_calls": mismatch,
        "requested_ne_served_pct": _pct(mismatch, total_calls),
        "priced_at_requested_calls": sum(p.calls or 0 for p in priced if p.priced_at_requested),
        "repriced_from_tokens": any(p.cost_source in (COST_PRICING_TABLE, COST_ESTIMATED_TOKENS) for p in priced),
    }


def _trust(priced: Sequence[PricedRecord], baseline: float) -> dict:
    calls_known = sum(p.calls for p in priced if p.calls is not None)
    priced_calls = sum(p.calls for p in priced if p.calls is not None and p.cost is not None)
    tokens = sum(p.tokens for p in priced)
    priced_tokens = sum(p.tokens for p in priced if p.cost is not None)
    estimated = math.fsum(p.cost for p in priced if p.cost_source == COST_ESTIMATED_TOKENS)
    null_tokens_pct = _pct(tokens - priced_tokens, tokens)
    est_pct = _pct(estimated, baseline)
    caveats = []
    if est_pct is not None and est_pct > 5:
        caveats.append(
            f"{est_pct}% of baseline spend comes from estimated token counts (M§4.3)."
        )
    if null_tokens_pct is not None and null_tokens_pct > 10:
        caveats.append(
            f"{null_tokens_pct}% of tokens could not be priced; savings figures cover only "
            "the priced part of the traffic (M§4.5)."
        )
    return {
        "priced_calls_pct": _pct(priced_calls, calls_known),
        "priced_tokens_pct": _pct(priced_tokens, tokens),
        "estimated_spend_pct": est_pct if est_pct is not None else 0.0,
        "null_tokens_pct": null_tokens_pct,
        "caveats": caveats,
    }


def _attribution(priced: Sequence[PricedRecord], baseline: float, opts: ReportOptions) -> dict:
    rows = []
    for k, c in _cells(priced, lambda p: (p.record.business_unit, p.record.use_case),
                       opts.min_cell_calls):
        if k == (OTHER,):
            bu, uc = OTHER, None
        elif k == (None, None):
            bu, uc = "(unattributed)", None
        else:
            bu, uc = k[0] or "(none)", k[1] or "(none)"
        rows.append({"business_unit": bu, "use_case": uc, **c.as_dict(baseline)})

    drivers = []
    for k, c in _cells(priced, lambda p: (p.record.use_case, p.model), opts.min_cell_calls):
        if k == (OTHER,) or not c.priced or len(drivers) >= opts.top_drivers:
            continue
        per_call = round(c.tokens_in / c.calls) if c.calls and not c.calls_unknown else None
        drivers.append({
            "use_case": k[0] or "(unattributed)",
            "model": k[1],
            "calls": c.calls,
            "calls_unknown_rows": c.calls_unknown,
            "spend": _r(c.spend),
            "share_pct": _pct(c.spend, baseline),
            "avg_input_tokens_per_call": per_call,
            "cached_input_pct": _pct(c.tokens_cached, c.tokens_in) or 0.0,
        })
    return {"rows": rows, "drivers": drivers}


def _arbitrage(priced: Sequence[PricedRecord], source: PriceSource, baseline: float,
               opts: ReportOptions,
               complies: Callable[[PricedRecord, str, str], bool] | None = None) -> dict:
    """Same-model arbitrage (M§6.1).

    ``complies(record, provider, model)`` checks the cheaper host against the
    route policy's data policy at the call's effective level (with a route
    policy); a host whose sovereignty facts are unknown does not comply.
    """
    allowed = set(opts.allowed_providers) if opts.allowed_providers else None
    cells: dict[tuple[str, str], dict] = {}
    total_saving = 0.0
    blocked_calls = 0
    for p in priced:
        # Only token-priced calls can be re-priced at another host on the
        # same basis; provider-billed amounts are left as they are.
        if p.cost is None or p.cost_source not in (COST_PRICING_TABLE, COST_ESTIMATED_TOKENS):
            continue
        best = None
        blocked = False
        for alt_provider, alt_model in source.equivalents(p.provider, p.model):
            if allowed is not None and alt_provider not in allowed:
                continue
            if not source.covers(p.record, alt_provider, alt_model):
                continue
            alt = source.cost(p.record, alt_provider, alt_model)
            if alt is None or alt >= p.cost or (best is not None and alt >= best[0]):
                continue
            if complies is not None and not complies(p, alt_provider, alt_model):
                blocked = True
                continue
            best = (alt, alt_provider)
        if best is None:
            blocked_calls += (p.calls or 0) if blocked else 0
            continue
        cell = cells.setdefault((p.model, p.provider), {
            "spend_now": 0.0, "spend_after": 0.0, "to": Counter(), "calls": 0,
        })
        cell["spend_now"] += p.cost
        cell["spend_after"] += best[0]
        cell["to"][best[1]] += 1
        cell["calls"] += p.calls or 0
        total_saving += p.cost - best[0]
    rows = []
    for (model, provider), c in sorted(cells.items(), key=lambda kv: kv[1]["spend_after"] - kv[1]["spend_now"]):
        saving = c["spend_now"] - c["spend_after"]
        rows.append({
            "model": model,
            "current_provider": provider,
            "cheaper_provider": c["to"].most_common(1)[0][0],
            "calls": c["calls"],
            "spend_now": _r(c["spend_now"]),
            "spend_after": _r(c["spend_after"]),
            "saving": _r(saving),
            "saving_pct": _pct(saving, c["spend_now"]),
        })
    return {
        "projected_spend": _r(baseline - total_saving),
        "saving": _r(total_saving),
        "saving_pct": _pct(total_saving, baseline) if baseline else None,
        "rows": rows,
        "allowed_providers": sorted(allowed) if allowed is not None else None,
        # Without a route policy there is no data policy to check the
        # cheaper host against.
        "compliance_checked": complies is not None,
        "calls_blocked_by_data_policy": blocked_calls if complies is not None else None,
    }


def _level(declared: str | None, default: str) -> str:
    d = (declared or "").strip().lower()
    return d if d in LEVELS else default


def _cache(
    priced: Sequence[PricedRecord],
    baseline: float,
    opts: ReportOptions,
    level_of: Callable[[PricedRecord], str] | None = None,
    unclassified_default: str | None = None,
) -> dict:
    """Exact-match cache-hit potential (M§6.4).

    ``level_of`` gives a call's effective level under a route policy (PII
    escalation included); a level outside ``LEVELS`` counts as cache-off.
    Without it, the level is declared, else ``unclassified_default``.
    """
    off_at = LEVELS.index(opts.cache_off_at)
    hashed = [p for p in priced if p.record.prompt_hash and not p.record.aggregated]
    no_ts = [p for p in hashed if p.record.timestamp is None]

    def rank(p: PricedRecord) -> int:
        if level_of is None:
            return LEVELS.index(_level(p.record.data_classification, opts.unclassified_default))
        level = level_of(p)
        return LEVELS.index(level) if level in LEVELS else len(LEVELS)

    excluded = [p for p in hashed if p.record.timestamp is not None and rank(p) >= off_at]
    excluded_ids = {id(p) for p in excluded}
    eligible = sorted(
        (p for p in hashed if p.record.timestamp is not None and id(p) not in excluded_ids),
        key=lambda p: p.record.timestamp,
    )
    written: dict[str, datetime] = {}
    hits, saving = 0, 0.0
    for p in eligible:
        h, t = p.record.prompt_hash, p.record.timestamp
        w = written.get(h)
        if w is not None and (t - w).total_seconds() <= opts.cache_ttl_seconds:
            hits += 1
            saving += p.cost or 0.0
        else:
            written[h] = t
    total_calls = sum(p.calls or 0 for p in priced)
    return {
        "ttl_seconds": opts.cache_ttl_seconds,
        "cache_off_at": opts.cache_off_at,
        "unclassified_default": unclassified_default or opts.unclassified_default,
        "levels_from": "route_policy" if level_of is not None else "declared",
        "calls_with_hash": len(hashed),
        "calls_with_hash_pct": _pct(len(hashed), total_calls),
        "hit_calls": hits,
        "hit_calls_pct": _pct(hits, total_calls),
        "saving": _r(saving),
        "saving_pct": _pct(saving, baseline) if baseline else None,
        "excluded_cache_off_calls": len(excluded),
        "excluded_cache_off_spend": _r(math.fsum(p.cost or 0.0 for p in excluded)),
        "excluded_no_timestamp_calls": len(no_ts),
    }


def _pct100(x: float | None) -> float | None:
    """A 0–1 share as a percentage (the report's unit)."""
    return None if x is None else round(100 * x, 2)


def build_report(
    priced: Sequence[PricedRecord],
    sources: Sequence[SourceStats],
    options: ReportOptions | None = None,
    price_source: PriceSource | None = None,
) -> dict:
    """The teardown report as a JSON-ready dict (template sections).

    Cost-only sections always; with ``options.policy`` also the sensitivity
    mix and policy replay, and with ``options.eval_results`` quality parity
    and savings at parity. Sections whose input is missing stay in
    ``report["pending"]``.
    """
    opts = options or ReportOptions()
    if opts.cache_off_at not in LEVELS or opts.unclassified_default not in LEVELS:
        raise ValueError(f"levels must be one of {', '.join(LEVELS)}")
    if opts.eval_results is not None and opts.policy is None:
        raise ValueError("eval results need a route policy: savings at parity are "
                         "projected through policy replay (M§6.2)")
    source = price_source or SdkPriceSource()
    window = _window(priced)
    base = _baseline(priced, opts)
    spend = base["spend"] or 0.0
    trust = _trust(priced, spend)
    pending = dict(PENDING)
    caveats = list(trust["caveats"])
    sections: dict[str, Any] = {}

    level_of = None
    unclassified_default = None
    if opts.policy is not None:
        calls = policy_mode.replay_priced(priced, opts.policy, source,
                                          scan_prompts=opts.scan_prompts)
        replayed = policy_mode.replay_sections(
            calls, opts.policy, policy_name=opts.policy_name, scan_prompts=opts.scan_prompts)
        sections["policy_replay"] = replayed
        levels = {id(p): c.sensitivity_level for p, c in zip(priced, calls, strict=True)}

        def level_of(p: PricedRecord) -> str:
            return levels[id(p)]

        unclassified_default = replayed["policy"]["unclassified_default"]
        del pending["sensitivity_mix"], pending["policy_replay"]
        if opts.eval_results is not None:
            sections.update(policy_mode.parity_sections(
                priced, calls, opts.policy, source, opts.eval_results,
                scan_prompts=opts.scan_prompts))
            del pending["quality_parity"], pending["savings_at_parity"]
            caveats += sections["quality_parity"]["caveats"]
    complies = (policy_mode.arbitrage_check(opts.policy, level_of)
                if level_of is not None else None)
    arb = _arbitrage(priced, source, spend, opts, complies)
    cache = _cache(priced, spend, opts, level_of, unclassified_default)
    days = window["days"]

    summary: dict[str, Any] = {
        "baseline_window": base["spend"],
        "baseline_month": _month(base["spend"], days),
        "arbitrage_window": arb["projected_spend"],
        "arbitrage_month": _month(arb["projected_spend"], days),
        "arbitrage_saving_pct": arb["saving_pct"],
        "cache_window": cache["saving"],
        "cache_month": _month(cache["saving"], days),
        "cache_pct": cache["saving_pct"],
        "parity": {"pending": PENDING["savings_at_parity"]},
        "sovereign": {"pending": PENDING["sensitivity_mix"]},
        "gate1": {"pending": PENDING["quality_parity"]},
    }
    if "policy_replay" in sections:
        sov = sections["policy_replay"]["sensitivity_mix"]["sovereign"]
        summary["sovereign"] = {
            "spend": sov["spend"],
            "spend_pct": _pct100(sov["share_spend"]),
            "calls_pct": _pct100(sov["share_calls"]),
        }
    if "savings_at_parity" in sections:
        sap = sections["savings_at_parity"]
        point, cons = sap["policy_at_parity"], sap["conservative"]
        summary["parity"] = {
            "window": point["projected_spend"],
            "compared_baseline": point["baseline_spend_compared"],
            "month": _month(point["projected_spend"], days),
            "saving_pct": _pct100(point["savings_pct"]),
            "ci_pct": (None if sap["saving_ci"] is None
                       else [_pct100(x) for x in sap["saving_ci"]]),
            "conservative_saving_pct": _pct100(cons["savings_pct"]),
        }
        summary["gate1"] = {"reading": sap["gate1"]}

    if opts.scan_prompts:
        sensitivity_input = ("declared data_classification, recorded detected_pii_types and "
                             "a local PII scan of message text, escalated per the route policy")
    elif opts.policy is not None:
        sensitivity_input = ("declared data_classification and recorded detected_pii_types, "
                             "escalated per the route policy (no prompt text)")
    else:
        sensitivity_input = ("declared data_classification only; PII escalation needs a "
                             "route policy (" + PENDING["sensitivity_mix"] + ")")
    return {
        "schema": REPORT_SCHEMA,
        "report": {
            "id": opts.report_id,
            "date": opts.report_date,
            "customer_label": opts.customer_label,
            "mode": "full" if opts.scan_prompts or opts.eval_results is not None else "cost-only",
            "tool_version": opts.tool_version,
            "catalog_version": opts.catalog_version or source.version,
            "currency": "USD",
            "min_cell_calls": opts.min_cell_calls,
            "sensitivity_input": sensitivity_input,
            "prompt_text": {
                "scanned_locally": opts.scan_prompts,
                "reexecuted_by_ps_eval": opts.eval_results is not None,
            },
        },
        "window": window,
        "summary": summary,
        "trust": {**trust, "caveats": caveats + list(opts.extra_caveats)},
        "sources": [
            {
                "source": s.source,
                "file": s.file,
                "rows_read": s.rows_read,
                "rows_used": s.rows_used,
                "dropped": dict(sorted(s.dropped.items())),
            }
            for s in sources
        ],
        "baseline": base,
        "attribution": _attribution(priced, spend, opts),
        "arbitrage": arb,
        "cache": cache,
        **sections,
        "pending": pending,
    }
