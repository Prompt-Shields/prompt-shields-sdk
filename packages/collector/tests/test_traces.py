"""Agent trace span mapping (#385). No Postgres."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from collector import traces as tr
from collector.otel_ingest import classify_spans
from collector.otel_mapping import normalize_id
from collector.otlp_protobuf import decode_traces

NOW = datetime(2026, 11, 24, 12, tzinfo=UTC)
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


def _attr(key, value):
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": value}}


def _span(span_id="00f067aa0ba902b7", attrs=None, **over):
    start = int((NOW - timedelta(seconds=5)).timestamp() * 1e9)
    span = {
        "traceId": TRACE,
        "spanId": span_id,
        "name": "chat gpt-4o",
        "kind": 3,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(start + 250_000_000),
        "attributes": [_attr(k, v) for k, v in (attrs or {}).items()],
    }
    span.update(over)
    return span


def _payload(*spans, service="svc"):
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_attr("service.name", service)]},
                "scopeSpans": [{"spans": list(spans)}],
            }
        ]
    }


@pytest.mark.parametrize(
    ("value", "nbytes", "expected"),
    [
        (TRACE, 16, TRACE),
        (TRACE.upper(), 16, TRACE),
        (base64.b64encode(bytes.fromhex(TRACE)).decode(), 16, TRACE),
        ("00f067aa0ba902b7", 8, "00f067aa0ba902b7"),
        (base64.b64encode(bytes.fromhex("00f067aa0ba902b7")).decode(), 8, "00f067aa0ba902b7"),
        ("0" * 32, 16, None),  # invalid all-zero id
        ("zz" * 16, 16, None),
        (TRACE[:-2], 16, None),
        ("", 16, None),
        (None, 16, None),
        (12, 8, None),
        ("00f067aa0ba902b7", 16, None),
    ],
)
def test_normalize_id(value, nbytes, expected):
    assert normalize_id(value, nbytes) == expected


def test_map_reads_the_allowlist_only():
    span = _span(
        attrs={
            "gen_ai.operation.name": "chat",
            "gen_ai.system": "OpenAI",
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.usage.prompt_tokens": 7,
            "gen_ai.usage.completion_tokens": 3,
            "gen_ai.conversation.id": "s-1",
            "enduser.id": "alice",
            "error.type": "timeout",
            "gen_ai.input.messages": "secret",
        },
        parentSpanId="a000000000000001",
        status={"code": "STATUS_CODE_ERROR", "message": "secret"},
        events=[{"name": "gen_ai.content.prompt", "attributes": [_attr("x", "secret")]}],
    )
    row, genai = tr.map_trace_span(span, {"deployment.environment": "prod"}, NOW)
    assert genai is True
    assert row["provider"] == "openai" and row["status"] == "error"
    assert (row["tokens_in"], row["tokens_out"]) == (7, 3)
    assert row["kind"] == "client" and row["duration_ms"] == 250
    assert row["parent_span_id"] == "a000000000000001"
    assert row["user_id"] == "alice" and row["session_id"] == "s-1"
    assert row["error_type"] == "timeout" and row["environment"] == "prod"
    assert "secret" not in repr(row)


@pytest.mark.parametrize(
    "over",
    [
        {"traceId": "0" * 32},
        {"spanId": "nope"},
        {"startTimeUnixNano": None},
        {"startTimeUnixNano": str(int((NOW + timedelta(days=2)).timestamp() * 1e9))},
    ],
)
def test_unusable_spans_are_skipped(over):
    assert tr.map_trace_span(_span(**over), {}, NOW) is None


def test_end_before_start_has_no_end():
    span = _span(endTimeUnixNano="1")
    row, _ = tr.map_trace_span(span, {}, NOW)
    assert row["end_time"] is None and row["duration_ms"] is None


def test_long_names_are_truncated_and_long_labels_dropped():
    row, _ = tr.map_trace_span(
        _span(name="x" * 1000, attrs={"gen_ai.tool.name": "t" * 1000}), {}, NOW
    )
    assert len(row["name"]) == tr.MAX_NAME_LENGTH and row["tool_name"] is None


def test_collect_marks_genai_traces_and_dedupes():
    other = "00f067aa0ba902b700f067aa0ba902b7"
    rows, genai = tr.collect_spans(
        _payload(
            _span("a000000000000001", {"gen_ai.operation.name": "execute_tool"}),
            _span("a000000000000002", {"http.request.method": "GET"}),
            _span("a000000000000002", {"http.request.method": "GET"}),
            _span("a000000000000003", {"mcp.method.name": "tools/call"}, traceId=other),
            _span("a000000000000004", {}, traceId="1" * 32),
        ),
        NOW,
    )
    assert len(rows) == 4
    assert genai == {TRACE, other}


def test_protobuf_ids_decode_to_hex():
    pb = pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")
    msg = pb.ExportTraceServiceRequest()
    span = msg.resource_spans.add().scope_spans.add().spans.add()
    span.trace_id = bytes.fromhex(TRACE)
    span.span_id = bytes.fromhex("00f067aa0ba902b7")
    span.parent_span_id = bytes.fromhex("a000000000000001")
    span.start_time_unix_nano = int(NOW.timestamp() * 1e9)
    span.name = "x"
    rows, _ = tr.collect_spans(decode_traces(msg.SerializeToString()), NOW)
    assert [(r["trace_id"], r["span_id"], r["parent_span_id"]) for r in rows] == [
        (TRACE, "00f067aa0ba902b7", "a000000000000001")
    ]


def test_tool_and_agent_spans_are_not_usage_events_or_rejections():
    mapped, counts = classify_spans(
        _payload(
            _span(attrs={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "t"}),
            _span(attrs={"gen_ai.operation.name": "invoke_agent"}),
            _span(attrs={"gen_ai.operation.name": "chat"}),  # no provider
            _span(attrs={"gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai"}),
        )
    )
    assert len(mapped) == 1 and mapped[0].trace_id == TRACE
    (c,) = counts.values()
    assert c.ignored == {"span:execute_tool": 1, "span:agent": 1}
    assert c.rejected == {"no_vendor": 1}


def test_cursor_round_trip_and_rejects():
    start = datetime(2026, 11, 1, tzinfo=UTC)
    assert tr.decode_cursor(tr.encode_cursor(start, TRACE)) == (start, TRACE)
    for bad in ("!!", "x" * 300, base64.urlsafe_b64encode(b"2026-11-01T00:00:00|nope").decode()):
        with pytest.raises(ValueError):
            tr.decode_cursor(bad)


def _row(span_id, parent=None, start=0, end=10, **over):
    base = datetime(2026, 11, 1, tzinfo=UTC)
    fields = dict.fromkeys(tr.SPAN_FIELDS)
    fields.update(
        span_id=span_id,
        parent_span_id=parent,
        name=f"span {span_id}",
        status="ok",
        start_time=base + timedelta(milliseconds=start),
        end_time=base + timedelta(milliseconds=end),
    )
    fields.update(over)
    return SimpleNamespace(**fields)


def test_summary_root_and_totals():
    rows = [
        # The app's own root span was never exported: the earliest span
        # whose parent is not stored is the root.
        _row("a", parent="app", start=0, end=500),
        _row("b", parent="a", start=10, end=900, tokens_in=5, status="error"),
        _row("c", parent="a", start=20, end=30, tokens_in=7, session_id="s"),
    ]
    s = tr.summarize(TRACE, rows)
    assert s["root_name"] == "span a"
    assert (s["span_count"], s["error_count"], s["status"]) == (3, 1, "error")
    assert s["duration_ms"] == 900 and s["tokens_in"] == 12 and s["tokens_out"] is None
    assert s["session_id"] == "s"
    assert tr.summarize(TRACE, [_row("x"), _row("y", parent=None, start=5)])["root_name"] == "span x"
