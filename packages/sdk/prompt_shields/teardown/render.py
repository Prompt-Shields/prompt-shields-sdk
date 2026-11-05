"""Render the teardown report dict as Markdown, HTML or JSON.

The Markdown follows ``docs/teardown/teardown-report-template.md`` section by
section. Computed fields are filled in; fields only a person can fill (the
customer's name, consent, invoice reconciliation, recommendations) keep their
``{{placeholder}}``; sections whose input the run did not have (a route
policy for §7–8, ``ps-eval`` results for §9–10) keep the template's "Not
measured in this teardown — pending #N." line. HTML is the
same document, self-contained (no external assets), so it can be opened
offline on the machine that produced it.
"""

from __future__ import annotations

import html
import json
import re

PENDING_LINE = "*Not measured in this teardown — pending {issue}.*"


def render_report(report: dict, fmt: str = "markdown") -> str:
    """Render ``report`` (from ``build_report``) as ``markdown``, ``html`` or ``json``."""
    if fmt == "json":
        return json.dumps(report, indent=2, sort_keys=False) + "\n"
    md = render_markdown(report)
    if fmt == "markdown":
        return md
    if fmt == "html":
        return markdown_to_html(md, title=f"AI bill teardown — {report['report']['customer_label']}")
    raise ValueError(f"unknown report format {fmt!r}")


# --- formatting -----------------------------------------------------------------


def money(x: float | None, currency: str = "USD") -> str:
    if x is None:
        return "—"
    if x != 0 and abs(x) < 1:
        return f"{currency} {x:,.4f}"
    return f"{currency} {x:,.2f}"


def pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.1f}%"


def num(x: int | None) -> str:
    return "—" if x is None else f"{x:,}"


def plural(n: int | None, word: str) -> str:
    return f"{num(n)} {word}{'' if n == 1 else 's'}"


def _driver_calls(d: dict) -> str:
    if d.get("calls_unknown_rows"):
        return f"call count {calls(d)}"
    return plural(d["calls"], "call")


def calls(row: dict) -> str:
    n, unknown = row.get("calls", 0), row.get("calls_unknown_rows", 0)
    if unknown and not n:
        return "not reported"
    return f"{n:,}+" if unknown else f"{n:,}"


def _table(header: list[str], rows: list[list[str]]) -> str:
    def esc(cell: str) -> str:
        return str(cell).replace("|", "\\|")

    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def _pending(report: dict, key: str) -> str:
    return PENDING_LINE.format(issue=report["pending"][key])


def share(x: float | None) -> str:
    """A 0–1 share (policy replay, ps-eval) as a percentage."""
    return "—" if x is None else f"{100 * x:.1f}%"


def _vs(x: float | None) -> str:
    """A 0–1 saving as "N% lower" or, when negative, "N% higher"."""
    if x is None:
        return "—"
    return f"{share(x)} lower" if x >= 0 else f"{share(-x)} higher"


def _ci(ci: list | tuple | None, fmt=pct) -> str:
    return "—" if not ci else f"{fmt(ci[0])} – {fmt(ci[1])}"


_PARITY_STATUS = {"at_parity": "yes", "below_parity": "no",
                  "insufficient_sample": "insufficient sample"}


# --- markdown ---------------------------------------------------------------------


def render_markdown(r: dict) -> str:
    meta, win, s, base = r["report"], r["window"], r["summary"], r["baseline"]
    arb, cache, trust = r["arbitrage"], r["cache"], r["trust"]
    cur = meta["currency"]
    m = lambda x: money(x, cur)
    days = win["days"]
    out: list[str] = []
    add = out.append

    add(f"# AI bill teardown — {meta['customer_label']}\n")
    add(_table(["", ""], [
        ["**Prepared for**", "{{customer_name}}, {{recipient_names_roles}}"],
        ["**Prepared by**", "{{preparer}}, Prompt Shields"],
        ["**Report id / date**", f"{meta['id']} / {meta['date']}"],
        ["**Window analysed**",
         (f"{win['start'] or 'unknown'} – {win['end'] or 'unknown'} "
         f"({days if days is not None else 'unknown'} days)")],
        ["**Mode**", meta["mode"]],
        ["**Confidentiality**", "{{confidentiality_statement}}"],
    ]))

    # 1. Summary
    parity, sov = s["parity"], s["sovereign"]
    if "pending" in parity:
        parity_row = [f"*pending {parity['pending']}*", ""]
    else:
        parity_row = [
            (f"{m(parity['window'])} ({pct(parity['saving_pct'])} lower, 95% CI "
             f"{_ci(parity['ci_pct'])})"), m(parity["month"]),
        ]
    if "pending" in sov:
        sov_row = [f"*pending {sov['pending']}*", ""]
    else:
        sov_row = [f"{pct(sov['spend_pct'])} of spend, {pct(sov['calls_pct'])} of calls", ""]
    add("\n## 1. Summary\n")
    add(_table(["", "Window", "Per month (extrapolated, M§3)"], [
        ["Baseline spend", m(s["baseline_window"]), m(s["baseline_month"])],
        ["Projected spend, same-model arbitrage",
         f"{m(s['arbitrage_window'])} ({pct(s['arbitrage_saving_pct'])} lower)",
         m(s["arbitrage_month"])],
        ["Projected spend, policy at parity", *parity_row],
        ["Cache-hit potential (upper bound)",
         f"{m(s['cache_window'])} ({pct(s['cache_pct'])} of baseline)", m(s["cache_month"])],
        ["Spend that must stay sovereign", *sov_row],
    ]))
    add(
        f"\n**In one sentence:** Over {days if days is not None else 'an unknown number of'} "
        f"days the logged AI traffic cost {m(s['baseline_window'])}"
        + (f" (about {m(s['baseline_month'])} a month)" if s["baseline_month"] is not None else "")
        + f"; same-model price arbitrage would save {pct(s['arbitrage_saving_pct'])} of it and "
        f"exact-match caching up to {pct(s['cache_pct'])}. "
        + (f"Routing at quality parity under the policy would save {pct(parity['saving_pct'])} "
           f"of the compared spend (conservative: {pct(parity['conservative_saving_pct'])})."
           if "pending" not in parity else
           f"Savings from routing at quality parity are not measured (pending "
           f"{parity['pending']}).")
        + (f" {pct(sov['spend_pct'])} of spend must stay sovereign under the policy."
           if "pending" not in sov else "")
    )
    if "pending" in s["gate1"]:
        add("\n**Gate 1 reading** (≥ 30% savings at ≥ 95% parity): "
            f"not measured — pending {s['gate1']['pending']}.")
    else:
        add(f"\n**Gate 1 reading** (≥ 30% savings at ≥ 95% parity): {s['gate1']['reading']}.\n"
            f"Based on: point estimate {pct(parity['saving_pct'])} (95% CI "
            f"{_ci(parity['ci_pct'])}), conservative (Wilson lower bound) "
            f"{pct(parity['conservative_saving_pct'])} (M§7.2).")
    add("\n**How much to trust these numbers:**\n")
    add("- Baseline reconciliation with the invoice: {{gap_pct}}% → "
        "{{reconciled / unreconciled / no invoice}} (M§3.1).")
    add(f"- Priced calls: {pct(trust['priced_calls_pct'])} of calls, "
        f"{pct(trust['priced_tokens_pct'])} of tokens. Null-cost calls are left out (M§4.5).")
    add(f"- Estimated token counts: {pct(trust['estimated_spend_pct'])} of baseline spend (M§4.3).")
    for c in trust["caveats"]:
        add(f"- {c}")
    if not arb["compliance_checked"]:
        add("- Same-model arbitrage does not check the cheaper host against a data policy "
            "(the price catalog has no jurisdiction data yet, #7 / #8)"
            + (f"; it is limited to: {', '.join(arb['allowed_providers'])}."
               if arb["allowed_providers"] else "."))

    # 2. Scope and data
    add("\n## 2. Scope and data\n")
    add(_table(["Source", "Rows read", "Rows used", "Dropped (reason)"], [
        [f"{src['source']} (`{src['file']}`)", num(src["rows_read"]), num(src["rows_used"]),
         ", ".join(f"{n} ({k})" for k, n in src["dropped"].items()) or "0"]
        for src in r["sources"]
    ]))
    prompt = meta.get("prompt_text") or {}
    if prompt.get("scanned_locally"):
        add("\n- Prompt text: read on this machine only, to compute a request hash and prompt "
            "features, to scan for PII (only category names were kept) and to run the policy "
            "heuristic. No text is in this report.")
    else:
        add("\n- Prompt text: not used by `ps-teardown`. Where an input carried messages, they "
            "were read on this machine only to compute a request hash and prompt features; no "
            "text was kept.")
    if prompt.get("reexecuted_by_ps_eval"):
        add("- Quality parity: a stratified sample was re-run by a separate `ps-eval` run "
            "(Appendix A); this report reads its results file and executed no prompt.")
    add(f"- Sensitivity input: {meta['sensitivity_input']}; see M§5.3.")
    add("- Was the window typical? {{customer answer}}")
    if win["records_without_timestamp"]:
        add(f"- Records without a timestamp: {num(win['records_without_timestamp'])} "
            "(priced, but outside the window and the cache analysis).")

    # 3. Baseline
    add("\n## 3. Baseline spend\n")
    rows = [
        [row["provider"], row["model"], calls(row), num(row["tokens_in"]), num(row["tokens_out"]),
         num(row["tokens_cached"]), m(row["spend"]), pct(row["share_pct"])]
        for row in base["rows"]
    ]
    rows.append(["**Total**", "", calls({"calls": base["total_calls"],
                                         "calls_unknown_rows": base["total_calls_unknown_rows"]}), "", "", "", f"**{m(base['spend'])}**",
                 "100%"])
    add(_table(["Provider", "Model (served)", "Calls", "Input tokens", "Output tokens",
                "Cached tokens", "Spend", "Share"], rows))
    add("")
    by_src = base["by_cost_source"]
    src_rows = []
    for name in ["provider_reported", "pricing_table", "pricing_table_estimated_tokens", "litellm",
                 *sorted(k for k in by_src if k not in (
                     "provider_reported", "pricing_table", "pricing_table_estimated_tokens",
                     "litellm", "null"))]:
        row = by_src.get(name, {"calls": 0, "calls_unknown_rows": 0, "spend": 0.0})
        label = "litellm (LiteLLM's own price)" if name == "litellm" else name
        src_rows.append([label, calls(row), m(row["spend"])])
    null = by_src.get("null", {"calls": 0, "calls_unknown_rows": 0})
    src_rows.append(["null (not priced)", calls(null), "—"])
    add(_table(["Cost source (M§4.3)", "Calls", "Spend"], src_rows))
    if base["null_cost_by_reason"]:
        add("\nNull-cost calls by reason (M§4.5):\n")
        add(_table(["Reason", "Calls", "Tokens"], [
            [k, calls(v), num(v["tokens"])] for k, v in base["null_cost_by_reason"].items()
        ]))
    add(
        f"\nCalls where requested model ≠ served model: {num(base['requested_ne_served_calls'])} "
        f"({pct(base['requested_ne_served_pct'])})."
        + (" Re-priced from tokens at served_model, per M§4.2." if base["repriced_from_tokens"] else "")
        + (f" Calls with no served model, priced at the requested model: "
           f"{num(base['priced_at_requested_calls'])}." if base["priced_at_requested_calls"] else "")
    )

    # 4. Where the money goes
    att = r["attribution"]
    add("\n## 4. Where the money goes\n")
    add("### 4.1 By business unit and use case\n")
    add(_table(["Business unit", "Use case", "Calls", "Spend", "Share", "Main model"], [
        [row["business_unit"], row["use_case"] or "", calls(row), m(row["spend"]),
         pct(row["share_pct"]), row["main_model"] or ""]
        for row in att["rows"]
    ]))
    add("\n### 4.2 Top cost drivers\n")
    if not att["drivers"]:
        add("No priced cell has enough calls to list.")
    for i, d in enumerate(att["drivers"], 1):
        per_call = (f", {num(d['avg_input_tokens_per_call'])} input tokens per call on average"
                    if d["avg_input_tokens_per_call"] is not None else "")
        add(f"{i}. Use case `{d['use_case']}` on `{d['model']}`: {m(d['spend'])} "
            f"({pct(d['share_pct'])} of baseline spend), {_driver_calls(d)}{per_call}, "
            f"{pct(d['cached_input_pct'])} of input tokens read from the provider's prompt cache.")

    # 5. Arbitrage
    add("\n## 5. Same-model price arbitrage\n")
    if arb["rows"]:
        add(_table(["Model", "Current provider", "Cheaper compliant provider", "Spend now",
                    "Spend after", "Saving"], [
            [a["model"], a["current_provider"], a["cheaper_provider"], m(a["spend_now"]),
             m(a["spend_after"]), f"{m(a['saving'])} ({pct(a['saving_pct'])})"]
            for a in arb["rows"]
        ]))
        add("\n\"Compliant\" here means allowed by `--allow-provider`, not checked against a "
            "data policy: the price catalog has no jurisdiction data yet (#7 / #8).")
    else:
        add("No model in the window is sold cheaper by another host in catalog "
            f"`{meta['catalog_version']}`. Saving: {m(0.0)}.")

    # 6. Cache
    add("\n## 6. Cache-hit potential\n")
    add(
        f"TTL {cache['ttl_seconds'] // 60} minutes. Calls with a request hash: "
        f"{num(cache['calls_with_hash'])} ({pct(cache['calls_with_hash_pct'])} of calls). "
        f"Exact repeats of the message text: {plural(cache['hit_calls'], 'call')} "
        f"({pct(cache['hit_calls_pct'])} of calls), {m(cache['saving'])} "
        f"({pct(cache['saving_pct'])} of baseline). This is an upper bound (M§6.4). Calls whose "
        f"sensitivity turns the cache off under the policy are excluded: "
        f"{num(cache['excluded_cache_off_calls'])} ({m(cache['excluded_cache_off_spend'])}); the "
        f"cache is off at `{cache['cache_off_at']}` and above, and "
        + ("a call's level is its effective level under the route policy, PII escalation "
           f"included; with no declared classification it starts at "
           f"`{cache['unclassified_default']}` (M§5.1)."
           if cache.get("levels_from") == "route_policy" else
           f"a call with no declared classification counts as "
           f"`{cache['unclassified_default']}` (M§5.1).")
    )
    if cache["excluded_no_timestamp_calls"]:
        add(f"\nHashed calls without a timestamp, not analysed: "
            f"{num(cache['excluded_no_timestamp_calls'])}.")

    # 7–10. Policy replay (#13) and quality parity (#14), or pending
    _policy_sections(r, add, m)

    # 11–12
    add("\n## 11. Recommendations and next steps\n")
    add("1. {{quick win that needs no routing, e.g. turn on provider prompt caching for use case X}}")
    add("2. {{routing change, limited to use cases that passed parity}}")
    add("3. {{sovereignty finding for the CISO / DPO}}")
    add("\n**Proposed pilot:** {{scope, use cases, gateway or SDK, duration, how savings will be "
        "verified against a frozen baseline (PRD §7.10), commercial terms}}.")
    add("\n## 12. Limits of this report\n")
    add(f"This report is a projection from {days if days is not None else 'an unknown number of'} "
        "days of past traffic. It does not guarantee future savings, replace an invoice, assess "
        "compliance, prove general model quality, or measure latency or reliability. Migration "
        "cost is not included. Full list: M§8.\n")
    add("Specific to this teardown:\n")
    missing = [name for key, name in (
        ("policy_replay", "no policy replay or sensitivity mix (no route policy given)"),
        ("quality_parity", "no quality parity (no `ps-eval` results given)"),
    ) if key in r["pending"]]
    if meta["mode"] == "cost-only":
        missing.insert(0, "no prompt text")
    if missing:
        add(f"- {meta['mode'].capitalize()} mode: " + ", ".join(missing) + ".")
    if "policy_replay" in r:
        add("- Policy replay assumes the same tokens on every model, no failures or capacity "
            "limits, and no change in what users send (M§6.3).")
    if "quality_parity" in r:
        add("- Parity holds only for the sampled prompts, the scoring method and the judge "
            "recorded below (M§8).")
    add("- Costs are list prices at the catalog version below, in USD.")
    add("- {{run-specific limit}}")

    # Appendix A
    qp = r.get("quality_parity")
    sap = r.get("savings_at_parity")
    pending_14 = f"pending {r['pending'].get('quality_parity', '#14')}"
    pol = (r.get("policy_replay") or {}).get("policy")
    by_src_counts = ", ".join(
        f"{k} {calls(v)}" for k, v in base["by_cost_source"].items()
    ) or "none"
    null_counts = ", ".join(
        f"{k} {calls(v)}" for k, v in base["null_cost_by_reason"].items()
    ) or "none"
    add("\n## Appendix A — Methodology record\n")
    add(_table(["Field", "Value"], [
        ["Report id / date", f"{meta['id']} / {meta['date']}"],
        ["Customer (or anonymised label)", meta["customer_label"]],
        ["Consent: signer, date, scope", "{{consent}}"],
        ["Mode", meta["mode"]],
        ["Tool and version", meta["tool_version"]],
        ["Input sources and row counts", "; ".join(
            f"{src['source']} {src['file']}: {src['rows_read']} read, {src['rows_used']} used"
            for src in r["sources"])],
        ["Window (UTC)", (f"{win['start'] or 'unknown'} – {win['end'] or 'unknown'}, "
                         f"{days if days is not None else 'unknown'} days")],
        ["Window typical?", "{{answer}}"],
        ["Catalog version", meta["catalog_version"]],
        ["Re-priced from tokens?", "yes" if base["repriced_from_tokens"] else "no"],
        ["Currency and FX rate (source, date)", f"{cur}, no conversion"],
        ["Baseline definition", "Observed historical mix over the window (M§3)"],
        ["Invoice reconciliation", "{{gap_pct}}%, {{status}}"],
        ["Calls by cost source", by_src_counts],
        ["Null-cost calls by reason", null_counts],
        ["`requested_model ≠ served_model` calls", num(base["requested_ne_served_calls"])],
        ["Route policy file, `policy_version`, SHA-256",
         (f"`{pol['file']}`, {pol['policy_version']}, {pol['sha256']}" if pol else
          f"n/a: no route policy given (pending {r['pending']['policy_replay']})")],
        ["Sensitivity input", meta["sensitivity_input"]],
        ["Cache TTL", f"{cache['ttl_seconds']} seconds"],
        ["Sample size per stratum, seed", _sample_text(qp) if qp else pending_14],
        ["Scoring method per use case", _scoring_text(qp) if qp else pending_14],
        ["Judge model, provider, jurisdiction, prompt version",
         ("; ".join(qp["judges"]) or "none (no judge scorer)") if qp else pending_14],
        ["Re-execution temperature, spend", _reexec_text(qp) if qp else pending_14],
        ["`min_parity`, `min_cell_samples`, `min_cell_calls`",
         (f"{qp['min_parity']}, {qp['min_cell_samples']}, {meta['min_cell_calls']}" if qp
          else f"{pending_14}, {pending_14}, {meta['min_cell_calls']}")],
        ["CI methods",
         (f"Wilson 95% two-sided per cell (ps-eval); bootstrap over the parity sample, "
          f"{sap['bootstrap_resamples']} resamples, seed {sap['bootstrap_seed']}" if sap
          else pending_14)],
        ["Known limits specific to this run", "{{limits}}"],
        ["Intermediate data deleted on", "{{date}}"],
    ]))
    add("\n## Appendix B — Data handling statement\n")
    add("The analysis ran on {{where}}, under {{customer}}'s control, with `ps-teardown` reading "
        "local files only and making no network calls. Logs did not leave that environment. "
        + ("Sampled prompts were re-run by `ps-eval` only on models, and judged only by models, "
           "that the route policy allows for their sensitivity level, using the customer's own "
           "provider keys. " if qp and qp["eval_policy_is_route_policy"] else
           "Sampled prompts were re-run by `ps-eval` with the customer's own provider keys, "
           "under {{eval policy}}. " if qp else "")
        + "This report contains aggregates only: no prompt text, response text, request hashes "
        "or user identifiers. Intermediate files were deleted on {{date}}.")
    return "\n".join(out) + "\n"


def _sample_text(qp: dict) -> str:
    sample = qp["sample"]
    strata = "; ".join(
        f"{st['use_case']}/{st['sensitivity']}/{st['complexity']}: {st['sampled']} of "
        f"{st['population']}" for st in sample["strata"]
    ) or "no strata"
    return f"up to {sample['per_stratum']} per stratum, seed {sample['seed']} ({strata})"


def _scoring_text(qp: dict) -> str:
    return ", ".join(f"{uc}: {method}" for uc, method in qp["scoring"].items()) or "none"


def _reexec_text(qp: dict) -> str:
    x = qp["reexecution"]
    spend = x.get("spend_usd")
    cap = x.get("max_spend_usd")
    money_text = "unknown" if spend is None else f"USD {spend:.4f}"
    if cap is not None:
        money_text += f" of a USD {cap:.2f} cap"
    sent = "prompts sent to providers" if x.get("prompts_sent_to_providers") else (
        "no prompt sent to a provider (mock or recorded answers)")
    return (f"{x.get('temperature_default')} unless the logged call's is known; {money_text}; "
            f"{x.get('calls')} calls, {x.get('judge_calls')} judge calls; {sent}")


def _policy_sections(r: dict, add, m) -> None:
    """Template §7–§10: filled from policy replay and eval results, or pending."""
    pr = r.get("policy_replay")
    add("\n## 7. Sensitivity mix\n")
    if pr is None:
        add(_pending(r, "sensitivity_mix"))
    else:
        pol, mix = pr["policy"], pr["sensitivity_mix"]
        add(f"Policy: `{pol['file']}`, `policy_version` {pol['policy_version']}. "
            f"`unclassified_default` = {pol['unclassified_default']}.\n")
        add(_table(["Level", "Calls", "Share of calls", "Spend", "Share of spend",
                    "declared / pii_escalated / default", "Must stay sovereign?"], [
            [lv["level"], num(lv["calls"]), share(lv["share_calls"]), m(lv["spend"]),
             share(lv["share_spend"]),
             (f"{lv['by_source']['declared']} / {lv['by_source']['pii_escalated']} / "
              f"{lv['by_source']['default']}"),
             "yes" if lv["must_stay_sovereign"] else "no"]
            for lv in mix["levels"]
        ]))
        sov = mix["sovereign"]
        cats = ", ".join(f"{k} {num(v)}" for k, v in mix["pii_categories"].items()) or "none"
        add(f"\nMust stay sovereign: {m(sov['spend'])} ({share(sov['share_spend'])} of spend), "
            f"{num(sov['calls'])} calls ({share(sov['share_calls'])}). PII escalation raised "
            f"{share(mix['pii_escalated_share_calls'])} of calls; categories seen: {cats}.")
        nc = pr["baseline_noncompliant"]
        add(f"\nToday's traffic outside the policy: {num(nc['calls'])} calls "
            f"({m(nc['spend'])}) ran on a model the data policy does not allow at their level; "
            f"{num(nc['calls_served_model_not_in_policy'])} calls ran on a model the policy does "
            "not list.")
        if pr["calls_unknown_rows"]:
            add(f"\nUsage-export rows with no request count: {num(pr['calls_unknown_rows'])} "
                "(their spend is included; their calls are not counted).")
        add("\n{{Short reading, e.g. \"62% of spend must stay in the EEA; 18 points of that "
            "come from the confidential default for calls with no declared classification.\"}}")

    add("\n## 8. Policy replay — projected spend by strategy\n")
    if pr is None:
        add(_pending(r, "policy_replay"))
    else:
        base = r["baseline"]["spend"]
        arb, cache = r["arbitrage"], r["cache"]
        pc = pr["policy_cheapest"]
        sap = r.get("savings_at_parity")
        nocomp = num(pc["outcomes"]["no_compliant_candidate"])
        rows = [
            ["Baseline (observed)", m(base), "—", "—"],
            ["Same-model arbitrage", m(arb["projected_spend"]),
             f"{pct(arb['saving_pct'])} lower", "—"],
            ["Policy, cheapest (not quality-checked)",
             f"{m(pc['projected_spend'])} (vs. {m(pc['baseline_spend_compared'])} compared)",
             _vs(pc["savings_pct"]), nocomp],
        ]
        if sap:
            pp = sap["policy_at_parity"]
            rows.append(["Policy, at parity",
                         (f"{m(pp['projected_spend'])} (vs. "
                          f"{m(pp['baseline_spend_compared'])} compared)"),
                         _vs(pp["savings_pct"]),
                         num(pp["calls_by_status"].get("no_compliant_candidate", 0))])
        else:
            rows.append(["Policy, at parity", f"*pending {r['pending']['savings_at_parity']}*",
                         "—", "—"])
        rows.append(["Cache (separate, not additive)", m(cache["saving"]),
                     f"{pct(cache['saving_pct'])} of baseline", "—"])
        add(_table(["Strategy (M§6.2)", "Projected spend", "vs. baseline",
                    "Calls with no compliant candidate"], rows))
        add("\n\"Policy, cheapest\" is not a savings claim. It shows the ceiling before quality "
            "is checked. A negative saving means the policy moves traffic to more expensive, "
            "compliant models.")
        oc = pc["outcomes"]
        add(f"\nOutcomes: {num(oc['routed'])} calls routed to another model, "
            f"{num(oc['unchanged'])} unchanged, {nocomp} with no compliant candidate (kept at "
            f"baseline cost). Calls with a null baseline or projected cost are left out of the "
            f"comparison: {num(pc['calls_projected_null'])} projected null; "
            f"{num(pc['calls_priced_at_policy_rates'])} priced at the policy's per-1k rates "
            "because the catalog has no price for the model (M§4.6).")
        add("\n" + _table(["Routed model", "Calls", "Projected spend"], [
            [model, num(b["calls"]), m(b["spend"])] for model, b in pc["by_model"].items()
        ]))

    qp = r.get("quality_parity")
    add("\n## 9. Quality parity\n")
    if qp is None:
        add(_pending(r, "quality_parity"))
    else:
        scoring = _scoring_text(qp)
        judges = "; ".join(qp["judges"]) or "none"
        add(f"Scoring: {scoring}. Judge: {judges}.\n")
        add(_table(["Use case", "Candidate model", "Sample n", "Parity", "95% Wilson CI",
                    "At parity?"], [
            [c["use_case"], c["candidate"], num(c["n"]), share(c["parity"]),
             _ci((c["ci_low"], c["ci_high"]) if c["ci_low"] is not None else None, share),
             _PARITY_STATUS[c["status"]]]
            for c in qp["cells"]
        ]))
        add(f"\nEvery tested cell is listed, including the ones that failed (M§7.4). At parity: "
            f"point estimate ≥ {share(qp['min_parity'])} and n ≥ {qp['min_cell_samples']}.")
        if not qp["eval_policy_is_route_policy"]:
            add("\nThe eval was not run under this route policy (`ps-eval --route-policy`); "
                "see the caveats in §1.")

    sap = r.get("savings_at_parity")
    add("\n## 10. Savings at parity\n")
    if sap is None:
        add(_pending(r, "savings_at_parity"))
    else:
        pp, cons = sap["policy_at_parity"], sap["conservative"]
        rows, other = [], None
        for u in sap["use_cases"]:
            if not u["calls_unknown_rows"] and u["calls"] < r["report"]["min_cell_calls"]:
                other = other or {"base": 0.0, "parity": 0.0}
                other["base"] += u["baseline"]
                other["parity"] += u["at_parity"]
                continue
            if u["eval"] == "no_eval":
                change = "no eval set"
            elif u["eval"] == "none_at_parity":
                change = "none at parity"
            elif u["to_model"] and u["to_model"] != u["from_model"]:
                change = f"{u['from_model']} → {u['to_model']}"
            else:
                change = "no change"
            rows.append([u["use_case"], m(u["baseline"]), m(u["at_parity"]),
                         share(u["saving_pct"]), change])
        if other:
            saved = ((other["base"] - other["parity"]) / other["base"]
                     if other["base"] else None)
            rows.append([f"Other (use cases under {r['report']['min_cell_calls']} calls)",
                         m(other["base"]), m(other["parity"]), share(saved), "various"])
        rows.append(["**Total (spend-weighted)**", f"**{m(pp['baseline_spend_compared'])}**",
                     f"**{m(pp['projected_spend'])}**",
                     f"**{share(pp['savings_pct'])} (95% CI {_ci(sap['saving_ci'], share)})**",
                     ""])
        rows.append(["Conservative (Wilson lower bound)", "", m(cons["projected_spend"]),
                     share(cons["savings_pct"]), ""])
        add(_table(["Use case", "Baseline spend", "At-parity spend", "Saving",
                    "Model change"], rows))
        add("\nUse cases with no eval set count as zero savings here (M§7.4). The at-parity "
            "strategy replays the route policy with each use case limited to the models that "
            "reached parity for it, plus the model it runs on today (M§6.2). The interval "
            "resamples the parity sample; the logged traffic is the whole population.")
        x = sap["ps_eval_cross_check"]
        if x.get("saving_pct") is not None:
            add(f"\nCross-check: `ps-eval`'s own estimate (per-use-case model swap over its "
                f"{num(x.get('population_calls'))} records) is {share(x['saving_pct'])} "
                f"(95% CI {_ci(x.get('saving_ci'), share)}).")


# --- minimal markdown -> HTML (only what render_markdown emits) --------------------

_INLINE = [
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<![*\w])\*([^*]+)\*(?![*\w])"), r"<em>\1</em>"),
]


def _inline(text: str) -> str:
    out = html.escape(text, quote=False)
    for pattern, repl in _INLINE:
        out = pattern.sub(repl, out)
    return out


def _cells(line: str) -> list[str]:
    parts = re.split(r"(?<!\\)\|", line.strip()[1:-1])
    return [p.strip().replace("\\|", "|") for p in parts]


_CSS = """
:root{color-scheme:light dark;--fg:#1d1d1f;--bg:#fff;--muted:#6e6e73;--line:#d2d2d7;--head:#f5f5f7}
@media (prefers-color-scheme:dark){:root{--fg:#f5f5f7;--bg:#1d1d1f;--muted:#a1a1a6;--line:#424245;--head:#2c2c2e}}
body{font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--fg);background:var(--bg);
max-width:960px;margin:0 auto;padding:24px 16px}
h1{font-size:1.6em}h2{margin-top:2em;border-bottom:1px solid var(--line);padding-bottom:4px}
table{border-collapse:collapse;margin:12px 0;display:block;overflow-x:auto;max-width:100%}
th,td{border:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}
th{background:var(--head)}code{font-size:.9em}em{color:var(--muted)}
"""


def markdown_to_html(md: str, title: str = "AI bill teardown") -> str:
    lines = md.splitlines()
    body: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        heading = re.match(r"^(#{1,6}) (.*)$", line)
        if heading:
            level = len(heading.group(1))
            body.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            i += 1
        elif line.startswith("|"):
            block = []
            while i < len(lines) and lines[i].startswith("|"):
                block.append(lines[i])
                i += 1
            head, rows = _cells(block[0]), [_cells(b) for b in block[2:]]
            body.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head)
                        + "</tr></thead><tbody>"
                        + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>"
                                  for row in rows)
                        + "</tbody></table>")
        elif re.match(r"^(- |\d+\. )", line):
            ordered = not line.startswith("- ")
            items = []
            while i < len(lines) and re.match(r"^(- |\d+\. )", lines[i]):
                items.append(re.sub(r"^(- |\d+\. )", "", lines[i]))
                i += 1
            tag = "ol" if ordered else "ul"
            body.append(f"<{tag}>" + "".join(f"<li>{_inline(t)}</li>" for t in items) + f"</{tag}>")
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not re.match(r"^(#|\||- |\d+\. )", lines[i]):
                para.append(lines[i])
                i += 1
            body.append(f"<p>{_inline(' '.join(para))}</p>")
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body>\n"
        + "\n".join(body)
        + "\n</body></html>\n"
    )
