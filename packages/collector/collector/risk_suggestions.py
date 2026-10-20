"""Rule-based risk classification suggestions (EU AI Act, ISO 42001, NIST AI RMF).

Rules are data: each JSON file in ``risk_rules/`` is a versioned ruleset
(``eu_ai_act_annex_iii.json`` is the EU AI Act Annex III use-case taxonomy,
``eu_ai_act_article_5.json`` the Article 5 prohibited practices, flagged
``critical``, and ``eu_ai_act_article_50.json`` the Article 50 transparency
obligations).
A rule maps signals about an asset to a framework, category and level:

* registry fields: ``use_case``, ``business_unit``, ``calling_service``,
  ``vendor``, ``model``, ``environment``
* telemetry-derived data flows (#96): ``data_sources`` (input flow sources),
  ``output_destinations``, ``pii_types`` (union of detected PII categories)
  and ``data_classification`` (highest of the asset and its flows)

``text`` is shorthand for use case, calling service, data sources and output
destinations, i.e. what the system does and touches.

A rule fires when every ``when.all`` condition and at least one ``when.any``
condition match and no ``unless`` condition does. A condition can carry its
own ``context`` (at least one of these conditions must also match, e.g.
``applicant*`` counts only next to employment words) and ``exclude`` (none
of these may match, e.g. ``admission*`` not next to ``hospital*``), so an
ambiguous keyword can be scoped without dropping the rest of the rule.
``context`` and ``exclude`` look at the whole asset, like ``unless``, so an
exclusion that should only count next to the ambiguous word is written as
phrases (``loan* applicant*``: every word ending in ``*`` is a prefix); context
matches are recorded in the evidence with ``role: context``. ``supporting`` conditions
never fire a rule on their own; they add evidence and raise confidence.
Confidence is ``low`` when only ``"strength": "weak"`` triggers matched,
``high`` when a supporting condition also matched, else ``medium``.

Every suggestion is stored in ``risk_mappings`` with ``source='rules'``,
``status='suggested'``, its rule id, ruleset version, framework reference and
the evidence (which signal, which value, which keyword or PII category). The
engine never touches a row a human set: it only creates, refreshes or
withdraws rows that are still ``suggested``. A confirmed or rejected rule is
not suggested again, and no rule suggests a framework/category pair that
already has a hand-entered mapping on the asset. That holds across ruleset
versions: a review is a decision about the asset and the rule id, so a new
version never re-opens it. Narrowing a rule cannot make a rejection wrong;
a rule whose meaning changes materially should get a new id instead, which is
suggested afresh.

Entry points:

* :func:`suggest_risks` for one tenant (the caller commits).
* ``POST /api/v1/admin/risk-suggestions/run`` for the caller's tenant.
* ``POST /api/v1/admin/risk-mappings/{risk_id}/review`` to confirm or reject.
* ``python -m collector.risk_suggestions --tenant <id>`` or ``--all-tenants``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from db.models import AIAsset, DataFlow, RiskMapping, Tenant, uuid7
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from collector import rls
from collector.admin_auth import AdminPrincipal, require_admin
from collector.audit import bearer_key_prefix, record_read
from collector.dedup import CLASSIFICATION_ORDER
from collector.sanitize import BoundedJsonRoute, StorableStr

RULES_DIR = Path(__file__).parent / "risk_rules"

SUGGESTED = "suggested"
CONFIRMED = "confirmed"
REJECTED = "rejected"
SOURCE_RULES = "rules"
SOURCE_MANUAL = "manual"
RiskStatus = Literal["suggested", "confirmed", "rejected"]

FRAMEWORKS = frozenset({"EU_AI_ACT", "NIST_AI_RMF", "ISO_42001", "custom"})
LEVELS = frozenset({"critical", "high", "medium", "low"})
TEXT_FIELDS = ("use_case", "calling_service", "data_sources", "output_destinations")
KEYWORD_SIGNALS = frozenset(
    {"text", *TEXT_FIELDS, "business_unit", "vendor", "model", "environment"}
)
WEAK = "weak"


# ──────────────────────────────────────────────────────────────────────
# Rules
# ──────────────────────────────────────────────────────────────────────


def _normalise(text: str) -> str:
    """Lowercase words separated by single spaces: 'ResumeScreening_v2' ->
    'resume screening v2'."""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _keyword_pattern(keyword: str) -> re.Pattern:
    """Whole words; a word ending in ``*`` is a prefix, so ``loan* applicant*``
    matches 'loans applicants' and 'loan applicant scoring'. A phrase scopes an
    ambiguous term to its neighbour (``university applicant*`` does not match
    'applicants for university staff roles')."""
    parts = []
    for word in keyword.split():
        prefix = word.endswith("*")
        norm = _normalise(word.rstrip("*"))
        if norm:
            parts.append(re.escape(norm) + (r"[a-z0-9]*" if prefix else ""))
    if not parts:
        raise ValueError(f"empty keyword {keyword!r}")
    return re.compile(r"(?<![a-z0-9])" + " ".join(parts) + r"(?![a-z0-9])")


@dataclass(frozen=True)
class Condition:
    signal: str
    keywords: tuple[tuple[str, re.Pattern], ...] = ()
    any_of: frozenset[str] = frozenset()
    present: bool = False
    at_least: str | None = None
    strength: str = "strong"
    context: tuple[Condition, ...] = ()
    exclude: tuple[Condition, ...] = ()


@dataclass(frozen=True)
class Rule:
    id: str
    framework: str
    category: str
    level: str
    reference: str
    title: str
    ruleset: str
    ruleset_version: str
    all: tuple[Condition, ...] = ()
    any: tuple[Condition, ...] = ()
    unless: tuple[Condition, ...] = ()
    supporting: tuple[Condition, ...] = ()


@dataclass(frozen=True)
class Ruleset:
    name: str
    version: str
    rules: tuple[Rule, ...]


def _resolve_list(value: Any, lists: dict[str, list[str]], where: str) -> list[str]:
    if isinstance(value, str) and value.startswith("@"):
        if value[1:] not in lists:
            raise ValueError(f"{where}: unknown list {value}")
        value = lists[value[1:]]
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(v, str) and v.strip() for v in value)
    ):
        raise ValueError(f"{where}: expected a non-empty list of strings")
    return value


def _parse_condition(raw: Any, lists: dict[str, list[str]], where: str) -> Condition:
    cond = _parse_base_condition(raw, lists, where)
    scoped = {}
    for key in ("context", "exclude"):
        if key not in raw:
            continue
        items = raw[key]
        if not isinstance(items, list) or not items:
            raise ValueError(f"{where}: {key} must be a non-empty list of conditions")
        scoped[key] = tuple(_parse_condition(c, lists, where) for c in items)
    return replace(cond, **scoped) if scoped else cond


def _parse_base_condition(
    raw: Any, lists: dict[str, list[str]], where: str
) -> Condition:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: condition must be an object")  # noqa: TRY004
    signal = raw.get("signal")
    strength = raw.get("strength", "strong")
    if strength not in ("strong", WEAK):
        raise ValueError(f"{where}: strength must be 'strong' or 'weak'")
    if signal in KEYWORD_SIGNALS:
        words = _resolve_list(raw.get("keywords"), lists, where)
        return Condition(
            signal=signal,
            keywords=tuple((w, _keyword_pattern(w)) for w in words),
            strength=strength,
        )
    if signal == "pii_types":
        if raw.get("present") is True:
            return Condition(signal=signal, present=True, strength=strength)
        values = _resolve_list(raw.get("any_of"), lists, where)
        return Condition(
            signal=signal,
            any_of=frozenset(v.lower() for v in values),
            strength=strength,
        )
    if signal == "data_classification":
        at_least = raw.get("at_least")
        if at_least not in CLASSIFICATION_ORDER:
            raise ValueError(
                f"{where}: at_least must be one of {sorted(CLASSIFICATION_ORDER)}"
            )
        return Condition(signal=signal, at_least=at_least, strength=strength)
    raise ValueError(f"{where}: unknown signal {signal!r}")


def parse_ruleset(data: dict) -> Ruleset:
    """Validate and compile one ruleset document. Raises ValueError."""
    name = data.get("ruleset")
    version = data.get("version")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(version, str)
        or not version
    ):
        raise ValueError("ruleset needs a 'ruleset' name and a 'version'")
    lists = data.get("lists") or {}
    rules: list[Rule] = []
    seen: set[str] = set()
    for raw in data.get("rules") or []:
        rid = raw.get("id")
        if not isinstance(rid, str) or not rid:
            raise ValueError(f"{name}: rule without an id")
        if rid in seen:
            raise ValueError(f"{name}: duplicate rule id {rid}")
        seen.add(rid)
        framework = raw.get("framework", data.get("framework"))
        if framework not in FRAMEWORKS:
            raise ValueError(f"{rid}: framework must be one of {sorted(FRAMEWORKS)}")
        if raw.get("level") not in LEVELS:
            raise ValueError(f"{rid}: level must be one of {sorted(LEVELS)}")
        for key in ("category", "reference", "title"):
            if not isinstance(raw.get(key), str) or not raw[key]:
                raise ValueError(f"{rid}: missing {key}")
        when = raw.get("when") or {}
        all_ = tuple(_parse_condition(c, lists, rid) for c in when.get("all") or [])
        any_ = tuple(_parse_condition(c, lists, rid) for c in when.get("any") or [])
        if not all_ and not any_:
            raise ValueError(f"{rid}: 'when' needs at least one condition")
        rules.append(
            Rule(
                id=rid,
                framework=framework,
                category=raw["category"],
                level=raw["level"],
                reference=raw["reference"],
                title=raw["title"],
                ruleset=name,
                ruleset_version=version,
                all=all_,
                any=any_,
                unless=tuple(
                    _parse_condition(c, lists, rid) for c in raw.get("unless") or []
                ),
                supporting=tuple(
                    _parse_condition(c, lists, rid) for c in raw.get("supporting") or []
                ),
            )
        )
    return Ruleset(name=name, version=version, rules=tuple(rules))


def load_rulesets(directory: Path | None = None) -> tuple[Ruleset, ...]:
    """Every ``*.json`` ruleset in ``directory`` (default: the shipped rules)."""
    if directory is None:
        return _default_rulesets()
    rulesets = tuple(
        parse_ruleset(json.loads(p.read_text(encoding="utf-8")))
        for p in sorted(Path(directory).glob("*.json"))
    )
    ids = [r.id for rs in rulesets for r in rs.rules]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"rule ids used in more than one ruleset: {dupes}")
    return rulesets


@lru_cache(maxsize=1)
def _default_rulesets() -> tuple[Ruleset, ...]:
    return load_rulesets(RULES_DIR)


# ──────────────────────────────────────────────────────────────────────
# Signals and evaluation
# ──────────────────────────────────────────────────────────────────────


@dataclass
class AssetSignals:
    use_case: str | None = None
    business_unit: str | None = None
    calling_service: str | None = None
    vendor: str | None = None
    model: str | None = None
    environment: str | None = None
    data_sources: list[str] = field(default_factory=list)
    output_destinations: list[str] = field(default_factory=list)
    pii_types: list[str] = field(default_factory=list)
    data_classification: str | None = None


def _rank(classification: str | None) -> int:
    return CLASSIFICATION_ORDER.get((classification or "").lower(), -1)


def collect_signals(asset: AIAsset, flows: Iterable[DataFlow] = ()) -> AssetSignals:
    """Signals for one asset from its registry row and its data flows."""
    s = AssetSignals(
        use_case=asset.use_case_name,
        business_unit=asset.business_unit,
        calling_service=asset.calling_service,
        vendor=asset.vendor,
        model=asset.model,
        environment=asset.environment,
        data_classification=(asset.data_classification or "").lower() or None,
    )
    pii: set[str] = set()
    for f in flows:
        if f.direction == "input" and f.source_system not in s.data_sources:
            s.data_sources.append(f.source_system)
        if (
            f.direction == "output"
            and f.destination_system not in s.output_destinations
        ):
            s.output_destinations.append(f.destination_system)
        if isinstance(f.detected_pii_types, list):
            pii.update(str(p).lower() for p in f.detected_pii_types if p)
        if _rank(f.data_classification) > _rank(s.data_classification):
            s.data_classification = f.data_classification.lower()
    s.data_sources.sort()
    s.output_destinations.sort()
    s.pii_types = sorted(pii)
    return s


def _field_values(signals: AssetSignals, name: str) -> list[str]:
    value = getattr(signals, name)
    if isinstance(value, list):
        return [v for v in value if v]
    return [value] if value else []


def _match(cond: Condition, signals: AssetSignals) -> list[dict]:
    """Evidence items for a matching condition, [] when it does not match.
    Scoped by the condition's ``context`` and ``exclude``."""
    out = _match_base(cond, signals)
    if not out:
        return []
    if any(_match(c, signals) for c in cond.exclude):
        return []
    if cond.context:
        ctx = [e for c in cond.context for e in _match(c, signals)]
        if not ctx:
            return []
        out += [dict(e, role="context") for e in ctx if e not in out]
    return out


def _match_base(cond: Condition, signals: AssetSignals) -> list[dict]:
    if cond.keywords:
        fields = TEXT_FIELDS if cond.signal == "text" else (cond.signal,)
        out = []
        for name in fields:
            for value in _field_values(signals, name):
                norm = _normalise(value)
                hits = [kw for kw, pattern in cond.keywords if pattern.search(norm)]
                if hits:
                    out.append({"signal": name, "value": value, "matched": hits})
        return out
    if cond.signal == "pii_types":
        hits = (
            signals.pii_types
            if cond.present
            else sorted(cond.any_of & set(signals.pii_types))
        )
        return (
            [{"signal": "pii_types", "value": signals.pii_types, "matched": hits}]
            if hits
            else []
        )
    if cond.signal == "data_classification":
        c = signals.data_classification
        if c is not None and _rank(c) >= _rank(cond.at_least):
            return [
                {
                    "signal": "data_classification",
                    "value": c,
                    "matched": [f">= {cond.at_least}"],
                }
            ]
    return []


@dataclass(frozen=True)
class Suggestion:
    rule: Rule
    confidence: str
    evidence: dict


def _summary(rule: Rule, items: list[dict]) -> str:
    parts = []
    for e in items:
        value = e["value"] if isinstance(e["value"], str) else ", ".join(e["value"])
        parts.append(
            f"{e['signal']} '{value}' matched {', '.join(e['matched'])} ({e['role']})"
        )
    return f"{rule.reference}, {rule.title}: " + "; ".join(parts)


def evaluate_rule(rule: Rule, signals: AssetSignals) -> Suggestion | None:
    triggers: list[tuple[Condition, list[dict]]] = []
    for cond in rule.all:
        ev = _match(cond, signals)
        if not ev:
            return None
        triggers.append((cond, ev))
    if rule.any:
        matched_any = [(c, ev) for c in rule.any if (ev := _match(c, signals))]
        if not matched_any:
            return None
        triggers.extend(matched_any)
    if any(_match(c, signals) for c in rule.unless):
        return None
    support = [ev for c in rule.supporting for ev in _match(c, signals)]

    if all(c.strength == WEAK for c, _ in triggers):
        confidence = "low"
    elif support:
        confidence = "high"
    else:
        confidence = "medium"

    items = [dict(e, role=e.get("role", "trigger")) for _, ev in triggers for e in ev]
    items += [dict(e, role="supporting") for e in support]
    evidence = {
        "rule_title": rule.title,
        "ruleset": rule.ruleset,
        "summary": _summary(rule, items),
        "signals": items,
    }
    return Suggestion(rule=rule, confidence=confidence, evidence=evidence)


def evaluate(
    signals: AssetSignals, rulesets: Sequence[Ruleset] | None = None
) -> list[Suggestion]:
    """Every rule that fires for ``signals``, ordered by rule id."""
    rulesets = _default_rulesets() if rulesets is None else rulesets
    out = [s for rs in rulesets for r in rs.rules if (s := evaluate_rule(r, signals))]
    return sorted(out, key=lambda s: s.rule.id)


# ──────────────────────────────────────────────────────────────────────
# Persistence
# ──────────────────────────────────────────────────────────────────────


@dataclass
class SuggestResult:
    tenant_id: str
    assets: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    withdrawn: int = 0
    # Fired but left alone: the rule was reviewed, or a hand-entered mapping
    # already covers that framework and category.
    skipped_human_set: int = 0


def _fields(s: Suggestion) -> dict:
    r = s.rule
    return {
        "framework": r.framework,
        "risk_category": r.category,
        "risk_level": r.level,
        "framework_ref": r.reference,
        "ruleset_version": r.ruleset_version,
        "confidence": s.confidence,
        "evidence": s.evidence,
    }


async def suggest_risks(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    rulesets: Sequence[Ruleset] | None = None,
    now: datetime | None = None,
) -> SuggestResult:
    """Re-evaluate every asset of one tenant and sync its suggested mappings.

    Idempotent: a second run with unchanged inputs changes nothing. Only rows
    with ``status='suggested'`` are updated or deleted. The caller commits.
    """
    now = now or datetime.now(UTC)
    result = SuggestResult(tenant_id=str(tenant_id))

    assets = (
        (await db.execute(select(AIAsset).where(AIAsset.tenant_id == tenant_id)))
        .scalars()
        .all()
    )
    flows_by_asset: dict[UUID, list[DataFlow]] = {}
    for f in (
        await db.execute(select(DataFlow).where(DataFlow.tenant_id == tenant_id))
    ).scalars():
        flows_by_asset.setdefault(f.asset_id, []).append(f)
    risks_by_asset: dict[UUID, list[RiskMapping]] = {}
    for r in (
        await db.execute(select(RiskMapping).where(RiskMapping.tenant_id == tenant_id))
    ).scalars():
        risks_by_asset.setdefault(r.asset_id, []).append(r)

    inserts: list[dict] = []
    for asset in sorted(assets, key=lambda a: str(a.id)):
        result.assets += 1
        existing = risks_by_asset.get(asset.id, [])
        by_rule = {r.rule_id: r for r in existing if r.rule_id}
        manual = {(r.framework, r.risk_category) for r in existing if not r.rule_id}
        fired: set[str] = set()

        for s in evaluate(
            collect_signals(asset, flows_by_asset.get(asset.id, [])), rulesets
        ):
            fired.add(s.rule.id)
            row = by_rule.get(s.rule.id)
            if row is not None and row.status != SUGGESTED:
                result.skipped_human_set += 1
                continue
            if row is None and (s.rule.framework, s.rule.category) in manual:
                result.skipped_human_set += 1
                continue
            values = _fields(s)
            if row is None:
                inserts.append(
                    {
                        "tenant_id": tenant_id,
                        "asset_id": asset.id,
                        "rule_id": s.rule.id,
                        "status": SUGGESTED,
                        "source": SOURCE_RULES,
                        "created_at": now,
                        "updated_at": now,
                        **values,
                    }
                )
                result.created += 1
            elif any(getattr(row, k) != v for k, v in values.items()):
                # Guarded on status so a review that lands while this run is
                # in flight is never overwritten.
                res = await db.execute(
                    update(RiskMapping)
                    .where(RiskMapping.id == row.id, RiskMapping.status == SUGGESTED)
                    .values(**values, updated_at=now)
                    .execution_options(synchronize_session=False)
                )
                if res.rowcount:
                    result.updated += 1
                else:
                    result.skipped_human_set += 1
            else:
                result.unchanged += 1

        for row in existing:
            if row.rule_id and row.status == SUGGESTED and row.rule_id not in fired:
                res = await db.execute(
                    delete(RiskMapping)
                    .where(RiskMapping.id == row.id, RiskMapping.status == SUGGESTED)
                    .execution_options(synchronize_session=False)
                )
                result.withdrawn += res.rowcount or 0

    if inserts:
        for row in inserts:
            row["id"] = uuid7()
        stmt = pg_insert(RiskMapping.__table__).values(inserts)
        # A concurrent run may have inserted the same rule: keep that row.
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["tenant_id", "asset_id", "rule_id"],
            index_where=RiskMapping.__table__.c.rule_id.is_not(None),
        )
        await db.execute(stmt)
    await db.flush()
    return result


def risk_status_filter(status: str | None, *, default: str | None = None):
    """WHERE clause for the ``status`` query parameter of the risk read
    endpoints: one status, else ``default`` (one status), else everything but
    rejected rows. The Partner API defaults to ``confirmed`` so existing
    integrations keep receiving only human-set classifications."""
    status = status or default
    if status:
        return RiskMapping.status == status
    return RiskMapping.status != REJECTED


def risk_dict(r: RiskMapping) -> dict:
    """API shape of a risk mapping, shared by the registry and Partner API."""
    return {
        "id": str(r.id),
        "risk_category": r.risk_category,
        "risk_level": r.risk_level,
        "framework": r.framework,
        "status": r.status or CONFIRMED,
        "source": r.source or SOURCE_MANUAL,
        "rule_id": r.rule_id,
        "ruleset_version": r.ruleset_version,
        "framework_ref": r.framework_ref,
        "confidence": r.confidence,
        "evidence": r.evidence,
        "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
        "reviewed_by": r.reviewed_by,
    }


# ──────────────────────────────────────────────────────────────────────
# Admin endpoints (tenant API key, like the lifecycle sweep)
# ──────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/v1/admin", tags=["Admin"], route_class=BoundedJsonRoute)

_REGISTRY = Depends(require_admin("admin:registry"))


def _not_found(detail: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "type": "about:blank",
            "title": "Not Found",
            "status": 404,
            "detail": detail,
        },
    )


@router.post(
    "/risk-suggestions/run",
    summary="Re-evaluate risk classification suggestions for the tenant's assets",
)
async def run_suggestions(request: Request, principal: AdminPrincipal = _REGISTRY) -> dict:
    tenant_id = principal.tenant_id
    db = request.state.db
    result = await suggest_risks(db, tenant_id)
    await db.commit()
    return asdict(result)


class ReviewBody(BaseModel):
    status: Literal["confirmed", "rejected"]
    # A tenant API key is a machine credential, so the human who decided is
    # named here (as for attestations); the key prefix is stored next to it.
    reviewed_by: StorableStr = Field(
        ..., min_length=1, max_length=320, description="The person deciding, e.g. an email"
    )


@router.post(
    "/risk-mappings/{risk_id}/review",
    summary="Confirm or reject a risk mapping (a human decision the engine keeps)",
)
async def review_risk(
    risk_id: UUID, body: ReviewBody, request: Request, principal: AdminPrincipal = _REGISTRY
) -> dict:
    tenant_id = principal.tenant_id
    db = request.state.db
    row = (
        await db.execute(
            select(RiskMapping).where(
                RiskMapping.id == risk_id, RiskMapping.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        await record_read(request, tenant_id=tenant_id, status_code=404, response_count=0)
        raise _not_found("Risk mapping not found")
    now = datetime.now(UTC)
    row.status = body.status
    row.reviewed_at = now
    row.reviewed_by = body.reviewed_by.strip()
    row.reviewed_key_prefix = bearer_key_prefix(request)
    row.updated_at = now
    out = risk_dict(row)
    await db.commit()
    await record_read(request, tenant_id=tenant_id, status_code=200, response_count=1)
    return out


# ──────────────────────────────────────────────────────────────────────
# CLI: python -m collector.risk_suggestions
# ──────────────────────────────────────────────────────────────────────


async def _run_cli(tenant_ids: list[UUID] | None, database_url: str) -> int:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, class_=AsyncSession)
    try:
        if tenant_ids is None:
            async with session_factory() as db:
                await rls.bypass(db, "risk-suggestions-cli:list-tenants", actor="cli")
                tenant_ids = list((await db.execute(select(Tenant.id))).scalars())
        for tenant_id in tenant_ids:
            async with session_factory() as db:
                await rls.set_tenant(db, tenant_id)
                r = await suggest_risks(db, tenant_id)
                await db.commit()
            print(
                f"{tenant_id}: {r.assets} assets, {r.created} created, {r.updated} updated, "
                f"{r.withdrawn} withdrawn, {r.skipped_human_set} left to human decisions"
            )
    finally:
        await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m collector.risk_suggestions",
        description="Suggest risk classifications (EU AI Act, ISO 42001, NIST AI RMF).",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--tenant", action="append", type=UUID, help="tenant id (repeatable)"
    )
    group.add_argument("--all-tenants", action="store_true")
    args = parser.parse_args(argv)

    from collector.config import settings

    return asyncio.run(_run_cli(args.tenant, settings.database_url))


if __name__ == "__main__":
    sys.exit(main())
