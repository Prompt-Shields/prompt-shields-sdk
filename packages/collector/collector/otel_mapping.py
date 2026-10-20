"""Map OTLP/JSON trace data that follows the OTel GenAI semantic conventions
to the collector's usage-event model.

Only an allowlist of metadata attributes is ever read. Prompt and completion
content (``gen_ai.prompt*``, ``gen_ai.completion*``, ``gen_ai.input.messages``
and friends, and every span event) is dropped before mapping, so it can never
reach the database.

MCP ``tools/call`` spans (OTel MCP semantic conventions: ``mcp.method.name``,
``gen_ai.tool.name``, ``server.address``, ``network.transport``,
``error.type``) map to an MCP tool call (``map_mcp_span``), not a usage
event: names, outcome and duration only. Tool arguments and results
(``gen_ai.tool.call.arguments`` / ``.result``) are dropped like any content.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from collector.mcp_usage import (
    MAX_LABEL_LENGTH,
    OTEL_SOURCE,
    UNKNOWN_SERVER,
    McpCall,
    clean_name,
    server_identity,
)
from collector.sanitize import is_storable_text

# Attribute keys (exact or prefix) that may carry message content. They are
# stripped before any mapping happens.
CONTENT_ATTRIBUTE_PREFIXES: tuple[str, ...] = (
    "gen_ai.prompt",
    "gen_ai.completion",
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.system_instructions",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
    "gen_ai.tool.definitions",
    "llm.prompts",
    "llm.completions",
    "traceloop.entity.input",
    "traceloop.entity.output",
)

# OTLP status code for an errored span (enum value 2 or its JSON name).
_STATUS_ERROR = {2, "2", "STATUS_CODE_ERROR"}


@dataclass(frozen=True)
class GenAISpan:
    vendor: str
    requested_model: str | None
    served_model: str | None
    api_surface: str | None
    tokens_in: int | None
    tokens_out: int | None
    latency_ms: int | None
    is_error: bool
    calling_service: str | None
    environment: str | None
    session_id: str | None

    @property
    def model(self) -> str | None:
        # Asset merge key. The SDK and /ingest/events key assets on the
        # requested model, so prefer it here too; otherwise every dated
        # snapshot the provider serves would become a separate asset.
        return self.requested_model or self.served_model


def is_content_attribute(key: str) -> bool:
    return key.startswith(CONTENT_ATTRIBUTE_PREFIXES)


def _any_value(value: Any) -> Any:
    """Decode an OTLP/JSON AnyValue. Arrays and maps are not needed for the
    allowlisted keys and are returned as None. A string the database cannot
    store (NUL, lone surrogate; collector/sanitize.py) is None too, so it
    costs that attribute rather than every span in the request."""
    if not isinstance(value, dict):
        return None
    if "stringValue" in value:
        text = value["stringValue"]
        if isinstance(text, str) and not is_storable_text(text):
            return None
        return text
    if "intValue" in value:  # int64 is encoded as a JSON string
        try:
            return int(value["intValue"])
        except (TypeError, ValueError):
            return None
    if "doubleValue" in value:
        return value["doubleValue"]
    if "boolValue" in value:
        return value["boolValue"]
    return None


def safe_attributes(attributes: Any) -> dict[str, Any]:
    """Flatten an OTLP attribute list, dropping content-bearing keys."""
    out: dict[str, Any] = {}
    if not isinstance(attributes, list):
        return out
    for attr in attributes:
        if not isinstance(attr, dict):
            continue
        key = attr.get("key")
        if not isinstance(key, str) or is_content_attribute(key):
            continue
        out[key] = _any_value(attr.get("value"))
    return out


def _str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


# Usage columns are Postgres INTEGER. A value outside it would fail the
# INSERT and drop every span in the request, so it is treated as missing.
_PG_INT_MAX = 2**31 - 1


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # json.loads accepts Infinity and NaN, which int() cannot convert.
        return int(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _count(value: Any) -> int | None:
    """A non-negative value that fits an INTEGER column, else None."""
    n = _int(value)
    return n if n is not None and 0 <= n <= _PG_INT_MAX else None


def _duration_ms(span: dict) -> int | None:
    start = _int(span.get("startTimeUnixNano"))
    end = _int(span.get("endTimeUnixNano"))
    if start is None or end is None or end < start:
        return None
    return _count((end - start) // 1_000_000)


def is_genai_span(attrs: dict[str, Any]) -> bool:
    return any(
        k in attrs
        for k in ("gen_ai.operation.name", "gen_ai.provider.name", "gen_ai.system")
    )


def map_span(span: dict, resource_attrs: dict[str, Any]) -> GenAISpan | None:
    """Return the mapped span, or None for non-GenAI spans and GenAI spans
    without an identifiable vendor. Span events are never read."""
    attrs = safe_attributes(span.get("attributes"))
    if not is_genai_span(attrs):
        return None
    vendor = _str(attrs.get("gen_ai.provider.name")) or _str(attrs.get("gen_ai.system"))
    if vendor is None:
        return None

    tokens_in = _count(attrs.get("gen_ai.usage.input_tokens"))
    if tokens_in is None:  # pre-1.27 semconv name
        tokens_in = _count(attrs.get("gen_ai.usage.prompt_tokens"))
    tokens_out = _count(attrs.get("gen_ai.usage.output_tokens"))
    if tokens_out is None:
        tokens_out = _count(attrs.get("gen_ai.usage.completion_tokens"))

    status = span.get("status") if isinstance(span.get("status"), dict) else {}
    environment = _str(resource_attrs.get("deployment.environment.name")) or _str(
        resource_attrs.get("deployment.environment")
    )

    return GenAISpan(
        vendor=vendor.lower(),
        requested_model=_str(attrs.get("gen_ai.request.model")),
        served_model=_str(attrs.get("gen_ai.response.model")),
        api_surface=_str(attrs.get("gen_ai.operation.name")),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=_duration_ms(span),
        is_error=status.get("code") in _STATUS_ERROR,
        calling_service=_str(resource_attrs.get("service.name")),
        environment=environment,
        session_id=_str(attrs.get("gen_ai.conversation.id")),
    )


_COUNT_ATTRIBUTES = (
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.prompt_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.completion_tokens",
)


# String attributes map_span stores.
_STRING_ATTRIBUTES = (
    "gen_ai.provider.name",
    "gen_ai.system",
    "gen_ai.request.model",
    "gen_ai.response.model",
    "gen_ai.operation.name",
    "gen_ai.conversation.id",
)


def _unstorable_strings(attributes: Any) -> set[str]:
    """Keys of the stored string attributes whose value _any_value dropped
    because the database cannot store it."""
    out: set[str] = set()
    if not isinstance(attributes, list):
        return out
    for attr in attributes:
        if not isinstance(attr, dict) or attr.get("key") not in _STRING_ATTRIBUTES:
            continue
        value = attr.get("value")
        text = value.get("stringValue") if isinstance(value, dict) else None
        if isinstance(text, str) and not is_storable_text(text):
            out.add(attr["key"])
    return out


def invalid_attributes(span: dict) -> dict[str, int]:
    """Values map_span drops as unusable (stored as null), keyed
    "<attribute>:invalid" like /ingest/events reports invalid fields (#87):
    token counts that are not a non-negative INTEGER, strings the database
    cannot store, and start/end times that give no duration."""
    attrs = safe_attributes(span.get("attributes"))
    out: dict[str, int] = {}
    for key in _COUNT_ATTRIBUTES:
        if key in attrs and _count(attrs[key]) is None:
            out[f"{key}:invalid"] = 1
    for key in _unstorable_strings(span.get("attributes")):
        out[f"{key}:invalid"] = 1
    if (
        span.get("startTimeUnixNano") is not None
        and span.get("endTimeUnixNano") is not None
        and _duration_ms(span) is None
    ):
        out["span.duration:invalid"] = 1
    return out


# --- MCP spans --------------------------------------------------------------

MCP_METHOD = "mcp.method.name"
MCP_TOOLS_CALL = "tools/call"
# OTLP span kind SERVER (enum value 2 or its JSON name).
_KIND_SERVER = {2, "2", "SPAN_KIND_SERVER"}


def is_mcp_span(attrs: dict[str, Any]) -> bool:
    return MCP_METHOD in attrs


def _tool_from_span_name(name: Any) -> str | None:
    """The target in a ``{mcp.method.name} {target}`` span name. Some
    clients (the Python SDK's "MCP send tools/call <tool>") set no
    ``gen_ai.tool.name``."""
    if not isinstance(name, str):
        return None
    marker = MCP_TOOLS_CALL + " "
    idx = name.find(marker)
    return name[idx + len(marker) :] if idx >= 0 else None


def _mcp_transport(attrs: dict[str, Any]) -> str | None:
    transport = _str(attrs.get("network.transport"))
    if transport == "pipe":
        return "stdio"
    if _str(attrs.get("network.protocol.name")) == "http":
        return "http"
    return clean_name(transport, MAX_LABEL_LENGTH)


def map_mcp_span(span: dict, resource_attrs: dict[str, Any]) -> McpCall | None:
    """An MCP ``tools/call`` span as a tool call, else None.

    Server identity: on a server span (kind SERVER) the server's own
    ``service.name``; otherwise (client spans) the ``server.address`` host,
    or "unknown" (stdio servers have no address). The calling service is
    the client's ``service.name``; a server span does not know its caller.
    Errors: status ERROR or any ``error.type`` (``tool_error`` for a result
    with ``isError``).
    """
    attrs = safe_attributes(span.get("attributes"))
    if _str(attrs.get(MCP_METHOD)) != MCP_TOOLS_CALL:
        return None
    tool = clean_name(attrs.get("gen_ai.tool.name")) or clean_name(
        _tool_from_span_name(span.get("name"))
    )
    if tool is None:
        return None
    service = clean_name(resource_attrs.get("service.name"))
    if span.get("kind") in _KIND_SERVER:
        server, caller = service, None
    else:
        server, caller = server_identity(attrs.get("server.address")), service
    status = span.get("status") if isinstance(span.get("status"), dict) else {}
    environment = _str(resource_attrs.get("deployment.environment.name")) or _str(
        resource_attrs.get("deployment.environment")
    )
    return McpCall(
        source=OTEL_SOURCE,
        server_name=server or UNKNOWN_SERVER,
        tool_name=tool,
        is_error=status.get("code") in _STATUS_ERROR
        or attrs.get("error.type") is not None,
        latency_ms=_duration_ms(span),
        calling_service=caller or "",
        environment=clean_name(environment, MAX_LABEL_LENGTH) or "",
        transport=_mcp_transport(attrs),
    )


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def iter_spans(payload: dict):
    """Yield (span, resource_attrs) for every span in an
    ExportTraceServiceRequest JSON body."""
    for rs in _list(payload.get("resourceSpans")):
        if not isinstance(rs, dict):
            continue
        resource = rs.get("resource") if isinstance(rs.get("resource"), dict) else {}
        resource_attrs = safe_attributes(resource.get("attributes"))
        for ss in _list(rs.get("scopeSpans")):
            if not isinstance(ss, dict):
                continue
            for span in _list(ss.get("spans")):
                if isinstance(span, dict):
                    yield span, resource_attrs


def count_spans(payload: dict) -> int:
    return sum(1 for _ in iter_spans(payload))
