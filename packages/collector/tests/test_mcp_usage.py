"""MCP tool usage (#63 Phase 0): event validation, aggregation and OTel MCP
span mapping. No database; the ingest and read routes are covered in
test_mcp_usage_db.py and, with RLS enforced, test_rls_db.py."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from collector.mcp_usage import (
    MAX_NAME_LENGTH,
    McpCall,
    aggregate,
    parse_event,
    server_identity,
)
from collector.otel_ingest import classify_spans
from collector.otel_mapping import map_mcp_span

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def _attrs(**kv):
    out = []
    for k, v in kv.items():
        key = k.replace("__", ".")
        if isinstance(v, int):
            out.append({"key": key, "value": {"intValue": str(v)}})
        else:
            out.append({"key": key, "value": {"stringValue": v}})
    return out


def _span(name="tools/call search", kind=3, status=None, **attrs):
    span = {
        "name": name,
        "kind": kind,
        "startTimeUnixNano": "1000000000",
        "endTimeUnixNano": "1042000000",
        "attributes": _attrs(**attrs),
    }
    if status is not None:
        span["status"] = {"code": status}
    return span


def _payload(*spans, service="billing-agent"):
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": _attrs(
                        service__name=service,
                        deployment__environment__name="prod",
                    )
                },
                "scopeSpans": [{"spans": list(spans)}],
            }
        ]
    }


# --- /ingest/mcp events --------------------------------------------------------


def test_parse_event_keeps_names_only():
    call = parse_event(
        {
            "server_name": "github",
            "tool_name": "create_issue",
            "outcome": "error",
            "latency_ms": 41,
            "calling_service": "billing-agent",
            "environment": "prod",
            "transport": "stdio",
            "arguments": {"token": "secret"},
            "result": "customer data",
        }
    )
    assert call == McpCall(
        source="sdk",
        server_name="github",
        tool_name="create_issue",
        is_error=True,
        latency_ms=41,
        calling_service="billing-agent",
        environment="prod",
        transport="stdio",
    )


@pytest.mark.parametrize(
    "event",
    [
        {"server_name": "github"},  # no tool
        {"tool_name": "  "},
        {"tool_name": "x" * (MAX_NAME_LENGTH + 1)},
        {"tool_name": 7},
        {"tool_name": "t", "outcome": "maybe"},
        {"tool_name": "t", "source": "otel"},  # only the OTLP receiver sets it
        {"tool_name": "t", "server_name": "https:///no-host"},
        {"tool_name": "t", "server_name": ["github"]},
        "not an object",
    ],
)
def test_parse_event_skips_unstorable_events(event):
    assert parse_event(event) is None


def test_parse_event_blocked_and_user():
    call = parse_event({
        "tool_name": "t", "source": "gateway", "outcome": "blocked",
        "user_id": "ab" * 16, "user_group": "quant-analysts",
    })
    assert (call.is_blocked, call.is_error) == (True, False)
    assert (call.user_hash, call.user_group) == ("ab" * 16, "quant-analysts")


@pytest.mark.parametrize(
    "user_id",
    ["jane.doe@bank.example", "user-00u1abcd", "AB" * 16, "ab" * 40, 7, None],
)
def test_parse_event_keeps_only_hashed_users(user_id):
    call = parse_event({"tool_name": "t", "user_id": user_id, "user_group": "a b"})
    assert (call.user_hash, call.user_group) == ("", "")


def test_aggregate_counts_blocked_apart_per_user():
    tid = uuid.uuid4()
    rows = aggregate(tid, [
        McpCall("gateway", "docs", "search", False, 5, is_blocked=True, user_hash="a" * 32),
        McpCall("gateway", "docs", "search", True, 5, user_hash="a" * 32),
        McpCall("gateway", "docs", "search", False, 5, user_hash="b" * 32),
    ], NOW)
    assert sorted(
        (r["user_hash"], r["call_count"], r["error_count"], r["blocked_count"])
        for r in rows
    ) == [("a" * 32, 2, 1, 1), ("b" * 32, 1, 0, 0)]


def test_parse_event_drops_bad_optional_fields():
    call = parse_event(
        {
            "tool_name": "t\x00\n",
            "latency_ms": -1,
            "environment": "e" * 65,
            "calling_service": 5,
        }
    )
    assert call.tool_name == "t"
    assert call.server_name == "unknown"
    assert (call.latency_ms, call.environment, call.calling_service) == (None, "", "")


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        ("https://user:pa55@mcp.example.com:8443/v1?key=abc", "mcp.example.com:8443"),
        ("http://mcp.internal/sse", "mcp.internal"),
        ("jira", "jira"),
        ("https://[::1", None),
    ],
)
def test_server_identity_never_keeps_credentials(value, stored):
    assert server_identity(value) == stored


def test_aggregate_sums_per_key():
    tid = uuid.uuid4()
    calls = [
        McpCall("sdk", "github", "search", False, 10),
        McpCall("sdk", "github", "search", True, 30, transport="stdio"),
        McpCall("sdk", "github", "search", False, None),
        McpCall("sdk", "github", "search", False, 5, calling_service="other"),
    ]
    rows = aggregate(tid, calls, NOW)
    assert len(rows) == 2
    row = next(r for r in rows if r["calling_service"] == "")
    assert (row["call_count"], row["error_count"]) == (3, 1)
    assert (row["latency_ms_total"], row["latency_count"]) == (40, 2)
    assert row["transport"] == "stdio"
    assert row["first_seen"] == row["last_seen"] == NOW


# --- OTel MCP spans --------------------------------------------------------------


def test_client_tools_call_span():
    span = _span(
        mcp__method__name="tools/call",
        gen_ai__tool__name="search",
        gen_ai__operation__name="execute_tool",
        server__address="mcp.example.com",
        server__port=443,
        network__protocol__name="http",
        network__transport="tcp",
        gen_ai__tool__call__arguments='{"q": "secret"}',
        gen_ai__tool__call__result="customer data",
    )
    call = map_mcp_span(span, {"service.name": "billing-agent",
                               "deployment.environment.name": "prod"})
    assert call == McpCall(
        source="otel",
        server_name="mcp.example.com",
        tool_name="search",
        is_error=False,
        latency_ms=42,
        calling_service="billing-agent",
        environment="prod",
        transport="http",
    )


def test_server_span_uses_its_own_service_name():
    span = _span(
        kind="SPAN_KIND_SERVER",
        mcp__method__name="tools/call",
        gen_ai__tool__name="lookup",
        network__transport="pipe",
        error__type="tool_error",
    )
    call = map_mcp_span(span, {"service.name": "crm-mcp-server"})
    assert (call.server_name, call.calling_service) == ("crm-mcp-server", "")
    assert call.is_error and call.transport == "stdio"


def test_tool_name_from_span_name_and_unknown_server():
    # The Python SDK's client span: no gen_ai.tool.name, no server.address.
    span = _span(name="MCP send tools/call forecast", status=2,
                 mcp__method__name="tools/call")
    call = map_mcp_span(span, {})
    assert (call.tool_name, call.server_name, call.is_error) == (
        "forecast", "unknown", True)


def test_other_mcp_methods_are_not_tool_calls():
    assert map_mcp_span(_span(mcp__method__name="tools/list"), {}) is None
    assert map_mcp_span(_span(name="tools/call", mcp__method__name="tools/call"),
                        {}) is None


def test_classify_spans_routes_mcp_tool_calls():
    payload = _payload(
        _span(mcp__method__name="tools/call", gen_ai__tool__name="search",
              gen_ai__operation__name="execute_tool"),
        _span(name="tools/list", mcp__method__name="tools/list"),
        _span(name="chat gpt-4o", gen_ai__provider__name="openai",
              gen_ai__request__model="gpt-4o"),
    )
    calls: list[McpCall] = []
    mapped, counts = classify_spans(payload, mcp_calls=calls)
    assert [c.tool_name for c in calls] == ["search"]
    assert calls[0].calling_service == "billing-agent"
    assert [m.vendor for m in mapped] == ["openai"]
    (c,) = counts.values()
    # The tools/call span is neither a usage event nor rejected (no vendor).
    assert c.received == 1 and c.rejected == {}
    assert c.ignored == {"span:non_genai": 1}
