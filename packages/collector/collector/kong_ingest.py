"""Receiver for Kong Gateway's stock ``http-log`` plugin.

A Kong (AI) Gateway points ``http-log`` at ``POST /ingest/kong`` with
``Authorization: Bearer <tenant ingest key>`` in the plugin's ``headers``
config. Kong sends one log entry as a JSON object, or a JSON array of entries
when ``queue.max_batch_size`` is above 1.

Only entries that carry Kong's AI analytics (``ai.<namespace>.meta`` /
``.usage``, written by ai-proxy and the other AI plugins when
``logging.log_statistics`` is on) become usage events. Everything else is
counted as ignored. The AI log shape has moved between Kong versions, and
Kong says it may change again in 4.0, so every field is read defensively:

* 3.6/3.7 key the analytics by plugin name (``ai["ai-proxy"]``,
  ``ai["ai-request-transformer"]``); later versions use ``ai.proxy``. Every
  namespace with a provider becomes its own event (each is a separate LLM
  call), so a request through ai-proxy plus ai-request-transformer is two.
* Token counts are read from ``prompt_tokens`` / ``completion_tokens`` and
  the singular spellings some Kong docs show.
* ``"UNSPECIFIED"`` (Kong's placeholder) counts as missing.

What is never read or stored: the AI payload containers (prompt and response
bodies, logged when ``log_payloads`` is on), any request or response header
other than the ``X-PS-*`` business headers (so ``authorization``,
``apikey``, ``x-api-key`` and cookies never leave this module), the query
string, and the client IP (an unsalted hash of an IPv4 address is
reversible, and the registry has no use for it).
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Request
from pydantic import ValidationError

from collector import loss_accounting
from collector.auth import resolve_tenant
from collector.ingest import LENIENT_FIELDS, IngestEvent, _store_events
from collector.otel_ingest import _problem, _read_bounded_body

router = APIRouter()

KONG_SOURCE = "kong"
# The body limit is the OTLP receiver's (4 MiB, otel_ingest.MAX_BODY_BYTES).
MAX_ENTRIES = 1000
# AI namespaces read per entry; Kong writes one per AI plugin on the route.
MAX_NAMESPACES = 8
MAX_HEADER_VALUE_LENGTH = 256
MAX_NAME_LENGTH = 256
MAX_VENDOR_LENGTH = 64

# Kong's provider names, mapped to the vendor names the SDK reports, so a
# Kong sighting and an SDK sighting of the same model merge into one asset.
VENDOR_ALIASES = {"azure": "azure_openai", "gemini": "google"}
# Kong's placeholder when it could not tell the provider or model.
_UNSPECIFIED = "unspecified"
# The gateway's X-PS-* business header contract (ps-telemetry.ts); nothing
# else is read from request.headers.
PS_HEADERS = {
    "x-ps-business-unit": "business_unit",
    "x-ps-use-case": "use_case_name",
    "x-ps-owner": "owner_email",
    "x-ps-data-classification": "data_classification",
    "x-ps-environment": "environment",
}
# Containers inside an AI namespace that never become a usage event.
_NON_ANALYTICS_NAMESPACES = {"payload"}

NON_AI = "entry:non_ai"
NO_VENDOR = "no_vendor"
NOT_AN_OBJECT = "not_an_object"


def _text(value: Any, max_length: int = MAX_NAME_LENGTH) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > max_length or value.lower() == _UNSPECIFIED:
        return None
    return value


def _first(container: dict, *keys: str) -> Any:
    for key in keys:
        value = container.get(key)
        if value is not None:
            return value
    return None


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _vendor(provider: Any) -> str | None:
    name = _text(provider, MAX_VENDOR_LENGTH)
    if name is None:
        return None
    name = name.lower()
    return VENDOR_ALIASES.get(name, name)


def _latency(meta: dict, entry: dict) -> Any:
    """Kong's LLM round trip (``meta.llm_latency``, 3.8+), else the
    upstream time from the standard ``latencies.proxy`` (-1 when Kong never
    reached an upstream, which the ingest model drops)."""
    value = meta.get("llm_latency")
    if value is None:
        value = _dict(entry.get("latencies")).get("proxy")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return value


def _ps_headers(entry: dict) -> dict[str, str]:
    headers = _dict(_dict(entry.get("request")).get("headers"))
    out: dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str):
            continue
        target = PS_HEADERS.get(name.lower())
        if target is None:
            continue
        if isinstance(value, list):  # a repeated header: Kong logs a list
            value = value[0] if value else None
        value = _text(value, MAX_HEADER_VALUE_LENGTH)
        if value is not None:
            out[target] = value
    return out


def _calling_service(entry: dict) -> str | None:
    """Who called: the authenticated Kong consumer (its username, else its
    custom_id), else the route, else the Kong service. In an AI gateway the
    Kong service usually names the LLM upstream, so it comes last."""
    consumer = _dict(entry.get("consumer"))
    return (
        _text(consumer.get("username"))
        or _text(consumer.get("custom_id"))
        or _text(_dict(entry.get("route")).get("name"))
        or _text(_dict(entry.get("service")).get("name"))
    )


def _namespaces(ai: dict) -> list[tuple[str, dict]]:
    """AI analytics namespaces, ``proxy`` (3.8+) first."""
    found = [
        (name, ns)
        for name, ns in ai.items()
        if isinstance(name, str)
        and name not in _NON_ANALYTICS_NAMESPACES
        and isinstance(ns, dict)
        and (isinstance(ns.get("meta"), dict) or isinstance(ns.get("usage"), dict))
    ]
    found.sort(key=lambda item: item[0] != "proxy")
    return found[:MAX_NAMESPACES]


@dataclass
class MappedEntry:
    """What one Kong log entry produced."""

    events: list[IngestEvent] = field(default_factory=list)
    rejected: dict[str, int] = field(default_factory=dict)
    ignored: dict[str, int] = field(default_factory=dict)


def _count(counts: dict[str, int], key: str, n: int = 1) -> None:
    counts[key] = counts.get(key, 0) + n


def map_entry(entry: Any) -> MappedEntry:
    """Map one http-log entry to zero or more ingest events.

    Reads only metadata: never a payload container, a header other than
    ``X-PS-*``, or the client IP.
    """
    out = MappedEntry()
    if not isinstance(entry, dict):
        _count(out.rejected, NOT_AN_OBJECT)
        return out
    namespaces = _namespaces(_dict(entry.get("ai")))
    if not namespaces:
        _count(out.ignored, NON_AI)
        return out

    business = _ps_headers(entry)
    calling_service = _calling_service(entry)
    for _name, ns in namespaces:
        meta = _dict(ns.get("meta"))
        usage = _dict(ns.get("usage"))
        vendor = _vendor(meta.get("provider_name"))
        if vendor is None:
            _count(out.rejected, NO_VENDOR)
            continue
        requested = _text(meta.get("request_model"))
        served = _text(meta.get("response_model"))
        cost = usage.get("cost")
        raw = {
            "vendor": vendor,
            # The asset is keyed on the requested model, as for OTel: the
            # served one often carries a date suffix and would split it.
            "model": requested or served,
            "source": KONG_SOURCE,
            "calling_service": calling_service,
            "requested_model": requested,
            "served_model": served,
            "tokens_in": _first(usage, "prompt_tokens", "prompt_token"),
            "tokens_out": _first(usage, "completion_tokens", "completion_token"),
            "latency_ms": _latency(meta, entry),
            "cost": cost,
            # Kong prices with the input_cost/output_cost set on the plugin.
            "cost_source": "kong_model_options" if cost is not None else None,
            **business,
        }
        try:
            event = IngestEvent(**raw)
        except ValidationError:  # a str field got a non-string; never 4xx a batch
            _count(out.rejected, "invalid")
            continue
        for key, n in loss_accounting.invalid_fields(
            [raw], [event], LENIENT_FIELDS
        ).items():
            if key == "latency_ms:invalid" and raw["latency_ms"] == -1:
                continue  # Kong's "no upstream" marker, not a bad value
            _count(out.ignored, key, n)
        out.events.append(event)
    return out


def map_payload(payload: Any) -> MappedEntry:
    entries = payload if isinstance(payload, list) else [payload]
    total = MappedEntry()
    for entry in entries:
        mapped = map_entry(entry)
        total.events.extend(mapped.events)
        for key, n in mapped.rejected.items():
            _count(total.rejected, key, n)
        for key, n in mapped.ignored.items():
            _count(total.ignored, key, n)
    return total


@router.post("/ingest/kong")
async def ingest_kong(request: Request):
    # Not under the /ingest/events request limit (like the OTLP receiver):
    # that counter is per tenant, so Kong traffic would use up the SDK's.
    tenant_id = await resolve_tenant(request)
    db = request.state.db
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise _problem(
            415, "Unsupported Media Type", "Kong http-log sends application/json"
        )

    raw = await _read_bounded_body(request)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise _problem(400, "Bad Request", "Body is not valid JSON") from None
    if not isinstance(payload, (dict, list)):
        raise _problem(
            400, "Bad Request", "Expected a Kong log entry or an array of entries"
        )
    if isinstance(payload, list) and len(payload) > MAX_ENTRIES:
        await loss_accounting.record_kong(
            db, tenant_id, rejected={"too_large": len(payload)}
        )
        raise _problem(
            413, "Payload Too Large", f"More than {MAX_ENTRIES} entries in one request"
        )

    mapped = map_payload(payload)
    assets_created = 0
    if mapped.events:
        try:
            assets_created = await _store_events(db, tenant_id, mapped.events)
            await db.commit()
        except Exception:
            with contextlib.suppress(Exception):
                await db.rollback()
            # Kong retries a 5xx until queue.max_retry_time, so this is not
            # final; count it like the SDK's retried rejections.
            await loss_accounting.record_kong(
                db, tenant_id, rejected={"write_failed": len(mapped.events)}
            )
            raise
    await loss_accounting.record_kong(
        db,
        tenant_id,
        received=len(mapped.events),
        rejected=mapped.rejected,
        ignored=mapped.ignored,
    )
    return {
        "ingested": len(mapped.events),
        "assets_created": assets_created,
        "ignored": mapped.ignored.get(NON_AI, 0),
        "rejected": sum(mapped.rejected.values()),
    }
