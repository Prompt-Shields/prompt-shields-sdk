"""OTLP/HTTP protobuf (``application/x-protobuf``) for the trace receiver.

OpenLLMetry and most OTel SDKs export protobuf by default
(``OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf``). A protobuf
``ExportTraceServiceRequest`` is decoded into the same dict an OTLP/JSON body
parses to (``MessageToDict``: camelCase keys, int64 as strings, enums by
name), so both encodings go through one mapping path (otel_mapping.py) and
get the same treatment:

* Size: the same body limit (otel_ingest.MAX_BODY_BYTES) applies before
  decoding, and the same span limit after parsing but before the message is
  converted to a dict (``TooManySpans``): a 4 MiB body of empty spans is ~2M
  messages, and converting them all would cost seconds and several hundred
  MB for a request that is refused anyway.
* Depth: the decoded value must nest no deeper than ``MAX_JSON_DEPTH``, the
  limit for a JSON body. A message nested deeper than the protobuf parser's
  own recursion limit is a decode error. Both are a 400.
* Text: protobuf strings must be valid UTF-8, so a lone surrogate cannot
  arrive this way (the request is a 400, like invalid UTF-8 in JSON). A NUL
  is valid UTF-8; it reaches the mapper as ``"\\x00"`` and is dropped per
  attribute exactly as in a JSON body (collector/sanitize.py).
"""

from __future__ import annotations

from typing import Any

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTracePartialSuccess,
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from collector.loss_accounting import otel_client_id
from collector.otel_mapping import GENAI_MARKER_KEYS
from collector.sanitize import (
    MAX_JSON_DEPTH,
    TOO_DEEP_DETAIL,
    BodyTooDeep,
    json_depth,
)

PROTOBUF_CONTENT_TYPES = frozenset({"application/x-protobuf", "application/protobuf"})
PROTOBUF_MEDIA_TYPE = "application/x-protobuf"


class TooManySpans(Exception):
    """The request has more spans than the limit. ``genai_clients`` holds the
    loss-accounting client of each GenAI span in it, for the 413 path."""

    def __init__(self, genai_clients: list[str]):
        super().__init__(f"{len(genai_clients)} GenAI spans")
        self.genai_clients = genai_clients


def _service_attrs(resource: Any) -> dict[str, Any]:
    """``service.name`` of a Resource, as ``safe_attributes`` reads it."""
    attrs: dict[str, Any] = {}
    for kv in resource.attributes:
        if kv.key == "service.name":
            attrs["service.name"] = (
                kv.value.string_value
                if kv.value.WhichOneof("value") == "string_value"
                else None
            )
    return attrs


def _genai_clients(message: ExportTraceServiceRequest) -> list[str]:
    clients: list[str] = []
    for rs in message.resource_spans:
        client = None
        for ss in rs.scope_spans:
            for span in ss.spans:
                if any(kv.key in GENAI_MARKER_KEYS for kv in span.attributes):
                    if client is None:
                        client = otel_client_id(_service_attrs(rs.resource))
                    clients.append(client)
    return clients


def decode_traces(raw: bytes, max_spans: int | None = None) -> dict[str, Any]:
    """An ExportTraceServiceRequest as its OTLP/JSON dict. Raises
    ``BodyTooDeep`` past the JSON depth limit, ``TooManySpans`` past
    ``max_spans`` and ``ValueError`` for a body that is not a valid
    message."""
    message = ExportTraceServiceRequest()
    try:
        message.ParseFromString(raw)
    except RecursionError:
        raise BodyTooDeep(TOO_DEEP_DETAIL) from None
    except DecodeError as exc:
        # Includes invalid UTF-8 in a string field and nesting past the
        # parser's recursion limit.
        raise ValueError(str(exc)) from None
    if max_spans is not None:
        spans = sum(len(ss.spans) for rs in message.resource_spans for ss in rs.scope_spans)
        if spans > max_spans:
            raise TooManySpans(_genai_clients(message))
    try:
        payload = MessageToDict(message)
    except RecursionError:
        raise BodyTooDeep(TOO_DEEP_DETAIL) from None
    if json_depth(payload) > MAX_JSON_DEPTH:
        raise BodyTooDeep(TOO_DEEP_DETAIL)
    return payload


def encode_response(rejected: int = 0, error_message: str = "") -> bytes:
    """ExportTraceServiceResponse: empty for full success, else with
    partial_success (OTLP/HTTP answers in the request's encoding)."""
    response = ExportTraceServiceResponse()
    if rejected:
        response.partial_success.CopyFrom(
            ExportTracePartialSuccess(
                rejected_spans=rejected, error_message=error_message
            )
        )
    return response.SerializeToString()
