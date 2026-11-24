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
    the data policy. The collector runs the gateway's checks before storing
    a version (collector/route_policy_schema.py: the same JSON Schema and
    cross-field rules), so a document the gateway would refuse is never
    activated; the gateway still validates it in full before applying it
    and refuses the whole bundle if it does not pass.
``access`` v1
    ``{"version": 1, "default": "deny"|"allow", "groups": [...]}``: who may
    call which providers, models and MCP tools, with an optional per-group
    rate limit, by identity-provider group. Same format as the gateway's
    PS_OIDC_POLICY (docs/guides/gateway-oidc.mdx) and enforced by the gateway's
    OIDC authentication, so it needs PS_OIDC_ENABLED there.
``prices`` v1
    ``{"version": 1, "currency": "USD", "overrides": [...]}``: the tenant's
    negotiated model prices (#348), effective-dated, per provider and model
    (or ``"*"`` for every model of a provider): either per-1k token rates
    or a ``multiplier`` on the list price. Applied by the gateway to its
    telemetry cost and cost metric, and by the collector at ingest. Only a
    tenant-scope policy may carry it (a negotiated contract is the
    tenant's); see collector/prices.py and docs/guides/negotiated-prices.mdx.
``rate_limits`` v1 (#344)
    ``{"version": 1, "on_store_error": "open"|"closed", "rules": [...]}``:
    request and token rate limits, enforced by the gateway (the same format
    as its PS_RATE_LIMITS; gateway/src/middlewares/psRateLimit/config.ts).
    Each rule::

        {"id": "team-chat",                 # [A-Za-z0-9_.:-]{1,64}, unique
         "scope": "global"|"virtual_key"|"team"|"project"|"user",
         "subjects": ["team-a"],            # optional, not with global; user: OIDC
                                            # sub, virtual_key: the virtual-key id
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
``guardrails`` v1
    ``{"version": 1, "rules": [...], "stream_window_chars": 256}``: checks
    the gateway runs itself on every model call's input and output,
    whatever guardrails the caller sends (#349). Each rule is
    ``{"id", "check": "pii"|"secrets"|"injection", "action":
    "deny"|"redact"|"log", "stages": ["input", "output"], "categories":
    [...], "models": [...]}``; ``stages`` defaults to both, ``categories``
    to every value category of the check (``health_data`` and
    ``financial_data`` only when named), ``models`` (glob patterns) to
    every model. Unlike other sections, guardrails add up across levels:
    the gateway applies the tenant's rules and those of the caller's team
    and project together, the strictest action winning, so a lower level
    can add rules but never drop the tenant's (docs/guides/guardrails.mdx).

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
import itertools
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from collector import route_policy_schema

SCHEMA = "ps-policy.v1"
BUNDLE_SCHEMA = "ps-policy-bundle.v1"
# A policy document is a few KB; anything larger is not one.
MAX_DOCUMENT_BYTES = 256 * 1024

# Planned sections, refused until the issue that enforces them adds them.
RESERVED_SECTIONS: dict[str, str] = {
    "pseudonymisation": "pseudonymisation settings (#350)",
    "budgets": "budget references",
}

ROUTE_SCHEMA_VERSION = "route-policy.v1"

# psOidc/config.ts parsePolicy / parseMcpGroupPolicy, mirrored (the shared
# vectors in schemas/central-policy/access-v1.vectors.json keep them equal).
ACCESS_GROUP_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,64}\Z")
ACCESS_GROUP_KEYS = frozenset({"name", "match", "providers", "models", "rate_limit", "mcp"})
MCP_SERVER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}\Z")
MCP_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}\Z")

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


# What JavaScript's String.prototype.trim removes (WhiteSpace and
# LineTerminator), which differs from str.strip(): it removes U+FEFF and
# keeps U+001C-U+001F and U+0085.
JS_WHITESPACE = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def _strings(at: str, v: Any, problems: list[str]) -> None:
    # psOidc/config.ts stringList: `s.trim()` must leave something.
    if not (
        isinstance(v, list)
        and v
        and all(isinstance(s, str) and s.strip(JS_WHITESPACE) for s in v)
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
    # The gateway's own checks (schema + cross-field rules), so a document
    # the gateway would refuse is never stored or activated.
    for p in route_policy_schema.route_policy_problems(policy):
        problems.append(f"{at}.policy{p[1:]}" if p.startswith("$") else f"{at}.policy: {p}")


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
        if "tools" not in rule:
            continue
        tools = rule["tools"]
        if not _is_obj(tools) or set(tools) - {"allow", "deny"}:
            problems.append(f'{where}.tools must be {{"allow": [...]}} and/or {{"deny": [...]}}')
            continue
        for k, names in tools.items():
            if not (isinstance(names, list) and all(isinstance(n, str) for n in names)):
                problems.append(f"{where}.tools.{k} must be an array of tool names")
                continue
            for n in names:
                if not MCP_TOOL_NAME.match(n):
                    problems.append(f"{where}.tools.{k} has an invalid tool name: {json.dumps(n)}")


def _access_v1(at: str, body: dict, problems: list[str]) -> None:
    extra = sorted(set(body) - {"version", "default", "groups"})
    if extra:
        problems.append(f"{at}: unknown keys {extra}")
    # JavaScript's `?? "deny"`: null means the default too.
    if body.get("default") not in (None, "deny", "allow"):
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
            # Number.isInteger: 5.0 is an integer.
            integral = isinstance(rpm, int) or (
                isinstance(rpm, float) and math.isfinite(rpm) and rpm.is_integer()
            )
            if not (integral and not isinstance(rpm, bool) and rpm >= 1):
                problems.append(
                    f"{where}.rate_limit.requests_per_minute must be a positive integer"
                )
            elif rl.get("per") not in (None, "user", "group"):
                problems.append(f'{where}.rate_limit.per must be "user" or "group"')
        if "mcp" in g:
            _mcp(f"{where}.mcp", g["mcp"], problems)


# prices v1 (#348).
PRICE_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
PRICE_CURRENCY = re.compile(r"^[A-Z]{3}$")
PRICE_RATE_KEYS = ("input_per_1k", "output_per_1k", "cached_input_per_1k", "cache_write_per_1k")
PRICE_OVERRIDE_KEYS = frozenset(
    {"provider", "model", "valid_from", "valid_to", "multiplier", *PRICE_RATE_KEYS}
)
MAX_PRICE_OVERRIDES = 1000
MAX_PRICE_MODEL_LENGTH = 200
# A multiplier above this is a typo, not a contract.
MAX_PRICE_MULTIPLIER = 10.0
# Sections only a tenant-scope policy may carry.
TENANT_ONLY_SECTIONS = frozenset({"prices"})


def parse_instant(value: Any) -> datetime | None:
    """``YYYY-MM-DD`` (midnight UTC) or an ISO 8601 date-time with a UTC
    offset, as an aware datetime; None if it is neither."""
    if not isinstance(value, str) or not value:
        return None
    try:
        if len(value) == 10:
            return datetime.combine(date.fromisoformat(value), datetime.min.time(), UTC)
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _prices_v1(at: str, body: dict, problems: list[str]) -> None:
    extra = sorted(set(body) - {"version", "currency", "usd_per_unit", "overrides"})
    if extra:
        problems.append(f"{at}: unknown keys {extra}")
    currency = body.get("currency", "USD")
    if not (isinstance(currency, str) and PRICE_CURRENCY.match(currency)):
        problems.append(f'{at}.currency must be an ISO 4217 code such as "USD"')
    elif currency == "USD":
        if body.get("usd_per_unit", 1) != 1:
            problems.append(f"{at}.usd_per_unit must be 1 (or left out) for USD prices")
    else:
        rate = body.get("usd_per_unit")
        if not (_number(rate) and rate > 0):
            problems.append(
                f"{at}.usd_per_unit (USD per 1 {currency}, the contract's fixed rate) "
                "is required for a non-USD currency and must be a positive number"
            )
    overrides = body.get("overrides")
    if not isinstance(overrides, list):
        problems.append(f"{at}.overrides must be an array")
        return
    if len(overrides) > MAX_PRICE_OVERRIDES:
        problems.append(f"{at}.overrides has more than {MAX_PRICE_OVERRIDES} entries")
        return
    windows: dict[tuple[str, str], list[tuple[datetime, datetime | None, int]]] = {}
    for i, o in enumerate(overrides):
        where = f"{at}.overrides[{i}]"
        if not _is_obj(o):
            problems.append(f"{where} must be an object")
            continue
        unknown = sorted(set(o) - PRICE_OVERRIDE_KEYS)
        if unknown:
            problems.append(f"{where}: unknown keys {unknown}")
        provider, model = o.get("provider"), o.get("model")
        ok = True
        if not (isinstance(provider, str) and PRICE_PROVIDER.match(provider)):
            problems.append(f"{where}.provider must be a lower-case provider id")
            ok = False
        if not (
            isinstance(model, str) and model.strip() and len(model) <= MAX_PRICE_MODEL_LENGTH
        ):
            problems.append(
                f'{where}.model must be a model id (1-{MAX_PRICE_MODEL_LENGTH} characters) or "*"'
            )
            ok = False
        start = parse_instant(o.get("valid_from"))
        if start is None:
            problems.append(
                f"{where}.valid_from must be a date (YYYY-MM-DD, UTC) or an ISO 8601 "
                "date-time with an offset"
            )
            ok = False
        end = None
        if o.get("valid_to") is not None:
            end = parse_instant(o["valid_to"])
            if end is None:
                problems.append(f"{where}.valid_to must be a date or date-time like valid_from")
                ok = False
            elif start is not None and end <= start:
                problems.append(f"{where}.valid_to must be after valid_from")
                ok = False
        rates = [k for k in PRICE_RATE_KEYS if k in o]
        if "multiplier" in o:
            m = o["multiplier"]
            if rates:
                problems.append(f"{where}: give either multiplier or per-1k rates, not both")
            elif not (_number(m) and 0 < m <= MAX_PRICE_MULTIPLIER):
                problems.append(
                    f"{where}.multiplier must be a number above 0 and at most "
                    f"{MAX_PRICE_MULTIPLIER:g}"
                )
        else:
            missing = [k for k in PRICE_RATE_KEYS[:2] if k not in o]
            if missing:
                problems.append(f"{where} needs a multiplier or at least {missing}")
            for k in rates:
                if not (_number(o[k]) and o[k] >= 0):
                    problems.append(f"{where}.{k} must be a number of at least 0")
        if ok:
            # Keyed as matched: "azure-openai" and "azure_openai" are one vendor.
            windows.setdefault((provider.replace("-", "_"), model), []).append((start, end, i))
    for (provider, model), spans in windows.items():
        spans.sort(key=lambda s: s[0])
        for (_s1, e1, i1), (s2, _e2, i2) in itertools.pairwise(spans):
            if e1 is None or e1 > s2:
                problems.append(
                    f"{at}.overrides[{i1}] and [{i2}] overlap for {provider}/{model}: "
                    "at most one price applies at a time"
                )


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


# Guardrails v1 (#349); mirrored by gateway/src/middlewares/psGuardrails/config.ts.
GUARDRAIL_RULE_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
GUARDRAIL_RULE_KEYS = frozenset({"id", "check", "action", "stages", "categories", "models"})
GUARDRAIL_STAGES = ("input", "output")
GUARDRAIL_MAX_RULES = 64
# PII categories with a value to redact (the gateway's findPiiSpans), and
# keyword categories that only mark context (deny or log).
PII_VALUE_CATEGORIES = (
    "credit_card",
    "email",
    "iban",
    "ip_address",
    "national_id",
    "phone",
    "ssn",
)
PII_CONTEXT_CATEGORIES = ("financial_data", "health_data")
SECRET_CATEGORIES = (
    "anthropic_key",
    "aws_access_key",
    "github_token",
    "google_api_key",
    "jwt",
    "openai_key",
    "private_key",
    "slack_token",
    "stripe_key",
)
GUARDRAIL_CHECKS: dict[str, dict[str, Any]] = {
    "pii": {
        "categories": PII_VALUE_CATEGORIES + PII_CONTEXT_CATEGORIES,
        "actions": ("deny", "redact", "log"),
    },
    "secrets": {"categories": SECRET_CATEGORIES, "actions": ("deny", "redact", "log")},
    "injection": {"categories": (), "actions": ("deny", "log")},
}
STREAM_WINDOW_RANGE = (64, 4096)
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9_.:@/ +*-]{1,128}$")


def _guardrails_v1(at: str, body: dict, problems: list[str]) -> None:
    extra = sorted(set(body) - {"version", "rules", "stream_window_chars"})
    if extra:
        problems.append(f"{at}: unknown keys {extra}")
    if "stream_window_chars" in body:
        w = body["stream_window_chars"]
        lo, hi = STREAM_WINDOW_RANGE
        if not (isinstance(w, int) and not isinstance(w, bool) and lo <= w <= hi):
            problems.append(f"{at}.stream_window_chars must be an integer {lo}-{hi}")
    rules = body.get("rules")
    if not isinstance(rules, list):
        problems.append(f"{at}.rules must be an array")
        return
    if len(rules) > GUARDRAIL_MAX_RULES:
        problems.append(f"{at}.rules: at most {GUARDRAIL_MAX_RULES} rules")
    seen: set[str] = set()
    for i, r in enumerate(rules):
        where = f"{at}.rules[{i}]"
        if not _is_obj(r):
            problems.append(f"{where} must be an object")
            continue
        unknown = sorted(set(r) - GUARDRAIL_RULE_KEYS)
        if unknown:
            problems.append(f"{where}: unknown keys {unknown}")
        rid = r.get("id")
        if not (isinstance(rid, str) and GUARDRAIL_RULE_ID.match(rid)):
            problems.append(f"{where}.id must be 1-64 characters of [a-z0-9_.-]")
        elif rid in seen:
            problems.append(f'{where}.id "{rid}" is used twice')
        else:
            seen.add(rid)
        check = r.get("check")
        spec = GUARDRAIL_CHECKS.get(check) if isinstance(check, str) else None
        if spec is None:
            problems.append(f"{where}.check must be one of {list(GUARDRAIL_CHECKS)}")
            continue
        action = r.get("action")
        if action not in spec["actions"]:
            problems.append(f"{where}.action must be one of {list(spec['actions'])} for {check}")
        if "stages" in r:
            st = r["stages"]
            if not (
                isinstance(st, list)
                and st
                and all(s in GUARDRAIL_STAGES for s in st)
                and len(set(st)) == len(st)
            ):
                problems.append(
                    f"{where}.stages must be a non-empty subset of {list(GUARDRAIL_STAGES)}"
                )
        if "categories" in r:
            cats = r["categories"]
            if not spec["categories"]:
                problems.append(f"{where}.categories: {check} has no categories")
            elif not (
                isinstance(cats, list)
                and cats
                and all(isinstance(x, str) and x in spec["categories"] for x in cats)
            ):
                problems.append(
                    f"{where}.categories must be a non-empty array of {list(spec['categories'])}"
                )
            elif action == "redact" and set(cats) & set(PII_CONTEXT_CATEGORIES):
                problems.append(
                    f"{where}: {sorted(set(cats) & set(PII_CONTEXT_CATEGORIES))} mark context, "
                    "not a value, and cannot be redacted (use deny or log)"
                )
        if "models" in r:
            models = r["models"]
            if not (
                isinstance(models, list)
                and models
                and all(isinstance(m, str) and MODEL_PATTERN.match(m) for m in models)
            ):
                problems.append(f"{where}.models must be a non-empty array of model patterns")


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
    "guardrails": Section(
        "Checks the gateway enforces on every model call's input and output "
        "(PII, secrets, prompt injection; deny, redact or log), added up across "
        "tenant, team and project",
        {1: _guardrails_v1},
    ),
    "prices": Section(
        "Negotiated model prices per provider and model, effective-dated "
        "(tenant-scope policies only)",
        {1: _prices_v1},
    ),
}


def validate_document(doc: Any, scope: str | None = None) -> dict:
    """The document unchanged if valid, else PolicyDocumentError listing
    every problem found. With ``scope`` (the policy's binding), sections a
    scope may not carry (`TENANT_ONLY_SECTIONS`) are refused too."""
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
        if scope is not None and scope != "tenant" and name in TENANT_ONLY_SECTIONS:
            problems.append(f"{at}: only a tenant-scope policy may carry this section")
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
