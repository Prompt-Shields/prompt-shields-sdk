"""Input records for the eval harness.

An :class:`EvalRecord` is one logged LLM call, normalised. The harness needs
very little from it: the prompt (to re-execute), the logged answer (to compare
against), the use case and sensitivity (to stratify and apply the policy) and
token counts (to price it). Everything else is optional.

Records are read from JSONL with :func:`load_records`. Each line is a JSON
object; :meth:`EvalRecord.from_dict` accepts:

- Prompt Shields SDK / registry events (``requested_model``, ``served_model``,
  ``tokens_in``, ``tokens_out``, ``use_case``, ``data_classification``,
  ``latency_ms``, ``prompt_text`` when the customer turned on
  ``send_prompt_text``);
- generic JSONL (``{messages, model, usage, metadata}``, M§2.1), with OpenAI
  (``prompt_tokens``/``completion_tokens``) or Anthropic
  (``input_tokens``/``output_tokens``) usage;
- the fields below under their own names.

=====================  =====================================================
Field                  Accepted keys (first present wins)
=====================  =====================================================
``record_id``          ``record_id``, ``id``, ``event_id``, else line number
``vendor``             ``vendor``, ``provider``
``model``              ``served_model``, ``model``, ``requested_model``
``messages``           ``messages``; or ``prompt_text`` / ``prompt`` (one
                       user message)
``response_text``      ``response_text``, ``completion_text``, ``response``
``expected``           ``expected``, ``reference`` (a gold answer; wins over
                       the logged response as the reference)
``use_case``           ``use_case``, ``use_case_name`` (also in
                       ``metadata``), else ``"(unattributed)"``
``data_classification`` ``sensitivity_level``, ``data_classification`` (also
                       in ``metadata``)
``tokens_in`` / out    ``tokens_in``/``tokens_out``, or ``usage`` as above
``tokens_cached``      ``tokens_cached``
``cost``               ``cost`` (USD; used only when the call cannot be
                       re-priced from tokens, M§4.2)
``latency_ms``         ``latency_ms``
``temperature``        ``temperature``
``max_tokens``         ``max_tokens``
=====================  =====================================================

The teardown record format (#12) is still being defined; when it lands,
``from_dict`` is the one place that needs to learn it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prompt_shields.pricing import estimate_cost
from prompt_shields.route_policy import estimate_input_tokens

SENSITIVITY_LEVELS = ("public", "internal", "confidential", "restricted")
UNATTRIBUTED = "(unattributed)"


def _first(sources: Iterable[Mapping[str, Any]], *keys: str) -> Any:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value is not None:
                return value
    return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class EvalRecord:
    """One logged call, normalised for the eval harness."""

    record_id: str
    vendor: str | None
    model: str | None
    messages: list[dict] | None
    use_case: str = UNATTRIBUTED
    data_classification: str | None = None
    response_text: str | None = None
    expected: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_cached: int | None = None
    cost: float | None = None
    latency_ms: int | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    extra: dict = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, default_id: str = "") -> EvalRecord:
        metadata = data.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        usage = data.get("usage")
        usage = usage if isinstance(usage, Mapping) else {}
        top = (data, metadata)

        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            prompt = _str(_first(top, "prompt_text", "prompt"))
            messages = [{"role": "user", "content": prompt}] if prompt else None

        tokens_in = _int(data.get("tokens_in"))
        if tokens_in is None:
            tokens_in = _int(_first((usage,), "prompt_tokens", "input_tokens"))
        tokens_out = _int(data.get("tokens_out"))
        if tokens_out is None:
            tokens_out = _int(_first((usage,), "completion_tokens", "output_tokens"))

        record_id = _first((data,), "record_id", "id", "event_id")
        return cls(
            record_id=str(record_id) if record_id is not None else default_id,
            vendor=_str(_first(top, "vendor", "provider")),
            model=_str(_first((data,), "served_model", "model", "requested_model")),
            messages=messages,
            use_case=_str(_first(top, "use_case", "use_case_name")) or UNATTRIBUTED,
            data_classification=_str(
                _first(top, "sensitivity_level", "data_classification")
            ),
            response_text=_str(
                _first((data,), "response_text", "completion_text", "response")
            ),
            expected=_str(_first(top, "expected", "reference")),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_cached=_int(data.get("tokens_cached")),
            cost=_float(data.get("cost")),
            latency_ms=_int(data.get("latency_ms")),
            temperature=_float(data.get("temperature")),
            max_tokens=_int(data.get("max_tokens")),
        )

    # --- derived values -----------------------------------------------------

    @property
    def has_prompt(self) -> bool:
        return bool(self.messages)

    @property
    def reference(self) -> str | None:
        """The answer a candidate is compared with: gold answer, else logged answer."""
        return self.expected if self.expected is not None else self.response_text

    def sensitivity(self, unclassified_default: str = "confidential") -> str:
        """Declared level, else the policy default (M§5.1; PII escalation is #13)."""
        level = (self.data_classification or "").lower()
        return level if level in SENSITIVITY_LEVELS else unclassified_default

    def input_tokens(self) -> int:
        """Logged input tokens, else ~4 characters per token over the messages."""
        if self.tokens_in is not None:
            return self.tokens_in
        return estimate_input_tokens(self.messages or [])

    def baseline_cost(self, pricing_table: dict | None = None) -> float | None:
        """Cost of the logged call: re-priced from tokens at the served model
        when possible (M§4.2), else the logged ``cost``, else None."""
        if self.vendor and self.model:
            priced = estimate_cost(
                self.vendor, self.model, self.tokens_in, self.tokens_out,
                pricing_table, tokens_cached=self.tokens_cached,
            )
            if priced is not None:
                return priced
        return self.cost


def complexity_band(tokens_in: int) -> str:
    """Coarse complexity band from prompt size, used as a stratification key."""
    if tokens_in < 1_000:
        return "short"
    if tokens_in < 8_000:
        return "medium"
    return "long"


def iter_records(lines: Iterable[str]) -> Iterator[EvalRecord]:
    for number, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        data = json.loads(line)
        if not isinstance(data, Mapping):
            raise TypeError(f"line {number}: expected a JSON object")
        yield EvalRecord.from_dict(data, default_id=f"line-{number}")


def load_records(path: str | Path) -> list[EvalRecord]:
    """Read records from a JSONL file (one JSON object per line)."""
    with open(path, encoding="utf-8") as handle:
        return list(iter_records(handle))
