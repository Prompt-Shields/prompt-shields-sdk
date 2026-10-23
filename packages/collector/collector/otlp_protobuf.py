"""OTLP/HTTP protobuf (``application/x-protobuf``) for the trace receiver.

OpenLLMetry and most OTel SDKs export protobuf by default
(``OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf``). A protobuf
``ExportTraceServiceRequest`` is decoded into the same dict an OTLP/JSON body
parses to (``MessageToDict``: camelCase keys, int64 as strings, enums by
name), so both encodings go through one mapping path (otel_mapping.py) and
get the same treatment:

* Size: the same body limit (otel_ingest.MAX_BODY_BYTES) applies before
  decoding, and the same span limit after.
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

from collector.sanitize import (
    MAX_JSON_DEPTH,
    TOO_DEEP_DETAIL,
    BodyTooDeep,
    json_depth,
)

PROTOBUF_CONTENT_TYPES = frozenset({"application/x-protobuf", "application/protobuf"})
PROTOBUF_MEDIA_TYPE = "application/x-protobuf"


def decode_traces(raw: bytes) -> dict[str, Any]:
    """An ExportTraceServiceRequest as its OTLP/JSON dict. Raises
    ``BodyTooDeep`` past the JSON depth limit and ``ValueError`` for a body
    that is not a valid message."""
    message = ExportTraceServiceRequest()
    try:
        message.ParseFromString(raw)
    except RecursionError:
        raise BodyTooDeep(TOO_DEEP_DETAIL) from None
    except DecodeError as exc:
        # Includes invalid UTF-8 in a string field and nesting past the
        # parser's recursion limit.
        raise ValueError(str(exc)) from None
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
