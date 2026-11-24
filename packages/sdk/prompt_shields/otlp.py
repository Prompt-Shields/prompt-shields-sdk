"""OTLP/HTTP trace export from the SDK (#386).

Every call the SDK records (``ShieldsClient`` / ``AsyncShieldsClient``,
``ShieldsRouter``, ``instrument()`` and the framework integrations) can also
be sent as one OpenTelemetry GenAI span to any OTLP/HTTP backend, such as
Langfuse, Grafana Tempo, Honeycomb or an OpenTelemetry Collector, next to the
Prompt Shields collector (dual export: the collector events are unchanged).

Off unless configured::

    import prompt_shields
    prompt_shields.configure_otlp()        # from OTEL_EXPORTER_OTLP_* variables
    prompt_shields.configure_langfuse()    # from LANGFUSE_* variables

or with ``PS_OTLP_EXPORT=true`` (standard ``OTEL_EXPORTER_OTLP_*`` variables)
and/or ``PS_LANGFUSE_EXPORT=true`` (``LANGFUSE_*`` variables) in the
environment. Each call adds an exporter, so several backends can receive the
spans at once.

Standard variables read (the ``TRACES_`` form wins over the generic one):
``OTEL_EXPORTER_OTLP_[TRACES_]ENDPOINT`` (the generic one gets ``/v1/traces``
appended), ``..._HEADERS`` (``k=v,k2=v2``, values percent-decoded),
``..._PROTOCOL`` (``http/protobuf``, the default, or ``http/json``; gRPC is
not supported), ``..._TIMEOUT`` (ms), ``..._COMPRESSION`` (``gzip`` or
``none``), ``OTEL_BSP_MAX_QUEUE_SIZE`` (2048), ``OTEL_BSP_MAX_EXPORT_BATCH_SIZE``
(512), ``OTEL_BSP_SCHEDULE_DELAY`` (ms, 5000), ``OTEL_SERVICE_NAME``,
``OTEL_RESOURCE_ATTRIBUTES`` and ``OTEL_SDK_DISABLED``.

Delivery: ``export_event`` only builds the span and appends it to a bounded
queue (no I/O on the caller's thread); a daemon thread sends batches.
Fail-open: an unreachable or failing backend never raises into, or slows, the
LLM call. Retryable failures (connection errors, 408, 429, 5xx) keep the
batch queued and back off (1 s doubling to 30 s, or ``Retry-After``); the
queue never holds more than ``max_queue_size`` spans, the oldest being dropped
and counted (``stats()``) when it is full. Other 4xx answers drop the batch.

Privacy: no prompt or completion text leaves the process unless content
capture is on (``capture_content=True``, ``PS_OTLP_CAPTURE_CONTENT=true`` or
the standard ``OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true``).
When on, the input messages are sent as ``gen_ai.input.messages`` after the
same PII redaction the gateway's ``/log/stream`` applies (each email, card,
phone, SSN, IP address, IBAN and national id replaced by ``[EMAIL]``,
``[CREDIT_CARD]``, ...). The user id is never sent: only a SHA-256 hash
(HMAC with ``PS_OTLP_USER_HASH_SALT`` when set) as ``ps.user.hash`` /
``user.id``.

Spans follow the attribute names of the SDK spans in ``prompt_shields.tracing``
(``chat {model}``, CLIENT, ``gen_ai.*``, ``gen_ai.conversation.id``,
``ps.user.hash``) and join the caller's active OpenTelemetry span when
``opentelemetry-api`` is installed. Cost (``gen_ai.usage.cost``, USD) is the
event's cost from the shared price table (``prompt_shields.pricing``).

One span per call: when the call already has a recording SDK span (the app's
own ``TracerProvider`` exports ``prompt_shields.tracing`` spans), an exporter
puts its attributes, never content, on that span instead of exporting a
second one (``sdk_spans="attach"``, the default; ``PS_OTLP_SDK_SPANS=export``
sends its own span anyway).
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import struct
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import unquote

import httpx

from prompt_shields.pii import find_pii_spans
from prompt_shields.telemetry import (
    parse_retry_after,
    register_exit_flush,
    unregister_exit_flush,
)

logger = logging.getLogger("prompt_shields.otlp")

PROTOCOL_PROTOBUF = "http/protobuf"
PROTOCOL_JSON = "http/json"
PROTOCOLS = (PROTOCOL_PROTOBUF, PROTOCOL_JSON)

# What an exporter does for a call that already has a recording SDK span
# (prompt_shields.tracing, #385): put its attributes on that span, which the
# app's TracerProvider exports (default; one span per call), or export its own
# span as well (two spans per call if both go to the same backend).
SDK_SPANS_ATTACH = "attach"
SDK_SPANS_EXPORT = "export"
SDK_SPANS_MODES = (SDK_SPANS_ATTACH, SDK_SPANS_EXPORT)

# Attributes never put on an SDK span: #385 keeps prompt and completion text
# off them, whatever this exporter's content capture says.
_CONTENT_ATTRIBUTES = frozenset({"gen_ai.input.messages", "langfuse.observation.input"})

PROFILE_DEFAULT = "default"
PROFILE_LANGFUSE = "langfuse"

LANGFUSE_DEFAULT_HOST = "https://cloud.langfuse.com"
LANGFUSE_OTEL_PATH = "/api/public/otel/v1/traces"

SCOPE_NAME = "prompt_shields"
SCHEMA_URL = "https://opentelemetry.io/schemas/1.40.0"

# Drop reasons counted by OTLPSpanExporter.stats().
DROP_REASONS = ("queue_full", "rejected", "build_failed", "shutdown")

MAX_BACKOFF_SECONDS = 30.0
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

# SDK vendor names -> gen_ai.provider.name (as prompt_shields.tracing).
PROVIDER_NAMES = {
    "openai": "openai",
    "anthropic": "anthropic",
    "azure_openai": "azure.ai.openai",
    "bedrock": "aws.bedrock",
    "google": "gcp.gemini",
    "vertex": "gcp.vertex_ai",
    "cohere": "cohere",
    "mistral": "mistral_ai",
    "groq": "groq",
    "deepseek": "deepseek",
}

# The gateway's /log/stream placeholders (gateway/src/middlewares/router/pii.ts).
PII_PLACEHOLDERS = {
    "email": "[EMAIL]",
    "credit_card": "[CREDIT_CARD]",
    "phone": "[PHONE]",
    "ssn": "[SSN]",
    "ip_address": "[IP_ADDRESS]",
    "iban": "[IBAN]",
    "national_id": "[NATIONAL_ID]",
}

_TRUE = ("1", "true", "yes", "on")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE


def redact_pii(text: str) -> str:
    """``text`` with every PII entity replaced by its category placeholder."""
    spans = find_pii_spans(text)
    if not spans:
        return text
    out: list[str] = []
    pos = 0
    for start, end, category in spans:
        out.append(text[pos:start])
        out.append(PII_PLACEHOLDERS.get(category, f"[{category.upper()}]"))
        pos = end
    out.append(text[pos:])
    return "".join(out)


def parse_headers(raw: str | None) -> dict[str, str]:
    """``k1=v1,k2=v2`` as in ``OTEL_EXPORTER_OTLP_HEADERS``."""
    out: dict[str, str] = {}
    for pair in (raw or "").split(","):
        key, sep, value = pair.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        out[key] = unquote(value.strip())
    return out


def _parse_resource_attributes(raw: str | None) -> dict[str, str]:
    return parse_headers(raw)


def _pos_int(value: str | None, default: int) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _pos_ms(value: str | None, default_s: float) -> float:
    try:
        n = float(str(value).strip())
    except (TypeError, ValueError):
        return default_s
    return n / 1000.0 if n > 0 and not math.isnan(n) else default_s


@dataclass(frozen=True)
class OTLPConfig:
    """Where and how one exporter sends spans."""

    endpoint: str
    headers: Mapping[str, str] = field(default_factory=dict)
    protocol: str = PROTOCOL_PROTOBUF
    timeout: float = 10.0
    compression: str | None = None
    max_queue_size: int = 2048
    max_export_batch_size: int = 512
    schedule_delay: float = 5.0
    service_name: str = "unknown_service"
    resource_attributes: Mapping[str, str] = field(default_factory=dict)
    capture_content: bool = False
    profile: str = PROFILE_DEFAULT
    user_hash_salt: str | None = None
    sdk_spans: str = SDK_SPANS_ATTACH

    def __post_init__(self) -> None:
        if self.sdk_spans not in SDK_SPANS_MODES:
            raise ValueError(
                f"sdk_spans must be one of {SDK_SPANS_MODES}, not {self.sdk_spans!r}"
            )
        if self.protocol not in PROTOCOLS:
            raise ValueError(
                f"OTLP protocol must be one of {PROTOCOLS}, not {self.protocol!r}"
            )
        if self.compression not in (None, "gzip"):
            raise ValueError("OTLP compression must be 'gzip' or None")
        if not self.endpoint:
            raise ValueError("OTLP endpoint is empty")
        if self.max_export_batch_size > self.max_queue_size:
            object.__setattr__(self, "max_export_batch_size", self.max_queue_size)

    def __repr__(self) -> str:  # headers carry credentials: never print them
        return (f"OTLPConfig(endpoint={self.endpoint!r}, protocol={self.protocol!r}, "
                f"profile={self.profile!r}, headers=<{len(self.headers)} hidden>)")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **overrides: Any) -> OTLPConfig:
        """Settings from the standard ``OTEL_*`` variables; keyword
        arguments win over the environment."""
        env = os.environ if env is None else env

        def get(name: str) -> str | None:
            value = env.get(f"OTEL_EXPORTER_OTLP_TRACES_{name}")
            if value is None or not value.strip():
                value = env.get(f"OTEL_EXPORTER_OTLP_{name}")
            return value.strip() if value and value.strip() else None

        endpoint = (env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or "").strip()
        if not endpoint:
            base = (env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()
            if base:
                endpoint = base.rstrip("/") + "/v1/traces"
        headers = parse_headers(env.get("OTEL_EXPORTER_OTLP_HEADERS"))
        headers.update(parse_headers(env.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS")))
        protocol = (get("PROTOCOL") or PROTOCOL_PROTOBUF).lower()
        compression = (get("COMPRESSION") or "none").lower()
        resource = _parse_resource_attributes(env.get("OTEL_RESOURCE_ATTRIBUTES"))
        service = (env.get("OTEL_SERVICE_NAME") or "").strip() or resource.get(
            "service.name") or "unknown_service"
        values: dict[str, Any] = {
            "endpoint": endpoint,
            "headers": headers,
            "protocol": protocol,
            "timeout": _pos_ms(get("TIMEOUT"), 10.0),
            "compression": None if compression == "none" else compression,
            "max_queue_size": _pos_int(env.get("OTEL_BSP_MAX_QUEUE_SIZE"), 2048),
            "max_export_batch_size": _pos_int(
                env.get("OTEL_BSP_MAX_EXPORT_BATCH_SIZE"), 512),
            "schedule_delay": _pos_ms(env.get("OTEL_BSP_SCHEDULE_DELAY"), 5.0),
            "service_name": service,
            "resource_attributes": resource,
            "capture_content": _truthy(env.get("PS_OTLP_CAPTURE_CONTENT"))
            or _truthy(env.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT")),
            "user_hash_salt": env.get("PS_OTLP_USER_HASH_SALT") or None,
            "sdk_spans": (env.get("PS_OTLP_SDK_SPANS") or "").strip().lower()
            or SDK_SPANS_ATTACH,
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)


def langfuse_config(
    public_key: str | None = None,
    secret_key: str | None = None,
    host: str | None = None,
    env: Mapping[str, str] | None = None,
    **overrides: Any,
) -> OTLPConfig:
    """OTLP settings for Langfuse's OpenTelemetry endpoint.

    ``{host}/api/public/otel/v1/traces`` with Basic auth from the project's
    public and secret keys (``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY``,
    ``LANGFUSE_HOST`` or ``LANGFUSE_BASE_URL``, default Langfuse Cloud EU).
    Spans carry the ``langfuse.*`` attributes Langfuse maps (observation
    type, model, usage, cost, session, user, environment) next to the GenAI
    ones. Batching, protocol and content capture come from the ``OTEL_*`` /
    ``PS_OTLP_*`` variables as in ``OTLPConfig.from_env``; the ``OTEL_*``
    endpoint and headers are not used.
    """
    env = os.environ if env is None else env
    public_key = public_key or env.get("LANGFUSE_PUBLIC_KEY")
    secret_key = secret_key or env.get("LANGFUSE_SECRET_KEY")
    if not public_key or not secret_key:
        raise ValueError(
            "Langfuse export needs public_key and secret_key "
            "(or LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY)"
        )
    host = (host or env.get("LANGFUSE_HOST") or env.get("LANGFUSE_BASE_URL")
            or LANGFUSE_DEFAULT_HOST).strip().rstrip("/")
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode("ascii")
    stripped = {k: v for k, v in env.items()
                if not k.startswith(("OTEL_EXPORTER_OTLP_ENDPOINT",
                                     "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                                     "OTEL_EXPORTER_OTLP_HEADERS",
                                     "OTEL_EXPORTER_OTLP_TRACES_HEADERS"))}
    stripped["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = host + LANGFUSE_OTEL_PATH
    overrides.setdefault("profile", PROFILE_LANGFUSE)
    headers = {"Authorization": f"Basic {token}"}
    headers.update(overrides.pop("headers", None) or {})
    return OTLPConfig.from_env(stripped, headers=headers, **overrides)


# --- span building ---------------------------------------------------------------


def user_hash(user_id: str, salt: str | None = None) -> str:
    """Hex SHA-256 of the user id (HMAC-SHA-256 keyed by ``salt`` when set)."""
    data = str(user_id).encode("utf-8")
    if salt:
        return hmac.new(salt.encode("utf-8"), data, hashlib.sha256).hexdigest()
    return hashlib.sha256(data).hexdigest()


def _active_parent() -> tuple[str, str] | None:
    """(trace id, span id) of the caller's active OpenTelemetry span."""
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    try:
        ctx = trace.get_current_span().get_span_context()
        if ctx is not None and ctx.is_valid:
            return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"
    except Exception:  # noqa: BLE001 - a broken tracer never breaks a call
        return None
    return None


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _content_messages(messages: Iterable[Any]) -> list[dict]:
    """Input messages in the semconv 1.40 shape, text redacted."""
    out: list[dict] = []
    for m in messages or ():
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        parts: list[dict] = []
        if isinstance(content, str):
            parts.append({"type": "text", "content": redact_pii(content)})
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append({"type": "text", "content": redact_pii(part["text"])})
                elif isinstance(part, dict):
                    parts.append({"type": str(part.get("type", "unknown"))})
        out.append({"role": str(m.get("role", "user")), "parts": parts})
    return out


def span_attributes(event: Mapping[str, Any], messages: Iterable[Any],
                    config: OTLPConfig) -> dict[str, Any]:
    """The span's attributes for one SDK telemetry event."""
    model = event.get("requested_model") or event.get("model")
    served = event.get("served_model")
    vendor = event.get("vendor")
    tokens_in = _number(event.get("tokens_in"))
    tokens_out = _number(event.get("tokens_out"))
    cost = _number(event.get("cost"))
    session = event.get("session_id")
    session = session if isinstance(session, str) and session else None
    uid = event.get("user_id")
    uhash = user_hash(uid, config.user_hash_salt) if isinstance(uid, str) and uid else None
    attrs: dict[str, Any] = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": PROVIDER_NAMES.get(str(vendor or "").lower(),
                                                   vendor or "unknown"),
        "gen_ai.request.model": model if isinstance(model, str) else None,
        "gen_ai.response.model": served if isinstance(served, str) else None,
        "gen_ai.usage.input_tokens": tokens_in,
        "gen_ai.usage.output_tokens": tokens_out,
        "gen_ai.usage.cache_read.input_tokens": _number(event.get("tokens_cached")),
        "gen_ai.usage.cache_creation.input_tokens": _number(
            event.get("tokens_cache_write")),
        "gen_ai.usage.reasoning.output_tokens": _number(event.get("tokens_reasoning")),
        "gen_ai.usage.cost": cost,
        "gen_ai.conversation.id": session,
        "session.id": session,
        "ps.user.hash": uhash,
        "user.id": uhash,
        "ps.cost.usd": cost,
        "ps.cost_source": event.get("cost_source"),
        "ps.call_id": event.get("call_id"),
        "ps.source": event.get("source"),
        "ps.business_unit": event.get("business_unit"),
        "ps.use_case": event.get("use_case_name"),
        "ps.team": event.get("team"),
        "ps.project": event.get("project"),
        "ps.data_classification": event.get("data_classification"),
        "ps.calling_service": event.get("calling_service"),
        "ps.framework": event.get("framework"),
        "ps.stream": True if event.get("stream") else None,
        "ps.tokens_estimated": True if event.get("tokens_estimated") else None,
        "ps.pii_categories": [c for c in event.get("detected_pii_types") or []
                              if isinstance(c, str)] or None,
    }
    tags = event.get("tags")
    if isinstance(tags, (list, tuple)):
        attrs["ps.tags"] = [t for t in tags if isinstance(t, str)] or None
    input_messages = None
    if config.capture_content:
        input_messages = json.dumps(_content_messages(messages), ensure_ascii=False)
        attrs["gen_ai.input.messages"] = input_messages
    if config.profile == PROFILE_LANGFUSE:
        usage: dict[str, int | float] = {}
        if tokens_in is not None:
            usage["input"] = tokens_in
        if tokens_out is not None:
            usage["output"] = tokens_out
        attrs.update({
            "langfuse.observation.type": "generation",
            "langfuse.observation.model.name": served or model,
            "langfuse.observation.usage_details": json.dumps(usage) if usage else None,
            "langfuse.observation.cost_details": (
                json.dumps({"total": cost}) if cost is not None else None),
            "langfuse.session.id": session,
            "langfuse.user.id": uhash,
            "langfuse.environment": event.get("environment"),
            "langfuse.trace.name": f"chat {model}" if isinstance(model, str) else "chat",
            "langfuse.observation.input": input_messages,
        })
    return {k: v for k, v in attrs.items() if v is not None and v != ""}


def _id(nbytes: int) -> str:
    while True:
        value = secrets.token_hex(nbytes)
        if value.strip("0"):
            return value


def build_span(event: Mapping[str, Any], messages: Iterable[Any], config: OTLPConfig,
               *, end_ns: int | None = None,
               parent: tuple[str, str] | None = None) -> dict:
    """One OTLP JSON span (``chat {model}``, CLIENT) for an SDK event."""
    end_ns = time.time_ns() if end_ns is None else end_ns
    latency = _number(event.get("latency_ms")) or 0
    start_ns = end_ns - max(0, int(latency * 1_000_000))
    parent = parent if parent is not None else _active_parent()
    model = event.get("requested_model") or event.get("model")
    span: dict[str, Any] = {
        "traceId": parent[0] if parent else _id(16),
        "spanId": _id(8),
        "name": f"chat {model}" if isinstance(model, str) and model else "chat",
        "kind": 3,  # SPAN_KIND_CLIENT
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": to_otlp_attributes(span_attributes(event, messages, config)),
        "status": {"code": 0},
    }
    if parent:
        span["parentSpanId"] = parent[1]
    return span


def _any_value(value: Any) -> dict | None:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, (list, tuple)):
        values = [v for v in (_any_value(x) for x in value) if v is not None]
        return {"arrayValue": {"values": values}}
    return None


def to_otlp_attributes(attrs: Mapping[str, Any]) -> list[dict]:
    out = []
    for key, value in attrs.items():
        any_value = _any_value(value)
        if any_value is not None:
            out.append({"key": key, "value": any_value})
    return out


def build_request(spans: Iterable[tuple[dict, str | None]], config: OTLPConfig) -> dict:
    """ExportTraceServiceRequest (OTLP JSON), one resource per environment."""
    from prompt_shields import __version__

    by_env: dict[str, list[dict]] = {}
    for span, environment in spans:
        by_env.setdefault(environment or "", []).append(span)
    resource_spans = []
    for environment, items in by_env.items():
        resource = {"telemetry.sdk.name": "prompt-shields",
                    "telemetry.sdk.language": "python",
                    "telemetry.sdk.version": __version__}
        resource.update(config.resource_attributes)
        resource["service.name"] = config.service_name
        if environment:
            resource["deployment.environment.name"] = environment
        resource_spans.append({
            "resource": {"attributes": to_otlp_attributes(resource)},
            "scopeSpans": [{
                "scope": {"name": SCOPE_NAME, "version": __version__},
                "spans": items,
                "schemaUrl": SCHEMA_URL,
            }],
            "schemaUrl": SCHEMA_URL,
        })
    return {"resourceSpans": resource_spans}


# --- protobuf encoding (opentelemetry-proto trace/v1, no dependency) ----------------


def _varint(n: int) -> bytes:
    n &= 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _key(field_no: int, wire: int) -> bytes:
    return _varint((field_no << 3) | wire)


def _len(field_no: int, payload: bytes) -> bytes:
    return _key(field_no, 2) + _varint(len(payload)) + payload


def _str(field_no: int, value: str) -> bytes:
    return _len(field_no, value.encode("utf-8")) if value else b""


def _fixed64(field_no: int, value: int) -> bytes:
    return _key(field_no, 1) + struct.pack("<Q", value)


def _pb_any(value: Mapping[str, Any]) -> bytes:
    if "stringValue" in value:
        return _len(1, value["stringValue"].encode("utf-8"))
    if "boolValue" in value:
        return _key(2, 0) + _varint(1 if value["boolValue"] else 0)
    if "intValue" in value:
        return _key(3, 0) + _varint(int(value["intValue"]))
    if "doubleValue" in value:
        return _key(4, 1) + struct.pack("<d", float(value["doubleValue"]))
    if "arrayValue" in value:
        inner = b"".join(_len(1, _pb_any(v)) for v in value["arrayValue"]["values"])
        return _len(5, inner)
    return b""


def _pb_attrs(field_no: int, attrs: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(
        _len(field_no, _str(1, a["key"]) + _len(2, _pb_any(a["value"])))
        for a in attrs
    )


def _pb_span(span: Mapping[str, Any]) -> bytes:
    out = _len(1, bytes.fromhex(span["traceId"])) + _len(2, bytes.fromhex(span["spanId"]))
    if span.get("parentSpanId"):
        out += _len(4, bytes.fromhex(span["parentSpanId"]))
    out += _str(5, span["name"])
    out += _key(6, 0) + _varint(int(span["kind"]))
    out += _fixed64(7, int(span["startTimeUnixNano"]))
    out += _fixed64(8, int(span["endTimeUnixNano"]))
    out += _pb_attrs(9, span.get("attributes", ()))
    status = span.get("status") or {}
    status_pb = _str(2, status.get("message", ""))
    if status.get("code"):
        status_pb += _key(3, 0) + _varint(int(status["code"]))
    out += _len(15, status_pb)
    return out


def encode_protobuf(request: Mapping[str, Any]) -> bytes:
    """``ExportTraceServiceRequest`` protobuf bytes for an OTLP JSON request."""
    out = b""
    for rs in request.get("resourceSpans", ()):
        resource = _pb_attrs(1, rs.get("resource", {}).get("attributes", ()))
        body = _len(1, resource)
        for ss in rs.get("scopeSpans", ()):
            scope = ss.get("scope", {})
            scope_pb = _str(1, scope.get("name", "")) + _str(2, scope.get("version", ""))
            ss_pb = _len(1, scope_pb)
            ss_pb += b"".join(_len(2, _pb_span(s)) for s in ss.get("spans", ()))
            ss_pb += _str(3, ss.get("schemaUrl", ""))
            body += _len(2, ss_pb)
        body += _str(3, rs.get("schemaUrl", ""))
        out += _len(1, body)
    return out


def encode_request(request: Mapping[str, Any], config: OTLPConfig) -> tuple[bytes, dict]:
    """Body and content headers for one export request."""
    if config.protocol == PROTOCOL_JSON:
        body = json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json"}
    else:
        body = encode_protobuf(request)
        headers = {"Content-Type": "application/x-protobuf"}
    if config.compression == "gzip":
        body = gzip.compress(body)
        headers["Content-Encoding"] = "gzip"
    return body, headers


# --- exporter ---------------------------------------------------------------------


Transport = Callable[[str, bytes, Mapping[str, str], float], tuple[int, Mapping[str, str]]]

_live_exporters: weakref.WeakSet[OTLPSpanExporter] = weakref.WeakSet()


def _reset_after_fork() -> None:
    for exporter in list(_live_exporters):
        with contextlib.suppress(Exception):
            exporter._reset_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


class OTLPSpanExporter:
    """Batches spans to one OTLP/HTTP endpoint from a daemon thread.

    ``export_event`` never does I/O and never raises. ``transport`` (tests)
    replaces the HTTP POST: ``(url, body, headers, timeout) -> (status,
    response headers)``; ``clock`` replaces ``time.monotonic`` for back-off;
    ``start_thread=False`` leaves sending to explicit ``flush()`` calls.
    """

    def __init__(self, config: OTLPConfig, *, transport: Transport | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 start_thread: bool = True):
        self.config = config
        self._transport = transport
        self._clock = clock
        self._start_thread = start_thread
        self._client: httpx.Client | None = None
        self._init_state()
        _live_exporters.add(self)

    def _init_state(self) -> None:
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._wake = threading.Event()
        self._queue: deque[tuple[dict, str | None]] = deque()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._retry_at = 0.0
        self._backoff = 0.0
        self._exported = 0
        self._attached = 0
        self._failed_requests = 0
        self._dropped = dict.fromkeys(DROP_REASONS, 0)
        self._warned = False

    def _reset_after_fork(self) -> None:
        # The parent's thread and lock holders do not exist in the child, and
        # the queued spans are the parent's to send.
        self._client = None
        self._init_state()

    # --- caller side --------------------------------------------------------

    def export_event(self, event: Mapping[str, Any], messages: Iterable[Any] = ()) -> None:
        """Queue one span for ``event``. No I/O, never raises."""
        try:
            span = build_span(event, messages, self.config)
            env = event.get("environment")
            self._enqueue(span, env if isinstance(env, str) else None)
        except Exception as e:  # noqa: BLE001 - fail-open
            with self._lock:
                self._dropped["build_failed"] += 1
            logger.debug("OTLP span not built: %s", e)

    def attach_to(self, sdk_span: Any, event: Mapping[str, Any],
                  messages: Iterable[Any] = ()) -> bool:
        """Put this exporter's attributes (never content) on ``sdk_span``, a
        recording SDK span, instead of queueing a span of its own. False
        (nothing done) when this exporter exports its own spans."""
        if self.config.sdk_spans != SDK_SPANS_ATTACH:
            return False
        try:
            attrs = span_attributes(event, messages, self.config)
            for key, value in attrs.items():
                if key not in _CONTENT_ATTRIBUTES:
                    sdk_span.set_attribute(key, value)
        except Exception as e:  # noqa: BLE001 - fail-open: export it instead
            logger.debug("OTLP attributes not attached: %s", e)
            return False
        with self._lock:
            self._attached += 1
        return True

    def _enqueue(self, span: dict, environment: str | None) -> None:
        with self._lock:
            if self._stopping:
                self._dropped["shutdown"] += 1
                return
            self._queue.append((span, environment))
            self._trim()
            full = len(self._queue) >= self.config.max_export_batch_size
            if self._thread is None and self._start_thread:
                self._start()
        if full and not self._backing_off():
            self._wake.set()

    def _trim(self) -> None:
        over = len(self._queue) - self.config.max_queue_size
        if over > 0:
            for _ in range(over):
                self._queue.popleft()
            self._dropped["queue_full"] += over

    def _start(self) -> None:
        thread = threading.Thread(target=self._run, name="prompt-shields-otlp",
                                  daemon=True)
        try:
            thread.start()
        except RuntimeError as e:  # interpreter shutdown
            logger.debug("OTLP thread not started: %s", e)
            return
        self._thread = thread

    def stats(self) -> dict:
        with self._lock:
            dropped = dict(self._dropped)
            return {
                "endpoint": self.config.endpoint,
                "queued": len(self._queue),
                "exported": self._exported,
                "attached_to_sdk_spans": self._attached,
                "failed_requests": self._failed_requests,
                "dropped": dropped,
                "dropped_total": sum(dropped.values()),
                "retry_after_seconds": max(0.0, self._retry_at - self._clock()),
            }

    # --- sending ------------------------------------------------------------

    def _backing_off(self) -> bool:
        return self._clock() < self._retry_at

    def _run(self) -> None:
        while True:
            self._wake.wait(self.config.schedule_delay)
            self._wake.clear()
            if self._stopping:
                return
            try:
                self.flush()
            except Exception as e:  # noqa: BLE001 - keep the thread alive
                logger.debug("OTLP flush error: %s", e)

    def _post(self, body: bytes, headers: Mapping[str, str],
              timeout: float) -> tuple[int, Mapping[str, str]]:
        if self._transport is not None:
            return self._transport(self.config.endpoint, body, headers, timeout)
        if self._client is None:
            self._client = httpx.Client(follow_redirects=False)
        resp = self._client.post(self.config.endpoint, content=body,
                                 headers=dict(headers), timeout=timeout)
        return resp.status_code, resp.headers

    def flush(self, timeout: float | None = None) -> bool:
        """Send what is queued, batch by batch, now (on this thread).
        Returns True when the queue is empty afterwards. Never raises.
        While backing off after a failure nothing is sent."""
        deadline = None if timeout is None else self._clock() + timeout
        # A send in flight on the background thread holds the lock: wait for
        # it only as long as the caller allows.
        if not self._send_lock.acquire(timeout=-1 if timeout is None
                                       else max(0.0, timeout)):
            return False
        try:
            while True:
                if self._backing_off():
                    return False
                with self._lock:
                    if not self._queue:
                        return True
                    n = min(len(self._queue), self.config.max_export_batch_size)
                    batch = [self._queue.popleft() for _ in range(n)]
                remaining = self.config.timeout
                if deadline is not None:
                    remaining = min(remaining, deadline - self._clock())
                    if remaining <= 0:
                        self._requeue(batch)
                        return False
                if not self._send(batch, remaining):
                    return False
        finally:
            self._send_lock.release()

    def _send(self, batch: list[tuple[dict, str | None]], timeout: float) -> bool:
        try:
            body, content_headers = encode_request(build_request(batch, self.config),
                                                   self.config)
            headers = {**self.config.headers, **content_headers}
        except Exception as e:  # noqa: BLE001 - unsendable batch
            with self._lock:
                self._dropped["build_failed"] += len(batch)
            logger.debug("OTLP batch not encoded: %s", e)
            return True
        retry_after = None
        try:
            status, resp_headers = self._post(body, headers, timeout)
        except Exception as e:  # noqa: BLE001 - network failure: retry later
            status, detail = None, e
        else:
            detail = f"HTTP {status}"
            if 200 <= status < 300:
                with self._lock:
                    self._exported += len(batch)
                    self._backoff = 0.0
                    self._retry_at = 0.0
                return True
            retry_after = parse_retry_after((resp_headers or {}).get("Retry-After"))
        with self._lock:
            self._failed_requests += 1
        self._warn(detail)
        if status is not None and status not in _RETRYABLE_STATUS:
            with self._lock:
                self._dropped["rejected"] += len(batch)
            return True
        with self._lock:
            self._backoff = min(MAX_BACKOFF_SECONDS, max(1.0, self._backoff * 2))
            delay = retry_after if retry_after is not None else self._backoff
            self._retry_at = self._clock() + delay
        self._requeue(batch)
        return False

    def _requeue(self, batch: list[tuple[dict, str | None]]) -> None:
        with self._lock:
            self._queue.extendleft(reversed(batch))
            # Newer spans win: the queue's oldest go when it is over its bound.
            self._trim()

    def _warn(self, detail: Any) -> None:
        if self._warned:
            logger.debug("OTLP export failed: %s", detail)
            return
        self._warned = True
        logger.warning("OTLP span export to %s failed (further failures logged at "
                       "debug level): %s", self.config.endpoint, detail)

    # --- lifecycle ----------------------------------------------------------

    def flush_at_exit(self, timeout: float) -> None:
        """Bounded final send (``register_exit_flush``); never raises."""
        try:
            self._stopping = True
            self._wake.set()
            self._retry_at = 0.0
            self.flush(timeout=timeout)
        except Exception:  # noqa: BLE001, S110 - never raise at exit
            pass

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the thread, send what is queued (within ``timeout``), and
        count what is left as dropped."""
        self.flush_at_exit(timeout)
        with self._lock:
            left = len(self._queue)
            self._queue.clear()
            self._dropped["shutdown"] += left
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()


# --- process-wide registry -----------------------------------------------------------

_registry_lock = threading.Lock()
_exporters: list[OTLPSpanExporter] = []
_env_checked = False


def configure_otlp(config: OTLPConfig | None = None, **kwargs: Any) -> OTLPSpanExporter | None:
    """Add an exporter for every SDK call (``OTLPConfig.from_env(**kwargs)``
    when no config is given). Returns None when ``OTEL_SDK_DISABLED=true``.
    Raises ValueError when no endpoint is configured."""
    if _truthy(os.environ.get("OTEL_SDK_DISABLED")):
        return None
    exporter_kwargs = {k: kwargs.pop(k) for k in ("transport", "clock", "start_thread")
                       if k in kwargs}
    if config is None:
        config = OTLPConfig.from_env(**kwargs)
    elif kwargs:
        config = replace(config, **kwargs)
    exporter = OTLPSpanExporter(config, **exporter_kwargs)
    with _registry_lock:
        _exporters.append(exporter)
    register_exit_flush(exporter)
    return exporter


def configure_langfuse(public_key: str | None = None, secret_key: str | None = None,
                       host: str | None = None, **kwargs: Any) -> OTLPSpanExporter | None:
    """``configure_otlp`` with ``langfuse_config`` (see there)."""
    exporter_kwargs = {k: kwargs.pop(k) for k in ("transport", "clock", "start_thread")
                       if k in kwargs}
    return configure_otlp(langfuse_config(public_key, secret_key, host, **kwargs),
                          **exporter_kwargs)


def exporters() -> list[OTLPSpanExporter]:
    with _registry_lock:
        return list(_exporters)


def otlp_stats() -> list[dict]:
    """``stats()`` of every configured exporter."""
    return [e.stats() for e in exporters()]


def shutdown_otlp(timeout: float = 5.0) -> None:
    """Flush and remove every exporter (also re-reads the environment on
    the next call)."""
    global _env_checked
    with _registry_lock:
        current = list(_exporters)
        _exporters.clear()
        _env_checked = False
    for exporter in current:
        unregister_exit_flush(exporter)
        exporter.shutdown(timeout)


def configure_from_env() -> None:
    """Configure exporters from ``PS_OTLP_EXPORT`` / ``PS_LANGFUSE_EXPORT``,
    once per process (no-op afterwards). Never raises: a bad setting is
    logged and the export stays off."""
    global _env_checked
    if _env_checked:
        return
    with _registry_lock:
        if _env_checked:
            return
        _env_checked = True
    try:
        if _truthy(os.environ.get("PS_OTLP_EXPORT")):
            configure_otlp()
    except Exception as e:  # noqa: BLE001
        logger.warning("PS_OTLP_EXPORT is set but OTLP export is off: %s", e)
    try:
        if _truthy(os.environ.get("PS_LANGFUSE_EXPORT")):
            configure_langfuse()
    except Exception as e:  # noqa: BLE001
        logger.warning("PS_LANGFUSE_EXPORT is set but Langfuse export is off: %s", e)


_CURRENT: Any = object()


def export_event(event: Mapping[str, Any], messages: Iterable[Any] = (), *,
                 sdk_span: Any = _CURRENT) -> None:
    """Hand one SDK telemetry event to every configured exporter. Called by
    the clients and ``instrument()`` next to the collector send; no I/O.

    ``sdk_span``: the call's SDK chat span (``prompt_shields.tracing``,
    #385), by default the current span if it is one. While it is recording,
    exporters in ``sdk_spans="attach"`` mode put their attributes on it
    instead of exporting a second span for the same call."""
    if not _env_checked:
        configure_from_env()
    if not _exporters:
        return
    live = None
    if sdk_span is not None:
        try:
            from prompt_shields import tracing

            live = tracing.live_chat_span(None if sdk_span is _CURRENT else sdk_span)
        except Exception:  # noqa: BLE001 - fail-open
            live = None
    for exporter in exporters():
        if live is not None and exporter.attach_to(live, event, messages):
            continue
        exporter.export_event(event, messages)
