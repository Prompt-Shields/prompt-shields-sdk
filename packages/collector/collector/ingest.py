import hashlib
import math
import re
from typing import Annotated, Any

from db.models import AIUsageEvent
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, PrivateAttr, field_validator, model_validator

from collector import (
    cost_attribution,
    loss_accounting,
    metrics,
    tenant_keys,
    tenant_limits,
    user_identity,
)
from collector.auth import resolve_tenant
from collector.data_flows import record_event_flows
from collector.dedup import AssetKey, find_or_create_asset, lock_asset_keys
from collector.embeddings import build_asset_text, get_embedding
from collector.partner_rate_limit import RateLimiter, get_ingest_rate_limiter
from collector.sanitize import BoundedJsonRoute, is_storable, storable_text
from collector.webhooks import AssetChanges

router = APIRouter(route_class=BoundedJsonRoute)


class IngestEvent(BaseModel):
    vendor: str
    model: str | None = None
    use_case_name: str | None = None
    business_unit: str | None = None
    owner_email: str | None = None
    environment: str | None = None
    data_classification: str | None = None
    calling_service: str | None = None
    source: str = "sdk"
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost: float | None = None
    latency_ms: int | None = None
    session_id: str | None = None
    tool_calls_used: list[dict] | None = None
    prompt_text: str | None = None
    # Data-flow metadata (ps_metadata on the SDK); see collector/data_flows.py.
    # Loosely typed on purpose: these fields used to be ignored, so a malformed
    # value must not 422 the whole batch. build_flow_context drops what it
    # can't use.
    data_sources: Any = None
    output_destination: Any = None
    detected_pii_types: Any = None
    region: Any = None
    # Extended usage (#9, #90, #75). Lenient like the data-flow fields: an
    # SDK or gateway version that sends an unexpected value loses that field
    # (stored as null), never the batch. See _lenient_* below.
    requested_model: str | None = None
    served_model: str | None = None
    tokens_cached: int | None = None
    tokens_cache_write: int | None = None
    tokens_cache_write_1h: int | None = None
    tokens_reasoning: int | None = None
    cost_source: str | None = None
    service_tier: str | None = None
    framework: str | None = None
    modality: str | None = None
    usage_units: float | None = None
    usage_unit_type: str | None = None
    # SDK's sha256(api key)[:16] (#93). Anything else (a raw or redacted
    # key) is dropped, so a key can never be stored here.
    api_key_fingerprint: str | None = None
    # Routing, sovereignty and savings metadata (PRD 0001 §7.12, #25).
    # Lenient like the extended usage fields above.
    api_surface: str | None = None
    route_mode: str | None = None
    route_group: str | None = None
    route_reason: str | None = None
    route_est_cost: float | None = None
    sensitivity_level: str | None = None
    sensitivity_source: str | None = None
    served_provider: str | None = None
    served_jurisdiction: str | None = None
    policy_version: str | None = None
    fallback_chain: list[str] | None = None
    counterfactual_cost: float | None = None
    baseline_id: str | None = None
    # Per-call id (#43) that /ingest/feedback names. Anything that is not a
    # short token is dropped (counted as call_id:invalid).
    call_id: str | None = None
    # Spend budgets (#28 gateway, #29 SDK; schemas/budget/README.md) and the
    # gateway cache (#33). Lenient like the route fields: an invalid value is
    # stored as null and counted as "<field>:invalid".
    budget_state: str | None = None
    budget_id: str | None = None
    budget_action: str | None = None
    cache_status: str | None = None
    cache_saved_cost: float | None = None
    est_tokens_saved: int | None = None
    # Routing learner auto mode (#34): set by the SDK router when the learner
    # decided the call. Lenient like the route fields.
    route_learner_mode: str | None = None
    route_propensity: float | None = None
    route_learner_version: str | None = None
    route_learner_frozen: bool | None = None
    # Implicit outcome signals (#44): the SDK's (and gateway's) outcome
    # classes for the call. Known class names only, see _outcome_signals.
    outcome_signals: list[str] | None = None
    outcome_signals_version: int | None = None
    # Gateway identity and trace fields (#334). user_id is never stored as
    # sent unless the tenant opted in: see collector/user_identity.py.
    # Lenient like the route fields.
    user_id: str | None = None
    user_group: str | None = None
    trace_id: str | None = None
    retry_count: int | None = None
    attempts: list[dict] | None = None
    stream_truncated: bool | None = None
    stream_cancelled: bool | None = None
    # Cost attribution (#347; collector/cost_attribution.py): the tenant's
    # team and project (id or exact name, resolved at ingest; unknown ones
    # are dropped) and up to 20 cost tags. Lenient like the route fields.
    team: str | None = None
    project: str | None = None
    tags: dict[str, str] | None = None

    @model_validator(mode="before")
    @classmethod
    def _drop_unstorable(cls, data: Any) -> Any:
        # A value the database cannot store (NUL, lone surrogate; see
        # collector/sanitize.py) is dropped as if not sent, so it costs that
        # field rather than the batch. It is counted as "<field>:invalid".
        # Aliases (the gateway's route_sensitivity_*) are applied first.
        data = canonical_event(data)
        if not isinstance(data, dict):
            return data
        bad = [k for k in SANITISED_FIELDS if k in data and not is_storable(data[k])]
        if not bad:
            return data
        return {k: v for k, v in data.items() if k not in bad}

    @field_validator("api_key_fingerprint", mode="before")
    @classmethod
    def _fingerprint(cls, value: Any) -> str | None:
        # Hex case and stray whitespace don't change the key it identifies.
        if not isinstance(value, str):
            return None
        value = value.strip().lower()
        return value if FINGERPRINT_RE.fullmatch(value) else None

    @field_validator("call_id", mode="before")
    @classmethod
    def _call_id(cls, value: Any) -> str | None:
        return value if isinstance(value, str) and CALL_ID_RE.fullmatch(value) else None

    @field_validator("outcome_signals", mode="before")
    @classmethod
    def _outcome_signals(cls, value: Any) -> list[str] | None:
        # Class names only: anything that is not a short list of short
        # snake_case tokens drops the whole field (counted as
        # outcome_signals:invalid), so nothing else, such as model output,
        # can be stored here. Tokens that are not a known class (a newer
        # SDK's) are left out; the known ones are kept. An empty list is
        # kept: it means "measured, nothing happened".
        if not isinstance(value, list) or len(value) > MAX_OUTCOME_SIGNALS:
            return None
        if not all(isinstance(v, str) and ERROR_CLASS_RE.fullmatch(v) for v in value):
            return None
        return sorted(set(value) & set(OUTCOME_SIGNAL_CLASSES))

    @field_validator("outcome_signals_version", mode="before")
    @classmethod
    def _outcome_signals_version(cls, value: Any) -> int | None:
        value = _lenient_count(value)
        return value if value is not None and 1 <= value <= MAX_OUTCOME_SIGNALS_VERSION else None

    @model_validator(mode="after")
    def _outcome_signals_pair(self) -> "IngestEvent":
        # A version without classes describes nothing.
        if self.outcome_signals is None:
            self.outcome_signals_version = None
        return self

    @field_validator("user_id", mode="before")
    @classmethod
    def _user_id(cls, value: Any) -> str | None:
        return _lenient_str(value, MAX_USER_ID_LENGTH)

    @field_validator("user_group", mode="before")
    @classmethod
    def _user_group(cls, value: Any) -> str | None:
        # The gateway sends the operator-chosen name of its policy group.
        value = _lenient_str(value, MAX_LABEL_LENGTH)
        return value if value is not None and USER_GROUP_RE.fullmatch(value) else None

    @field_validator("team", "project", mode="before")
    @classmethod
    def _attribution_ref(cls, value: Any) -> str | None:
        return cost_attribution.parse_ref(value)

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, value: Any) -> dict[str, str] | None:
        return cost_attribution.parse_tags(value)

    @field_validator("trace_id", mode="before")
    @classmethod
    def _trace_id(cls, value: Any) -> str | None:
        # The gateway's traceIdOf: a caller's short token or a UUID.
        return value if isinstance(value, str) and CALL_ID_RE.fullmatch(value) else None

    @field_validator("retry_count", mode="before")
    @classmethod
    def _retry_count(cls, value: Any) -> int | None:
        return _lenient_count(value)

    @field_validator("attempts", mode="before")
    @classmethod
    def _attempts(cls, value: Any) -> list[dict] | None:
        # The gateway's AttemptRecord list (psAttempts.ts): metadata only.
        # Known keys are kept once validated and others left out; a record
        # that is not an object, lacks its attempt number or latency, or has
        # a bad known value drops the whole field (attempts:invalid), so
        # content such as an error message can never be stored here.
        if not isinstance(value, list) or len(value) > MAX_FALLBACK_CHAIN:
            return None
        records = []
        for raw in value:
            record = _attempt_record(raw)
            if record is None:
                return None
            records.append(record)
        return records

    @field_validator("stream_truncated", "stream_cancelled", mode="before")
    @classmethod
    def _stream_flag(cls, value: Any) -> bool | None:
        # JSON booleans only: "false" or 0 must not be read as a flag.
        return value if isinstance(value, bool) else None

    @field_validator("requested_model", "served_model", mode="before")
    @classmethod
    def _model_name(cls, value: Any) -> str | None:
        return _lenient_str(value, MAX_MODEL_NAME_LENGTH)

    @field_validator(
        "cost_source",
        "service_tier",
        "framework",
        "usage_unit_type",
        "api_surface",
        "route_group",
        "sensitivity_level",
        "served_provider",
        "served_jurisdiction",
        mode="before",
    )
    @classmethod
    def _label(cls, value: Any) -> str | None:
        return _lenient_str(value, MAX_LABEL_LENGTH)

    @field_validator("route_reason", mode="before")
    @classmethod
    def _reason(cls, value: Any) -> str | None:
        return _lenient_str(value, MAX_REASON_LENGTH)

    @field_validator("policy_version", "baseline_id", mode="before")
    @classmethod
    def _identifier(cls, value: Any) -> str | None:
        return _lenient_str(value, MAX_IDENTIFIER_LENGTH)

    @field_validator("route_mode", mode="before")
    @classmethod
    def _route_mode(cls, value: Any) -> str | None:
        return _lenient_choice(value, ROUTE_MODES)

    @field_validator("sensitivity_source", mode="before")
    @classmethod
    def _sensitivity_source(cls, value: Any) -> str | None:
        return _lenient_choice(value, SENSITIVITY_SOURCES)

    @field_validator("fallback_chain", mode="before")
    @classmethod
    def _fallback_chain(cls, value: Any) -> list[str] | None:
        # Error classes only (rate_limited, server_error, ...): anything
        # that is not a short snake_case token, such as an error message,
        # drops the whole field so content can never be stored here.
        if not isinstance(value, list) or len(value) > MAX_FALLBACK_CHAIN:
            return None
        if not all(isinstance(v, str) and ERROR_CLASS_RE.fullmatch(v) for v in value):
            return None
        return value

    @field_validator("budget_state", mode="before")
    @classmethod
    def _budget_state(cls, value: Any) -> str | None:
        return _lenient_choice(value, BUDGET_STATES)

    @field_validator("budget_action", mode="before")
    @classmethod
    def _budget_action(cls, value: Any) -> str | None:
        return _lenient_choice(value, BUDGET_ACTIONS)

    @field_validator("budget_id", mode="before")
    @classmethod
    def _budget_id(cls, value: Any) -> str | None:
        value = _lenient_str(value, MAX_IDENTIFIER_LENGTH)
        return value if value is not None and BUDGET_ID_RE.fullmatch(value) else None

    @field_validator("cache_status", mode="before")
    @classmethod
    def _cache_status(cls, value: Any) -> str | None:
        return _lenient_choice(value, CACHE_STATUSES)

    @field_validator("route_learner_mode", mode="before")
    @classmethod
    def _learner_mode(cls, value: Any) -> str | None:
        return _lenient_choice(value, LEARNER_MODES)

    @field_validator("route_learner_version", mode="before")
    @classmethod
    def _learner_version(cls, value: Any) -> str | None:
        value = _lenient_str(value, MAX_IDENTIFIER_LENGTH)
        return value if value is not None and LEARNER_VERSION_RE.fullmatch(value) else None

    @field_validator("route_propensity", mode="before")
    @classmethod
    def _propensity(cls, value: Any) -> float | None:
        # A probability: (0, 1]. 0 would mean the served model could not
        # have been picked, so it is as invalid as a value above 1.
        value = _lenient_amount(value, 1.0)
        return value if value is not None and round(value, 6) > 0 else None

    @field_validator("route_learner_frozen", mode="before")
    @classmethod
    def _learner_frozen(cls, value: Any) -> bool | None:
        # JSON booleans only: "false" or 0 must not be read as a flag.
        return value if isinstance(value, bool) else None

    @field_validator("route_est_cost", "counterfactual_cost", "cache_saved_cost", mode="before")
    @classmethod
    def _route_cost(cls, value: Any) -> float | None:
        return _lenient_amount(value, MAX_COST)

    @field_validator("modality", mode="before")
    @classmethod
    def _modality(cls, value: Any) -> str | None:
        value = _lenient_str(value, MAX_LABEL_LENGTH)
        if value is None:
            return None
        value = value.lower()
        # An unknown value would register the asset under a modality nothing
        # filters on; it is dropped (counted as modality:invalid) instead.
        return value if value in MODALITIES else None

    @field_validator(
        "tokens_in",
        "tokens_out",
        "latency_ms",
        "tokens_cached",
        "tokens_cache_write",
        "tokens_cache_write_1h",
        "tokens_reasoning",
        "est_tokens_saved",
        mode="before",
    )
    @classmethod
    def _token_count(cls, value: Any) -> int | None:
        return _lenient_count(value)

    @field_validator("usage_units", mode="before")
    @classmethod
    def _usage_units(cls, value: Any) -> float | None:
        return _lenient_amount(value, MAX_USAGE_UNITS)

    @field_validator("cost", mode="before")
    @classmethod
    def _cost(cls, value: Any) -> float | None:
        return _lenient_amount(value, MAX_COST)


# Limits for the lenient fields (extended usage, cost and the core counts).
# Longer or out-of-range values are dropped (stored as null), not truncated:
# half a model name is wrong data.
MAX_MODEL_NAME_LENGTH = 256
MAX_SERVICE_NAME_LENGTH = 256  # the event's calling_service (#347)
MAX_LABEL_LENGTH = 64
MAX_TOKEN_COUNT = 2**31 - 1  # Integer column
MAX_USAGE_UNITS = 1e12  # fits Numeric(20, 6)
MAX_COST = 9999.999999  # largest Numeric(10, 6) value
FINGERPRINT_RE = re.compile(r"[0-9a-f]{16}")
# The SDK and gateway send a UUID; any short token is accepted (the gateway
# takes a caller's X-PS-Call-Id of this shape).
CALL_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
MAX_REASON_LENGTH = 256
# user_id: an opaque identifier (the gateway's 32-hex OIDC subject hash, a
# raw `sub` claim, or the SDK's ps_metadata.user_id).
MAX_USER_ID_LENGTH = 256
# The gateway's policy group names; as collector/mcp_usage.py USER_GROUP_RE.
USER_GROUP_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
HTTP_STATUS_MIN, HTTP_STATUS_MAX = 100, 599
MAX_IDENTIFIER_LENGTH = 128
# An attempt record's provider and model (#334): identifier characters only,
# no spaces, so the attempts JSON cannot carry free text such as a message.
ATTEMPT_PROVIDER_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
ATTEMPT_MODEL_RE = re.compile(r"[A-Za-z0-9_.:/@+=-]{1,256}")
# The gateway keeps at most MAX_ATTEMPT_RECORDS (32) attempts per request.
MAX_FALLBACK_CHAIN = 32
ERROR_CLASS_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
# Outcome signal classes (#44), as packages/sdk/prompt_shields/outcome_signals.py
# SIGNAL_CLASSES defines them (a test keeps the two in step). Version 1.
OUTCOME_SIGNAL_CLASSES = (
    "abandoned",
    "fallback",
    "max_tokens",
    "refusal",
    "regenerate",
    "retry",
    "schema_invalid",
    "stream_error",
    "tool_call_invalid",
)
MAX_OUTCOME_SIGNALS = 32
MAX_OUTCOME_SIGNALS_VERSION = 1000  # SmallInteger column, generous headroom
# PRD 0001 §7.12. Matched case-insensitively, stored lower-case.
ROUTE_MODES = frozenset({"gateway", "local", "none"})
SENSITIVITY_SOURCES = frozenset({"declared", "pii_escalated", "default"})
# schemas/budget/README.md (budget.v1). Matched case-insensitively, stored
# lower-case. budget_action "blocked" is accepted for an emitter that reports
# refused calls; the SDK and gateway send no event for those today.
BUDGET_STATES = frozenset({"ok", "warn", "unknown", "exceeded"})
BUDGET_ACTIONS = frozenset({"none", "downgraded", "blocked"})
# budget.v1 ids are tokens: ^[a-z0-9][a-z0-9._-]*$
BUDGET_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
# The gateway's cache status header values (exact and semantic cache).
# Matched case-insensitively, stored lower-case ("semantic hit").
CACHE_STATUSES = frozenset(
    {"hit", "miss", "semantic hit", "semantic miss", "semantic bypass", "refresh", "disabled"}
)
CACHE_HIT_STATUSES = ("hit", "semantic hit")
# Routing learner auto mode (#34): prompt_shields.learner_auto.ChoiceMode.
# Matched case-insensitively, stored lower-case.
LEARNER_MODES = frozenset({"policy", "exploit", "explore"})
LEARNER_EXPLORE = "explore"
# learner_version of a learner snapshot (collector/learner.py VERSION_RE).
LEARNER_VERSION_RE = re.compile(r"lv1-[0-9a-f]{16}")
# cost_source of a response served from the gateway cache (psPricing.ts).
COST_SOURCE_CACHE = "cache"

# The modalities telemetry reports, as the gateway's modalityOf() sets them
# from the endpoint: chat, completions, messages, responses, embeddings and
# rerank are text; /v1/images is image; /v1/audio is audio; /v1/ocr is
# document. Matched case-insensitively, stored lower-case.
MODALITIES = frozenset({"text", "image", "audio", "document"})

# PRD 0001 §7.12 routing metadata (#25), stored as sent once validated.
ROUTE_FIELDS = (
    "api_surface",
    "route_mode",
    "route_group",
    "route_reason",
    "route_est_cost",
    "sensitivity_level",
    "sensitivity_source",
    "served_provider",
    "served_jurisdiction",
    "policy_version",
    "fallback_chain",
    "counterfactual_cost",
    "baseline_id",
)

# Budget and cache fields (#28, #29, #33), stored as sent once validated.
BUDGET_CACHE_FIELDS = (
    "budget_state",
    "budget_id",
    "budget_action",
    "cache_status",
    "cache_saved_cost",
    "est_tokens_saved",
)

# Routing learner auto-mode fields (#34), stored as sent once validated.
LEARNER_FIELDS = (
    "route_learner_mode",
    "route_propensity",
    "route_learner_version",
    "route_learner_frozen",
)

# Gateway identity and trace fields (#334). user_id is stored as
# user_id_hash (and user_id_raw if the tenant opted in; see
# collector/user_identity.py), the rest as sent once validated.
IDENTITY_TRACE_FIELDS = (
    "user_id",
    "user_group",
    "trace_id",
    "retry_count",
    "attempts",
    "stream_truncated",
    "stream_cancelled",
)
STORED_AS_SENT_IDENTITY_FIELDS = tuple(f for f in IDENTITY_TRACE_FIELDS if f != "user_id")

# Cost attribution fields (#347): team and project are resolved to ids at
# ingest, tags stored once validated.
ATTRIBUTION_FIELDS = ("team", "project", "tags")

# Other names emitters use for a field: the gateway sends its effective
# sensitivity as route_sensitivity_level / route_sensitivity_source. The
# canonical name wins when an event carries both.
FIELD_ALIASES = {
    "route_sensitivity_level": "sensitivity_level",
    "route_sensitivity_source": "sensitivity_source",
}


def canonical_event(data: Any) -> Any:
    """The raw event with aliased keys renamed to their canonical field."""
    if not isinstance(data, dict) or not any(a in data for a in FIELD_ALIASES):
        return data
    out = {k: v for k, v in data.items() if k not in FIELD_ALIASES}
    for alias, name in FIELD_ALIASES.items():
        if alias in data and out.get(name) is None:
            out[name] = data[alias]
    return out


# Lenient fields whose dropped values are reported in ignored_fields as
# "<field>:invalid", so a client sending bad values shows up in
# /telemetry/loss instead of losing the data silently.
LENIENT_FIELDS = (
    "tokens_in",
    "tokens_out",
    "cost",
    "latency_ms",
    "requested_model",
    "served_model",
    "tokens_cached",
    "tokens_cache_write",
    "tokens_cache_write_1h",
    "tokens_reasoning",
    "cost_source",
    "service_tier",
    "framework",
    "modality",
    "usage_units",
    "usage_unit_type",
    "api_key_fingerprint",
    "call_id",
    "outcome_signals",
    "outcome_signals_version",
) + (
    ROUTE_FIELDS
    + BUDGET_CACHE_FIELDS
    + LEARNER_FIELDS
    + IDENTITY_TRACE_FIELDS
    + ATTRIBUTION_FIELDS
)


def _lenient_str(value: Any, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > max_length:
        return None
    return value


def _lenient_choice(value: Any, choices: frozenset[str]) -> str | None:
    value = _lenient_str(value, MAX_LABEL_LENGTH)
    if value is None:
        return None
    value = value.lower()
    return value if value in choices else None


def _lenient_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or value < 0 or value > MAX_TOKEN_COUNT:
        return None
    return value


def _attempt_record(raw: Any) -> dict | None:
    """One validated attempt record, or None if it is not one."""
    if not isinstance(raw, dict):
        return None
    attempt = _lenient_count(raw.get("attempt"))
    latency = _lenient_count(raw.get("latency_ms"))
    if not attempt or latency is None:
        return None
    record: dict = {"attempt": attempt, "latency_ms": latency}
    if raw.get("status") is not None:
        status = _lenient_count(raw["status"])
        if status is None or not HTTP_STATUS_MIN <= status <= HTTP_STATUS_MAX:
            return None
        record["status"] = status
    for key, pattern in (("provider", ATTEMPT_PROVIDER_RE), ("model", ATTEMPT_MODEL_RE)):
        if raw.get(key) is not None:
            text = raw[key]
            if not isinstance(text, str) or not pattern.fullmatch(text):
                return None
            record[key] = text
    if raw.get("error_class") is not None:
        error_class = raw["error_class"]
        if not isinstance(error_class, str) or not ERROR_CLASS_RE.fullmatch(error_class):
            return None
        record["error_class"] = error_class
    return record


def _lenient_amount(value: Any, maximum: float) -> float | None:
    """A finite number in [0, maximum] once rounded to the column's 6 decimal
    places (9999.9999999 rounds to 10000 and overflows Numeric(10, 6))."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # Check ints before float(): a huge JSON integer overflows float()
    # with OverflowError, which pydantic does not catch (500, batch lost).
    if isinstance(value, int) and not 0 <= value <= maximum:
        return None
    value = float(value)
    if not math.isfinite(value) or value < 0 or round(value, 6) > maximum:
        return None
    return value


# Fields dropped when they hold a value the database cannot store. The
# vendor is required, so an event whose vendor cannot be stored is skipped
# instead (unstorable_event). The prompt text is only hashed, never stored.
SANITISED_FIELDS = tuple(
    name for name in IngestEvent.model_fields if name not in ("vendor", "prompt_text")
)
# Fields whose dropped values are reported as "<field>:invalid": the lenient
# ones plus every sanitised one that has no default to fall back to.
REPORTED_FIELDS = tuple(
    dict.fromkeys(
        LENIENT_FIELDS
        + tuple(n for n in SANITISED_FIELDS if IngestEvent.model_fields[n].default is None)
    )
)
UNSTORABLE = "unstorable"


def unstorable_event(raw: Any) -> bool:
    """An event that cannot be stored at all: its vendor is a string the
    database cannot hold."""
    if not isinstance(raw, dict):
        return False
    vendor = raw.get("vendor")
    return isinstance(vendor, str) and storable_text(vendor) is None


def storable_events(raw_events: Any) -> list:
    """The raw events that are not skipped by ``unstorable_event``."""
    if not isinstance(raw_events, list):
        return []
    return [e for e in raw_events if not unstorable_event(e)]


class IngestRequest(BaseModel):
    events: list[IngestEvent]
    # SDK delivery accounting (#87); parsed leniently by loss_accounting.
    metadata: dict | None = None
    # Events skipped by unstorable_event; never read from the body.
    _unstorable: int = PrivateAttr(default=0)

    @model_validator(mode="wrap")
    @classmethod
    def _skip_unstorable_events(cls, data: Any, handler):
        skipped = 0
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            kept = storable_events(data["events"])
            skipped = len(data["events"]) - len(kept)
            if skipped:
                data = {**data, "events": kept}
        model = handler(data)
        model._unstorable = skipped
        return model


async def _enforce_rate_limit(
    limiter: RateLimiter, tenant_id, response: Response, limit: int
) -> None:
    """Per-tenant request limit on ingest. Sets X-RateLimit-* headers and
    raises 429 with Retry-After once the tenant exceeds the window. ``limit``
    is the tenant's limit (collector/tenant_limits.py); 0 or less turns it
    off."""
    if limit <= 0:
        return
    result = await limiter.acheck(tenant_id, limit)
    headers = {
        "X-RateLimit-Limit": str(result.limit),
        "X-RateLimit-Remaining": str(result.remaining),
        "X-RateLimit-Reset": str(result.reset_at),
    }
    if not result.allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "type": "about:blank",
                "title": "Too Many Requests",
                "status": 429,
                "detail": f"Rate limit exceeded. Retry after {result.retry_after} seconds.",
            },
            headers={**headers, "Retry-After": str(result.retry_after)},
        )
    response.headers.update(headers)


@router.post("/ingest/events")
async def ingest_events(
    body: IngestRequest,
    request: Request,
    response: Response,
    limiter: Annotated[RateLimiter, Depends(get_ingest_rate_limiter)],
):
    tenant_id = await resolve_tenant(request, tenant_keys.INGEST_WRITE)
    db = request.state.db
    meta = loss_accounting.parse_metadata(body.metadata)
    try:
        await _enforce_rate_limit(
            limiter, tenant_id, response, await tenant_limits.limit_for(db, tenant_id, "events")
        )
    except HTTPException as exc:
        if exc.status_code == 429:
            metrics.record_ingest("events", "rate_limited", len(body.events))
            await loss_accounting.record_rejected(
                db, tenant_id, meta, len(body.events), "rate_limited"
            )
        raise

    try:
        assets_created = await _store_events(db, tenant_id, body.events)
        if body.events:
            raw_events = [
                canonical_event(e)
                for e in storable_events((await request.json()).get("events"))
            ]
            ignored = loss_accounting.ignored_fields(
                raw_events, set(IngestEvent.model_fields)
            )
            for key, n in loss_accounting.invalid_fields(
                raw_events, body.events, REPORTED_FIELDS
            ).items():
                ignored[key] = ignored.get(key, 0) + n
            await loss_accounting.record_received(
                db, tenant_id, meta, len(body.events), ignored
            )
        await db.commit()
    except Exception:
        await db.rollback()
        metrics.record_ingest("events", "write_failed", len(body.events))
        await loss_accounting.record_rejected(
            db, tenant_id, meta, len(body.events), "write_failed"
        )
        raise

    metrics.record_ingest("events", "accepted", len(body.events))
    metrics.record_assets_created("events", assets_created)
    if body._unstorable:
        # Answered 200, so the client will not resend them.
        metrics.record_ingest("events", "rejected", body._unstorable)
        await loss_accounting.record_rejected(
            db, tenant_id, meta, body._unstorable, UNSTORABLE
        )
    return {
        "ingested": len(body.events),
        "assets_created": assets_created,
        "rejected": body._unstorable,
    }


async def _store_events(db, tenant_id, events: list[IngestEvent]) -> int:
    assets_created = 0
    changes = AssetChanges()
    # Only a batch that carries a user_id reads (and on first use creates)
    # the tenant's salt.
    user_ids = (
        await user_identity.policy_for(db, tenant_id)
        if any(e.user_id is not None for e in events)
        else None
    )
    attribution = (
        await cost_attribution.resolve_refs(
            db, tenant_id, ((e.team, e.project) for e in events)
        )
        if any(e.team or e.project for e in events)
        else {}
    )
    await lock_asset_keys(
        db,
        tenant_id,
        (
            AssetKey(e.vendor, e.model, e.use_case_name, e.business_unit, e.environment)
            for e in events
        ),
    )
    for event in events:
        asset, is_new = await find_or_create_asset(
            db=db,
            tenant_id=tenant_id,
            vendor=event.vendor,
            model=event.model,
            use_case_name=event.use_case_name,
            business_unit=event.business_unit,
            calling_service=event.calling_service,
            source=event.source,
            owner_email=event.owner_email,
            environment=event.environment,
            data_classification=event.data_classification,
            modality=event.modality,
        )
        changes.record(asset, is_new)
        if is_new:
            assets_created += 1
            await db.flush()

        # Generate embedding for new assets (async, fail-open)
        if is_new and asset.embedding is None:
            asset_text = build_asset_text(
                vendor=event.vendor,
                model=event.model,
                use_case_name=event.use_case_name,
                business_unit=event.business_unit,
                data_classification=event.data_classification,
            )
            embedding = await get_embedding(asset_text)
            if embedding:
                asset.embedding = embedding

        prompt_hash = None
        if event.prompt_text:
            # surrogatepass: a lone surrogate (never stored) must not fail
            # the batch's write; valid text hashes as before.
            prompt_hash = hashlib.sha256(
                event.prompt_text.encode("utf-8", "surrogatepass")
            ).hexdigest()

        attributed = attribution.get(
            (event.team, event.project), cost_attribution.Attribution()
        )
        usage_event = AIUsageEvent(
            tenant_id=tenant_id,
            asset_id=asset.id,
            tokens_in=event.tokens_in,
            tokens_out=event.tokens_out,
            cost=event.cost,
            latency_ms=event.latency_ms,
            source=event.source,
            session_id=event.session_id,
            tool_calls_used=event.tool_calls_used,
            prompt_hash=prompt_hash,
            requested_model=event.requested_model,
            served_model=event.served_model,
            tokens_cached=event.tokens_cached,
            tokens_cache_write=event.tokens_cache_write,
            tokens_cache_write_1h=event.tokens_cache_write_1h,
            tokens_reasoning=event.tokens_reasoning,
            cost_source=event.cost_source,
            service_tier=event.service_tier,
            framework=event.framework,
            modality=event.modality,
            usage_units=event.usage_units,
            usage_unit_type=event.usage_unit_type,
            api_key_fingerprint=event.api_key_fingerprint,
            call_id=event.call_id,
            outcome_signals=event.outcome_signals,
            outcome_signals_version=event.outcome_signals_version,
            **{
                name: getattr(event, name)
                for name in ROUTE_FIELDS
                + BUDGET_CACHE_FIELDS
                + LEARNER_FIELDS
                + STORED_AS_SENT_IDENTITY_FIELDS
            },
            **(user_ids.columns(event.user_id) if user_ids else {}),
            team_id=attributed.team_id,
            project_id=attributed.project_id,
            # A rollup and showback dimension: bounded like a model name
            # (longer is dropped, not truncated; the asset keeps it as sent).
            calling_service=_lenient_str(event.calling_service, MAX_SERVICE_NAME_LENGTH),
            tags=event.tags,
        )
        db.add(usage_event)
        await record_event_flows(
            db,
            tenant_id,
            asset,
            usage_event,
            data_sources=event.data_sources,
            output_destination=event.output_destination,
            pii_types=event.detected_pii_types,
            region=event.region,
            data_classification=event.data_classification,
            calling_service=event.calling_service,
        )

    await changes.enqueue(db, tenant_id)
    return assets_created
