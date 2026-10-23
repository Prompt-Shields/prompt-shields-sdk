"""Policy replay and sensitivity mix for ``ps-teardown`` (#13).

Runs every logged call through the Python reference router under the whole
route-policy.v1 document (``route_policy.resolve_policy_route``: sensitivity,
PII escalation, data policy, use-case quality bar, then the strategy), prices
the decision, and aggregates:

* the **sensitivity mix**: calls and spend per effective level, split by
  ``sensitivity_source``, which levels must stay sovereign and the
  jurisdictions each level allows (methodology §5);
* the **policy outcome** and **projected spend per strategy**: baseline,
  policy (cheapest compliant candidate in the group the policy picks) and
  policy at parity, which stays "not measured" until the eval harness (#14)
  exists (methodology §6).

The method is ``docs/teardown/methodology.md`` §4.6, §5 and §6.

Local analysis only. Nothing here opens a socket, and prompt text is read only
when the caller passes ``scan_prompts=True`` (full mode), only to run the local
PII detector (``pii.scan_messages``) and the heuristic's code/schema check. The
report holds aggregates and category names only: no prompt text, no
``prompt_hash``, no call ids, no user identifiers.

Records
-------
Replay consumes *normalised usage records*: one per logged call, already
mapped from the source format (provider export, SDK events, gateway logs,
generic JSONL). ``UsageRecord`` is the shape; ``UsageRecord.from_mapping``
accepts a ``dict`` with the same keys (unknown keys are ignored), so an
ingest step that yields dicts or its own dataclass can feed ``replay``
directly. Fields:

==========================  ===================================================
``vendor``                  Pricing vendor that served the call (``openai``,
                            ``anthropic``, ...), as ``pricing.py`` keys it.
``requested_model``         Model the caller asked for (``auto`` allowed).
                            Key ``model`` is accepted as an alias.
``served_model``            Model that served the call. Baseline is priced
                            here; ``requested_model`` is the fallback (§4.2).
``tokens_in``/``tokens_out``  Token counts. ``tokens_in`` is the whole prompt.
``tokens_cached`` ...       Every ``pricing.PRICING_DIMENSIONS`` field, plus
                            ``provider_cost``, ``usage_units`` and
                            ``usage_unit_type``, with ``usage_cost`` meanings.
``baseline_cost``           Optional USD cost the ingest step already computed
                            per §4. When absent, replay re-prices from tokens.
                            An SDK event's ``cost`` field is *not* read (§4.2).
``data_classification``     Declared level (``X-PS-Data-Classification``).
``detected_pii_types``      PII categories the SDK already recorded.
``use_case``                Declared use case (quality bar lookup).
``quality``/``max_cost``/   Route hints as logged (``X-PS-Quality``,
``route_group``             ``X-PS-Max-Cost``, ``X-PS-Route``).
``max_output_tokens``       Output bound from the request, if logged. The
                            router's cost estimate uses ``tokens_out`` when
                            absent.
``messages``                Prompt messages, full mode only. Read locally,
                            never copied to the report.
==========================  ===================================================

Standalone use until the ``ps-teardown`` CLI (#12) wires this in::

    python -m prompt_shields.teardown_replay --policy policy.json calls.jsonl
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from prompt_shields.pii import scan_messages
from prompt_shields.pricing import PRICING_DIMENSIONS, usage_cost
from prompt_shields.route_policy import (
    EEA_COUNTRIES,
    PolicyViolation,
    RouteRequest,
    classify_sensitivity,
    data_policy_rule,
    estimate_input_tokens,
    resolve_policy_route,
    tier_complies,
)

REPORT_SCHEMA = "teardown-replay.v1"

#: Jurisdictions that count as sovereign for the "must stay sovereign" share.
DEFAULT_SOVEREIGN_JURISDICTIONS = frozenset({"EEA"} | EEA_COUNTRIES)

#: Level name used in the mix when the policy has no levels and none is declared.
UNCLASSIFIED = "unclassified"

#: Per-call outcomes.
OUTCOME_ROUTED = "routed"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_NO_COMPLIANT = "no_compliant_candidate"

_USAGE_FIELDS = (
    "tokens_in",
    "tokens_out",
    *PRICING_DIMENSIONS,
    "provider_cost",
    "usage_units",
    "usage_unit_type",
)


@dataclass
class UsageRecord:
    """One logged call, normalised for replay. See the module docstring."""

    vendor: str | None = None
    requested_model: str | None = None
    served_model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_cached: int | None = None
    tokens_cache_write: int | None = None
    tokens_cache_write_1h: int | None = None
    tokens_audio_in: int | None = None
    tokens_audio_out: int | None = None
    web_search_requests: int | None = None
    service_tier: str | None = None
    provider_cost: float | None = None
    usage_units: float | None = None
    usage_unit_type: str | None = None
    baseline_cost: float | None = None
    data_classification: str | None = None
    detected_pii_types: tuple[str, ...] = ()
    use_case: str | None = None
    quality: str | None = None
    max_cost: float | None = None
    route_group: str | None = None
    max_output_tokens: int | None = None
    messages: list[Any] | None = field(default=None, repr=False)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> UsageRecord:
        """Build from a dict with the field names above; extra keys are ignored."""
        names = {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in names}
        if "requested_model" not in kwargs and data.get("model") is not None:
            kwargs["requested_model"] = data["model"]
        pii = kwargs.get("detected_pii_types")
        kwargs["detected_pii_types"] = tuple(pii or ())
        return cls(**kwargs)

    def usage_fields(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in _USAGE_FIELDS}

    def baseline_model(self) -> str | None:
        """The model the baseline is priced at: served, else requested (§4.2)."""
        if self.served_model:
            return self.served_model
        if self.requested_model and self.requested_model != "auto":
            return self.requested_model
        return None


@dataclass(frozen=True)
class CallReplay:
    """Replay result for one call. Holds no prompt text and no identifiers."""

    sensitivity_level: str
    sensitivity_source: str
    pii_categories: tuple[str, ...]
    sovereign: bool
    outcome: str
    reason: str
    routed_model: str | None
    routed_group: str | None
    routed_jurisdiction: str | None
    baseline_model: str | None
    baseline_jurisdiction: str | None
    baseline_compliant: bool | None
    priced_at_requested: bool
    baseline_cost: float | None
    baseline_null_reason: str | None
    projected_cost: float | None
    projected_pricing: str | None


def _to_record(item: UsageRecord | Mapping[str, Any] | Any) -> UsageRecord:
    if isinstance(item, UsageRecord):
        return item
    if isinstance(item, Mapping):
        return UsageRecord.from_mapping(item)
    if dataclasses.is_dataclass(item) and not isinstance(item, type):
        return UsageRecord.from_mapping(dataclasses.asdict(item))
    raise TypeError(f"cannot replay a {type(item).__name__}; pass a mapping")


def policy_sha256(policy: Mapping[str, Any]) -> str:
    """SHA-256 of the policy's canonical JSON, for the methodology record."""
    canonical = json.dumps(policy, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def level_is_sovereign(
    policy: Mapping[str, Any],
    level: str | None,
    sovereign_jurisdictions: Iterable[str] = DEFAULT_SOVEREIGN_JURISDICTIONS,
) -> bool:
    """True when the data policy keeps calls at ``level`` sovereign (§5.2).

    That is a rule with ``us_cloud_act_exposure: false``, or one whose
    ``jurisdictions`` are all in ``sovereign_jurisdictions`` (EEA by default).
    A level with no rule, or one that allows ``any``, is not sovereign.
    """
    rule = data_policy_rule(policy, level)
    if rule is None:
        return False
    if rule.get("us_cloud_act_exposure") is False:
        return True
    allowed = set(rule["jurisdictions"])
    return "any" not in allowed and allowed <= set(sovereign_jurisdictions)


def _policy_tiers(policy: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    tiers = []
    for group_tiers in policy["groups"].values():
        if isinstance(group_tiers, list):
            tiers.extend(group_tiers)
    return tiers


def _find_tier(
    policy: Mapping[str, Any], model: str | None, vendor: str | None = None
) -> Mapping[str, Any] | None:
    """The policy tier for ``model``, preferring one whose provider is ``vendor``."""
    if model is None:
        return None
    matches = [t for t in _policy_tiers(policy) if t["model"] == model]
    for tier in matches:
        if vendor is not None and tier.get("provider") == vendor:
            return tier
    return matches[0] if matches else None


def _baseline(record: UsageRecord) -> tuple[float | None, str | None]:
    """Baseline cost and, when it is null, why (§4.5)."""
    if record.baseline_cost is not None:
        return float(record.baseline_cost), None
    model = record.baseline_model()
    fields = record.usage_fields()
    if model is None and fields.get("provider_cost") is None:
        return None, "no_model"
    if record.vendor is None and fields.get("provider_cost") is None:
        return None, "no_vendor"
    cost = usage_cost(record.vendor or "", model, fields)
    if cost is not None:
        return cost, None
    if record.tokens_in is None or record.tokens_out is None:
        return None, "missing_tokens"
    return None, "unpriced"


def _project(
    record: UsageRecord, tier: Mapping[str, Any]
) -> tuple[float | None, str | None]:
    """Cost of this call's usage on the policy's candidate (§4.6).

    Catalog prices first, with the call's cache and tier dimensions. A tier
    the candidate does not offer (e.g. batch) is priced at standard, so
    savings are understated rather than overstated. A candidate not in the
    catalog is priced at the policy's per-1k rates, without cache or tier
    discounts. Missing token counts give a null cost.
    """
    if record.tokens_in is None or record.tokens_out is None:
        return None, None
    fields = record.usage_fields()
    fields["provider_cost"] = None
    fields["usage_units"] = fields["usage_unit_type"] = None
    provider = tier.get("provider")
    if provider:
        cost = usage_cost(provider, tier["model"], fields)
        if cost is None and fields.get("service_tier") is not None:
            cost = usage_cost(provider, tier["model"], {**fields, "service_tier": None})
        if cost is not None:
            return cost, "catalog"
    cost = (record.tokens_in / 1000) * tier["input_cost_per_1k"] + (
        record.tokens_out / 1000
    ) * tier["output_cost_per_1k"]
    return round(cost, 6), "policy_rates"


def replay_call(
    record: UsageRecord | Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    scan_prompts: bool = False,
    sovereign_jurisdictions: Iterable[str] = DEFAULT_SOVEREIGN_JURISDICTIONS,
) -> CallReplay:
    """Replay one call under ``policy`` (methodology §6.1)."""
    record = _to_record(record)
    messages = record.messages if scan_prompts and record.messages else []

    pii = set(record.detected_pii_types)
    if messages:
        pii.update(scan_messages(messages))
    pii_categories = tuple(sorted(pii))

    if record.tokens_in is not None:
        approx_in = record.tokens_in
    else:
        approx_in = estimate_input_tokens(messages)
    max_out = record.max_output_tokens
    if max_out is None:
        max_out = record.tokens_out
    req = RouteRequest(
        messages=list(messages),
        requested_model=record.requested_model or "auto",
        approx_input_tokens=approx_in,
        max_output_tokens=max_out,
        quality=record.quality,  # type: ignore[arg-type]
        max_cost=record.max_cost,
        explicit_group=record.route_group,
        data_classification=record.data_classification,
        pii_detected=list(pii_categories),
        use_case=record.use_case,
    )

    level, source = classify_sensitivity(
        policy, record.data_classification, pii_categories
    )
    baseline_cost, null_reason = _baseline(record)
    baseline_model = record.baseline_model()
    baseline_tier = _find_tier(policy, baseline_model, record.vendor)
    rule = data_policy_rule(policy, level)
    baseline_compliant = (
        tier_complies(baseline_tier, rule) if baseline_tier is not None else None
    )
    common: dict[str, Any] = {
        "sensitivity_level": level or UNCLASSIFIED,
        "sensitivity_source": source,
        "pii_categories": pii_categories,
        "sovereign": level_is_sovereign(policy, level, sovereign_jurisdictions),
        "baseline_model": baseline_model,
        "baseline_jurisdiction": (baseline_tier or {}).get("jurisdiction"),
        "baseline_compliant": baseline_compliant,
        "priced_at_requested": not record.served_model and baseline_model is not None,
        "baseline_cost": baseline_cost,
        "baseline_null_reason": null_reason,
    }

    try:
        decision = resolve_policy_route(req, policy)
    except PolicyViolation as exc:
        return CallReplay(
            outcome=OUTCOME_NO_COMPLIANT,
            reason=str(exc),
            routed_model=None,
            routed_group=exc.group,
            routed_jurisdiction=None,
            projected_cost=baseline_cost,
            projected_pricing="baseline",
            **common,
        )

    tier = _find_tier(policy, decision.model)
    if decision.model == baseline_model:
        outcome = OUTCOME_UNCHANGED
        projected, pricing = baseline_cost, "baseline"
    else:
        outcome = OUTCOME_ROUTED
        projected, pricing = _project(record, tier) if tier else (None, None)
    return CallReplay(
        outcome=outcome,
        reason=decision.reason,
        routed_model=decision.model,
        routed_group=decision.group,
        routed_jurisdiction=(tier or {}).get("jurisdiction"),
        projected_cost=projected,
        projected_pricing=pricing,
        **common,
    )


# --- aggregation -------------------------------------------------------------


def _share(part: float, whole: float) -> float | None:
    return round(part / whole, 6) if whole else None


def _money(value: float) -> float:
    return round(float(value), 6)


def _bucket() -> dict[str, Any]:
    return {"calls": 0, "spend": 0.0}


def _add(bucket: dict[str, Any], cost: float | None) -> None:
    bucket["calls"] += 1
    if cost is not None:
        bucket["spend"] += cost


def summarize(
    calls: list[CallReplay],
    policy: Mapping[str, Any],
    *,
    mode: str = "cost_only",
    sovereign_jurisdictions: Iterable[str] = DEFAULT_SOVEREIGN_JURISDICTIONS,
) -> dict[str, Any]:
    """Aggregate per-call results into the replay report (JSON-ready dict).

    Spend is baseline spend over priced calls; null-cost calls are counted,
    never priced at zero. Savings compare only calls priced on both sides.
    """
    n = len(calls)
    priced = [c for c in calls if c.baseline_cost is not None]
    baseline_spend = sum(c.baseline_cost for c in priced)  # type: ignore[misc]

    null_by_reason: dict[str, int] = {}
    for c in calls:
        if c.baseline_cost is None:
            key = c.baseline_null_reason or "unpriced"
            null_by_reason[key] = null_by_reason.get(key, 0) + 1

    # Sensitivity mix (§5.2), levels in policy order then any others.
    sensitivity = policy.get("sensitivity") or {}
    order = list(sensitivity.get("levels") or [])
    for c in calls:
        if c.sensitivity_level not in order:
            order.append(c.sensitivity_level)
    levels = []
    for level in order:
        rows = [c for c in calls if c.sensitivity_level == level]
        spend = sum(c.baseline_cost or 0.0 for c in rows)
        policy_level = level if level != UNCLASSIFIED else None
        rule = data_policy_rule(policy, policy_level)
        by_source = {"declared": 0, "pii_escalated": 0, "default": 0}
        for c in rows:
            by_source[c.sensitivity_source] += 1
        levels.append(
            {
                "level": level,
                "calls": len(rows),
                "share_calls": _share(len(rows), n),
                "spend": _money(spend),
                "share_spend": _share(spend, baseline_spend),
                "by_source": by_source,
                "must_stay_sovereign": level_is_sovereign(
                    policy, policy_level, sovereign_jurisdictions
                ),
                "allowed_jurisdictions": list(rule["jurisdictions"]) if rule else None,
            }
        )
    sovereign_rows = [c for c in calls if c.sovereign]
    sovereign_spend = sum(c.baseline_cost or 0.0 for c in sovereign_rows)

    pii_counts: dict[str, int] = {}
    for c in calls:
        for category in c.pii_categories:
            pii_counts[category] = pii_counts.get(category, 0) + 1

    # Jurisdictions: where the baseline ran and where the policy would route.
    baseline_jur: dict[str, dict[str, Any]] = {}
    policy_jur: dict[str, dict[str, Any]] = {}
    for c in calls:
        _add(
            baseline_jur.setdefault(c.baseline_jurisdiction or "unknown", _bucket()),
            c.baseline_cost,
        )
        key = c.routed_jurisdiction or (
            "no_compliant_candidate" if c.outcome == OUTCOME_NO_COMPLIANT else "unknown"
        )
        _add(policy_jur.setdefault(key, _bucket()), c.projected_cost)
    for table in (baseline_jur, policy_jur):
        for bucket in table.values():
            bucket["spend"] = _money(bucket["spend"])

    noncompliant = [c for c in calls if c.baseline_compliant is False]

    # Policy strategy (§6.2): compare only calls priced on both sides.
    outcomes = {OUTCOME_ROUTED: 0, OUTCOME_UNCHANGED: 0, OUTCOME_NO_COMPLIANT: 0}
    for c in calls:
        outcomes[c.outcome] += 1
    compared = [
        c for c in calls if c.baseline_cost is not None and c.projected_cost is not None
    ]
    cmp_baseline = sum(c.baseline_cost for c in compared)  # type: ignore[misc]
    cmp_projected = sum(c.projected_cost for c in compared)  # type: ignore[misc]
    by_model: dict[str, dict[str, Any]] = {}
    for c in calls:
        key = c.routed_model or OUTCOME_NO_COMPLIANT
        _add(by_model.setdefault(key, _bucket()), c.projected_cost)
    for bucket in by_model.values():
        bucket["spend"] = _money(bucket["spend"])

    return {
        "schema": REPORT_SCHEMA,
        "mode": mode,
        "policy": {
            "policy_version": policy.get("policy_version"),
            "sha256": policy_sha256(policy),
            "strategy": policy.get("strategy", "heuristic"),
        },
        "calls": n,
        "baseline": {
            "spend": _money(baseline_spend),
            "calls_priced": len(priced),
            "calls_null_cost": n - len(priced),
            "null_cost_by_reason": dict(sorted(null_by_reason.items())),
            "priced_at_requested": sum(1 for c in calls if c.priced_at_requested),
        },
        "sensitivity_mix": {
            "levels": levels,
            "sovereign": {
                "calls": len(sovereign_rows),
                "share_calls": _share(len(sovereign_rows), n),
                "spend": _money(sovereign_spend),
                "share_spend": _share(sovereign_spend, baseline_spend),
            },
            "pii_escalated_share_calls": _share(
                sum(1 for c in calls if c.sensitivity_source == "pii_escalated"), n
            ),
            "pii_categories": dict(sorted(pii_counts.items())),
        },
        "jurisdictions": {
            "baseline": dict(sorted(baseline_jur.items())),
            "policy": dict(sorted(policy_jur.items())),
        },
        "baseline_noncompliant": {
            "calls": len(noncompliant),
            "spend": _money(sum(c.baseline_cost or 0.0 for c in noncompliant)),
            "calls_served_model_not_in_policy": sum(
                1 for c in calls if c.baseline_compliant is None
            ),
        },
        "strategies": {
            "baseline": {"spend": _money(baseline_spend)},
            "policy_cheapest": {
                "outcomes": outcomes,
                "calls_compared": len(compared),
                "calls_projected_null": sum(
                    1 for c in calls if c.projected_cost is None
                ),
                "calls_priced_at_policy_rates": sum(
                    1 for c in calls if c.projected_pricing == "policy_rates"
                ),
                "baseline_spend_compared": _money(cmp_baseline),
                "projected_spend": _money(cmp_projected),
                "savings": _money(cmp_baseline - cmp_projected),
                "savings_pct": _share(cmp_baseline - cmp_projected, cmp_baseline),
                "by_model": dict(sorted(by_model.items())),
            },
            "policy_at_parity": {
                "status": "not_measured",
                "pending": "#14 (eval harness)",
            },
        },
    }


def replay(
    records: Iterable[UsageRecord | Mapping[str, Any]],
    policy: Mapping[str, Any],
    *,
    scan_prompts: bool = False,
    sovereign_jurisdictions: Iterable[str] = DEFAULT_SOVEREIGN_JURISDICTIONS,
) -> dict[str, Any]:
    """Replay ``records`` under ``policy`` and return the aggregated report.

    ``scan_prompts=True`` is full mode: record ``messages`` are scanned locally
    for PII and used by the heuristic. Otherwise (cost-only) prompt content is
    ignored and only ``detected_pii_types`` and declared levels count (§5.3).
    """
    sovereign = frozenset(sovereign_jurisdictions)
    calls = [
        replay_call(
            r, policy, scan_prompts=scan_prompts, sovereign_jurisdictions=sovereign
        )
        for r in records
    ]
    return summarize(
        calls,
        policy,
        mode="full" if scan_prompts else "cost_only",
        sovereign_jurisdictions=sovereign,
    )


# --- standalone entry point (until the ps-teardown CLI wires this in) --------


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    stream = sys.stdin if path == "-" else open(path, encoding="utf-8")  # noqa: SIM115
    try:
        return [json.loads(line) for line in stream if line.strip()]
    finally:
        if stream is not sys.stdin:
            stream.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m prompt_shields.teardown_replay",
        description=(
            "Replay normalised usage records (JSONL) against a route-policy.v1 "
            "file and print the sensitivity mix and projected spend as JSON. "
            "Local only: nothing is sent anywhere."
        ),
    )
    parser.add_argument("records", help="JSONL file of normalised records, or -")
    parser.add_argument("--policy", required=True, help="route-policy.v1 JSON file")
    parser.add_argument(
        "--scan-prompts",
        action="store_true",
        help="full mode: scan record messages locally for PII",
    )
    parser.add_argument("--output", help="write the report here instead of stdout")
    args = parser.parse_args(argv)

    with open(args.policy, encoding="utf-8") as fh:
        policy = json.load(fh)
    report = replay(_read_jsonl(args.records), policy, scan_prompts=args.scan_prompts)
    text = json.dumps(report, indent=2, sort_keys=False) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
