"""Input formats for ``ps-teardown`` and the normalised record they become.

Every input row is turned into one :class:`TeardownRecord`. Pricing, the
report and later modes (policy replay and sensitivity mix, #13) work on these
records only, never on the raw input. The schema is described field by field
on the class and in ``docs/teardown/cli.md``.

Input formats (``FORMATS``), all read locally:

``openai-usage``
    OpenAI organisation usage API pages (``GET /v1/organization/usage/
    completions``, JSON, one page or a list of pages), or a CSV with the same
    result column names plus ``start_time`` / ``end_time`` (unix seconds or
    ISO 8601). One record per (bucket, project, key, model) row; ``calls`` is
    ``num_model_requests``. The export's ``model`` is the model that served
    the requests, so it is recorded as ``served_model``.
``anthropic-usage``
    Anthropic usage report pages (``GET /v1/organizations/usage_report/
    messages``, JSON), or a CSV with the result column names flattened
    (``ephemeral_5m_input_tokens``, ``ephemeral_1h_input_tokens``,
    ``web_search_requests``) plus ``starting_at`` / ``ending_at``. The report
    has no request count, so ``calls`` is None.
``ps-events``
    Prompt Shields SDK events, gateway telemetry events and registry
    (``ai_usage_events``) rows: JSONL, a JSON array, or CSV with the event
    field names. The event's own ``cost`` is not summed (methodology M§4.2:
    SDK events before #229 were priced at the requested model); calls are
    re-priced from token counts at ``served_model``. A ``provider_reported``
    cost is kept as the provider's billed amount.
``generic-jsonl``
    One ``{"messages", "model", "usage", "metadata"}`` object per line.
    ``usage`` may be OpenAI, Anthropic or Gemini shaped. Message text is read
    only to compute ``prompt_hash`` (the SDK's algorithm, so hashes match SDK
    events) and the local prompt ``features``; it is not kept on the record.

Rows that cannot be used are counted per reason in :class:`SourceStats`, not
silently skipped. Rows with a model but no token counts are kept: they become
null-cost calls (M§4.5).
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from prompt_shields.prompt_features import extract_prompt_features
from prompt_shields.providers import (
    anthropic_usage_extras,
    anthropic_usage_fields,
    gemini_usage_fields,
    openai_usage_fields,
)
from prompt_shields.telemetry import hash_messages

FORMATS = ("openai-usage", "anthropic-usage", "ps-events", "generic-jsonl")


@dataclass
class TeardownRecord:
    """One normalised call, or one aggregated usage-export row.

    All token fields follow the SDK event conventions (``pricing.py``):
    ``tokens_in`` is the whole prompt including cache reads and writes;
    ``tokens_cached``, ``tokens_cache_write`` and ``tokens_audio_in`` are
    subsets of it; ``tokens_cache_write_1h`` is a subset of
    ``tokens_cache_write``; ``tokens_reasoning`` and ``tokens_audio_out`` are
    subsets of ``tokens_out``. None means "not reported", never zero.

    The record never holds prompt text, response text, user ids or session
    ids. ``prompt_hash`` is kept for cache-hit analysis only and never
    appears in a report or an export.
    """

    #: Input format id (one of ``FORMATS``) and the file it came from.
    source: str
    source_file: str
    #: Call time (UTC), or the bucket start for an aggregated row.
    timestamp: datetime | None = None
    #: Bucket end for an aggregated row; equals ``timestamp`` for a call.
    timestamp_end: datetime | None = None
    #: True for usage-export rows that sum many requests.
    aggregated: bool = False
    #: Requests this record stands for: 1 for a call; the export's request
    #: count for an aggregated row; None when the source does not report it.
    calls: int | None = 1

    #: Normalised provider slug (``openai``, ``anthropic``, ``azure_openai``,
    #: ``bedrock``, ``google``, ``mistral``, ``cohere``, ...).
    provider: str | None = None
    requested_model: str | None = None
    served_model: str | None = None

    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_cached: int | None = None
    tokens_cache_write: int | None = None
    tokens_cache_write_1h: int | None = None
    tokens_reasoning: int | None = None
    tokens_audio_in: int | None = None
    tokens_audio_out: int | None = None
    web_search_requests: int | None = None
    service_tier: str | None = None
    usage_units: float | None = None
    usage_unit_type: str | None = None
    image_quality: str | None = None
    image_size: str | None = None
    #: Token counts were estimated (~4 chars/token), not provider-reported.
    tokens_estimated: bool = False
    #: Long-context band the provider reported for an aggregated row
    #: (Anthropic ``context_window``, e.g. ``"200k-1M"``).
    context_window: str | None = None

    #: Amount the provider billed for this record (USD), when the input
    #: carries it (``cost_source: provider_reported``, OpenRouter cost).
    provider_cost: float | None = None
    #: A cost the input carries from another price list (e.g. LiteLLM's
    #: ``response_cost``) and its source name. Used only when the catalog
    #: has no price for the model (M§4.3).
    framework_cost: float | None = None
    framework_cost_source: str | None = None

    business_unit: str | None = None
    use_case: str | None = None
    #: Declared classification (``public``, ``internal``, ``confidential``,
    #: ``restricted``), as given; None when not declared.
    data_classification: str | None = None
    detected_pii_types: tuple[str, ...] = ()
    #: Project / workspace or API key id from a usage export (opaque id),
    #: used only to map rows to a business unit and use case.
    project: str | None = None
    api_key_id: str | None = None
    route_group: str | None = None
    prompt_hash: str | None = None
    #: Prompt features computed locally (see :func:`prompt_features`), or
    #: None when the input has no message text.
    features: dict[str, Any] | None = None

    @property
    def pricing_model(self) -> str | None:
        """``served_model`` when known, else ``requested_model`` (M§4.2)."""
        return self.served_model or self.requested_model


@dataclass
class SourceStats:
    """Row counts for one input file (template §2)."""

    source: str
    file: str
    rows_read: int = 0
    rows_used: int = 0
    dropped: Counter = field(default_factory=Counter)

    def drop(self, reason: str) -> None:
        self.dropped[reason] += 1


@dataclass
class LoadResult:
    records: list[TeardownRecord]
    sources: list[SourceStats]


# --- small value helpers -----------------------------------------------------

_PROVIDER_ALIASES = {
    "azure": "azure_openai",
    "azure-openai": "azure_openai",
    "azureopenai": "azure_openai",
    "gemini": "google",
    "vertex": "google",
    "vertex-ai": "google",
    "vertex_ai": "google",
    "google-ai": "google",
    "aws-bedrock": "bedrock",
    "amazon-bedrock": "bedrock",
    "mistral-ai": "mistral",
}


def normalise_provider(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip().lower()
    return _PROVIDER_ALIASES.get(v, v)


# Checked in order: Bedrock model ids ("anthropic.claude-...") come first.
_MODEL_PREFIX_PROVIDER = (
    (re.compile(r"^(?:(?:us|eu|apac|global)\.)?(?:anthropic|amazon|meta|cohere|mistral)\."), "bedrock"),
    (re.compile(r"^(gpt-|o\d|chatgpt|text-embedding|whisper|tts|dall-e|gpt-image)"), "openai"),
    (re.compile(r"^claude"), "anthropic"),
    (re.compile(r"^gemini"), "google"),
    (re.compile(r"^(mistral|codestral|magistral|pixtral|ministral)"), "mistral"),
    (re.compile(r"^(command|rerank)"), "cohere"),
)


def infer_provider(model: str | None) -> str | None:
    """Best guess at the provider from a model name, for inputs without one."""
    if not model:
        return None
    m = model.lower()
    for pattern, provider in _MODEL_PREFIX_PROVIDER:
        if pattern.search(m):
            return provider
    return None


def _int(value: Any) -> int | None:
    """A non-negative integer count, else None (a negative count is invalid)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value) if value >= 0 else None
    if isinstance(value, str) and value.strip():
        try:
            f = float(value)
        except ValueError:
            return None
        return int(f) if math.isfinite(f) and f.is_integer() and f >= 0 else None
    return None


def _float(value: Any) -> float | None:
    """A finite, non-negative amount (cost, usage units), else None."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) and f >= 0 else None


_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def _str(value: Any) -> str | None:
    """A trimmed string label. Control characters (newlines included) become
    one space, so an input value cannot add lines to the Markdown report."""
    if isinstance(value, str):
        value = _CONTROL.sub(" ", value).strip()
        return value or None
    return None


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "t")
    return bool(value)


def parse_time(value: Any) -> datetime | None:
    """Unix seconds / milliseconds or ISO 8601, as an aware UTC datetime."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and re.fullmatch(r"\s*\d+(\.\d+)?\s*", value)
    ):
        seconds = float(value)
        if seconds > 1e11:  # milliseconds
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):  # out of range
            return None
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00").replace("z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return None


def _pii(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [v for v in re.split(r"[,;\s]+", value) if v]
        if len(value) == 1 and value[0].startswith("["):
            try:
                value = json.loads(value[0])
            except ValueError:
                pass
    if isinstance(value, (list, tuple)):
        return tuple(sorted({str(v) for v in value if isinstance(v, str) and v}))
    return ()


# --- prompt features (#42) ---------------------------------------------------


def prompt_features(messages: Any, use_case: str | None = None) -> dict[str, Any]:
    """Exportable prompt features, computed locally (PRD §6.2, §7.13).

    The shared extractor's :meth:`PromptFeatures.to_dict`
    (:mod:`prompt_shields.prompt_features`, #45): approximate input tokens,
    message count, code fences, JSON-schema wording, language, use case and
    the router heuristic's complexity label. No text, hash or embedding.
    """
    return extract_prompt_features(messages, use_case=use_case).to_dict()


# --- readers -----------------------------------------------------------------


def _read_json_rows(text: str, stats: SourceStats) -> Iterator[Any]:
    """Yield JSON values from a JSON document (array -> items) or JSONL."""
    stripped = text.lstrip()
    if stripped.startswith("["):
        try:
            doc = json.loads(text)
        except (ValueError, RecursionError):
            doc = None
        if isinstance(doc, list):
            yield from doc
            return
    if stripped.startswith("{"):
        try:
            doc = json.loads(text)
        except (ValueError, RecursionError):
            doc = None
        if doc is not None:
            yield doc
            return
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (ValueError, RecursionError):
            stats.rows_read += 1
            stats.drop("invalid_json")
            continue
        yield value


def _read_csv_rows(text: str, stats: SourceStats) -> Iterator[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    while True:
        try:
            row = next(reader)
        except StopIteration:
            return
        except csv.Error:  # e.g. a field over the csv module's size limit
            stats.rows_read += 1
            stats.drop("invalid_csv")
            continue
        yield {k.strip(): (v if v != "" else None) for k, v in row.items() if k}


def _first_line(text: str) -> str:
    lines = text.lstrip().splitlines()
    return lines[0] if lines else ""


def _is_csv(path: Path, text: str) -> bool:
    if path.suffix.lower() == ".csv":
        return True
    first = text.lstrip()[:1]
    return bool(first) and first not in ("{", "[") and "," in _first_line(text)


def detect_format(path: Path, text: str) -> str:
    """Guess the input format from the file contents."""
    head = text[:20000]
    if _is_csv(path, text):
        header = _first_line(text).lower()
        if "num_model_requests" in header:
            return "openai-usage"
        if "uncached_input_tokens" in header:
            return "anthropic-usage"
        return "ps-events"
    if '"num_model_requests"' in head or '"organization.usage' in head:
        return "openai-usage"
    if '"uncached_input_tokens"' in head or '"starting_at"' in head:
        return "anthropic-usage"
    if '"messages"' in head and '"usage"' in head:
        return "generic-jsonl"
    return "ps-events"


def _pages(rows: Iterable[Any]) -> Iterator[Mapping[str, Any]]:
    """Buckets from one usage page, a list of pages, or a bare bucket list."""
    for row in rows:
        if isinstance(row, Mapping) and isinstance(row.get("data"), list):
            yield from (b for b in row["data"] if isinstance(b, Mapping))
        elif isinstance(row, Mapping):
            yield row


def _openai_usage_record(r: Mapping[str, Any], start: Any, end: Any, file: str) -> TeardownRecord:
    batch = r.get("batch")
    return TeardownRecord(
        source="openai-usage",
        source_file=file,
        timestamp=parse_time(start),
        timestamp_end=parse_time(end),
        aggregated=True,
        calls=_int(r.get("num_model_requests")),
        provider="openai",
        served_model=_str(r.get("model")),
        tokens_in=_int(r.get("input_tokens")) or 0,
        tokens_out=_int(r.get("output_tokens")) or 0,
        tokens_cached=_int(r.get("input_cached_tokens")),
        tokens_audio_in=_int(r.get("input_audio_tokens")) or None,
        tokens_audio_out=_int(r.get("output_audio_tokens")) or None,
        service_tier="batch" if batch is not None and _bool(batch) else None,
        project=_str(r.get("project_id")),
        api_key_id=_str(r.get("api_key_id")),
    )


def _anthropic_usage_record(r: Mapping[str, Any], start: Any, end: Any, file: str) -> TeardownRecord:
    creation = r.get("cache_creation") if isinstance(r.get("cache_creation"), Mapping) else {}
    write_5m = _int(creation.get("ephemeral_5m_input_tokens", r.get("ephemeral_5m_input_tokens"))) or 0
    write_1h = _int(creation.get("ephemeral_1h_input_tokens", r.get("ephemeral_1h_input_tokens"))) or 0
    if not creation and r.get("cache_creation_input_tokens") is not None:
        write_5m = max((_int(r.get("cache_creation_input_tokens")) or 0) - write_1h, 0)
    reads = _int(r.get("cache_read_input_tokens")) or 0
    uncached = _int(r.get("uncached_input_tokens")) or 0
    tool = r.get("server_tool_use") if isinstance(r.get("server_tool_use"), Mapping) else {}
    searches = _int(tool.get("web_search_requests", r.get("web_search_requests")))
    return TeardownRecord(
        source="anthropic-usage",
        source_file=file,
        timestamp=parse_time(start),
        timestamp_end=parse_time(end),
        aggregated=True,
        calls=None,
        provider="anthropic",
        served_model=_str(r.get("model")),
        tokens_in=uncached + reads + write_5m + write_1h,
        tokens_out=_int(r.get("output_tokens")) or 0,
        tokens_cached=reads,
        tokens_cache_write=write_5m + write_1h,
        tokens_cache_write_1h=write_1h or None,
        web_search_requests=searches or None,
        service_tier=_str(r.get("service_tier")),
        context_window=_str(r.get("context_window")),
        project=_str(r.get("workspace_id")),
        api_key_id=_str(r.get("api_key_id")),
    )


def _usage_rows(rows: Iterable[Any], stats: SourceStats, kind: str) -> Iterator[TeardownRecord]:
    start_key, end_key = (
        ("start_time", "end_time") if kind == "openai-usage" else ("starting_at", "ending_at")
    )
    build = _openai_usage_record if kind == "openai-usage" else _anthropic_usage_record
    for bucket in _pages(rows):
        results = bucket.get("results")
        if isinstance(results, list):
            start, end = bucket.get(start_key), bucket.get(end_key)
            items = [(r, start, end) for r in results]
        else:  # a CSV row or a flat result row carries its own bucket times
            start = bucket.get(start_key) or bucket.get("date")
            end = bucket.get(end_key)
            items = [(bucket, start, end)]
        for r, s, e in items:
            stats.rows_read += 1
            if not isinstance(r, Mapping):
                stats.drop("not_an_object")
                continue
            rec = build(r, s, e, stats.file)
            if not rec.served_model:
                stats.drop("no_model")
                continue
            if not (rec.tokens_in or rec.tokens_out or rec.calls or rec.web_search_requests):
                stats.drop("zero_usage")
                continue
            stats.rows_used += 1
            yield rec


_PS_COST_SOURCES = {"pricing_table", "pricing_table_estimated_tokens", "provider_reported"}


def _ps_event_record(e: Mapping[str, Any], file: str) -> TeardownRecord:
    flow = e.get("flow_context") if isinstance(e.get("flow_context"), Mapping) else {}
    requested = _str(e.get("requested_model")) or _str(e.get("model"))
    cost = _float(e.get("cost"))
    cost_source = _str(e.get("cost_source"))
    ts = parse_time(
        e.get("timestamp") or e.get("occurred_at") or e.get("ts") or e.get("created_at")
    )
    return TeardownRecord(
        source="ps-events",
        source_file=file,
        timestamp=ts,
        timestamp_end=ts,
        provider=normalise_provider(e.get("vendor") or e.get("provider")) or infer_provider(requested),
        requested_model=requested,
        served_model=_str(e.get("served_model")),
        tokens_in=_int(e.get("tokens_in")),
        tokens_out=_int(e.get("tokens_out")),
        tokens_cached=_int(e.get("tokens_cached")),
        tokens_cache_write=_int(e.get("tokens_cache_write")),
        tokens_cache_write_1h=_int(e.get("tokens_cache_write_1h")),
        tokens_reasoning=_int(e.get("tokens_reasoning")),
        tokens_audio_in=_int(e.get("tokens_audio_in")),
        tokens_audio_out=_int(e.get("tokens_audio_out")),
        web_search_requests=_int(e.get("web_search_requests")),
        service_tier=_str(e.get("service_tier")),
        usage_units=_float(e.get("usage_units")),
        usage_unit_type=_str(e.get("usage_unit_type")),
        image_quality=_str(e.get("image_quality")),
        image_size=_str(e.get("image_size")),
        tokens_estimated=_bool(e.get("tokens_estimated")) or cost_source == "pricing_table_estimated_tokens",
        provider_cost=cost if cost_source == "provider_reported" else None,
        framework_cost=cost if cost_source and cost_source not in _PS_COST_SOURCES else None,
        framework_cost_source=cost_source if cost_source not in _PS_COST_SOURCES else None,
        business_unit=_str(e.get("business_unit")),
        use_case=_str(e.get("use_case_name")) or _str(e.get("use_case")),
        data_classification=_str(e.get("data_classification")),
        detected_pii_types=_pii(e.get("detected_pii_types") or flow.get("detected_pii_types")),
        route_group=_str(e.get("route_group")),
        prompt_hash=_str(e.get("prompt_hash")),
    )


def _ps_events(rows: Iterable[Any], stats: SourceStats) -> Iterator[TeardownRecord]:
    for e in rows:
        stats.rows_read += 1
        if not isinstance(e, Mapping):
            stats.drop("not_an_object")
            continue
        if e.get("cache_status") is not None and e.get("tokens_in") in (None, ""):
            stats.drop("gateway_cache_event")
            continue
        rec = _ps_event_record(e, stats.file)
        if not rec.requested_model and not rec.served_model and not rec.provider:
            stats.drop("no_model")
            continue
        stats.rows_used += 1
        yield rec


def _generic_usage(provider: str | None, usage: Any) -> dict[str, Any]:
    if not isinstance(usage, Mapping):
        return {}
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        return openai_usage_fields(usage)
    if "promptTokenCount" in usage or "prompt_token_count" in usage:
        return gemini_usage_fields(usage)
    if "input_tokens" in usage or "output_tokens" in usage:
        if provider == "openai" or "input_tokens_details" in usage:
            # OpenAI Responses API usage: input_tokens includes cached ones.
            details = usage.get("input_tokens_details")
            details = details if isinstance(details, Mapping) else {}
            out_details = usage.get("output_tokens_details")
            out_details = out_details if isinstance(out_details, Mapping) else {}
            return {
                "tokens_in": _int(usage.get("input_tokens")),
                "tokens_out": _int(usage.get("output_tokens")),
                "tokens_cached": _int(details.get("cached_tokens")),
                "tokens_reasoning": _int(out_details.get("reasoning_tokens")),
            }
        fields = anthropic_usage_fields(
            _int(usage.get("input_tokens")),
            _int(usage.get("output_tokens")),
            _int(usage.get("cache_read_input_tokens")),
            _int(usage.get("cache_creation_input_tokens")),
        )
        fields.update(anthropic_usage_extras(usage))
        return fields
    return {}


def _generic_record(obj: Mapping[str, Any], file: str) -> TeardownRecord:
    meta = obj.get("metadata") if isinstance(obj.get("metadata"), Mapping) else {}

    def pick(*keys: str) -> Any:
        for k in keys:
            if obj.get(k) is not None:
                return obj[k]
            if meta.get(k) is not None:
                return meta[k]
        return None

    model = _str(pick("model", "requested_model"))
    response = obj.get("response") if isinstance(obj.get("response"), Mapping) else {}
    served = _str(pick("served_model")) or _str(response.get("model"))
    provider = normalise_provider(pick("provider", "vendor")) or infer_provider(served or model)
    usage = _generic_usage(provider, obj.get("usage"))
    messages = obj.get("messages")
    ts = parse_time(pick("timestamp", "created_at", "created", "ts"))
    return TeardownRecord(
        source="generic-jsonl",
        source_file=file,
        timestamp=ts,
        timestamp_end=ts,
        provider=provider,
        requested_model=model,
        served_model=served,
        tokens_in=usage.get("tokens_in"),
        tokens_out=usage.get("tokens_out"),
        tokens_cached=usage.get("tokens_cached"),
        tokens_cache_write=usage.get("tokens_cache_write"),
        tokens_cache_write_1h=usage.get("tokens_cache_write_1h"),
        tokens_reasoning=usage.get("tokens_reasoning"),
        tokens_audio_in=usage.get("tokens_audio_in"),
        tokens_audio_out=usage.get("tokens_audio_out"),
        web_search_requests=usage.get("web_search_requests"),
        service_tier=_str(pick("service_tier")) or usage.get("service_tier"),
        provider_cost=usage.get("provider_cost"),
        business_unit=_str(pick("business_unit")),
        use_case=_str(pick("use_case", "use_case_name")),
        data_classification=_str(pick("data_classification")),
        detected_pii_types=_pii(pick("detected_pii_types")),
        prompt_hash=hash_messages(messages) if isinstance(messages, list) else None,
        features=(
            prompt_features(messages, _str(pick("use_case", "use_case_name")))
            if isinstance(messages, list)
            else None
        ),
    )


def _generic(rows: Iterable[Any], stats: SourceStats) -> Iterator[TeardownRecord]:
    for obj in rows:
        stats.rows_read += 1
        if not isinstance(obj, Mapping):
            stats.drop("not_an_object")
            continue
        rec = _generic_record(obj, stats.file)
        if not rec.requested_model and not rec.served_model:
            stats.drop("no_model")
            continue
        stats.rows_used += 1
        yield rec


# --- key map -----------------------------------------------------------------


def apply_key_map(records: Iterable[TeardownRecord], key_map: Mapping[str, Any]) -> None:
    """Fill business unit / use case from a per-project or per-key mapping.

    ``key_map`` maps a project / workspace id or an API key id to
    ``{"business_unit": ..., "use_case": ..., "data_classification": ...}``.
    Values already on a record win; the key id is checked before the project.
    """
    for rec in records:
        for key in (rec.api_key_id, rec.project):
            entry = key_map.get(key) if key else None
            if isinstance(entry, Mapping):
                rec.business_unit = rec.business_unit or _str(entry.get("business_unit"))
                rec.use_case = rec.use_case or _str(entry.get("use_case"))
                rec.data_classification = rec.data_classification or _str(
                    entry.get("data_classification")
                )
                break


# --- entry point -------------------------------------------------------------


def load_records(
    paths: Iterable[str | Path],
    *,
    fmt: str | None = None,
    key_map: Mapping[str, Any] | None = None,
) -> LoadResult:
    """Read and normalise input files into :class:`TeardownRecord` objects.

    ``fmt`` forces one of ``FORMATS`` for every file; by default each file's
    format is detected from its contents. Files are read in full, locally.
    """
    if fmt is not None and fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}; expected one of {', '.join(FORMATS)}")
    records: list[TeardownRecord] = []
    sources: list[SourceStats] = []
    for p in paths:
        path = Path(p)
        # Untrusted input: undecodable bytes become U+FFFD instead of
        # aborting the run.
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        kind = fmt or detect_format(path, text)
        stats = SourceStats(source=kind, file=path.name)
        rows: Iterable[Any] = (
            _read_csv_rows(text, stats) if _is_csv(path, text) else _read_json_rows(text, stats)
        )
        if kind in ("openai-usage", "anthropic-usage"):
            records.extend(_usage_rows(rows, stats, kind))
        elif kind == "generic-jsonl":
            records.extend(_generic(rows, stats))
        else:
            records.extend(_ps_events(rows, stats))
        sources.append(stats)
    if key_map:
        apply_key_map(records, key_map)
    return LoadResult(records=records, sources=sources)
