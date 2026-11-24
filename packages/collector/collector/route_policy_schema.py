"""route-policy.v1 validation for the collector, the same as the gateway's (#383).

A ``route`` section of a central policy (collector/policy_doc.py) carries a
route-policy.v1 document that the gateway checks in full before applying it
(gateway/src/middlewares/router/policyDoc.ts ``parseRoutePolicyDoc``). The
collector runs the same checks before it stores and activates a version, so
a document the gateway would refuse is never activated:

  - the JSON Schema: schemas/route-policy.v1.schema.json next to this module,
    an exact copy of schemas/route-policy/route-policy.v1.schema.json (the
    collector image is built from packages/collector/collector, so it cannot
    read the repository's schemas/ directory at runtime); and
  - the cross-field rules the schema states in its description but cannot
    express (group and sensitivity-level references).

``validate`` is a port of the gateway's minimal JSON Schema validator
(gateway/src/middlewares/router/jsonSchema.ts): the same keywords with the
same JavaScript semantics (an integral float is an integer, string length in
UTF-16 code units, ``$`` only at the very end). ajv/jsonschema are not
runtime dependencies of either side.

Drift is caught by tests: test_route_policy_schema.py checks the copy equals
the shared schema, that the schema uses no keyword this port lacks, and runs
the shared vectors (schemas/central-policy/route-v1.vectors.json) that the
gateway's policyPull tests also run.
"""

from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).parent / "schemas" / "route-policy.v1.schema.json"

# jsonSchema.ts ANNOTATIONS / ASSERTIONS.
ANNOTATIONS = frozenset({"$schema", "$id", "$defs", "title", "description", "default"})
ASSERTIONS = frozenset(
    {
        "$ref",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "propertyNames",
        "minProperties",
        "items",
        "minItems",
        "uniqueItems",
        "enum",
        "const",
        "minLength",
        "pattern",
        "minimum",
        "maximum",
    }
)

# constraints.ts DEFAULT_LEVELS.
DEFAULT_LEVELS = ("public", "internal", "confidential", "restricted")


@lru_cache(maxsize=1)
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _type_of(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "integer" if math.isfinite(v) and v.is_integer() else "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def _js_value(v: Any) -> Any:
    """``v`` as JavaScript sees it after JSON.parse (1.0 is 1)."""
    if isinstance(v, float) and math.isfinite(v) and v.is_integer():
        return int(v)
    if isinstance(v, list):
        return [_js_value(x) for x in v]
    if isinstance(v, dict):
        return {k: _js_value(x) for k, x in v.items()}
    return v


def _stringify(v: Any) -> str:
    """JSON.stringify(v), for the equality checks (enum, const, uniqueItems)."""
    return json.dumps(_js_value(v), separators=(",", ":"), ensure_ascii=False)


def _dump(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False)


def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


@lru_cache(maxsize=64)
def _regex(pattern: str) -> re.Pattern:
    # JavaScript's `$` (no m flag) matches only at the very end; Python's also
    # before a trailing newline.
    return re.compile(re.sub(r"(?<!\\)\$$", r"\\Z", pattern))


def validate(root: Any, sch: Any, value: Any, at: str = "$", errors: list[str] | None = None) -> list[str]:
    """Errors (``<json path>: <problem>``) for ``value`` against ``sch``; [] if valid."""
    if errors is None:
        errors = []
    if sch is True:
        return errors
    if sch is False:
        errors.append(f"{at}: not allowed")
        return errors
    if "$ref" in sch:
        name = re.sub(r"^#/\$defs/", "", str(sch["$ref"]))
        validate(root, root["$defs"][name], value, at, errors)
    t = _type_of(value)
    if "type" in sch:
        want = sch["type"]
        if not (want == t or (want == "number" and t == "integer")):
            errors.append(f"{at}: expected {want}, got {t}")
            return errors
    if "enum" in sch and not any(_stringify(e) == _stringify(value) for e in sch["enum"]):
        errors.append(f"{at}: not one of {_dump(sch['enum'])}")
    if "const" in sch and _stringify(sch["const"]) != _stringify(value):
        errors.append(f"{at}: must be {_dump(sch['const'])}")
    if t == "string":
        if sch.get("minLength") is not None and _utf16_len(value) < sch["minLength"]:
            errors.append(f"{at}: shorter than {sch['minLength']}")
        if sch.get("pattern") and not _regex(sch["pattern"]).search(value):
            errors.append(f"{at}: does not match {sch['pattern']}")
    if t in ("number", "integer"):
        if sch.get("minimum") is not None and value < sch["minimum"]:
            errors.append(f"{at}: below {sch['minimum']}")
        if sch.get("maximum") is not None and value > sch["maximum"]:
            errors.append(f"{at}: above {sch['maximum']}")
    if t == "array":
        if sch.get("minItems") is not None and len(value) < sch["minItems"]:
            errors.append(f"{at}: fewer than {sch['minItems']} items")
        if sch.get("uniqueItems"):
            seen = [_stringify(v) for v in value]
            if len(set(seen)) != len(seen):
                errors.append(f"{at}: items not unique")
        if "items" in sch:
            for i, v in enumerate(value):
                validate(root, sch["items"], v, f"{at}[{i}]", errors)
    if t == "object":
        for r in sch.get("required") or []:
            if r not in value:
                errors.append(f"{at}: missing {r}")
        if sch.get("minProperties") is not None and len(value) < sch["minProperties"]:
            errors.append(f"{at}: fewer than {sch['minProperties']} properties")
        props = sch.get("properties")
        for k in value:
            sub = f"{at}.{k}"
            if "propertyNames" in sch:
                validate(root, sch["propertyNames"], k, f"{sub} (name)", errors)
            if props is not None and k in props:
                validate(root, props[k], value[k], sub, errors)
            elif "additionalProperties" in sch:
                validate(root, sch["additionalProperties"], value[k], sub, errors)
    return errors


def keywords_in(sch: Any, out: set[str] | None = None) -> set[str]:
    """Every keyword used anywhere in a schema (jsonSchema.ts keywordsIn)."""
    if out is None:
        out = set()
    if not isinstance(sch, dict):
        return out
    for k, v in sch.items():
        out.add(k)
        if k in ("properties", "$defs"):
            for sub in v.values():
                keywords_in(sub, out)
        elif k in ("items", "additionalProperties", "propertyNames"):
            keywords_in(v, out)
    return out


def _cross_field_problems(doc: dict) -> list[str]:
    """policyDoc.ts crossFieldProblems."""
    problems: list[str] = []
    groups = set(doc.get("groups") or {})

    def group(at: str, name: Any) -> None:
        if isinstance(name, str) and name not in groups:
            problems.append(f'{at}: "{name}" is not a group')

    group("$.default_group", doc.get("default_group"))
    for k, v in (doc.get("quality_to_group") or {}).items():
        group(f"$.quality_to_group.{k}", v)
    for i, g in enumerate(doc.get("group_order") or []):
        group(f"$.group_order[{i}]", g)
    for k, b in (doc.get("use_cases") or {}).items():
        group(f"$.use_cases.{k}.min_group", b.get("min_group") if isinstance(b, dict) else None)

    s = doc.get("sensitivity")
    raw_levels = s.get("levels") if isinstance(s, dict) else None
    levels = set(DEFAULT_LEVELS if raw_levels is None else raw_levels)

    def level(at: str, name: Any) -> None:
        if isinstance(name, str) and name not in levels:
            problems.append(f'{at}: "{name}" is not a sensitivity level')

    if s:
        level("$.sensitivity.unclassified_default", s.get("unclassified_default"))
        for k, v in (s.get("pii_escalation") or {}).items():
            level(f"$.sensitivity.pii_escalation.{k}", v)
        for i, lv in enumerate(s.get("anonymise_then_route") or []):
            level(f"$.sensitivity.anonymise_then_route[{i}]", lv)
    for k in doc.get("data_policy") or {}:
        level(f"$.data_policy.{k}", k)
    return problems


def route_policy_problems(doc: Any) -> list[str]:
    """Every reason the gateway would refuse ``doc`` as a route-policy.v1
    document (paths start at ``$``, the document); [] if it would accept it."""
    if not isinstance(doc, dict):
        return ["$: route policy must be a JSON object"]
    root = schema()
    problems = validate(root, root, doc)
    if problems:
        return problems
    return _cross_field_problems(doc)
