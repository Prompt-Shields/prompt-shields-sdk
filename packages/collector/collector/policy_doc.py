"""The ps-policy.v1 governance policy document and the compiled bundle (#340).

No database here: validation, digests and compilation only. The routes are
in collector/policies_api.py; docs/guides/central-policy.mdx is the user
guide.

Document
--------
A policy version holds one JSON document::

    {
      "schema": "ps-policy.v1",
      "sections": {
        "route":  {"version": 1, "policy": { ...route-policy.v1... }},
        "access": {"version": 1, "default": "deny", "groups": [ ... ]}
      }
    }

Each section is an object with an integer ``version`` (the version of that
section's own format) and a body whose shape the section defines. A section
is changed incompatibly by adding a new version to ``SECTIONS``, never by
changing an existing one, so a gateway that only knows ``route`` v1 refuses
a bundle carrying ``route`` v2 (it keeps its last good policy) instead of
misreading it. New sections are added to ``SECTIONS`` by the issue that
enforces them; ``RESERVED_SECTIONS`` names those already planned. Unknown
and reserved names are refused, so nothing is stored that nothing enforces.

Sections today:

``route`` v1
    ``{"version": 1, "policy": <route-policy.v1>}``: the gateway router's
    route policy (schemas/route-policy/route-policy.v1.schema.json), the same
    document as PS_ROUTE_POLICY_FILE: model groups, sensitivity levels and
    the data policy. The collector checks its outline (the schema's required
    keys and ``schema_version``); the gateway validates it in full before
    applying it and refuses the whole bundle if it does not pass.
``access`` v1
    ``{"version": 1, "default": "deny"|"allow", "groups": [...]}``: who may
    call which providers, models and MCP tools, with an optional per-group
    rate limit, by identity-provider group. Same format as the gateway's
    PS_OIDC_POLICY (docs/guides/gateway-oidc.mdx) and enforced by the gateway's
    OIDC authentication, so it needs PS_OIDC_ENABLED there.
``budgets`` v1
    ``{"version": 1, "on_store_error": "open"|"closed", "budgets": [...]}``:
    spend budgets (#29), each a budget.v1 budget
    (schemas/budget/budget.v1.schema.json): a USD (``limit_usd``) or token
    (``limit_tokens``) limit per scope (global, business_unit, use_case,
    owner, team, project, user, service, model) and UTC day, ISO week or
    month, mode warn / downgrade / block. The gateway replaces the budgets of
    PS_BUDGETS / PS_BUDGETS_FILE with them; the counter namespace stays the
    gateway's (PS_BUDGET_NAMESPACE), so it is not part of the section. The
    collector checks every budget as the SDK and the gateway do.

Inheritance
-----------
A policy is bound to the tenant, a team or a project. The effective policy
of a project is its tenant's sections, replaced section by section by its
team's, then by its own: a section is taken whole from the most specific
active policy that has it (project > team > tenant). Sections are never
merged field by field, so a team's ``access`` section is exactly what the
team gets.

Bundle
------
``GET /api/v1/policy/effective`` returns every effective policy of the
tenant in one document, so a gateway needs one request (and one ETag)::

    {
      "schema": "ps-policy-bundle.v1",
      "tenant":   {"version": "default@3", "sections": {...}},
      "teams":    {"<team id>":    {"name": ..., "version": ..., "sections": {...}}},
      "projects": {"<project id>": {"name": ..., "team_id": ..., "version": ..., "sections": {...}}}
    }

``version`` names the policy versions that contributed, most general first
(``default@3+team-eu@2``); the gateway records it as ``policy_version`` in
telemetry. ``teams`` and ``projects`` list only those with an active policy
of their own (or, for a project, whose team has one). The bundle's ETag is
the SHA-256 of its canonical JSON.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

SCHEMA = "ps-policy.v1"
BUNDLE_SCHEMA = "ps-policy-bundle.v1"
# A policy document is a few KB; anything larger is not one.
MAX_DOCUMENT_BYTES = 256 * 1024

# Planned sections, refused until the issue that enforces them adds them.
RESERVED_SECTIONS: dict[str, str] = {
    "rate_limits": "rate-limit policies (#344)",
    "prices": "model prices (#348)",
    "guardrails": "guardrail settings (#349)",
    "pseudonymisation": "pseudonymisation settings (#350)",
}

ROUTE_SCHEMA_VERSION = "route-policy.v1"
# route-policy.v1's required keys (schemas/route-policy/route-policy.v1.schema.json).
ROUTE_REQUIRED = ("schema_version", "default_group", "groups", "quality_to_group")
QUALITY_KEYS = ("draft", "balanced", "critical")

# psOidc/config.ts parsePolicy / parseMcpGroupPolicy, mirrored.
ACCESS_GROUP_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
ACCESS_GROUP_KEYS = frozenset({"name", "match", "providers", "models", "rate_limit", "mcp"})
MCP_SERVER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class PolicyDocumentError(ValueError):
    """The document is not a valid ps-policy.v1 document."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def _is_obj(v: Any) -> bool:
    return isinstance(v, dict)


def _strings(at: str, v: Any, problems: list[str]) -> None:
    if not (
        isinstance(v, list) and v and all(isinstance(s, str) and s.strip() for s in v)
    ):
        problems.append(f"{at} must be a non-empty array of strings")


def _route_v1(at: str, body: dict, problems: list[str]) -> None:
    extra = sorted(set(body) - {"version", "policy"})
    if extra:
        problems.append(f"{at}: unknown keys {extra}")
    policy = body.get("policy")
    if not _is_obj(policy):
        problems.append(f"{at}.policy must be a route-policy.v1 object")
        return
    missing = [k for k in ROUTE_REQUIRED if k not in policy]
    if missing:
        problems.append(f"{at}.policy is missing {missing}")
    if policy.get("schema_version", ROUTE_SCHEMA_VERSION) != ROUTE_SCHEMA_VERSION:
        problems.append(f'{at}.policy.schema_version must be "{ROUTE_SCHEMA_VERSION}"')
    groups = policy.get("groups")
    if groups is not None and not (_is_obj(groups) and groups):
        problems.append(f"{at}.policy.groups must be a non-empty object")
    elif _is_obj(groups):
        for name, ref in [("default_group", policy.get("default_group"))] + [
            (f"quality_to_group.{k}", v)
            for k, v in (policy.get("quality_to_group") or {}).items()
        ]:
            if isinstance(ref, str) and ref not in groups:
                problems.append(f'{at}.policy.{name}: "{ref}" is not a group')
    qtg = policy.get("quality_to_group")
    if qtg is not None and not (_is_obj(qtg) and set(qtg) == set(QUALITY_KEYS)):
        problems.append(f"{at}.policy.quality_to_group must map exactly {list(QUALITY_KEYS)}")


def _mcp(at: str, raw: Any, problems: list[str]) -> None:
    if not _is_obj(raw) or set(raw) - {"servers"} or not _is_obj(raw.get("servers")):
        problems.append(f'{at} must be {{"servers": {{"<server id>": {{...}}}}}}')
        return
    for sid, rule in raw["servers"].items():
        where = f"{at}.servers.{sid}"
        if not MCP_SERVER_ID.match(sid):
            problems.append(f"{where}: the server id must match [a-z0-9][a-z0-9_-]{{0,62}}")
        if not _is_obj(rule) or set(rule) - {"tools"}:
            problems.append(f'{where} must be {{}} or {{"tools": {{...}}}}')
            continue
        tools = rule.get("tools")
        if tools is None:
            continue
        if not _is_obj(tools) or set(tools) - {"allow", "deny"}:
            problems.append(f'{where}.tools must be {{"allow": [...]}} and/or {{"deny": [...]}}')
            continue
        for k, names in tools.items():
            if not (isinstance(names, list) and all(isinstance(n, str) for n in names)):
                problems.append(f"{where}.tools.{k} must be an array of tool names")


def _access_v1(at: str, body: dict, problems: list[str]) -> None:
    extra = sorted(set(body) - {"version", "default", "groups"})
    if extra:
        problems.append(f"{at}: unknown keys {extra}")
    if body.get("default", "deny") not in ("deny", "allow"):
        problems.append(f'{at}.default must be "deny" or "allow"')
    groups = body.get("groups")
    if not isinstance(groups, list):
        problems.append(f"{at}.groups must be an array")
        return
    seen: set[str] = set()
    for i, g in enumerate(groups):
        where = f"{at}.groups[{i}]"
        if not _is_obj(g):
            problems.append(f"{where} must be an object")
            continue
        unknown = sorted(set(g) - ACCESS_GROUP_KEYS)
        if unknown:
            problems.append(f"{where}: unknown keys {unknown}")
        name = g.get("name")
        if not (isinstance(name, str) and ACCESS_GROUP_NAME.match(name)):
            problems.append(f"{where}.name must be 1-64 characters of [A-Za-z0-9_.:-]")
        elif name in seen:
            problems.append(f'{where}.name "{name}" is used twice')
        else:
            seen.add(name)
        _strings(f"{where}.match", g.get("match"), problems)
        for k in ("providers", "models"):
            if k in g:
                _strings(f"{where}.{k}", g[k], problems)
        if "rate_limit" in g:
            rl = g["rate_limit"]
            rpm = rl.get("requests_per_minute") if _is_obj(rl) else None
            if not (isinstance(rpm, int) and not isinstance(rpm, bool) and rpm >= 1):
                problems.append(
                    f"{where}.rate_limit.requests_per_minute must be a positive integer"
                )
            elif rl.get("per", "user") not in ("user", "group"):
                problems.append(f'{where}.rate_limit.per must be "user" or "group"')
        if "mcp" in g:
            _mcp(f"{where}.mcp", g["mcp"], problems)


# budget.v1 (schemas/budget/budget.v1.schema.json), mirrored from the SDK's
# prompt_shields.budgets.parse_budget and the gateway's psBudget/config.ts.
BUDGET_KEYS = frozenset({"id", "scope", "subject", "window", "limit_usd",
                         "limit_tokens", "mode", "warn_at", "downgrade_model"})
BUDGET_TOKEN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
BUDGET_SCOPES = ("global", "business_unit", "use_case", "owner", "team",
                 "project", "user", "service", "model")
BUDGET_WINDOWS = ("day", "week", "month")
BUDGET_MODES = ("warn", "downgrade", "block")
MAX_BUDGETS = 500


def _num(v: Any) -> bool:
    """A finite number (NaN fails both comparisons), never a bool."""
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and float("-inf") < v < float("inf"))


def _budget(where: str, b: Any, problems: list[str]) -> str | None:
    """Check one budget; returns its id when it has a valid one."""
    if not _is_obj(b):
        problems.append(f"{where} must be an object")
        return None
    unknown = sorted(set(b) - BUDGET_KEYS)
    if unknown:
        problems.append(f"{where}: unknown keys {unknown}")
    bid = b.get("id")
    if not (isinstance(bid, str) and BUDGET_TOKEN.match(bid)):
        problems.append(f"{where}.id must match {BUDGET_TOKEN.pattern}")
        bid = None
    scope = b.get("scope")
    if scope not in BUDGET_SCOPES:
        problems.append(f"{where}.scope must be one of {list(BUDGET_SCOPES)}")
    subject = b.get("subject")
    if scope == "global":
        if subject is not None:
            problems.append(f"{where}: a global budget has no subject")
    elif scope in BUDGET_SCOPES and not (isinstance(subject, str) and subject):
        problems.append(f'{where}.subject is required for scope "{scope}" ("*" for each value)')
    if b.get("window") not in BUDGET_WINDOWS:
        problems.append(f"{where}.window must be one of {list(BUDGET_WINDOWS)}")
    has_usd, has_tokens = "limit_usd" in b, "limit_tokens" in b
    if has_usd == has_tokens:
        problems.append(f"{where} needs exactly one of limit_usd, limit_tokens")
    elif has_usd and not (_num(b["limit_usd"]) and b["limit_usd"] >= 0):
        problems.append(f"{where}.limit_usd must be a number >= 0")
    elif has_tokens and not (
        isinstance(b["limit_tokens"], int) and not isinstance(b["limit_tokens"], bool)
        and b["limit_tokens"] >= 0
    ):
        problems.append(f"{where}.limit_tokens must be an integer >= 0")
    mode = b.get("mode")
    if mode not in BUDGET_MODES:
        problems.append(f"{where}.mode must be one of {list(BUDGET_MODES)}")
    elif mode == "downgrade" and has_tokens and not has_usd:
        problems.append(f'{where}: a token budget\'s mode must be "warn" or "block"')
    if "warn_at" in b and not (_num(b["warn_at"]) and 0 < b["warn_at"] <= 1):
        problems.append(f"{where}.warn_at must be in (0, 1]")
    if "downgrade_model" in b:
        dm = b["downgrade_model"]
        if mode != "downgrade":
            problems.append(f'{where}.downgrade_model needs mode "downgrade"')
        elif not (isinstance(dm, str) and dm):
            problems.append(f"{where}.downgrade_model must be a model id")
    return bid


def _budgets_v1(at: str, body: dict, problems: list[str]) -> None:
    extra = sorted(set(body) - {"version", "on_store_error", "budgets"})
    if extra:
        problems.append(f"{at}: unknown keys {extra}")
    if body.get("on_store_error", "open") not in ("open", "closed"):
        problems.append(f'{at}.on_store_error must be "open" or "closed"')
    budgets = body.get("budgets")
    if not isinstance(budgets, list):
        problems.append(f"{at}.budgets must be an array")
        return
    if len(budgets) > MAX_BUDGETS:
        problems.append(f"{at}.budgets has more than {MAX_BUDGETS} budgets")
    seen: set[str] = set()
    for i, b in enumerate(budgets):
        bid = _budget(f"{at}.budgets[{i}]", b, problems)
        if bid is None:
            continue
        if bid in seen:
            problems.append(f'{at}.budgets[{i}].id "{bid}" is used twice')
        seen.add(bid)


@dataclass(frozen=True)
class Section:
    description: str
    # version -> validator(at, body, problems)
    versions: Mapping[int, Callable[[str, dict, list[str]], None]]


# Section name -> its formats. Order is the order docs and GET responses show.
SECTIONS: dict[str, Section] = {
    "route": Section(
        "Gateway route policy (route-policy.v1): model groups, sensitivity, data policy",
        {1: _route_v1},
    ),
    "access": Section(
        "Providers, models, MCP servers/tools and rate limits by identity-provider "
        "group (the gateway's PS_OIDC_POLICY format)",
        {1: _access_v1},
    ),
    "budgets": Section(
        "Spend budgets (budget.v1 budgets): USD or token limits per tenant, team, "
        "project, user, service, model and attribution, enforced by the gateway",
        {1: _budgets_v1},
    ),
}


def validate_document(doc: Any) -> dict:
    """The document unchanged if valid, else PolicyDocumentError listing
    every problem found."""
    if not _is_obj(doc):
        raise PolicyDocumentError(["the policy document must be a JSON object"])
    problems: list[str] = []
    if len(canonical_json(doc).encode()) > MAX_DOCUMENT_BYTES:
        raise PolicyDocumentError([f"the document is larger than {MAX_DOCUMENT_BYTES} bytes"])
    extra = sorted(set(doc) - {"schema", "sections", "description"})
    if extra:
        problems.append(f"$: unknown keys {extra}")
    if doc.get("schema") != SCHEMA:
        problems.append(f'$.schema must be "{SCHEMA}"')
    if "description" in doc and not isinstance(doc["description"], str):
        problems.append("$.description must be a string")
    sections = doc.get("sections")
    if not _is_obj(sections):
        problems.append("$.sections must be an object of section name -> section")
        raise PolicyDocumentError(problems)
    for name, body in sections.items():
        at = f"$.sections.{name}"
        spec = SECTIONS.get(name)
        if spec is None:
            if name in RESERVED_SECTIONS:
                problems.append(f"{at}: reserved for {RESERVED_SECTIONS[name]}, not supported yet")
            else:
                problems.append(f"{at}: unknown section (known: {list(SECTIONS)})")
            continue
        if not _is_obj(body):
            problems.append(f"{at} must be an object")
            continue
        version = body.get("version")
        if isinstance(version, bool) or version not in spec.versions:
            problems.append(f"{at}.version must be one of {sorted(spec.versions)}")
            continue
        spec.versions[version](at, body, problems)
    if problems:
        raise PolicyDocumentError(problems)
    return doc


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class ActivePolicy:
    """An active policy version, for compilation."""

    name: str
    version: int
    sections: Mapping[str, Any]

    @property
    def label(self) -> str:
        return f"{self.name}@{self.version}"


def compile_effective(layers: Iterable[ActivePolicy | None]) -> dict:
    """The effective policy of ``layers`` (most general first, None for a
    level without an active policy): sections replaced whole, later wins."""
    sections: dict[str, Any] = {}
    labels: list[str] = []
    for layer in layers:
        if layer is None:
            continue
        sections.update(layer.sections)
        labels.append(layer.label)
    return {"version": "+".join(labels) or None, "sections": sections}


def build_bundle(
    tenant: ActivePolicy | None,
    teams: Mapping[str, tuple[str, ActivePolicy | None]],
    projects: Mapping[str, tuple[str, str, ActivePolicy | None]],
) -> dict:
    """The bundle: ``teams`` is team id -> (name, its active policy);
    ``projects`` is project id -> (name, team id, its active policy).
    Entries with nothing of their own (or, for a project, from its team)
    are left out."""
    out_teams: dict[str, dict] = {}
    for team_id, (name, policy) in teams.items():
        if policy is not None:
            out_teams[team_id] = {"name": name, **compile_effective([tenant, policy])}
    out_projects: dict[str, dict] = {}
    for project_id, (name, team_id, policy) in projects.items():
        team_policy = teams.get(team_id, ("", None))[1]
        if policy is None and team_policy is None:
            continue
        out_projects[project_id] = {
            "name": name,
            "team_id": team_id,
            **compile_effective([tenant, team_policy, policy]),
        }
    return {
        "schema": BUNDLE_SCHEMA,
        "tenant": compile_effective([tenant]),
        "teams": dict(sorted(out_teams.items())),
        "projects": dict(sorted(out_projects.items())),
    }


def section_summary(document: Mapping[str, Any] | None) -> dict[str, int]:
    """Section name -> section version, for audit rows and listings."""
    sections = (document or {}).get("sections") or {}
    return {k: v.get("version") for k, v in sorted(sections.items()) if isinstance(v, dict)}
