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
``rate_limits`` v1 (#344)
    ``{"version": 1, "on_store_error": "open"|"closed", "rules": [...]}``:
    request and token rate limits, enforced by the gateway (the same format
    as its PS_RATE_LIMITS; gateway/src/middlewares/psRateLimit/config.ts).
    Each rule::

        {"id": "team-chat",                 # [A-Za-z0-9_.:-]{1,64}, unique
         "scope": "global"|"virtual_key"|"team"|"project"|"user",
         "subjects": ["team-a"],            # optional, not with global
         "match": {"model": "gpt-4o*", "provider": [...], "route": [...]},
         "rpm": 600, "tpm": 200000, "tpd": 5000000, "concurrency": 20}

    ``match`` values are a string or an array of strings, ``*`` is a
    wildcard; every given key must match. At least one of rpm (requests per
    minute), tpm (input + output tokens per minute), tpd (tokens per UTC
    day) and concurrency (calls in flight) is needed. Every rule that applies
    to a call is enforced, so the strictest wins. ``on_store_error`` (default
    ``open``) is what the gateway does when its shared Redis store fails:
    count in the replica's memory, or refuse with 503. At most 200 rules.
    Unlike other sections, the ``rate_limits`` of a team or project are
    applied on top of the tenant's (to calls that name it in X-PS-Team /
    X-PS-Project), never instead of them.

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
    "prices": "model prices (#348)",
    "guardrails": "guardrail settings (#349)",
    "pseudonymisation": "pseudonymisation settings (#350)",
    "budgets": "budget references",
}

ROUTE_SCHEMA_VERSION = "route-policy.v1"
# route-policy.v1's required keys (schemas/route-policy/route-policy.v1.schema.json).
ROUTE_REQUIRED = ("schema_version", "default_group", "groups", "quality_to_group")
QUALITY_KEYS = ("draft", "balanced", "critical")

# psOidc/config.ts parsePolicy / parseMcpGroupPolicy, mirrored.
ACCESS_GROUP_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
ACCESS_GROUP_KEYS = frozenset({"name", "match", "providers", "models", "rate_limit", "mcp"})
MCP_SERVER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

# psRateLimit/config.ts parseRateLimitSection, mirrored.
RATE_LIMIT_RULE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
RATE_LIMIT_SCOPES = ("global", "virtual_key", "team", "project", "user")
RATE_LIMIT_KINDS = ("rpm", "tpm", "tpd", "concurrency")
RATE_LIMIT_MATCH_KEYS = ("model", "provider", "route")
RATE_LIMIT_MAX_RULES = 200
RATE_LIMIT_MAX_VALUES = 50


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


def _match_values(at: str, v: Any, problems: list[str]) -> None:
    values = [v] if isinstance(v, str) else v
    if not (
        isinstance(values, list)
        and 0 < len(values) <= RATE_LIMIT_MAX_VALUES
        and all(isinstance(s, str) and s.strip() and len(s) <= 256 for s in values)
    ):
        problems.append(
            f"{at} must be a non-empty string or array of up to "
            f"{RATE_LIMIT_MAX_VALUES} non-empty strings"
        )


def _rate_limits_v1(at: str, body: dict, problems: list[str]) -> None:
    extra = sorted(set(body) - {"version", "on_store_error", "rules"})
    if extra:
        problems.append(f"{at}: unknown keys {extra}")
    if body.get("on_store_error", "open") not in ("open", "closed"):
        problems.append(f'{at}.on_store_error must be "open" or "closed"')
    rules = body.get("rules")
    if not isinstance(rules, list):
        problems.append(f"{at}.rules must be an array")
        return
    if len(rules) > RATE_LIMIT_MAX_RULES:
        problems.append(f"{at}.rules: at most {RATE_LIMIT_MAX_RULES} rules")
    seen: set[str] = set()
    for i, r in enumerate(rules):
        where = f"{at}.rules[{i}]"
        if not _is_obj(r):
            problems.append(f"{where} must be an object")
            continue
        unknown = sorted(set(r) - {"id", "scope", "subjects", "match", *RATE_LIMIT_KINDS})
        if unknown:
            problems.append(f"{where}: unknown keys {unknown}")
        rid = r.get("id")
        if not (isinstance(rid, str) and RATE_LIMIT_RULE_ID.match(rid)):
            problems.append(f"{where}.id must be 1-64 characters of [A-Za-z0-9_.:-]")
        elif rid in seen:
            problems.append(f'{where}.id "{rid}" is used twice')
        else:
            seen.add(rid)
        scope = r.get("scope")
        if scope not in RATE_LIMIT_SCOPES:
            problems.append(f"{where}.scope must be one of {list(RATE_LIMIT_SCOPES)}")
        if "subjects" in r:
            if scope == "global":
                problems.append(f'{where}.subjects is not allowed with scope "global"')
            else:
                _match_values(f"{where}.subjects", r["subjects"], problems)
        if "match" in r:
            m = r["match"]
            if not _is_obj(m):
                problems.append(f"{where}.match must be an object")
            else:
                bad = sorted(set(m) - set(RATE_LIMIT_MATCH_KEYS))
                if bad:
                    problems.append(
                        f"{where}.match: unknown keys {bad} (known: {list(RATE_LIMIT_MATCH_KEYS)})"
                    )
                for k in RATE_LIMIT_MATCH_KEYS:
                    if k in m:
                        _match_values(f"{where}.match.{k}", m[k], problems)
        limits = [k for k in RATE_LIMIT_KINDS if k in r]
        if not limits:
            problems.append(f"{where} needs at least one of {list(RATE_LIMIT_KINDS)}")
        for k in limits:
            v = r[k]
            if not (isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 2**53 - 1):
                problems.append(f"{where}.{k} must be a positive integer")


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
    "rate_limits": Section(
        "Request and token rate limits (rpm, tpm, tpd, concurrency) per tenant, "
        "virtual key, team, project or user, enforced by the gateway",
        {1: _rate_limits_v1},
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
